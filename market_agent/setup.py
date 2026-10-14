"""Local onboarding: never accepts a secret through command-line arguments."""

import getpass
import os
import re
import secrets
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from .config import Settings

DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def configure_live(path: Path, key: str, model=DEFAULT_MODEL):
    key, model = key.strip(), model.strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("Ключ должен содержать не менее 20 символов без пробелов.")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", model):
        raise ValueError("Неверный идентификатор модели.")
    current = dotenv_values(path) if path.exists() else {}
    updates = {
        "API_TOKEN": current.get("API_TOKEN") or secrets.token_urlsafe(32),
        "MODE": "live",
        "MARKETS_FILE": "markets.live.json",
        "LLM_ENABLED": "true",
        "LLM_BASE_URL": GEMINI_URL,
        "LLM_API_KEY": key,
        "LLM_MODEL": model,
        "NEWS_COLLECTION_ENABLED": "true",
        "NEWS_POLL_MINUTES": "30",
    }
    # Validate everything before writing. Retain DB path, token, schedule and custom settings.
    validated = Settings(
        _env_file=path if path.exists() else None, **{k.lower(): v for k, v in updates.items()}
    )
    validated.markets()
    text = (
        path.read_text(encoding="utf-8-sig")
        if path.exists()
        else Path(".env.example").read_text(encoding="utf-8")
    )
    lines, handled = [], set()
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=", line)
        name = match.group(1) if match else None
        if name in updates:
            if name not in handled:
                value = updates[name].replace("\\", "\\\\").replace("'", "\\'")
                lines.append(f"{name}='{value}'")
                handled.add(name)
        else:
            lines.append(line)
    for name, value in updates.items():
        if name not in handled:
            value = value.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"{name}='{value}'")
    descriptor, temporary = tempfile.mkstemp(dir=path.resolve().parent, prefix=".env-tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return validated


def interactive_setup(force=False):
    path = Path(".env")
    values = dotenv_values(path) if path.exists() else {}
    if (
        not force
        and values.get("MODE") == "live"
        and values.get("LLM_ENABLED", "").lower() == "true"
        and values.get("LLM_API_KEY")
        and values.get("MARKETS_FILE") == "markets.live.json"
    ):
        settings = Settings()
        settings.markets()
        print("Настройка MOEX + Gemini уже сохранена. Для смены ключа: python -m market_agent setup --force")
        return
    print("Подключение MOEX: SBER и GAZP, RSS-новости, Gemini.")
    print("Ключ будет сохранён только в локальном .env. Ввод скрыт; вставьте ключ и нажмите Enter.")
    print(
        "Используйте ключ проекта Free Tier для бесплатной квоты. Тариф вашего проекта программа не меняет."
    )
    key = getpass.getpass("Gemini API key: ").strip()
    model = input(f"Модель [{DEFAULT_MODEL}], Enter — оставить: ").strip() or DEFAULT_MODEL
    configure_live(path, key, model)
    print("Сохранено. Реальные данные и Gemini включены. Старый API_TOKEN и история сохранены.")
    print("Проверка подключения: python -m market_agent doctor")
