"""Offline commands and explicitly owned test conversations."""
from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from manager import Manager
from storage import Store
from city_sources import init_catalog, search_telegram_sources

LOG = logging.getLogger('softskills')


async def send_optins(app, client):
    print('Режим CSV-аутрича из v2 в FAMILY RADAR v3 отключён: он не подтверждает возраст ребёнка. Используйте регистрацию /семья и send-radar.')
    return


async def send_radar(app, client):
    """Unattended campaign sending is disabled: each list needs approval."""
    print('Для кампании: /кампания_список, проверка адресатов, /отправить. Автоматическая отправка отключена.')


async def discover(app, client):
    """Daily rotating search across Alexandrov categories. Community metadata only."""
    init_catalog()
    limit = max(1, min(12, int(os.getenv('CITY_RADAR_QUERIES_PER_RUN','8'))))
    stats = await search_telegram_sources(client, 'все', query_limit=limit)
    LOG.info('CITY RADAR: queries=%s new=%s total=%s errors=%s',
             stats['queries'], stats['new'], stats['total'], stats['errors'])
    return stats


async def retry_crm(app, client):
    for contact in app.STORE.list_ready():
        await app.maybe_crm(client, contact['user_id'])
        await asyncio.sleep(1)


def reset_test(app, user_id):
    previous = app.TEST_SESSIONS.pop(user_id, None)
    if previous:
        previous[0].db.close()
        previous[1].db.close()
    app.TEST_SESSIONS[user_id] = (Store(":memory:"), Manager(":memory:"))
    app.TEST_SESSIONS[user_id][0].analytics_test = True
    app.TEST_SESSIONS[user_id][1].bind(app.TEST_SESSIONS[user_id][0])
    app.TESTERS.add(user_id)


