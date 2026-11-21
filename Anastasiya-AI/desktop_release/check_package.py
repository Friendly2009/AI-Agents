"""Offline checks against a payload extracted from the built executable."""
import asyncio
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(sys.executable).parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
import desktop
import cli
import application
import profile_store


async def exercise(values):
    history = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def configure_delivery(self, *args):
            pass

        async def start(self, *, phone, code_callback, password):
            assert phone == values["TG_PHONE"]
            assert await code_callback() == "12345"
            assert await password() == "test-password"
            history.append("authorized")

        async def connect(self):
            history.append("unattended")

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=123, phone=values["TG_PHONE"][1:])

        async def disconnect(self):
            history.append("disconnected")

    class Pipe:
        def __init__(self):
            self.replies = []

        def poll(self):
            return bool(self.replies)

        def recv(self):
            return "reply", self.replies.pop(0)

        def send(self, message):
            action, kind = message
            history.append(kind if action == "prompt" else action)
            if action == "prompt":
                self.replies.append("12345" if kind == "code" else "test-password")

    stopped = threading.Event()

    async def listen(app, client):
        with urlopen("http://127.0.0.1:8765/", timeout=3) as response:
            assert response.status == 200
        history.append("panel")
        stopped.set()
        await asyncio.Event().wait()

    with patch.object(cli, "TelegramClient", Client), patch("telegram_adapter.listen", listen):
        await desktop.run_agent(Pipe(), values)
        assert history == ["code", "password", "authorized", "disconnected", "authorized"], history
        from telethon.errors import SessionPasswordNeededError
        from unittest.mock import AsyncMock
        tokens = [SimpleNamespace(url="tg://login?token=a", wait=AsyncMock(side_effect=TimeoutError())),
                  SimpleNamespace(url="tg://login?token=b", wait=AsyncMock(side_effect=SessionPasswordNeededError(None)))]
        with patch.object(Client, "is_user_authorized", new=AsyncMock(return_value=False)), \
             patch.object(Client, "qr_login", new=AsyncMock(side_effect=tokens), create=True), \
             patch.object(Client, "sign_in", new=AsyncMock(), create=True) as sign_in:
            await desktop.run_agent(Pipe(), values, use_qr=True)
            sign_in.assert_awaited_once_with(password="test-password")
            assert history[-5:] == ["qr", "qr", "password", "disconnected", "authorized"]
        with patch.object(Client, "get_me", new=AsyncMock(return_value=SimpleNamespace(phone="000"))), \
             patch.object(Client, "log_out", new=AsyncMock(), create=True) as logout:
            try:
                await desktop.run_agent(Pipe(), values, use_qr=True)
            except cli.WrongTelegramAccount:
                logout.assert_awaited_once()
            else:
                raise AssertionError("Mismatched account accepted")
        await desktop.run_agent(None, values, stopped)
        assert history[-3:] == ["unattended", "panel", "disconnected"], history


if __name__ == "__main__":
    assert ROOT.name == "qa-build" and not profile_store.PROFILE.exists()
    assert all(Path(p).is_relative_to(ROOT) for p in sys.path)
    values = {**desktop.DEFAULTS, "TG_API_ID": "1234", "TG_API_HASH": "a" * 32,
              "TG_PHONE": "+79990000000", "OPENAI_API_KEY": "offline-fixture-secret"}
    root = desktop.tk.Tk()
    root.withdraw()
    window = desktop.Window(root)
    assert set(window.entries) == set(values)
    window.show_qr("tg://login?token=offline")
    assert window.qr_canvas.find_all()
    window.hide_qr()
    root.destroy()
    desktop.validate(values)
    profile_store.save_settings(values)
    assert profile_store.read_settings() == values
    assert b"offline-fixture-secret" not in (profile_store.PROFILE / "settings.bin").read_bytes()
    asyncio.run(exercise(values))
    assert (profile_store.PROFILE / "data" / "softskills.db").exists()
    assert not (ROOT / "data" / "softskills.db").exists()
    print("PASS: isolated runtime, wizard, validation, DPAPI, Telegram prompts, unattended worker, panel and shutdown")
