"""Offline preflight; doesn't connect Telegram or leak credentials."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parent


def run() -> int:
    dotenv_path = ROOT / '.env'
    if dotenv_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
    print('ANASTASIA 4.5 — OFFLINE READINESS CHECK')
    print('Telegram network access: NONE. No outgoing messages.')
    print('dotenv:', 'exists' if dotenv_path.exists() else 'MISSING (.env.example -> .env)')
    print('Safe mode:', 'ON' if os.getenv('SAFE_TEST_MODE', 'true').lower() != 'false' else 'OFF — WARNING')
    for name in ['TG_API_ID','TG_API_HASH','OPENAI_API_KEY','ADMIN_OWNER_TG_ID','ADMIN_WIFE_TG_ID']:
        print(name, 'present' if os.getenv(name, '').strip() else 'not set')
    owner=os.getenv('ADMIN_OWNER_TG_ID','').strip()
    wife=os.getenv('ADMIN_WIFE_TG_ID','').strip()
    valid_ids=bool(owner and wife and owner.isdigit() and wife.isdigit() and owner!=wife)
    print('Two independent director accounts:', 'CONFIGURED' if valid_ids else 'LOCKED (set both numeric Telegram IDs)')
    try:
        label = os.getenv('TG_SESSION_NAME', 'anastasia_own_login')
        assert re.fullmatch('[A-Za-z0-9_-]{3,40}', label)
    except AssertionError:
        print('ERROR: TG_SESSION_NAME contains invalid characters')
        return 1
    print('New session name:', label, '(under data/sessions; no seller session import)')
    for name in ['telethon','dotenv','openai']:
        print('Package', name, 'installed' if importlib.util.find_spec(name) is not None else 'NOT INSTALLED')
    suspicious = [x.name for x in ROOT.glob('*.session')] + [x.name for x in ROOT.glob('tdata*')]
    if suspicious:
        print('WARNING: Legacy/vendor session artifacts found in root. Do not load:', ', '.join(suspicious))
    else:
        print('Vendor session files in project root: none')
    print('Ready for first test only when account ownership/authorization is legitimate.')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
