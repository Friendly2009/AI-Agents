"""Deliver the capability-bound acknowledgement after a durable receipt."""
from dialogue_control import ACK
from dialogue_control import PRIVACY_RECEIPTS


async def confirm_privacy(store, uid, event_id, action, send, safe=True):
    from delivery import Ledger
    key = f'privacy:{uid}:{action}:{event_id}'
    body = PRIVACY_RECEIPTS[action]
    ledger = Ledger(store)
    ledger.prepare(key, uid, 'privacy_ack', 'dialogue_control', body)
    ledger.validate(key)
    ledger.approve(key)
    return await ledger.deliver(key, lambda: send(body), safe=safe)

class HandoffAcknowledgement:
    def __init__(self, store):
        self.store = store

    def acknowledgement(self, uid):
        from delivery import Ledger
        current = self.store.control.get(uid)
        if current['state'] != 'human' or not current['receipt'] or not current['request']:
            return None
        key = current['request'] + ':ack'
        Ledger(self.store).prepare(key, uid, 'handoff_ack', 'dialogue_control', ACK)
        return key

    async def confirm(self, uid, send, safe=True):
        from delivery import Ledger
        key = self.acknowledgement(uid)
        if not key:
            return False
        ledger = Ledger(self.store)
        ledger.validate(key)
        ledger.approve(key)
        return await ledger.deliver(key, send, safe=safe)
