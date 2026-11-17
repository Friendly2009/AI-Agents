"""Deliver the capability-bound acknowledgement after a durable receipt."""
from dialogue_control import ACK

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
