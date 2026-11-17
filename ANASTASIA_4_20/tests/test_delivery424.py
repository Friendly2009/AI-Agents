import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telethon import TelegramClient, types
from telethon.errors import (PeerFloodError,FloodWaitError,UserPrivacyRestrictedError,
    InputUserDeactivatedError,AuthKeyUnregisteredError,PeerIdInvalidError,ServerError)
from delivery import Ledger, TestTransport as OfflineTransport, GuardedTelegramClient, PolicyBlocked
from storage import Store
from manager import Manager
from invitations import Invitations, greeting, parse_names
from outgoing import send_once

@pytest.fixture
def store():
    s=Store(':memory:')
    yield s
    s.db.close()

def approved(s,key='one',uid=42,kind='initial',batch='batch'):
    l=Ledger(s)
    if kind=='initial': l.grant(uid,'signed-form:123',date.today().isoformat())
    else: s.claim_inbound(uid,1,'вопрос')
    l.prepare(key,uid,kind,'test',greeting('Анна'),batch)
    assert l.validate(key)
    assert l.approve(key)
    return l

def run(coro): return asyncio.run(coro)

def test_one_initial_and_duplicate_contact(store):
    l=approved(store);t=OfflineTransport()
    assert run(l.deliver('one',t.send,simulated=True))
    assert not run(l.deliver('one',t.send,simulated=True))
    approved(store,'two')
    assert not run(l.deliver('two',t.send,simulated=True))
    assert len(t.messages)==1
    assert parse_names('42 42 @parent @PARENT')==['42','parent']

@pytest.mark.parametrize('limit,expected',[(3,3),(5,5)])
def test_five_recipients(store,monkeypatch,limit,expected):
    monkeypatch.setenv('INITIAL_DAILY_LIMIT',str(limit));t=OfflineTransport()
    for uid in range(1,6):
        l=approved(store,str(uid),uid);run(l.deliver(str(uid),t.send,simulated=True))
    assert len(t.messages)==expected
    assert all('Анастасия' in msg for _,msg in t.messages)

@pytest.mark.parametrize('after_first',[False,True])
def test_refusal_blocks_all_future_sends(store,after_first):
    l=approved(store);t=OfflineTransport()
    if after_first: assert run(l.deliver('one',t.send,simulated=True))
    store.erase(42)
    for kind in ('initial','reply','campaign','reminder','admin'):
        key=kind+'next';l.prepare(key,42,kind,'test');assert not l.validate(key)
        assert l.row(key)['error']=='recipient_declined'
    assert not run(l.deliver('one',t.send,simulated=True))
    with pytest.raises(ValueError): l.grant(42,'new form',date.today().isoformat())

@pytest.mark.parametrize('kind',['initial','reply','reminder','campaign'])
def test_missing_permission_or_context(store,kind):
    l=Ledger(store);l.prepare('x',42,kind,'test');assert not l.validate('x')
    assert l.row('x')['state']=='blocked_by_policy'

def test_expired_permission_after_preview(store):
    l=approved(store)
    with store.db: store.db.execute('UPDATE contact_permission SET granted=?',((date.today()-timedelta(days=91)).isoformat(),))
    t=OfflineTransport();assert not run(l.deliver('one',t.send,simulated=True));assert not t.messages

@pytest.mark.parametrize('error',[TimeoutError(),ConnectionError(),asyncio.CancelledError(),ServerError(None,'SERVER_ERROR')])
def test_ambiguous_delivery_never_retries(store,error):
    l=approved(store);t=OfflineTransport([error])
    with pytest.raises(type(error)): run(l.deliver('one',t.send,simulated=True))
    assert l.row('one')['state']=='uncertain'
    assert not run(l.deliver('one',t.send,simulated=True));assert len(t.messages)==1

@pytest.mark.parametrize('error',[PeerFloodError(None),FloodWaitError(None,capture=60),AuthKeyUnregisteredError(None)])
def test_restriction_blocks_entire_account(store,error):
    l=approved(store);approved(store,'two',43);t=OfflineTransport([error])
    with pytest.raises(type(error)): run(l.deliver('one',t.send,simulated=True))
    assert store.outbound_blocked();assert l.row('one')['state']=='failed'
    assert not run(l.deliver('two',t.send,simulated=True));assert len(t.messages)==1
    assert store.db.execute("SELECT value FROM safety_state WHERE name='restricted_until'").fetchone()

@pytest.mark.parametrize('error',[UserPrivacyRestrictedError(None),InputUserDeactivatedError(None),PeerIdInvalidError(None)])
def test_recipient_error_is_failed(store,error):
    l=approved(store);t=OfflineTransport([error])
    with pytest.raises(type(error)): run(l.deliver('one',t.send,simulated=True))
    assert l.row('one')['state']=='failed';assert not store.outbound_blocked()
    assert not run(l.deliver('one',t.send,simulated=True))

def test_restart_after_process_dies(tmp_path):
    path=str(tmp_path/'test.db');s=Store(path);l=approved(s)
    class ProcessDeath(BaseException): pass
    async def crash(): raise ProcessDeath()
    with pytest.raises(ProcessDeath): run(l.deliver('one',crash,safe=False))
    assert l.row('one')['state']=='sending';s.db.close();s=Store(path);l=Ledger(s)
    try:
        l.recover();assert l.row('one')['state']=='uncertain'
        send=AsyncMock();assert not run(l.deliver('one',send,safe=False));send.assert_not_called()
        assert s.db.execute("SELECT 1 FROM outbound_events WHERE event='send_uncertain'").fetchone()
    finally: s.db.close()

def test_two_connections_same_recipient_and_queue(tmp_path):
    path=str(tmp_path/'test.db');a=Store(path);b=Store(path)
    try:
        x=approved(a,'a');y=approved(b,'b')
        async def concurrent():
            calls=[]
            async def send(): calls.append(42);await asyncio.sleep(0)
            result=await asyncio.gather(x.deliver('a',send,safe=False),y.deliver('a',send,safe=False),y.deliver('b',send,safe=False))
            assert sum(result)==1;assert calls==[42]
        run(concurrent())
    finally: a.db.close();b.db.close()

def test_cancel_queue_persists(store):
    l=approved(store);l.cancel('batch');assert l.row('one')['state']=='cancelled'
    send=AsyncMock();assert not run(l.deliver('one',send,safe=False));send.assert_not_called()

def test_safe_never_calls_real_callback(store):
    l=approved(store);send=AsyncMock();assert not run(l.deliver('one',send));send.assert_not_called()
    assert l.row('one')['error']=='safe_test_mode'
    with pytest.raises(ValueError): run(l.deliver('one',send,simulated=True))

def test_journal_minimal_structured_data(store):
    l=approved(store);t=OfflineTransport();run(l.deliver('one',t.send,simulated=True))
    rows=[dict(r) for r in store.db.execute('SELECT * FROM outbound_events')]
    assert {'message_prepared','send_policy_checked','send_started','send_confirmed'} <= {r['event'] for r in rows}
    assert all(r['utc'].endswith('+00:00') and r['operation']=='one' and r['source']=='test' for r in rows)
    assert all('body' not in r and 'text' not in r for r in rows)

def test_handoff_and_campaign_no_reply(store):
    l=approved(store);assert l.check(42,'campaign')=='no_recipient_response'
    store.claim_inbound(42,1,'да');assert not l.check(42,'campaign')
    store.control.takeover(42)
    assert l.check(42,'reply')=='human_handoff';assert l.check(42,'initial')=='human_handoff'

async def make_guard(store,monkeypatch,admins=()):
    client=GuardedTelegramClient(None,123,'0'*32);manager=Manager(':memory:')
    client.configure_delivery(store,admins,manager)
    async def entity(value):
        if isinstance(value,types.InputPeerUser): return value
        return types.InputPeerUser(int(value),0)
    monkeypatch.setattr(client,'get_input_entity',entity)
    raw=AsyncMock(return_value=SimpleNamespace(id=17));monkeypatch.setattr(TelegramClient,'send_message',raw)
    return client,manager,raw

def test_client_boundary_raw_reply_and_handoff(store,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','false')
    async def scenario():
        client,m,raw=await make_guard(store,monkeypatch)
        try:
            with pytest.raises(PolicyBlocked): await client.send_message(42,'cold')
            store.claim_inbound(42,1,'да')
            assert (await client.send_message(42,'ответ')).id==17;assert raw.await_count==1
            with pytest.raises(PolicyBlocked): await client.send_message(42,'ответ')
            m.handoff(42)
            with pytest.raises(PolicyBlocked): await client.send_message(42,'другой ответ')
            assert raw.await_count==1
        finally: m.db.close()
    run(scenario())

def test_event_reply_uses_client_boundary(store,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','false')
    async def scenario():
        client,m,raw=await make_guard(store,monkeypatch)
        try:
            from telethon.tl.custom.message import Message
            event=Message(id=12,peer_id=types.PeerUser(42),message='вопрос');event._client=client;event._input_chat=types.InputPeerUser(42,0)
            with pytest.raises(PolicyBlocked): await event.reply('no context')
            store.claim_inbound(42,12,'вопрос');await event.reply('ответ');assert raw.await_count==1
        finally: m.db.close()
    run(scenario())

def test_client_safe_blocks_admin(store,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','true')
    async def scenario():
        client,m,raw=await make_guard(store,monkeypatch,(42,))
        try:
            with pytest.raises(PolicyBlocked): await client.send_message(42,'admin')
            raw.assert_not_called()
        finally: m.db.close()
    run(scenario())

def test_send_once_does_not_double_reserve(store,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','false');store.claim_inbound(42,1,'да')
    async def scenario():
        client,m,raw=await make_guard(store,monkeypatch)
        try:
            assert await send_once(store,'reply:42:1',42,lambda:client.send_message(42,'ответ'),lambda:False)
            assert raw.await_count==1
            assert store.db.execute('SELECT COUNT(*) FROM outbound_operations').fetchone()[0]==1
        finally: m.db.close()
    run(scenario())

def test_admin_prepare_exclude_confirm_restore(store):
    async def scenario():
        l=Ledger(store)
        for uid in (42,43): l.grant(uid,'signed form',date.today().isoformat())
        client=SimpleNamespace(get_entity=AsyncMock(side_effect=lambda uid: types.User(id=int(uid),first_name='Анна')),send_message=AsyncMock(return_value=SimpleNamespace(id=9)))
        q=Invitations(client,store,lambda:False);event=SimpleNamespace(sender_id=100,raw_text='/приглашения',reply=AsyncMock())
        await q.handle(event);event.raw_text='42 42 43';await q.handle(event);assert len(q.rows(100))==2
        event.raw_text='/исключить 43';await q.handle(event);batch=q.drafts[100]
        event.raw_text='/отправить';await q.handle(event);await q.task;assert client.send_message.await_count==1
        q2=Invitations(client,store,lambda:False);event.raw_text='/очередь '+batch;await q2.handle(event)
        event.raw_text='/отправить';await q2.handle(event);await q2.task;assert client.send_message.await_count==1
        event.raw_text='/результаты';await q2.handle(event)
        assert 'sent' in event.reply.call_args.args[0];assert 'cancelled' in event.reply.call_args.args[0]
    run(scenario())

def test_legacy_effect_is_never_replayed(store):
    store.claim_inbound(42,1,'hello')
    assert store.claim_effect('old-reminder')
    send=AsyncMock()
    assert not run(send_once(store,'old-reminder',42,send,lambda:False))
    send.assert_not_called()

def test_legacy_daily_sends_count_toward_limit(store):
    for uid in (1,2,3):
        store.ensure(uid);store.mark_sent(uid,'legacy')
    l=approved(store);send=AsyncMock()
    assert not run(l.deliver('one',send,safe=False))
    assert l.row('one')['error']=='daily_business_limit';send.assert_not_called()

def test_automated_outgoing_is_not_a_manual_opening(store):
    from manual_dialogue import remember_opening
    l=approved(store);m=Manager(':memory:')
    async def send():
        assert not remember_opening(store,m,42,'automatic greeting')
    try:
        assert run(l.deliver('one',send,safe=False))
        assert not store.history(42)
    finally: m.db.close()

def test_campaign_requires_reply_after_initial_attempt(store):
    store.claim_inbound(42,1,'registration')
    l=approved(store)
    assert run(l.deliver('one',AsyncMock(),safe=False))
    assert l.check(42,'campaign')=='no_recipient_response'
    store.claim_inbound(42,2,'да')
    assert not l.check(42,'campaign')

def test_manual_without_username_continues_on_short_reply(store,monkeypatch):
    from manual_dialogue import remember_opening
    m=Manager(':memory:')
    try:
        assert remember_opening(store,m,42,'Что вас интересует?',username='')
        assert not remember_opening(store,m,42,'Что вас интересует?',username='')
        store.claim_inbound(42,1,'да')
        assert not Ledger(store).check(42,'reply')
        assert len(store.history(42))==1
    finally: m.db.close()

def test_resolution_rpc_restriction_persists(store,monkeypatch):
    async def scenario():
        client,m,raw=await make_guard(store,monkeypatch)
        try:
            monkeypatch.setattr(TelegramClient,'__call__',AsyncMock(side_effect=FloodWaitError(None,capture=12)))
            with pytest.raises(FloodWaitError): await client(object())
            assert store.outbound_blocked()
            assert store.db.execute("SELECT 1 FROM outbound_events WHERE event='telegram_restricted'").fetchone()
        finally: m.db.close()
    run(scenario())

def test_long_campaign_preview_is_complete_and_chunked(store,monkeypatch):
    import campaigns
    monkeypatch.setattr(campaigns,'compose_first_message',lambda name:'x'*1100)
    async def scenario():
        ledger=Ledger(store)
        for uid in range(1,11): ledger.grant(uid,'signed form',date.today().isoformat())
        client=SimpleNamespace(get_entity=AsyncMock(side_effect=lambda uid:types.User(id=int(uid),first_name='Анна')))
        q=Invitations(client,store,lambda:True)
        event=SimpleNamespace(sender_id=100,raw_text='/кампания_список',reply=AsyncMock())
        await q.handle(event);event.reply.reset_mock();event.raw_text=' '.join(map(str,range(1,11)));await q.handle(event)
        chunks=[c.args[0] for c in event.reply.call_args_list]
        assert len(chunks)>1 and all(len(c)<=3500 for c in chunks)
        assert ''.join(chunks).count('x'*1100)==10
        assert len(q.rows(100))==10
    run(scenario())
