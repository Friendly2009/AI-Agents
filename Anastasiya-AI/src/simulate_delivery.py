"""Offline five-recipient delivery demonstration; never constructs a Telegram client."""
import argparse
import asyncio
from datetime import date
import json
import os
from storage import Store
from delivery import Ledger, TestTransport
from invitations import greeting

async def simulate(limit=3, error='none'):
    store=Store(':memory:');ledger=Ledger(store)
    previous=os.environ.get('INITIAL_DAILY_LIMIT')
    os.environ['INITIAL_DAILY_LIMIT']=str(limit)
    try:
        from telethon.errors import PeerFloodError, FloodWaitError
        failures={'none':[], 'timeout':[TimeoutError()], 'disconnect':[ConnectionError()],
                  'peerflood':[PeerFloodError(None)],'floodwait':[FloodWaitError(None,capture=60)]}
        transport=TestTransport(failures[error])
        for uid in range(1,6):
            ledger.grant(uid,'offline:synthetic-permission',date.today().isoformat())
            ledger.prepare(str(uid),uid,'initial','offline-demo',greeting('Тестовый адресат'),'demo')
            ledger.validate(str(uid));ledger.approve(str(uid))
        for uid in range(1,6):
            try: await ledger.deliver(str(uid),transport.send,simulated=True)
            except Exception: break
        return {'external_deliveries':0,'simulated_requests':len(transport.messages),
                'results':[{'id':r['id'],'state':r['state'],'error':r['error']} for r in map(lambda i:ledger.row(str(i)),range(1,6))],
                'events':[dict(r) for r in store.db.execute('SELECT * FROM outbound_events')]}
    finally:
        store.db.close()
        if previous is None: os.environ.pop('INITIAL_DAILY_LIMIT',None)
        else: os.environ['INITIAL_DAILY_LIMIT']=previous

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,default=3)
    parser.add_argument('--error',choices=['none','timeout','disconnect','peerflood','floodwait'],default='none')
    args=parser.parse_args()
    print(json.dumps(asyncio.run(simulate(args.limit,args.error)),ensure_ascii=False,indent=2))
