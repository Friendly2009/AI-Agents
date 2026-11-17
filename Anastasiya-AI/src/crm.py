"""AlfaCRM v2api: creates an unconfirmed lead, not a booked lesson."""
from __future__ import annotations
import asyncio
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.parse import urlparse


def _post(url, data, headers=None):
    request = Request(url, method="POST", data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    with urlopen(request, timeout=12) as response:
        return json.load(response)


def _config(city):
    suffix = "ALEX" if city == "Александров" else "YARO" if city == "Ярославль" else ""
    if not suffix:
        return None
    base = os.getenv(f"ALFACRM_{suffix}_URL", "").strip().rstrip("/")
    email = os.getenv(f"ALFACRM_{suffix}_EMAIL", "").strip()
    key = os.getenv(f"ALFACRM_{suffix}_API_KEY", "").strip()
    branch = os.getenv(f"ALFACRM_{suffix}_BRANCH", "").strip()
    source = os.getenv(f"ALFACRM_{suffix}_LEAD_SOURCE", "").strip()
    if not all([base, email, key, branch]):
        return None
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in {'', '/'} or not branch.isdigit():
        raise ValueError('Используйте https://hostname и числовой ID филиала для AlfaCRM')
    return base, email, key, branch, source


def normalized_phone(value):
    digits = re.sub(r'\D', '', str(value))
    if len(digits)==11 and digits[0]=='8':
        digits='7'+digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError('Invalid phone')
    return digits


def _create(customer, lookup_only=False):
    c = _config(customer["city"])
    if not c:
        return None
    customer = dict(customer, phone=normalized_phone(customer['phone']))
    base, email, api_key, branch, source = c
    auth = _post(f"{base}/v2api/auth/login", {"email": email, "api_key": api_key})
    token = auth.get("token")
    if not token:
        raise RuntimeError('AlfaCRM не вернула токен')
    body = {"name": customer["name"], "phone": [customer["phone"]], "legal_type": 1, "is_study": 0,
            "note": "Telegram Outreach (AI). Просьба подтвердить запись. "
                    f"Город: {customer['city']}; направление: {customer.get('course') or 'не выбрано'}; "
                    f"возраст: {customer.get('child_age') or 'не указан'}; "
                    f"Telegram: @{customer.get('username') or 'без username'}; ID {customer.get('user_id')}; "
                    f"удобное время: {customer.get('wanted_time') or 'не указано'}."}
    if source.isdigit():
        body["lead_source_id"] = int(source)
    # Prevent duplicate leads in AlfaCRM when a matching phone already exists.
    def digits(value):
        try: return normalized_phone(value)
        except ValueError: return None
    for page in range(100):
        found = _post(f"{base}/v2api/{branch}/customer/index",
                      {"phone": customer["phone"], "is_study": 2, "removed": 1, "page": page},
                      {"X-ALFACRM-TOKEN": token})
        if not isinstance(found, dict) or not isinstance(found.get("items"), list):
            raise RuntimeError("AlfaCRM lookup failed; creation blocked")
        items = found['items']
        if not items:
            break
        for item in items:
            existing_id = item.get("id")
            phones = item.get('phone', [])
            if not isinstance(phones, list):
                phones = [phones]
            if existing_id and any(digits(x) == digits(customer['phone']) for x in phones):
                return int(existing_id)
    else:
        raise RuntimeError('AlfaCRM lookup pagination limit; creation blocked')
    if lookup_only:
        return None
    # In AlfaCRM each branch is routed through /{branch}/customer/create.
    result = _post(f"{base}/v2api/{branch}/customer/create", body, {"X-ALFACRM-TOKEN": token})
    if not result.get("success") or not result.get("model", {}).get("id"):
        raise RuntimeError('AlfaCRM отказала в создании лида')
    return int(result["model"]["id"])


async def create_lead(customer):
    """None means integration not configured; no fake success."""
    from runtime_policy import safe_mode
    if safe_mode():
        return None
    if not customer.get('crm_consent') or not customer.get('wants_trial') or customer.get('stopped'):
        raise ValueError('Explicit active consent and request required')
    if not str(customer.get('name') or '').strip():
        raise ValueError('Name required')
    return await asyncio.to_thread(_create, customer)


async def reconcile_lead(customer):
    """Read-only recovery: never create again after an ambiguous write."""
    from runtime_policy import safe_mode
    if safe_mode(): return None
    if not customer.get('crm_consent') or customer.get('stopped'):
        raise ValueError('Active consent required')
    return await asyncio.to_thread(_create, customer, True)
