import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from profile_store import PROFILE, read_settings, save_settings
import windows_service

DEFAULTS = dict(dotenv_values(ROOT / "defaults.env", interpolate=False))
MAIN = {
    "AGENT_NAME": "Имя агента", "TG_PHONE": "Телефон Telegram (+7…)",
    "TG_API_ID": "Telegram API ID", "TG_API_HASH": "Telegram API Hash",
    "OPENAI_API_KEY": "Ключ OpenAI", "OPENAI_MODEL": "Модель OpenAI",
    "ADMIN_OWNER_TG_ID": "Telegram ID владельца", "ADMIN_WIFE_TG_ID": "ID второго администратора",
    "SAFE_TEST_MODE": "Тестовый режим",
}
CRM_LABELS = {"URL": "Адрес https://", "EMAIL": "Электронная почта", "API_KEY": "Ключ API",
              "BRANCH": "ID филиала", "LEAD_SOURCE": "ID источника заявки"}


def validate(v):
    for key in ("AGENT_NAME", "OPENAI_API_KEY", "OPENAI_MODEL"):
        if not v[key]:
            raise ValueError("Заполните поле «" + MAIN[key] + "».")
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", v["TG_PHONE"]):
        raise ValueError("Введите телефон с кодом страны, например +79991234567.")
    if not re.fullmatch(r"[1-9][0-9]*", v["TG_API_ID"]) or not re.fullmatch(r"[a-fA-F0-9]{32}", v["TG_API_HASH"]):
        raise ValueError("Проверьте API ID (число) и API Hash (32 шестнадцатеричных символа) с my.telegram.org.")
    ids = [value for value in (v["ADMIN_OWNER_TG_ID"], v["ADMIN_WIFE_TG_ID"]) if value]
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Укажите положительные числовые Telegram ID. Повторять один ID нельзя.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,24}", v["TG_SESSION_NAME"]):
        raise ValueError("Имя сессии должно содержать от 3 до 24 латинских букв, цифр, дефисов или подчёркиваний.")
    for key, default in DEFAULTS.items():
        if default and default.isdecimal() and not v[key].isdecimal():
            raise ValueError("Введите целое число в поле " + key)
    if v["CRM_ENABLED"] == "true":
        complete = False
        for city in ("ALEX", "YARO"):
            url, email, secret, branch, source = [v[f"ALFACRM_{city}_{k}"] for k in CRM_LABELS]
            if any((url, email, secret, branch, source)):
                uri = urlsplit(url)
                if not (uri.scheme == "https" and uri.hostname and uri.path in ("", "/") and email and secret and branch.isdecimal() and (not source or source.isdecimal())):
                    raise ValueError("Проверьте адрес, почту, ключ API и числовые ID филиала AlfaCRM.")
                complete = True
        if not complete:
            raise ValueError("Для AlfaCRM заполните данные хотя бы одного филиала.")


async def run_agent(pipe, values, stop_event=None, use_qr=False):
    validate(values)
    os.environ.update(values)
    identity = hashlib.sha256(values["TG_PHONE"].encode()).hexdigest()[:12]
    os.environ["TG_SESSION_NAME"] += "_" + identity
    os.environ["ANASTASIYA_PROFILE"] = str(PROFILE)
    os.chdir(PROFILE)
    for part in ("data", "config"):
        (PROFILE / part).mkdir(exist_ok=True)
    for item in ("config/approved_groups.json",):
        if not (PROFILE / item).exists():
            shutil.copyfile(ROOT / item, PROFILE / item)
    sys.path.insert(0, str(ROOT / "src"))
    from application import create_application
    import cli
    app = create_application(PROFILE / "data")
    replies = asyncio.Queue()

    async def prompt(kind):
        pipe.send(("prompt", kind))
        return await replies.get()

    panel = None

    def connected():
        nonlocal panel
        from admin_panel import Panel, QuietHandler
        from wsgiref.simple_server import make_server
        panel = make_server("127.0.0.1", 8765, Panel(), handler_class=QuietHandler)
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        mode = "тестовый режим" if values["SAFE_TEST_MODE"] == "true" else "рабочий режим"
        (PROFILE / "service-status.txt").write_text("Подключён к Telegram · " + mode, encoding="utf-8")

    task = asyncio.create_task(cli.main(app, argv=["listen"], login={
        "code_callback": lambda: prompt("code"), "password": lambda: prompt("password")
    }, connected=connected, authorize_only=pipe is not None, unattended=pipe is None,
        display_qr=(lambda url: pipe.send(("qr", url))) if use_qr else None))
    try:
        while not task.done():
            if stop_event is not None and stop_event.is_set():
                break
            if pipe is not None and pipe.poll():
                action, value = pipe.recv()
                if action == "stop":
                    break
                if action == "reply":
                    replies.put_nowait(value)
            await asyncio.sleep(0.08)
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
            if pipe is not None:
                pipe.send(("authorized", ""))
        except asyncio.CancelledError:
            pass
        finally:
            if panel is not None:
                await asyncio.to_thread(panel.shutdown)
                panel.server_close()
            app.close()


def worker(pipe, values, use_qr=False):
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            asyncio.run(run_agent(pipe, values, use_qr=use_qr))
        except (Exception, SystemExit) as exc:
            kind = type(exc).__name__
            hints = {"WrongTelegramAccount": "Номер аккаунта отличается от номера в мастере. Выберите нужный аккаунт в Telegram.",
                     "PasswordHashInvalidError": "Telegram отклонил пароль двухэтапной проверки.",
                     "ApiIdInvalidError": "Telegram отклонил API ID или API Hash.",
                     "PhoneNumberInvalidError": "Telegram отклонил номер телефона.",
                     "FloodWaitError": "Telegram ограничил попытки входа. Повторите позже.",
                     "ConnectionError": "Проверьте подключение к Telegram.",
                     "RuntimeError": "Проверьте код подтверждения и пароль Telegram."}
            pipe.send(("error", hints.get(kind, "Не удалось запустить агента.") + " (" + kind + ")"))
        finally:
            pipe.close()


def service_worker():
    stopped = threading.Event()

    def wait_for_stop():
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            stopped.set()

    threading.Thread(target=wait_for_stop, daemon=True).start()
    path = PROFILE / "service-status.txt"
    while not stopped.is_set():
        path.write_text("Подключение к Telegram…", encoding="utf-8")
        try:
            asyncio.run(run_agent(None, read_settings(), stopped))
            if not stopped.is_set():
                raise ConnectionError()
        except (ConnectionError, TimeoutError, OSError) as exc:
            path.write_text("Повтор подключения через минуту (" + type(exc).__name__ + ")", encoding="utf-8")
            stopped.wait(60)
        except (Exception, SystemExit) as exc:
            path.write_text("Откройте мастер и проверьте настройки (" + type(exc).__name__ + ")", encoding="utf-8")
            return 1
    return 0


class Window:
    def __init__(self, root):
        self.root = root
        root.title("Анастасия AI")
        root.geometry("710x600")
        self.proc = None
        self.qr_window = None
        self.pipe = None
        self.closing = False
        self.cancelling = False
        self.state = tk.StringVar(value="Агент остановлен")
        self.values = read_settings()
        frame = ttk.Frame(root, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Анастасия AI", font=("Segoe UI", 21)).pack(anchor="w")
        ttk.Label(frame, textvariable=self.state).pack(anchor="w", pady=(2, 12))
        self.book = ttk.Notebook(frame)
        self.book.pack(fill="both", expand=True)
        self.entries = {}
        crm = {k: ("Александров" if "_ALEX_" in k else "Ярославль") + " · " + CRM_LABELS[k.split("_", 2)[2]]
               for k in DEFAULTS if k.startswith("ALFACRM_")}
        crm = {"CRM_ENABLED": "Подключить AlfaCRM", **crm}
        self.page("Агент", MAIN, "API ID и Hash выдаёт my.telegram.org. ID администраторов можно добавить позже.\nТестовый режим ограничивает работу командами из «Избранного».")
        self.page("AlfaCRM", crm, "Заполните данные используемых филиалов, если подключаете CRM.")
        self.page("Дополнительно", {k: k for k in DEFAULTS if k not in MAIN and k not in crm}, "")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(14, 0))
        self.start_button = ttk.Button(bar, text="Войти по QR-коду", command=lambda: self.start(True))
        self.start_button.pack(side="left")
        self.code_button = ttk.Button(bar, text="Войти по коду", command=self.start)
        self.code_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(bar, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.last_state = None
        self.refresh()

    def refresh(self):
        if not self.proc:
            try:
                state = windows_service.status()
                active = state not in (0, 1)
                if active:
                    path = PROFILE / "service-status.txt"
                    detail = path.read_text(encoding="utf-8") if path.exists() else ""
                    self.state.set("Служба Windows · " + (detail or "запускается"))
                elif state != self.last_state:
                    path = PROFILE / "service-status.txt"
                    detail = path.read_text(encoding="utf-8") if state and path.exists() else ""
                    self.state.set(detail if detail.startswith("Откройте") else
                                   ("Служба остановлена" if state else "Первичная настройка службы Windows"))
                self.last_state = state
                for _, field in self.entries.values():
                    field.state(["disabled" if active else "!disabled"])
                self.start_button.state(["disabled" if active else "!disabled"])
                self.code_button.state(["disabled" if active else "!disabled"])
                self.stop_button.state(["!disabled" if state == 4 else "disabled"])
            except (ValueError, OSError) as exc:
                self.state.set(str(exc))
        self.root.after(2000, self.refresh)

    def page(self, title, fields, help_text):
        tab = ttk.Frame(self.book)
        self.book.add(tab, text=title)
        canvas = tk.Canvas(tab, highlightthickness=0)
        scroll = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=12)
        item = canvas.create_window(0, 0, anchor="nw", window=inner)
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        inner.columnconfigure(1, weight=1)
        if help_text:
            ttk.Label(inner, text=help_text, wraplength=570).grid(row=0, columnspan=2, sticky="w", pady=(0, 8))
        for row, (key, label) in enumerate(fields.items(), 1):
            ttk.Label(inner, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            value = tk.StringVar(value=self.values.get(key, DEFAULTS[key]))
            if DEFAULTS[key] in ("true", "false"):
                field = ttk.Checkbutton(inner, variable=value, onvalue="true", offvalue="false")
            else:
                field = ttk.Entry(inner, textvariable=value, show="•" if "API_KEY" in key or "HASH" in key else "")
            field.grid(row=row, column=1, sticky="ew")
            self.entries[key] = (value, field)

    def hide_qr(self):
        if self.qr_window is not None:
            self.qr_window.destroy()
            self.qr_window = None

    def show_qr(self, url):
        import qrcode
        if self.qr_window is None:
            self.qr_window = tk.Toplevel(self.root)
            self.qr_window.title("Подключение Telegram")
            self.qr_window.transient(self.root)
            self.qr_window.protocol("WM_DELETE_WINDOW", self.stop)
            ttk.Label(self.qr_window, padding=12, text="На телефоне выберите аккаунт агента и откройте\nНастройки → Устройства → Подключить устройство.").pack()
            self.qr_canvas = tk.Canvas(self.qr_window, bg="white", highlightthickness=0)
            self.qr_canvas.pack(padx=12, pady=(0, 12))
        qr = qrcode.QRCode(border=4)
        qr.add_data(url)
        rows = qr.get_matrix()
        scale = 6
        self.qr_canvas.configure(width=len(rows)*scale, height=len(rows)*scale)
        self.qr_canvas.delete("all")
        for y, row in enumerate(rows):
            for x, dark in enumerate(row):
                if dark:
                    self.qr_canvas.create_rectangle(x*scale, y*scale, (x+1)*scale, (y+1)*scale, outline="", fill="black")

    def start(self, use_qr=False):
        if self.proc:
            return
        values = {k: v.get().strip() for k, (v, _) in self.entries.items()}
        self.cancelling = False
        try:
            validate(values)
            self.pending_values = values
            self.pipe, child = multiprocessing.Pipe()
            self.proc = multiprocessing.Process(target=worker, args=(child, values, use_qr))
            self.proc.start()
            child.close()
        except (ValueError, OSError) as exc:
            self.proc = None
            messagebox.showerror("Настройки", str(exc), parent=self.root)
            return
        for _, field in self.entries.values():
            field.state(["disabled"])
        self.start_button.state(["disabled"])
        self.code_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.state.set("Подключение к Telegram…")
        self.root.after(100, self.poll)

    def poll(self):
        eof = False
        try:
            while self.pipe.poll():
                kind, value = self.pipe.recv()
                if kind == "qr" and not self.closing and not self.cancelling:
                    self.show_qr(value)
                    continue
                self.hide_qr()
                if kind == "authorized" and not self.closing and not self.cancelling:
                    try:
                        save_settings(self.pending_values)
                        windows_service.install_start()
                        self.state.set("Служба запускается…")
                    except (ValueError, OSError) as exc:
                        self.state.set("Ошибка установки службы (" + str(exc) + ")")
                elif kind == "prompt" and not self.closing:
                    answer = simpledialog.askstring("Вход в Telegram",
                        "Введите код входа Telegram" if value == "code" else "Введите пароль двухэтапной проверки",
                        show="*" if value == "password" else "", parent=self.root)
                    if answer is None:
                        self.stop()
                    else:
                        self.pipe.send(("reply", answer))
                elif kind == "error":
                    self.state.set(value)
        except (EOFError, BrokenPipeError):
            eof = True
        if self.proc.is_alive() or (not eof and self.pipe.poll()):
            self.root.after(100, self.poll)
            return
        self.proc.join()
        self.hide_qr()
        code = self.proc.exitcode
        self.proc.close()
        self.proc = None
        self.pipe.close()
        if self.closing:
            self.root.destroy()
            return
        if self.state.get() in ("Остановка…", "Подключение к Telegram…"):
            self.state.set("Агент остановлен" if code == 0 else "Агент завершил работу с ошибкой " + str(code))
        for _, field in self.entries.values():
            field.state(["!disabled"])
        self.start_button.state(["!disabled"])
        self.code_button.state(["!disabled"])
        self.stop_button.state(["disabled"])

    def stop(self):
        self.hide_qr()
        if not self.proc:
            try:
                windows_service.stop()
                self.state.set("Остановка службы…")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Служба Windows", str(exc), parent=self.root)
            return
        self.state.set("Остановка…")
        self.cancelling = True
        self.stop_button.state(["disabled"])
        try:
            self.pipe.send(("stop", ""))
        except (EOFError, BrokenPipeError):
            pass

    def close(self):
        if self.proc:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    try:
        PROFILE.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(PROFILE / "desktop.lock", timeout=0) as lock:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                messagebox.showinfo("Анастасия AI", "Окно агента уже открыто.", parent=root)
                return
            Window(root)
            root.deiconify()
            root.mainloop()
    except Exception as exc:
        messagebox.showerror("Анастасия AI", "Не удалось открыть профиль. " + type(exc).__name__, parent=root)


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        raise SystemExit(service_worker())
    main()
