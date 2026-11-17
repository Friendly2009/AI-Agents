import asyncio

import pytest

from conversation_service import ConversationState
from delivery import PolicyBlocked
from storage import Store
from test_dialogue_control import setup, event
from unittest.mock import AsyncMock
from types import SimpleNamespace
from delivery import Ledger
from dialogue_control import PRIVACY_RECEIPTS
from handoff_ack import confirm_privacy


@pytest.mark.parametrize('command', ['/стоп', '/удалить'])
def test_privacy_confirmation_through_real_ledger(tmp_path, monkeypatch, command):
    store = Store(str(tmp_path / 'state.db'))
    app, client, manager, raw = setup(store, monkeypatch)
    async def run():
        store.control.takeover(42)
        await app.handle_dm(client, event(client, command))
        assert raw.await_count == 1
        assert store.get(42)['stopped']
        with pytest.raises(PolicyBlocked):
            await client.send_message(42, 'Обычный ответ')
        await app.handle_dm(client, event(client, command))
        assert raw.await_count == 1
        await app.handle_dm(client, event(client, '/продолжить', 2))
        await app.handle_dm(client, event(client, command))
        assert not store.get(42)['stopped']  # A stale command cannot undo resume.
    try:
        asyncio.run(run())
    finally:
        store.db.close()


def test_handoff_blocks_booking_commands_before_side_effects(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'state.db'))
    app, client, manager, raw = setup(store, monkeypatch)
    store.control.takeover(42)
    try:
        asyncio.run(app.handle_dm(client, event(client, '/согласие_на_запись')))
        assert not store.get(42)['crm_consent']
        assert raw.await_count == 0
    finally:
        store.db.close()


@pytest.mark.parametrize('restriction', ['account', 'safe', 'config', 'forged', 'body'])
def test_privacy_ack_cannot_bypass_other_restrictions(tmp_path, monkeypatch, restriction):
    store = Store(str(tmp_path / 'state.db'))
    app, client, manager, raw = setup(store, monkeypatch)
    key = 'privacy:42:stop:1'
    store.erase(42, receipt=None if restriction == 'forged' else key)
    if restriction == 'account':
        store.block_outbound('test')
    if restriction == 'config':
        monkeypatch.setenv('ADMIN_OWNER_TG_ID', '99')
        monkeypatch.setenv('ADMIN_WIFE_TG_ID', '98')
        store.configuration().save(99, 'global', 'global', dict(
            settings={'outbound_allowed': False}, prompt='', group='', enabled=True, deleted=False), 0)
    async def run():
        if restriction == 'body':
            with pytest.raises(PolicyBlocked):
                await confirm_privacy(store, 42, 1, 'stop',
                                      lambda body: client.send_message(42, 'Произвольный ответ'), safe=False)
        else:
            assert not await confirm_privacy(store, 42, 1, 'stop', event(client, '').reply,
                                             safe=restriction == 'safe')
    try:
        asyncio.run(run())
        assert raw.await_count == 0
    finally:
        store.db.close()


def test_privacy_timeout_restart_does_not_retry(tmp_path, monkeypatch):
    path = str(tmp_path / 'state.db')
    store = Store(path)
    app, client, manager, raw = setup(store, monkeypatch)
    raw.side_effect = TimeoutError()
    async def run():
        with pytest.raises(TimeoutError):
            await app.handle_dm(client, event(client, '/стоп'))
    asyncio.run(run())
    assert Ledger(store).row('privacy:42:stop:1')['state'] == 'uncertain'
    store.db.close()
    store = Store(path)
    app, client, manager, raw = setup(store, monkeypatch)
    try:
        asyncio.run(app.handle_dm(client, event(client, '/стоп')))
        assert raw.await_count == 0
        assert store.get(42)['stopped']
    finally:
        store.db.close()


@pytest.mark.parametrize('failure', [False, True])
def test_listener_joins_background_tasks(tmp_path, monkeypatch, failure):
    import telegram_adapter
    from application import create_application
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    monkeypatch.setenv('AUTO_DISCOVER_GROUPS', 'false')
    monkeypatch.setattr(telegram_adapter, 'init_catalog', lambda: None)
    monkeypatch.setattr(telegram_adapter, 'allowed_groups', lambda: [])
    app = create_application(tmp_path)
    async def run():
        before = asyncio.all_tasks()
        async def disconnected():
            await asyncio.sleep(0)
            assert len(asyncio.all_tasks() - before) == 4
            if failure:
                raise RuntimeError('transport disconnected')
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
                                 on=lambda spec: lambda handler: handler,
                                 run_until_disconnected=disconnected)
        if failure:
            with pytest.raises(RuntimeError):
                await app.listen(client)
        else:
            await app.listen(client)
        assert asyncio.all_tasks() == before
    try:
        asyncio.run(run())
    finally:
        app.close()


@pytest.mark.parametrize('failure', [False, True])
def test_voice_client_closed(tmp_path, monkeypatch, failure):
    import openai
    from application import create_application
    monkeypatch.setenv('ADMIN_OWNER_TG_ID', '99')
    monkeypatch.setenv('ADMIN_WIFE_TG_ID', '98')
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-placeholder')
    voice = tmp_path / 'voice.ogg'
    voice.write_bytes(b'offline fixture')
    speech = AsyncMock()
    speech.__aenter__.return_value = speech
    speech.audio.transcriptions.create = AsyncMock(return_value=SimpleNamespace(text=''))
    if failure:
        speech.audio.transcriptions.create.side_effect = RuntimeError('transcription failed')
    monkeypatch.setattr(openai, 'AsyncOpenAI', lambda **kwargs: speech)
    app = create_application(tmp_path / 'app')
    app.director_reply = AsyncMock()
    message = SimpleNamespace(voice=True, file=SimpleNamespace(size=10),
                              download_media=AsyncMock(return_value=str(voice)))
    command = SimpleNamespace(sender_id=99, chat_id=99, raw_text='', message=message, is_private=True)
    try:
        asyncio.run(app.director_message(None, command, 99))
        speech.__aenter__.assert_awaited_once()
        speech.__aexit__.assert_awaited_once()
    finally:
        app.close()


def test_panel_stop_during_model_failure_does_not_notify_manager(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'state.db'))
    app, client, manager, raw = setup(store, monkeypatch)
    async def model(*args, **kwargs):
        panel = Store(str(tmp_path / 'state.db'))
        try:
            ConversationState(panel).update(42, stopped=1)
        finally:
            panel.db.close()
        raise TimeoutError('model failure after stop')
    app.respond = model
    try:
        asyncio.run(app.handle_dm(client, event(client, 'Расскажите о занятиях')))
        assert raw.await_count == 0
        assert store.control.get(42)['request'] is None
    finally:
        store.db.close()


def test_erase_rolls_back_on_last_write_failure():
    import sqlite3
    store = Store(':memory:')
    store.ensure(42, username='preserved')
    ConversationState(store).claim_inbound(42, 1, 'private content')
    before = store.get(42), list(map(tuple, store.db.execute('SELECT * FROM inbox')))
    store.db.executescript('''CREATE TRIGGER fail_receipt BEFORE INSERT ON dialogue_transition_events
                            BEGIN SELECT RAISE(ABORT, 'injected failure'); END;''')
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.erase(42, receipt='privacy:42:stop:1')
        assert not store.db.in_transaction
        assert (store.get(42), list(map(tuple, store.db.execute('SELECT * FROM inbox')))) == before
    finally:
        store.db.close()
