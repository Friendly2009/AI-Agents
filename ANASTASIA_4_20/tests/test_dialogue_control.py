"""Full handlers + real Ledger + real guarded client, only network/model replaced."""
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, types
from telethon.errors import UserPrivacyRestrictedError
from storage import Store
from manager import Manager
from delivery import GuardedTelegramClient, Ledger, PolicyBlocked
from dialogue_control import ACK
from handoff_flow import transfer_to_managers, reply_to_handoff


def setup(store, monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    store.ensure(42)
    store.update(42, ai_consent=1, ai_notice_shown=1)
    manager = Manager(':memory:').bind(store)
    client = GuardedTelegramClient(None, 123, '0'*32)
    client.configure_delivery(store, {99}, manager)
    async def entity(value):
        return value if isinstance(value, types.InputPeerUser) else types.InputPeerUser(int(value), 0)
    monkeypatch.setattr(client, 'get_input_entity', entity)
    raw = AsyncMock(return_value=SimpleNamespace(id=17))
    monkeypatch.setattr(TelegramClient, 'send_message', raw)
    @asynccontextmanager
    async def action(*args):
        yield
    monkeypatch.setattr(client, 'action', action)
    import main
    for name, value in dict(STORE=store, M=manager, TESTERS=set(), CHAT_LOCKS={}).items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(main.RADAR, 'get', lambda uid: None)
    monkeypatch.setattr(main.LEARNING, 'mark_engaged', lambda *a: None)
    monkeypatch.setattr(main.LEARNING, 'record', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'director_admin_ids', lambda uid: {99})
    return main, client, manager, raw


def event(client, text, mid=1, uid=42):
    async def reply(body):
        return await client.send_message(uid, body, reply_to=mid)
    return SimpleNamespace(sender_id=uid, chat_id=uid, id=mid, raw_text=text, reply=reply,
        get_sender=AsyncMock(return_value=SimpleNamespace(bot=False, username='parent')))


def test_success_duplicate_and_block(tmp_path, monkeypatch):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    async def run():
        request = event(client, 'позовите менеджера')
        await main.handle_dm(client, request)
        assert [call.args[0].user_id for call in raw.call_args_list] == [99, 42]
        assert raw.call_args_list[-1].args[1] == ACK
        assert manager.paused(42)
        await main.handle_dm(client, request)
        await main.handle_dm(client, event(client, 'позовите менеджера', 2))
        assert raw.await_count == 2
        with pytest.raises(PolicyBlocked):
            await client.send_message(42, 'Ответ ИИ')
        # A kind string alone is not authorization, nor is an altered body.
        ledger = Ledger(store)
        ledger.prepare('forged', 42, 'handoff_ack', 'dialogue_control', ACK)
        assert not ledger.validate('forged')
        assert ledger.check(42, 'handoff_ack') == 'invalid_handoff_ack'
    asyncio.run(run())
    store.db.close()
    reopened = Store(str(tmp_path/'state.db'))
    assert reopened.control.blocked(42)
    assert reopened.control.acknowledgement(42)
    assert reopened.db.execute("SELECT state FROM outbound_operations WHERE kind='handoff_ack' AND id!='forged'").fetchone()[0] == 'sent'
    reopened.db.close()


@pytest.mark.parametrize('method', ['продолжим', '/продолжить', '/вернуть_ии'])
def test_all_resume_handlers(tmp_path, monkeypatch, method):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    store.claim_inbound(42, 1, 'вопрос')
    store.control.takeover(42)
    store.set_dialogue(42, paused=True, declined=True)
    store.update(42, stopped=1)
    async def run():
        if method == '/вернуть_ии':
            from manager import commands
            assert await commands(client, event(client, method+' 42', uid=99), store, admin=True)
        else:
            await main.handle_dm(client, event(client, method, 2))
        assert not manager.paused(42)
        assert store.dialogue(42)['handoff_status'] == 'none'
        assert not store.dialogue(42)['paused']
        assert not store.get(42)['stopped']
        await client.send_message(42, 'После возврата')
    asyncio.run(run())
    store.db.close()


@pytest.mark.parametrize('failure', ['definite', 'uncertain'])
def test_notification_failure(tmp_path, monkeypatch, failure):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    store.claim_inbound(42, 1, 'позовите менеджера')
    async def notify(*args):
        if failure == 'uncertain':
            raise TimeoutError()
        return 0
    async def run():
        reply = await transfer_to_managers(client, notify, manager, 42, {}, 'позовите менеджера', [], store=store)
        assert reply != ACK
        await reply_to_handoff(store, 42, reply, event(client, '').reply)
        assert not store.control.acknowledgement(42)
        assert all(c.args[1] != ACK for c in raw.call_args_list)
        assert store.control.blocked(42) == (failure == 'uncertain')
        assert not store.control.begin(42, 1)  # Same failed event is never retried.
    asyncio.run(run())
    store.db.close()


@pytest.mark.parametrize('error,expected', [(TimeoutError(), 'uncertain'), (UserPrivacyRestrictedError(request=None), 'failed')])
def test_ack_failure_restart_no_duplicate(tmp_path, monkeypatch, error, expected):
    path = str(tmp_path/'state.db')
    store = Store(path)
    main, client, manager, raw = setup(store, monkeypatch)
    async def transport(peer, body, *args, **kwargs):
        if peer.user_id == 42:
            raise error
        return SimpleNamespace(id=123)
    raw.side_effect = transport
    async def run():
        with pytest.raises(type(error)):
            await main.handle_dm(client, event(client, 'позовите менеджера'))
        assert store.control.blocked(42)
        assert store.db.execute("SELECT state FROM outbound_operations WHERE kind='handoff_ack'").fetchone()[0] == expected
    asyncio.run(run())
    store.db.close()
    store = Store(path)
    main, client, manager, raw = setup(store, monkeypatch)
    async def restart():
        await main.handle_dm(client, event(client, 'позовите менеджера'))
        await main.handle_dm(client, event(client, 'позовите менеджера', 2))
        assert raw.await_count == 0
        assert manager.paused(42)
    asyncio.run(restart())
    store.db.close()


def test_restart_during_notification(tmp_path, monkeypatch):
    path = str(tmp_path/'state.db')
    store = Store(path)
    store.control.begin(42, 1)
    store.db.close()
    store = Store(path)
    main, client, manager, raw = setup(store, monkeypatch)
    asyncio.run(main.handle_dm(client, event(client, 'позовите менеджера')))
    assert raw.await_count == 0
    assert store.control.blocked(42)
    assert not store.control.acknowledgement(42)
    store.db.close()


@pytest.mark.parametrize('resume_during_generation', [False, True])
def test_takeover_while_model_running(tmp_path, monkeypatch, resume_during_generation):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    async def model(*args, **kwargs):
        # Independent panel connection commits while model generation is suspended.
        panel = Store(str(tmp_path/'state.db'))
        panel.control.takeover(42)
        if resume_during_generation:
            panel.control.resume(42)
        panel.db.close()
        await asyncio.sleep(0)
        return dict(reply='Устаревший ответ', handoff=False, wants_trial=False, next_action='answer')
    monkeypatch.setattr(main, 'respond', model)
    asyncio.run(main.handle_dm(client, event(client, 'Расскажите подробнее')))
    assert raw.await_count == 0
    assert manager.paused(42) != resume_during_generation
    store.db.close()


def test_policy_rechecks_after_entity_resolution(tmp_path, monkeypatch):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    store.claim_inbound(42, 1, 'вопрос')
    ledger = Ledger(store)
    ledger.prepare('reply', 42, 'reply', 'test', 'Ответ')
    assert ledger.validate('reply') and ledger.approve('reply')
    async def resolve(value):
        store.control.takeover(42)
        return types.InputPeerUser(42, 0)
    monkeypatch.setattr(client, 'get_input_entity', resolve)
    async def run():
        with pytest.raises(PolicyBlocked):
            await ledger.deliver('reply', lambda: client.send_message(42, 'Ответ'), safe=False)
    asyncio.run(run())
    assert raw.await_count == 0
    store.db.close()


def test_migration_conflicts_and_idempotence(tmp_path):
    manager = sqlite3.connect(tmp_path/'manager.db')
    manager.execute('CREATE TABLE handoffs(uid INTEGER PRIMARY KEY,active INTEGER)')
    manager.executemany('INSERT INTO handoffs VALUES(?,?)', [(1,1), (2,0), (3,1), (4,0)])
    manager.commit()
    db = sqlite3.connect(tmp_path/'state.db')
    db.execute('CREATE TABLE dialogue_state(user_id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
    for uid, state in [(1,'none'), (2,'delivered'), (3,'failed'), (4,'pending'), (5,'failed')]:
        db.execute('INSERT INTO dialogue_state VALUES(?,?)', (uid,json.dumps(dict(handoff_status=state, original_question='Preserved'))))
    db.commit(); db.close()
    store = Store(str(tmp_path/'state.db'))
    assert [store.control.get(i)['state'] for i in range(1,6)] == ['human','human','human','review','ai']
    assert all(not store.control.acknowledgement(i) for i in range(1,6))
    before = list(manager.execute('SELECT * FROM handoffs'))
    store.control.resume(1)
    store.db.close()
    store = Store(str(tmp_path/'state.db'))
    assert not store.control.blocked(1)  # Old active=1 cannot resurrect ownership.
    assert list(manager.execute('SELECT * FROM handoffs')) == before
    assert store.dialogue(2)['original_question'] == 'Preserved'
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    store.db.close(); manager.close()


def test_stale_resume_event_cannot_release_new_takeover():
    store = Store(':memory:')
    store.control.takeover(42)
    assert store.control.resume(42, 'telegram:42:1')
    store.control.takeover(42)
    assert not store.control.resume(42, 'telegram:42:1')
    assert store.control.blocked(42)
    store.db.close()


def test_restart_after_receipt_before_ack(tmp_path, monkeypatch):
    path = str(tmp_path/'state.db')
    store = Store(path)
    store.ensure(42)
    store.claim_inbound(42, 1, 'позовите менеджера')
    key = store.control.begin(42, 1)
    store.control.finish(42, key, 1)
    store.db.close()
    store = Store(path)
    main, client, manager, raw = setup(store, monkeypatch)
    async def run():
        assert await store.control.confirm(42, lambda: client.send_message(42, ACK), safe=False)
        assert not await store.control.confirm(42, lambda: client.send_message(42, ACK), safe=False)
    asyncio.run(run())
    assert raw.await_count == 1
    store.db.close()


def test_ack_capability_cannot_send_model_text(tmp_path, monkeypatch):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    store.claim_inbound(42, 1, 'вопрос')
    key = store.control.begin(42, 1)
    store.control.finish(42, key, 1)
    async def run():
        with pytest.raises(PolicyBlocked):
            await store.control.confirm(42, lambda: client.send_message(42, 'Произвольный ответ модели'), safe=False)
    asyncio.run(run())
    assert raw.await_count == 0
    store.db.close()


@pytest.mark.parametrize('uncertain', [False, True])
def test_full_handler_notification_failure(tmp_path, monkeypatch, uncertain):
    store = Store(str(tmp_path/'state.db'))
    main, client, manager, raw = setup(store, monkeypatch)
    async def transport(peer, body, *args, **kwargs):
        if peer.user_id == 99:
            raise TimeoutError() if uncertain else UserPrivacyRestrictedError(request=None)
        return SimpleNamespace(id=17)
    raw.side_effect = transport
    asyncio.run(main.handle_dm(client, event(client, 'позовите менеджера')))
    assert all(c.args[1] != ACK for c in raw.call_args_list)
    assert raw.await_count == (1 if uncertain else 2)
    assert manager.paused(42) == uncertain
    assert not store.control.acknowledgement(42)
    store.db.close()
