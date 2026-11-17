"""Configuration and pure input helpers; no resource construction."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re

BASE = Path(__file__).resolve().parents[1]
PERMISSION = ('Здравствуйте! Я Анастасия, ассистент Soft Skills. '
              'Можно обработать ваш вопрос с помощью ИИ? Ответьте «да» или «нет».')


def is_greeting(text):
    return re.sub(r"[^а-яa-z]", "", text.lower()) in {"привет", "здравствуйте", "добрыйдень", "доброеутро", "добрыйвечер", "здравствуй"}


def safe_test_mode() -> bool:
    """Fail closed: no outgoing contact, joining, discovery, or inbound AI replies."""
    return os.getenv('SAFE_TEST_MODE', 'true').strip().lower() != 'false'


def session_path() -> Path:
    """Create a fresh Telethon session. Do not reuse a vendor's TData/Auth Key."""
    label = os.getenv('TG_SESSION_NAME', 'anastasia_own_login').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', label):
        raise SystemExit('TG_SESSION_NAME: только буквы, цифры, - и _, от 3 до 40 символов.')
    folder = BASE / 'data' / 'sessions'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / label


def settings():
    aid = os.getenv('TG_API_ID', '').strip()
    ahash = os.getenv('TG_API_HASH', '').strip()
    if not aid.isdigit() or not ahash:
        raise SystemExit('Укажите TG_API_ID и TG_API_HASH в .env (my.telegram.org).')
    return int(aid), ahash


def allowed_groups():
    path = BASE / 'config' / 'approved_groups.json'
    groups = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    return [g for g in groups if g.get('admin_permission') is True and g.get('chat') and g.get('marker')]


