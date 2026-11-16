"""Import *documented* first-party opt-ins into Anastasia's existing FamilyRadar.

No user enumeration, image analysis, Telegram profile scraping or cold-contact lists.
A CSV row is NOT evidence of consent: operator must independently verify it.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from family_radar import FamilyRadar, parse_ages, now

FIELDS = ('telegram_id', 'first_name', 'source', 'city', 'child_ages',
          'data_consent_at', 'marketing_consent_at', 'consent_evidence')


def _timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Дата должна быть в ISO 8601 с часовым поясом') from exc
    if dt.tzinfo is None:
        raise ValueError('Дата согласия должна содержать часовой пояс')
    dt = dt.astimezone(timezone.utc)
    difference = datetime.now(timezone.utc) - dt
    if difference < timedelta(0) or difference > timedelta(days=90):
        raise ValueError('Дата согласия должна быть реальной и не старше 90 дней')
    return dt.isoformat()


def parse_verified(row: dict) -> dict:
    """Validate eligibility only, never infer it from a username or profile image."""
    try:
        uid = int((row.get('telegram_id') or '').strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError('Нет корректного Telegram ID') from exc
    if uid <= 0:
        raise ValueError('Telegram ID должен быть положительным')
    name = (row.get('first_name') or '').strip()
    if not 1 <= len(name) <= 80 or any(ord(c)<32 for c in name):
        raise ValueError('Укажите имя совершеннолетнего родителя')
    source = (row.get('source') or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9_@.-]{4,60}', source):
        raise ValueError('Неверный идентификатор источника')
    if (row.get('city') or '').strip().casefold() != 'александров':
        raise ValueError('Нужна подтверждённая семья из Александрова')
    ages = parse_ages((row.get('child_ages') or '').strip())
    if not ages:
        raise ValueError('Возраст ребёнка не подтверждён в диапазоне 7–17')
    evidence = (row.get('consent_evidence') or '').strip()
    # Evidence is *operator-provided reference*, not independent proof of opt-in.
    if len(evidence) < 12 or len(evidence)>240 or evidence.lower() in {'yes','да','true','unknown'}:
        raise ValueError('Нужна ссылка/идентификатор сохранённой записи с отдельными согласиями')
    consent_data = _timestamp(row.get('data_consent_at') or '')
    consent_marketing = _timestamp(row.get('marketing_consent_at') or '')
    return {'user_id':uid,'first_name':name,'source':source,
            'child_ages':','.join(map(str,ages)),
            'data_consent_at':consent_data,'marketing_consent_at':consent_marketing,
            'consent_evidence':evidence}


def import_verified_csv(path: str | Path, radar: FamilyRadar) -> dict:
    """Import opt-ins only with a supplied consent reference. Skip blocked contacts.

    This function intentionally does not send any messages or create CRM leads.
    """
    p = Path(path)
    result = {'imported':0,'updated':0,'skipped':0,'errors':[]}
    with p.open('r', encoding='utf-8-sig', newline='') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames or not set(FIELDS).issubset(set(reader.fieldnames)):
            raise ValueError('CSV должен содержать колонки: '+', '.join(FIELDS))
        for num,row in enumerate(reader,start=2):
            try:
                obj=parse_verified(row)
                old = radar.get(obj['user_id'])
                if old and (old['stopped'] or old['marketing_revoked_at']):
                    raise ValueError('Контакт отказался от сообщений; повторный импорт запрещён')
                if old and old['data_consent_at'] and old['marketing_consent_at']:
                    result['skipped']+=1
                    continue  # Never overwrite stronger in-chat consent with CSV metadata.
                radar.import_optin(obj)
                result['imported']+=1
            except ValueError as exc:
                result['skipped']+=1
                result['errors'].append({'row':num,'error':str(exc)})
    return result


def source_breakdown(radar: FamilyRadar) -> dict:
    """Aggregated (not individual) community attribution and eligible families."""
    result=[]
    sql='''SELECT source,COUNT(*) AS total,
      SUM(CASE WHEN adult_confirmed=1 AND city='Александров' AND
               child_ages IS NOT NULL AND data_consent_at IS NOT NULL THEN 1 ELSE 0 END) AS verified,
      SUM(CASE WHEN stage='complete' AND stopped=0 AND
               marketing_consent_at IS NOT NULL THEN 1 ELSE 0 END) AS opted_in
      FROM families GROUP BY source ORDER BY total DESC'''
    for row in radar.db.execute(sql):
        result.append({'source':row['source'], 'total':row['total'],
                       'verified':row['verified'] or 0,'opted_in':row['opted_in'] or 0})
    return {'sources':result,'summary':radar.summary()}


def format_pipeline(data: dict) -> str:
    lines=['📊 ANASTASIA · КЛИЕНТСКАЯ ВОРОНКА',
           f'Регистраций: {data["summary"]["registered"]}',
           f'Подтверждённых семей Александрова: {data["summary"]["verified_families"]}',
           f'Согласий на приглашения: {data["summary"]["allowed_outreach"]}',
           'Источники:']
    for s in data['sources'][:15]:
        lines.append(f'• {s["source"]}: всего {s["total"]}, семей {s["verified"]}, для приглашений {s["opted_in"]}')
    if not data['sources']:
        lines.append('Пока нет зарегистрированных семей.')
    lines.append('Сканирование канала не создаёт лидов: необходимы данные и согласие родителя.')
    return '\n'.join(lines)
