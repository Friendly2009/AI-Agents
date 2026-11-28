"""Durable outgoing ledger. No automatic retry of any attempted operation."""
import asyncio
import hashlib
import os
from datetime import datetime, timezone, date, timedelta
from contextvars import ContextVar
from telethon import TelegramClient, utils
from telethon.errors import RPCError
from clock_utils import utc_now

active_send = ContextVar('active_send', default=None)
RESTRICTIONS = {'PeerFloodError','FloodWaitError','AuthKeyUnregisteredError',
                'SessionRevokedError','UserDeactivatedBanError','AuthKeyDuplicatedError',
                'UnauthorizedError','UserDeactivatedError'}

class PolicyBlocked(RuntimeError):
    pass

class Ledger:
    def __init__(self, store):
        self.store, self.db = store, store.db
        from delivery_schema import migrate
        migrate(self.db)

    def event(self, event, key='', kind='conversation', status='', error='', source='application'):
        self.db.execute('INSERT INTO outbound_events(utc,event,operation,kind,status,error,source) VALUES(?,?,?,?,?,?,?)',
                        (utc_now(), event, key, kind, status, error, source))

    def grant(self, uid, source, granted):
        if type(uid) is not int or uid <= 0 or len(source.strip()) < 5:
            raise ValueError('Нужны Telegram ID и проверяемый источник разрешения')
        day = date.fromisoformat(granted)
        if not date.today()-timedelta(days=90) <= day <= date.today():
            raise ValueError('Разрешение должно быть за последние 90 дней')
        # Import never clears an existing refusal.
        if (self.store.get(uid) or {}).get('stopped'):
            raise ValueError('Получатель отказался')
        with self.db:
            self.db.execute('INSERT INTO contact_permission VALUES(?,?,?,?,NULL) ON CONFLICT(uid) DO UPDATE SET source=excluded.source,granted=excluded.granted,status=excluded.status WHERE contact_permission.declined_at IS NULL',
                            (uid,source.strip(),granted,'granted'))

            if self.permission(uid):
                from funnel import Funnel
                Funnel(self.store).emit(uid,'contact_authorized',confirmation='permission_registry',source=source)

    def permission(self, uid):
        r = self.db.execute('SELECT * FROM contact_permission WHERE uid=?',(uid,)).fetchone()
        from policy import valid_contact_permission
        return valid_contact_permission(dict(r) if r else None)

    def check(self, uid, kind, operation=None):
        if self.store.outbound_blocked(): return 'account_restricted'
        privacy_ack = kind == 'privacy_ack' and self.store.control.permits_privacy_ack(operation)
        if kind == 'privacy_ack' and not privacy_ack: return 'invalid_privacy_ack'
        if (self.store.get(uid) or {}).get('stopped') and not privacy_ack: return 'recipient_declined'
        if kind == 'admin': return ''
        config = self.store.configuration().resolve(uid)['values']
        if config['mode']=='paused' or not config['outbound_allowed']: return 'configuration_paused'
        if privacy_ack: return ''
        state = self.store.dialogue(uid)
        if kind == 'handoff_ack':
            if not self.store.control.permits_ack(operation): return 'invalid_handoff_ack'
        elif self.store.control.blocked(uid) or state.get('paused'): return 'human_handoff'
        if kind == 'group_reply': return ''
        if kind == 'initial':
            if not self.permission(uid): return 'permission_missing_or_expired'
            if self.store.ever_contacted(uid) or self.store.history(uid): return 'already_contacted'
        elif kind in {'campaign', 'reminder'}:
            if not self.permission(uid): return 'permission_missing_or_expired'
            if kind == 'campaign':
                last=self.db.execute("SELECT MAX(attempted) FROM outbound_operations WHERE uid=? AND kind='initial' AND simulated=0",(uid,)).fetchone()[0]
                if last is None:
                    previous=self.db.execute('SELECT sent_at FROM sends WHERE user_id=?',(uid,)).fetchone()
                    last=previous[0] if previous else ''
                if not self.db.execute('SELECT 1 FROM inbox WHERE user_id=? AND created_at>?',(uid,last or '')).fetchone():
                    return 'no_recipient_response'
        elif not self.db.execute('SELECT 1 FROM inbox WHERE user_id=?',(uid,)).fetchone():
            return 'no_inbound_context'
        return ''

    def prepare(self, key, uid, kind, source, body='', batch=None):
        with self.db:
            added=self.db.execute('INSERT OR IGNORE INTO outbound_operations(id,uid,kind,source,body,state,created,batch) VALUES(?,?,?,?,?,\'draft\',?,?)',
                                 (key,uid,kind,source,body,utc_now(),batch)).rowcount
            if added: self.event('message_prepared',key,kind,'draft',source=source)
        return key

    def validate(self, key):
        with self.db:
            r=self.row(key)
            if r['state'] != 'draft': return r['state']=='validated'
            why=self.check(r['uid'],r['kind'],r)
            state='blocked_by_policy' if why else 'validated'
            self.db.execute('UPDATE outbound_operations SET state=?,error=? WHERE id=?',(state,why,key))
            self.event('send_policy_checked',key,r['kind'],state,why,r['source'])
        return not why

    def approve(self, key):
        with self.db:
            return self.db.execute("UPDATE outbound_operations SET state='approved' WHERE id=? AND state='validated'",(key,)).rowcount==1

    def row(self,key):
        r=self.db.execute('SELECT * FROM outbound_operations WHERE id=?',(key,)).fetchone()
        return dict(r) if r else None

    def cancel(self,batch):
        with self.db:
            self.db.execute("UPDATE outbound_operations SET state='cancelled' WHERE batch=? AND state IN ('draft','validated','approved')",(batch,))

    def recover(self):
        # Call ONLY after acquiring the existing exclusive process lock.
        with self.db:
            for r in self.db.execute("SELECT * FROM outbound_operations WHERE state='sending'").fetchall():
                self.db.execute("UPDATE outbound_operations SET state='uncertain',error='process_interrupted' WHERE id=?",(r['id'],))
                self.event('send_uncertain',r['id'],r['kind'],'uncertain','process_interrupted',r['source'])
            self.db.execute("UPDATE effects SET state='uncertain' WHERE state='sending'")

    async def deliver(self,key,send,*,safe=True,simulated=False):
        if simulated and not isinstance(getattr(send, '__self__', None), TestTransport):
            raise ValueError('Simulation requires bound TestTransport.send')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            r=self.row(key)
            if not r or r['state'] != 'approved':
                self.db.rollback(); return False
            why=self.check(r['uid'],r['kind'],r)
            if safe and not simulated: why='safe_test_mode'
            if r['kind']=='initial' and not why:
                prior=self.db.execute("SELECT 1 FROM outbound_operations WHERE uid=? AND kind='initial' AND attempted IS NOT NULL AND simulated=?",(r['uid'],int(simulated))).fetchone()
                if simulated:
                    count=self.db.execute("SELECT COUNT(*) FROM outbound_operations WHERE kind='initial' AND substr(attempted,1,10)=? AND simulated=1",(utc_now()[:10],)).fetchone()[0]
                else:
                    count=self.db.execute("""SELECT COUNT(*) FROM (
                        SELECT uid FROM outbound_operations WHERE kind='initial' AND substr(attempted,1,10)=? AND simulated=0
                        UNION SELECT user_id FROM sends WHERE substr(sent_at,1,10)=?
                        UNION SELECT user_id FROM send_audit WHERE substr(sent_at,1,10)=?)""",(utc_now()[:10],)*3).fetchone()[0]
                if prior: why='initial_already_attempted'
                elif count >= max(0,int(os.getenv('INITIAL_DAILY_LIMIT','3'))): why='daily_business_limit'
            if why:
                self.db.execute("UPDATE outbound_operations SET state='blocked_by_policy',error=? WHERE id=?",(why,key))
                self.event('send_policy_checked',key,r['kind'],'blocked_by_policy',why,r['source'])
                self.db.commit(); return False
            self.db.execute("UPDATE outbound_operations SET state='sending',attempted=?,simulated=? WHERE id=?",(utc_now(),int(simulated),key))
            self.event('send_policy_checked',key,r['kind'],'approved',source=r['source'])
            self.event('send_started',key,r['kind'],'sending',source=r['source'])
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise
        token=active_send.set((r['uid'],key,self.store))
        try:
            result=await send(r['uid'],r['body']) if simulated else await send()
        except (Exception, asyncio.CancelledError) as exc:
            code=type(exc).__name__
            restricted=code in RESTRICTIONS or getattr(exc,'code',None) in {401,420}
            state='failed' if isinstance(exc,RPCError) and (getattr(exc,'code',None) or 500)<500 else 'uncertain'
            with self.db:
                if restricted:
                    seconds=max(0,int(getattr(exc,'seconds',0)))
                    self.db.execute("INSERT OR REPLACE INTO safety_state VALUES('outbound_block',?)",(code,))
                    self.db.execute("INSERT OR REPLACE INTO safety_state VALUES('restricted_until',?)",((datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),))
                    self.event('telegram_restricted',key,r['kind'],state,code,r['source'])
                self.db.execute('UPDATE outbound_operations SET state=?,error=? WHERE id=?',(state,code,key))
                self.event('send_'+('failed' if state=='failed' else 'uncertain'),key,r['kind'],state,code,r['source'])
            raise
        finally:
            active_send.reset(token)
        with self.db:
            self.db.execute("UPDATE outbound_operations SET state='sent',message_id=? WHERE id=?",(getattr(result,'id',None) if type(getattr(result,'id',None)) is int else None,key))
            self.event('send_confirmed',key,r['kind'],'sent',source=r['source'])
            if r['kind'] == 'initial' and not simulated:
                from funnel import Funnel
                Funnel(self.store).emit(r['uid'],'conversation_started',confirmation='telegram_send',source=r['source'])
        return True

class TestTransport:
    """No Telegram client and no network capability; isolated test ledger required."""
    def __init__(self, outcomes=()): self.outcomes=list(outcomes); self.messages=[]
    async def send(self,uid,text):
        self.messages.append((uid,text))
        outcome=self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome,BaseException): raise outcome
        from types import SimpleNamespace
        return SimpleNamespace(id=len(self.messages))

class GuardedTelegramClient(TelegramClient):
    def configure_delivery(self,store,admins,manager):
        self.saved_send_lock = asyncio.Lock()
        manager.bind(store)
        self.delivery=Ledger(store);self.delivery_admins=set(admins);self.delivery_manager=manager

    async def __call__(self, request, *args, **kwargs):
        try:
            return await super().__call__(request, *args, **kwargs)
        except RPCError as exc:
            # Resolution/join/read requests can also restrict the account.
            if (type(exc).__name__ in RESTRICTIONS or getattr(exc,'code',None) in {401,420}) and hasattr(self, 'delivery'):
                code=type(exc).__name__
                with self.delivery.db:
                    self.delivery.db.execute("INSERT OR REPLACE INTO safety_state VALUES('outbound_block',?)",(code,))
                    until=(datetime.now(timezone.utc)+timedelta(seconds=max(0,int(getattr(exc,'seconds',0))))).isoformat()
                    self.delivery.db.execute("INSERT OR REPLACE INTO safety_state VALUES('restricted_until',?)",(until,))
                    self.delivery.event('telegram_restricted','rpc','rpc','restricted',code,'telegram_client')
            raise

    async def reply_to_saved_command(self, message_id, text):
        uid = self.saved_messages_user_id
        if not uid:
            raise PolicyBlocked('session_user_unknown')
        async with self.saved_send_lock:
            return await self._direct_admin_reply(uid, message_id, text, saved=True)

    async def generated_saved_message(self, message_id):
        async with self.saved_send_lock:
            return bool(self.delivery.db.execute(
                "SELECT 1 FROM outbound_operations WHERE uid=? AND message_id=? AND state='sent'",
                (self.saved_messages_user_id, message_id)).fetchone())

    async def reply_to_admin_chat(self, uid, message_id, text):
        if uid not in self.delivery_admins or uid <= 0 or not message_id:
            raise PolicyBlocked('unknown_admin_chat')
        return await self._direct_admin_reply(uid, message_id, text, saved=False)

    async def _direct_admin_reply(self, uid, message_id, text, *, saved):
        digest = hashlib.sha256(text.encode()).hexdigest()[:24]
        source = 'saved_messages' if saved else 'admin_conversation'
        prefix = 'saved_reply' if saved else 'admin_chat'
        key = f'{prefix}:{uid}:{message_id}:{digest}'
        ledger = self.delivery
        ledger.prepare(key, uid, 'admin', source)
        if ledger.row(key)['state'] == 'sent':
            return
        ledger.validate(key)
        ledger.approve(key)

        async def send():
            return await super(GuardedTelegramClient, self).send_message(
                'me' if saved else uid, text, reply_to=message_id, parse_mode=None)

        if not await ledger.deliver(key, send, safe=False):
            raise PolicyBlocked(ledger.row(key).get('error') or ledger.row(key)['state'])

    async def send_message(self,entity,message='',*args,**kwargs):
        peer=await self.get_input_entity(entity)
        uid=utils.get_peer_id(peer)
        if uid == getattr(self, 'saved_messages_user_id', None):
            async with self.saved_send_lock:
                return await self._guarded_send(peer, uid, message, *args, **kwargs)
        return await self._guarded_send(peer, uid, message, *args, **kwargs)

    async def _guarded_send(self, peer, uid, message, *args, **kwargs):
        current=active_send.get()
        if current and current[0]==uid and current[2] is self.delivery.store:
            operation = self.delivery.row(current[1])
            why = self.delivery.check(uid, operation['kind'], operation)
            if operation['kind'] in {'handoff_ack', 'privacy_ack'} and str(message) != operation['body']:
                why = 'invalid_handoff_ack'
            if why:
                raise PolicyBlocked(why)
            return await super().send_message(peer,message,*args,**kwargs)
        store=self.delivery.store
        kind='admin' if uid in self.delivery_admins else ('group_reply' if uid<0 and kwargs.get('reply_to') else 'reply')
        mid=store._inbound.get(uid) or kwargs.get('reply_to')
        # No implicit first contact from a raw send_message call.
        digest=hashlib.sha256(str(message).encode()).hexdigest()[:24]
        key=f'{kind}:{uid}:{getattr(mid,"id",mid)}:{digest}'
        self.delivery.prepare(key,uid,kind,'telegram_client')
        self.delivery.validate(key);self.delivery.approve(key)
        result=[]
        async def send():
            value=await super(GuardedTelegramClient,self).send_message(peer,message,*args,**kwargs)
            result.append(value);return value
        from runtime_policy import safe_mode
        admin_reply = kind == "admin" and bool(kwargs.get("reply_to"))
        if not await self.delivery.deliver(key,send,safe=safe_mode() and not admin_reply):
            raise PolicyBlocked(self.delivery.row(key).get('error') or self.delivery.row(key)['state'])
        return result[0]
