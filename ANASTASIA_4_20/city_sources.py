"""ANASTASIA CITY RADAR: Telegram public COMMUNITY discovery, not user profiling.

Uses Telegram global public-chat title/username search, public community metadata,
category classification and a persistent deduplicated local source catalogue.
No members, personal accounts, Stories, phone numbers or message histories are collected.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / 'data' / 'city_source_catalog.json'
STATE_PATH = ROOT / 'data' / 'city_search_state.json'

# Keywords are searches only: an actual local Telegram channel is NOT assumed to exist.
CATEGORIES = {
    'город': [
        'Александров новости', 'Александров афиша', 'Александров жители',
        'Александров объявления', 'Александров городской чат',
        'Александров', 'Alexandrov',
    ],
    'семьи': [
        'Александров родители', 'Александров мамы', 'Александров дети',
        'Александров с детьми',
    ],
    'развлечения': [
        'Александров детский развлекательный центр', 'Александров игровой центр',
        'Александров детские праздники', 'Диленд Александров', 'ТИМ ПАРК Александров',
        'Диленд', 'ТИМ ПАРК',
    ],
    'школы': [
        'Александров школа', 'Александров лицей', 'Александров гимназия',
        'Александров школьные новости', 'школа Александров Владимирская',
    ],
    'садики': [
        'Александров детский сад', 'Александров дошкольники',
    ],
    'культура': [
        'Александров дом культуры', 'Александров дворец культуры',
        'ДК Юбилейный Александров', 'Искож Александров',
        'ДК Юбилейный',
    ],
    'библиотеки': [
        'Александров библиотека', 'Александров детская библиотека',
        'Александров библиотека семейного чтения',
        'Библиотека семейного чтения',
    ],
    'кино': [
        'Александров кинотеатр', 'кинотеатр Сатурн Александров',
        'кинотеатр Южный Александров', 'кинотеатр Сатурн',
    ],
    'кафе': [
        'Александров кофейня', 'Александров кафе',
        'Мистер кофе Александров', 'Асахи Александров',
        'Мистер кофе', 'Кафе Асахи',
    ],
    'спорт': [
        'Александров спортивная школа', 'Александров секции',
        'Олимп Александров спорт', 'Александров футбол дети',
        'СК Олимп Александров',
    ],
    'творчество': [
        'Александров танцы', 'Александров художественная школа',
        'Александров музыка дети', 'Александров кружки',
    ],
    'магазины': [
        'Александров детский магазин', 'Александров товары для детей',
        'Александров торговый центр', 'ТЦ Саша Александров',
    ],
    'события': [
        'Александров мероприятия', 'Александров мастер классы',
        'Александров выходные', 'Александров праздники',
    ],
    'районы': [
        'Александров Карабаново', 'Александров Струнино',
        'Александров Балакирево', 'Александров район',
    ],
}
ALIASES = {
    'все': 'все', 'ещё': 'все', 'еще': 'все',
    'родители': 'семьи', 'мамы': 'семьи', 'дети': 'семьи',
    'дк': 'культура', 'дворцы': 'культура', 'дворцы культуры': 'культура',
    'библиотека': 'библиотеки', 'школа': 'школы',
    'сад': 'садики', 'детский сад': 'садики',
    'кофейни': 'кафе', 'кофейня': 'кафе',
    'кинотеатры': 'кино', 'кинотеатр': 'кино',
    'спортивные': 'спорт', 'спортивные секции': 'спорт',
    'развлекательные центры': 'развлечения', 'детские центры': 'развлечения',
    'кружки': 'творчество', 'торговые центры': 'магазины',
    'афиша': 'события', 'городские': 'город',
}
SEEDS = [
    {'username': '@alexautolive', 'category': 'город', 'basis': 'пользователь назвал городской канал'},
    {'username': '@stokehous', 'category': 'магазины', 'basis': 'пользователь назвал местное сообщество'},
    {'username': '@mikstovarov', 'category': 'магазины', 'basis': 'пользователь администрирует местную группу'},
    {'username': '@cafe_asahi', 'category': 'кафе', 'basis': 'публичные объявления указывают город Александров'},
    {'username': '@mrcoffeealeks', 'category': 'кафе', 'basis': 'публичное описание указывает адрес в Александрове'},
]


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean(s):
    return unicodedata.normalize('NFKC', str(s or '')).casefold().replace('ё', 'е').strip()


def resolve_category(raw: str) -> str:
    category = clean(raw)
    return ALIASES.get(category, category) if category else 'все'


def queries_for(category: str) -> list[tuple[str, str]]:
    category = resolve_category(category)
    if category == 'все':
        # Interleave so a daily short scan reaches different business categories.
        max_len = max(map(len, CATEGORIES.values()))
        return [(cat, words[i]) for i in range(max_len) for cat, words in CATEGORIES.items() if i < len(words)]
    if category not in CATEGORIES:
        raise ValueError('Неизвестная категория. Команда /категории покажет доступные.')
    return [(category, q) for q in CATEGORIES[category]]


def locality_signal(title: str, about: str = '', username: str = '') -> tuple[str, str]:
    """Conservative LOCATION evidence, not assertion about individual subscribers."""
    s = clean(' '.join([title, about, username]))
    if re.search(r'александровск(?:ое|ий|а|е)?\b', s):
        return 'проверить', 'Название может относиться к другому населённому пункту'
    if re.search(r'\b(обнинск|москва|московская обл|владивосток|петербург|тула|ярославль)\b', s):
        if not re.search(r'\b(владимирская област|александров[,. ]+владимирск)', s):
            return 'проверить', 'В описании упоминается другой город'
    city = bool(re.search(r'\bалександров\b|\baleksandrov\b|\balexandrov\b', s))
    region = bool(re.search(r'владимирск(?:ая|ой|ую|ои)\s+област|\b33\s*регион', s))
    precise = bool(re.search(r'\b(г\.?\s*александров|город\s+александров|александров\s*,\s*владимирск)', s))
    if city and (region or precise):
        return 'подтверждено_в_описании', 'Название/описание прямо указывает на Александров Владимирской области'
    if city:
        return 'возможно', 'Упомянут Александров, требуется проверка контекста'
    return 'проверить', 'Город не подтверждён в открытом описании'


def _load(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def read_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    return _load(Path(path), [])


def init_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    catalog = read_catalog(path)
    known = {r['username'] for r in catalog}
    for item in SEEDS:
        if item['username'] not in known:
            catalog.append({**item, 'title': '(название уточняется Telegram)', 'type': 'проверить',
                            'city_status': 'пользовательский_источник',
                            'city_evidence': item['basis'], 'member_count': None,
                            'first_seen': _utcnow(), 'last_checked': None, 'query': 'начальный источник'})
    _save(Path(path), catalog)
    return catalog


def upsert_sources(items: list[dict], path: Path = CATALOG_PATH) -> dict:
    existing = read_catalog(path)
    by_key = {r['username']: r for r in existing if r.get('username')}
    added = updated = 0
    for raw in items:
        username = str(raw.get('username', '')).lower().strip()
        if not re.fullmatch(r'@[a-z][a-z0-9_]{4,31}', username):
            continue
        if username in by_key:
            r = by_key[username]
            # Keep human verified locality if previously recorded; do not silently overrule.
            if r.get('city_status') == 'подтверждено_вручную':
                raw['city_status'] = r['city_status']
                raw['city_evidence'] = r.get('city_evidence', '')
            r.update({k: v for k, v in raw.items() if k not in {'first_seen'}})
            updated += 1
        else:
            item = {'first_seen': _utcnow(), **raw}
            existing.append(item)
            by_key[username] = item
            added += 1
    _save(Path(path), existing)
    return {'new': added, 'updated': updated, 'total': len(existing)}


def mark_local(username: str, path: Path = CATALOG_PATH) -> bool:
    username = username.strip().lower()
    if not username.startswith('@'):
        username = '@' + username
    items = read_catalog(path)
    for row in items:
        if row.get('username') == username:
            row['city_status'] = 'подтверждено_вручную'
            row['city_evidence'] = 'Владелец проверил сообщество вручную'
            _save(Path(path), items)
            return True
    return False


def catalog_report(category: str = 'все', limit: int = 25, path: Path = CATALOG_PATH):
    cat = resolve_category(category)
    if cat != 'все' and cat not in CATEGORIES:
        raise ValueError('Неизвестная категория')
    items = read_catalog(path)
    items = [r for r in items if cat == 'все' or r.get('category') == cat]
    # Confirmed city candidates first; show unverified honestly.
    rank = {'подтверждено_вручную': 4, 'подтверждено_в_описании': 3,
            'пользовательский_источник': 2, 'возможно': 1}
    items.sort(key=lambda r: (rank.get(r.get('city_status'), 0), r.get('last_checked') or ''), reverse=True)
    out = [f'🗺 Telegram-площадки: {cat} (всего {len(items)}; показано {min(len(items), limit)})']
    for r in items[:limit]:
        quality = r.get('city_status', 'проверить')
        c = r.get('member_count')
        count_str = f' · {c} подписчиков/участников' if isinstance(c, int) else ''
        out.append(f'• {r["username"]} · {r.get("category", "?")} · {quality}{count_str}')
    if not items:
        out.append('Пока нет результатов — /найти ' + (cat if cat != 'все' else 'все'))
    out.append('Это площадки, НЕ контакты родителей. Никакой рассылки нет.')
    return '\n'.join(out)


def export_catalog_csv(path: Path = CATALOG_PATH, export_path: Path | None = None) -> Path:
    export_path = Path(export_path or ROOT / 'data' / 'telegram_alexandrov_communities.csv')
    export_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['username','title','category','type','city_status','city_evidence','member_count','query','last_checked']
    with export_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(read_catalog(path))
    return export_path


def next_queries(category: str = 'все', limit: int = 8, state_path: Path = STATE_PATH) -> list[tuple[str,str]]:
    cat = resolve_category(category)
    q = queries_for(cat)
    if not q:
        return []
    st = _load(Path(state_path), {})
    key = 'cursor_' + cat
    cursor = int(st.get(key, 0)) % len(q)
    amount = min(max(1, int(limit)), len(q))
    results = [q[(cursor+i) % len(q)] for i in range(amount)]
    st[key] = (cursor + amount) % len(q)
    _save(Path(state_path), st)
    return results


async def search_telegram_sources(client, category='все', *, query_limit=8,
                                  per_query=12, sleep_seconds=2.5,
                                  path: Path = CATALOG_PATH, state_path: Path = STATE_PATH):
    """Global TELEGRAM chat-title search. No joining chats, no fetching posts, no member lists."""
    import asyncio
    from telethon import functions, types
    from telethon.errors import FloodWaitError, RPCError
    init_catalog(path)
    plan = next_queries(category, query_limit, state_path)
    seen = {}
    logs = []
    enriched = 0
    for idx,(cat, phrase) in enumerate(plan):
        try:
            result = await client(functions.contacts.SearchRequest(q=phrase, limit=min(per_query,25)))
            chats = getattr(result, 'chats', [])
            logs.append({'query':phrase, 'returned_chats':len(chats)})
            for entry in chats:
                if not isinstance(entry, types.Channel):
                    continue
                username = getattr(entry, 'username', '')
                if not username:
                    continue  # Never store private/invite-only communities
                username = '@' + username.lower()
                if username in seen:
                    continue
                title = str(getattr(entry, 'title', '') or '')[:180]
                kind = 'группа' if getattr(entry,'megagroup',False) else 'канал'
                about = ''
                count = getattr(entry, 'participants_count', None)
                # Enrich only a small number of public community DESCRIPTIONS.
                # This is NOT a messages or participant-list request.
                if enriched < 6:
                    try:
                        full = await client(functions.channels.GetFullChannelRequest(entry))
                        about = str(getattr(full.full_chat, 'about', '') or '')[:1000]
                        full_count = getattr(full.full_chat, 'participants_count', None)
                        count = full_count if isinstance(full_count, int) else count
                    except (RPCError, ValueError):
                        pass
                    enriched += 1
                status, basis = locality_signal(title, about, username)
                seen[username] = {
                    'username': username, 'title': title, 'category': cat,
                    'type': kind, 'city_status': status, 'city_evidence': basis,
                    'member_count': count if isinstance(count,int) and count >= 0 else None,
                    'query': phrase, 'last_checked': _utcnow(),
                }
        except FloodWaitError as e:
            logs.append({'query':phrase, 'error':f'Telegram требует паузу {e.seconds} сек. Поиск остановлен.'})
            break
        except RPCError as e:
            logs.append({'query':phrase, 'error':type(e).__name__})
        if idx + 1 < len(plan) and sleep_seconds:
            await asyncio.sleep(max(0,sleep_seconds))
    stats = upsert_sources(list(seen.values()), path)
    stats.update({'queries':len(logs), 'public_communities_this_scan':len(seen), 'errors':
                  sum(1 for l in logs if 'error' in l), 'details':logs})
    return stats

