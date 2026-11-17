"""Offline preflight; doesn't connect Telegram or leak credentials."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]


def configuration_errors(env):
    """Offline syntax checks only; never claim a credential is valid remotely."""
    errors = []
    for name in ('TG_API_ID', 'ADMIN_OWNER_TG_ID', 'ADMIN_WIFE_TG_ID'):
        value = env.get(name, '').strip()
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            errors.append(name + ': positive numeric ID required')
    if env.get('ADMIN_OWNER_TG_ID') == env.get('ADMIN_WIFE_TG_ID'):
        errors.append('Two distinct administrator IDs required')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', env.get('TG_API_HASH', '').strip()):
        errors.append('TG_API_HASH: 32 hexadecimal characters required')
    if not env.get('OPENAI_API_KEY', '').strip():
        errors.append('OPENAI_API_KEY: required; remote validity not checked')
    if not env.get('OPENAI_MODEL', 'gpt-5-mini').strip():
        errors.append('OPENAI_MODEL: must not be empty')
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', env.get('TG_SESSION_NAME', 'anastasia_own_login').strip()):
        errors.append('TG_SESSION_NAME: invalid label')
    for name in ('SAFE_TEST_MODE', 'CRM_ENABLED'):
        if env.get(name, 'true' if name == 'SAFE_TEST_MODE' else 'false').strip().lower() not in ('true', 'false'):
            errors.append(name + ': expected true or false')
    return errors


def run() -> int:
    dotenv_path = ROOT / '.env'
    if dotenv_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
    print('ANASTASIA 4.26 RC1 — OFFLINE READINESS CHECK')
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
    errors = configuration_errors(os.environ)
    for name in ['telethon','dotenv','openai']:
        print('Package', name, 'installed' if importlib.util.find_spec(name) is not None else 'NOT INSTALLED')
        if importlib.util.find_spec(name) is None:
            errors.append('Missing dependency: ' + name)
    suspicious = [x.name for x in ROOT.glob('*.session')] + [x.name for x in ROOT.glob('tdata*')]
    if suspicious:
        print('WARNING: Legacy/vendor session artifacts found in root. Do not load:', ', '.join(suspicious))
    else:
        print('Vendor session files in project root: none')
    print('Ready for first test only when account ownership/authorization is legitimate.')
    for error in errors:
        print('ERROR:', error)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(run())
