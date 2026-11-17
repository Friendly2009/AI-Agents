"""Owner-triggered inspection of *public community metadata*.

No member enumeration, no private identifiers, no Stories, no content scraping,
no automated outreach to users discovered through other communities.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parents[1]
SEED_COMMUNITIES = ('@alexautolive', '@stokehous', '@mikstovarov')
USER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{4,31}$')


def normalize_public_community(value: str) -> str:
    """Parse a public Telegram @username or t.me link (reject private invites)."""
    value = (value or '').strip()
    if value.startswith('@'):
        username = value[1:]
    else:
        if not value.startswith(('https://', 'http://')):
            value = 'https://' + value
        url = urlparse(value)
        if (url.hostname or '').lower() not in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'}:
            raise ValueError('Укажите публичную ссылку t.me/имя или @имя.')
        pieces = [p for p in url.path.split('/') if p]
        if pieces and pieces[0] == 's':
            pieces.pop(0)
        if len(pieces) != 1:
            raise ValueError('Нужна ссылка именно на публичное сообщество, без поста или приглашения.')
        username = pieces[0]
        if username.lower() in {'joinchat', 'addlist', 'c', 'invite', 'share', 'proxy', 'socks'}:
            raise ValueError('Закрытые приглашения и служебные ссылки не поддерживаются.')
    if not USER_RE.fullmatch(username):
        raise ValueError('Некорректное публичное имя сообщества Telegram.')
    return '@' + username.lower()


def metadata_result(*, username: str, title: str, kind: str, count=None, discussion=None,
                    access: str = 'public_metadata_only', error: str | None = None) -> dict:
    """Do not include user-level data anywhere in saved result."""
    return {
        'username': normalize_public_community(username), 'title': title[:180],
        'type': kind, 'members_or_subscribers': count if isinstance(count, int) and count >= 0 else None,
        'discussion': discussion if discussion and USER_RE.fullmatch(discussion.lstrip('@')) else None,
        'access': access, 'error': error,
        'checked_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds')
    }


async def inspect_community(client, target: str) -> dict:
    """Only queries public metadata. Does NOT request member lists or chat messages."""
    from telethon import functions, types
    source = normalize_public_community(target)
    entity = await client.get_entity(source)
    if not isinstance(entity, types.Channel):
        return metadata_result(username=source,title='Недоступно',kind='personal_account_or_other',
                               access='not_community',error='Ссылка не ведёт на группу или канал.')
    kind = 'группа' if getattr(entity, 'megagroup', False) else 'канал'
    count = getattr(entity, 'participants_count', None)
    discussion = None
    try:
        info = await client(functions.channels.GetFullChannelRequest(channel=entity))
        count = getattr(info.full_chat, 'participants_count', None) or count
        linked = getattr(info.full_chat, 'linked_chat_id', None)
        if linked:
            matching = next((x for x in getattr(info,'chats',[]) if getattr(x,'id',None) == linked),None)
            discussion = getattr(matching,'username',None) if matching else None
    except Exception:
        # Public title/type can still be useful when full-channel request is blocked.
        pass
    return metadata_result(username=source, title=getattr(entity,'title','Сообщество'),
                           kind=kind, count=count, discussion=discussion)


def save_scan(result: dict, file_path: Path | None = None) -> None:
    """Store community-level info only; no people or messages."""
    p = Path(file_path or BASE / 'data' / 'community_scan_history.json')
    p.parent.mkdir(parents=True,exist_ok=True)
    try:
        existing = json.loads(p.read_text(encoding='utf-8')) if p.exists() else []
        if not isinstance(existing,list):
            existing=[]
    except (OSError,ValueError):
        existing=[]
    existing = [x for x in existing if isinstance(x,dict) and x.get('username') != result.get('username')]
    existing.append({k:result.get(k) for k in ('username','title','type','members_or_subscribers',
                                                'discussion','access','error','checked_at_utc')})
    p.write_text(json.dumps(existing[-200:],ensure_ascii=False,indent=2),encoding='utf-8')


def read_scans(file_path: Path | None = None) -> list[dict]:
    p = Path(file_path or BASE/'data'/'community_scan_history.json')
    if not p.exists():
        return []
    try:
        d=json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return d if isinstance(d,list) else []


def format_scan(r: dict) -> str:
    if r['type'] == 'personal_account_or_other':
        return f'⚠️ {r["username"]}: {r.get("error") or "не сообщество"}'
    count = r.get('members_or_subscribers')
    return ('📡 Проверка сообщества: ' + r['username'] + '\n'
            + 'Название: ' + r['title'] + '\n'
            + 'Тип: ' + r['type'] + '\n'
            + 'Участники/подписчики (если доступно): ' + (str(count) if count is not None else 'неизвестно') + '\n'
            + 'Публичное обсуждение: ' + ('@'+r['discussion'].lstrip('@') if r.get('discussion') else 'не определено') + '\n'
            + 'Скрытые участники: без соответствующих прав недоступны.\n'
            + 'Данные о детях, возрастах и городе участников: не устанавливаются.\n'
            + 'Личные профили не выгружаются. Никому сообщения не отправлены.')
