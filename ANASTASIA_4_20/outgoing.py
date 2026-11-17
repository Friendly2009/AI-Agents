"""Shared policy and durable at-most-once wrapper for application effects."""
import os
from delivery import Ledger

from runtime_policy import safe_mode
from clock_utils import utc_now

async def send_once(store,key,uid,send,safe=safe_mode):
    ledger=Ledger(store)
    # Existing 4.20 effects are suppression records, including interrupted sends.
    if store.db.execute('SELECT 1 FROM effects WHERE effect_key=?',(key,)).fetchone():
        return False
    kind=('initial' if key.startswith('greeting:') else 'campaign' if key.startswith('radar:')
          else 'reminder' if key.startswith(('reminder:','booking:')) else 'reply')
    ledger.prepare(key,uid,kind,'send_once')
    ledger.validate(key);ledger.approve(key)
    try:
        sent=await ledger.deliver(key,send,safe=safe())
    except BaseException:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO effects VALUES(?,?,?)',(key,'uncertain',utc_now()))
        raise
    if sent:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO effects VALUES(?,?,?)',(key,'sent',utc_now()))
    return sent
