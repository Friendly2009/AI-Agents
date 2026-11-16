"""Conservative at-most-once effects. Ambiguous attempts require human review."""
import asyncio
import os
from telethon.errors import FloodWaitError, PeerFloodError


def safe_mode():
    return os.getenv('SAFE_TEST_MODE', 'true').strip().lower() != 'false'


async def send_once(store, key, uid, send, safe=safe_mode):
    config = store.configuration().resolve(uid)['values']
    if config['mode'] == 'paused' or not config['outbound_allowed']:
        return False
    if safe() or store.outbound_blocked() or (store.get(uid) or {}).get('stopped'):
        return False
    if not store.claim_effect(key):
        return False
    try:
        await send()
    except (FloodWaitError, PeerFloodError) as exc:
        store.block_outbound(type(exc).__name__)
        store.finish_effect(key, 'uncertain')
        raise
    except (Exception, asyncio.CancelledError):
        store.finish_effect(key, 'uncertain')
        raise
    store.finish_effect(key, 'sent')
    return True
