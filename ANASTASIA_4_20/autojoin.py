"""Conservative, opt-in Telegram public-community join manager.

Only public @username channels/supergroups that are confirmed local by the
owner or by explicit public city information are eligible. No member scraping,
no private chat invites, and no unsolicited messages. All joining uses the
account owner's own Telegram Client API session.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
from typing import Any

from city_sources import read_catalog

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "auto_join_state.json"
JOINABLE_LOCALITIES = {"подтверждено_вручную", "подтверждено_в_описании", "пользовательский_источник"}
FINAL_STATUSES = {"joined", "pending", "denied", "invalid", "blocked"}
USERNAME_RE = re.compile(r"^@[a-z][a-z0-9_]{4,31}$")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def day_now() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() == "true"


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def normalize_username(raw: str) -> str:
    raw = str(raw or "").strip().lower()
    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        raw = raw.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
    if raw.startswith("t.me/"):
        raw = raw[5:].split("?", 1)[0].strip("/")
    if not raw.startswith("@"):
        raw = "@" + raw
    if not USERNAME_RE.fullmatch(raw):
        raise ValueError("Поддерживаются только публичные Telegram-сообщества: @username или https://t.me/username.")
    return raw


def read_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("enabled", False)
    raw.setdefault("records", {})
    if not isinstance(raw["records"], dict):
        raw["records"] = {}
    return raw


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def enable(flag: bool, path: Path = STATE_FILE) -> dict:
    state = read_state(path)
    state["enabled"] = bool(flag)
    save_state(state, path)
    return state


def candidates(catalog=None, state=None, *, selected=None):
    """Return deduped, verified-local public communities in catalog order."""
    catalog = read_catalog() if catalog is None else catalog
    state = read_state() if state is None else state
    only = set(selected) if selected is not None else None
    seen = set()
    entries = []
    for row in catalog:
        try:
            username = normalize_username(row.get("username", ""))
        except ValueError:
            continue
        if username in seen or (only is not None and username not in only):
            continue
        seen.add(username)
        if row.get("city_status") not in JOINABLE_LOCALITIES:
            continue
        if state["records"].get(username, {}).get("status") in FINAL_STATUSES:
            continue
        if row.get("type") not in {"группа", "канал", "проверить"}:
            continue
        entries.append(username)
    return entries


def today_count(state: dict, now_day=None) -> int:
    today = now_day or day_now()
    return sum(1 for v in state.get("records", {}).values()
               if v.get("attempt_day") == today and v.get("status") in {"joined", "pending", "failed", "denied", "invalid", "blocked"})


def status_report(state=None) -> str:
    state = state or read_state()
    counts = {}
    for v in state["records"].values():
        counts[v.get("status", "unknown")] = counts.get(v.get("status", "unknown"), 0) + 1
    enabled = bool(state.get("enabled")) and env_bool("AUTO_JOIN_ENABLED")
    dry = env_bool("AUTO_JOIN_DRY_RUN", True)
    rows = ["📍 ANASTASIA AUTO JOIN 4.3", f"Автовступление: {'включено' if enabled else 'выключено'}",
            f"Режим: {'ПРОВЕРКА без вступлений' if dry else 'реальное вступление'}",
            f"Открытых вступлений подтверждено: {counts.get('joined', 0)}",
            f"Заявок ожидает: {counts.get('pending', 0)}",
            f"Ошибок/ограничений: {counts.get('failed',0) + counts.get('blocked',0) + counts.get('invalid',0)}",
            f"Попыток сегодня: {today_count(state)}"]
    for username, rec in list(state["records"].items())[-10:]:
        rows.append(f"• {username}: {rec.get('status', '—')}")
    return "\n".join(rows)


def _record(state, username, status, note=""):
    rec = state["records"].setdefault(username, {})
    if "attempt_day" not in rec:
        rec["attempt_day"] = day_now()
    rec.update({"status": status, "note": note[:250], "updated_at": iso_now()})


async def run_auto_join(client, *, selected: list[str] | None = None,
                        state_path: Path = STATE_FILE, catalog=None,
                        join_fn=None, pause_fn=None, dry_run: bool | None = None,
                        require_enabled: bool = True) -> dict:
    """Attempt at most a handful of public join operations; honor FloodWait immediately.

    `join_fn(username)` is injection point for tests. It returns 'joined' or
    'pending'. A real client uses channels.JoinChannelRequest only.
    """
    from outgoing import safe_mode
    state = read_state(state_path)
    if safe_mode() or state.get('restriction'):
        return {"message": "SAFE TEST или ограничение Telegram: вступления отключены",
                "attempts": 0, "joined": 0, "pending": 0, "preview": []}
    cooldown = state.get("cooldown_until")
    if cooldown:
        try:
            if datetime.fromisoformat(cooldown) > datetime.now(timezone.utc):
                return {"message": f"Пауза Telegram до {cooldown}", "attempts":0, "joined":0, "pending":0, "preview":[]}
        except (ValueError, TypeError):
            pass
    if require_enabled and (not state["enabled"] or not env_bool("AUTO_JOIN_ENABLED")):
        return {"message": "Автовступление выключено", "attempts": 0, "joined": 0, "pending": 0, "preview": []}
    dry = env_bool("AUTO_JOIN_DRY_RUN", True) if dry_run is None else dry_run
    daily_limit = _int_env("AUTO_JOIN_DAILY_LIMIT", 3, 1, 5)
    total_limit = _int_env("AUTO_JOIN_MAX_TOTAL", 70, 1, 200)
    pause = _int_env("AUTO_JOIN_PAUSE_SECONDS", 180, 120, 7200)
    attempts, joined, pending = 0, 0, 0
    preview: list[str] = []
    choices = candidates(catalog, state, selected=selected)
    if not dry:
        used = today_count(state)
        overall = sum(1 for v in state["records"].values() if v.get("status") in {"joined", "pending"})
        budget = max(0, min(daily_limit-used, total_limit-overall))
        choices = choices[:budget]
    else:
        choices = choices[:daily_limit]
    if not choices:
        return {"message": "Нет подходящих площадок или достигнут лимит", "attempts": 0,
                "joined": 0, "pending": 0, "preview": []}
    if join_fn is None:
        async def telegram_join(username):
            from telethon import functions
            from telethon.errors import RPCError, PeerFloodError
            try:
                entity = await client.get_entity(username)
                # Public Telegram channels and supergroups are Channel in MTProto.
                if not (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)):
                    return "invalid"
                await client(functions.channels.JoinChannelRequest(entity))
                return "joined"
            except PeerFloodError:
                return "blocked"
            except RPCError as exc:
                name = type(exc).__name__.lower()
                msg = str(exc).upper()
                if "INVITE_REQUEST_SENT" in msg or "inviterequestsent" in name:
                    return "pending"
                if "USER_ALREADY_PARTICIPANT" in msg or "useralreadyparticipant" in name:
                    return "joined"
                if "FLOOD_WAIT" in msg or "floodwait" in name:
                    raise JoinFloodWait(getattr(exc, "seconds", 0)) from exc
                if any(z in msg for z in ("BANNED", "CHANNELS_TOO_MUCH", "FROZEN")):
                    return "blocked"
                if any(z in msg for z in ("CHANNEL_PRIVATE", "CHANNEL_INVALID", "USERNAME_INVALID")):
                    return "invalid"
                raise
        join_fn = telegram_join
    for i, username in enumerate(choices):
        if dry:
            preview.append(username)
            continue
        try:
            outcome = await join_fn(username)
            if outcome not in {"joined", "pending", "blocked", "invalid"}:
                outcome = "failed"
            _record(state, username, outcome)
            attempts += 1
            joined += int(outcome == "joined")
            pending += int(outcome == "pending")
            if outcome == 'blocked':
                state['enabled'] = False
                state['restriction'] = 'Telegram blocked joins; manual review required'
            save_state(state, state_path)
            if outcome == 'blocked':
                break
        except JoinFloodWait as exc:
            state["enabled"] = False
            state["restriction"] = "FloodWait; manual review required"
            _record(state, username, "failed", "FloodWait: дальнейшие попытки остановлены")
            state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(seconds=max(300, exc.seconds))).isoformat(timespec="seconds")
            save_state(state, state_path)
            return {"message": f"Telegram ограничил запросы; остановлено. Пауза {exc.seconds} сек.",
                    "attempts": attempts, "joined": joined, "pending": pending, "preview": preview}
        except Exception as exc:
            _record(state, username, "failed", type(exc).__name__)
            save_state(state, state_path)
            attempts += 1
        if i + 1 < len(choices) and pause_fn is not None:
            await pause_fn(pause)
        elif i + 1 < len(choices):
            await asyncio.sleep(pause)
    return {"message": "Проверка без отправки" if dry else "Попытки вступления завершены",
            "attempts": attempts, "joined": joined, "pending": pending, "preview": preview}


class JoinFloodWait(Exception):
    def __init__(self, seconds: int = 0):
        super().__init__("Telegram FloodWait")
        self.seconds = seconds


async def refresh_pending(client, *, state_path: Path = STATE_FILE, fetch_dialog_ids=None) -> dict:
    """Mark pending joins as approved if the channel appears in account dialogs.

    An unapproved/rejected join cannot always be distinguished by MTProto,
    so 'pending' remains until visible membership or owner manually clears it.
    """
    state = read_state(state_path)
    pending = [u for u, r in state["records"].items() if r.get("status") == "pending"]
    if not pending:
        return {"checked": 0, "joined": 0}
    if fetch_dialog_ids is None:
        ids = set()
        async for dialog in client.iter_dialogs():
            username = getattr(dialog.entity, "username", None)
            if username:
                ids.add("@"+username.lower())
    else:
        ids = {normalize_username(v) for v in await fetch_dialog_ids()}
    updated = 0
    for u in pending:
        if u in ids:
            _record(state, u, "joined", "Одобрено: сообщество появилось в диалогах")
            updated += 1
    if updated:
        save_state(state, state_path)
    return {"checked": len(pending), "joined": updated}
