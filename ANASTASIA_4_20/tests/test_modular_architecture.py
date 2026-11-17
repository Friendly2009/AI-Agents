"""Architecture contracts tested with temporary stores and offline transports."""
import ast
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application import create_application
from conversation_service import ConversationState
from delivery import GuardedTelegramClient, Ledger, PolicyBlocked
from funnel import Funnel
from storage import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('reverse', [False, True])
def test_every_library_import_is_database_and_network_free(tmp_path, reverse):
    modules = sorted(p.stem for p in ROOT.glob('*.py') if p.stem != 'run_tests')
    if reverse:
        modules.reverse()
    code = '''
import importlib, sqlite3, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted database or network IO')
sqlite3.connect = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sys.path.insert(0, sys.argv[1])
for module in __import__('json').loads(sys.argv[2]):
    importlib.import_module(module)
print('imported', len(__import__('json').loads(sys.argv[2])))
'''
    result = subprocess.run([sys.executable, '-c', code, str(ROOT), json.dumps(modules)],
                            cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


def test_no_local_import_cycles_or_globals_lookup():
    graph = {p.stem: set() for p in ROOT.glob('*.py')}
    for name in graph:
        tree = ast.parse((ROOT / (name+'.py')).read_text(encoding='utf-8'))
        if name in {'consultation_service', 'submission_service', 'conversation_service'}:
            assert not any(isinstance(n, ast.ImportFrom) and (n.module or '').startswith('telethon')
                           for n in ast.walk(tree))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'globals'), name
            if isinstance(node, ast.ImportFrom) and node.module in graph:
                graph[name].add(node.module)
            if isinstance(node, ast.Import):
                graph[name].update(a.name for a in node.names if a.name in graph)
    visited, active = set(), []
    def visit(name):
        assert name not in active, ' -> '.join(active+[name])
        if name in visited:
            return
        active.append(name)
        for dependency in graph[name]:
            visit(dependency)
        active.pop()
        visited.add(name)
    for name in graph:
        visit(name)
    assert not graph['storage'] & {'delivery','brain','consultation_service','conversation_service','handoff_ack'}
    assert not graph['consultation_service'] & {'telethon','telegram_adapter'}


@asynccontextmanager
async def typing(*args):
    yield


def test_two_applications_process_same_event_independently_and_close(tmp_path, monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    apps = [create_application(tmp_path / label) for label in ('a', 'b')]
    clients = [SimpleNamespace(action=typing, send_message=AsyncMock()) for _ in apps]
    async def run():
        tasks = []
        for index, (app, client) in enumerate(zip(apps, clients)):
            app.STORE.ensure(42)
            ConversationState(app.STORE).update(42, ai_consent=1, ai_notice_shown=1, name=f'parent-{index}')
            async def model(*args, _app=app, _index=index, **kwargs):
                assert not _app.STORE.db.in_transaction
                await asyncio.sleep(0)
                return dict(reply=f'Ответ {_index}', handoff=False, wants_trial=False, next_action='answer', trial_decision=None)
            app.respond = model
            event = SimpleNamespace(sender_id=42, chat_id=42, id=1, raw_text='Расскажите о занятиях',
                                    get_sender=AsyncMock(return_value=SimpleNamespace(bot=False, username='parent')), reply=AsyncMock())
            tasks.append(app.handle_dm(client, event))
        await asyncio.gather(*tasks)
        # The identical Telegram event is suppressed independently in each inbox.
        assert all(app.STORE.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0] == 1 for app in apps)
    try:
        asyncio.run(run())
        for index, app in enumerate(apps):
            assert app.STORE.history(42)[-1]['content'] == f'Ответ {index}'
            assert app.STORE.get(42)['name'] == f'parent-{index}'
            assert clients[index].send_message.call_args.args == (42, f'Ответ {index}')
        apps[0].M.handoff(42)
        assert apps[0].M.paused(42) and not apps[1].M.paused(42)
        apps[0].reset_test(42)
        assert 42 not in apps[1].TESTERS and not apps[1].TEST_SESSIONS
        apps[0].PENDING_QUESTIONS[42] = ['private']
        assert not apps[1].PENDING_QUESTIONS
        assert apps[0].CHAT_LOCKS[42] is not apps[1].CHAT_LOCKS[42]
        apps[0].M.llm_request('test')
        assert apps[1].M.db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0] == 0
        apps[0].LEARNING.mark_user(42, 'paused')
        assert apps[0].LEARNING.db is not apps[1].LEARNING.db
        connections = [r.db for app in apps for r in (app.STORE,app.M,app.RADAR,app.LEARNING)]
        connections += [r.db for r in apps[0].TEST_SESSIONS[42]]
    finally:
        for app in apps:
            app.close()
            app.close()
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
    reopened = create_application(tmp_path/'b')
    try:
        assert reopened.STORE.get(42)['name'] == 'parent-1'
        assert not reopened.M.paused(42)
    finally:
        reopened.close()


@pytest.mark.parametrize('operation', ['claim','observe','consent','submitted','update'])
def test_related_local_writes_roll_back_together(monkeypatch, operation):
    store = Store(':memory:')
    try:
        store.ensure(42)
        before = store.get(42), store.dialogue(42)
        def fail(*args, **kwargs):
            raise RuntimeError('injected local write failure')
        monkeypatch.setattr(Funnel, 'emit', fail)
        state = ConversationState(store)
        calls = dict(claim=lambda: state.claim_inbound(42,1,'вопрос'),
                     observe=lambda: state.observe(42,'Хочу записаться'),
                     consent=lambda: state.recording_consent(42,True),
                     submitted=lambda: state.submitted(42,123,'crm:test'),
                     update=lambda: state.update(42,ai_consent=1))
        with pytest.raises(RuntimeError):
            calls[operation]()
        assert (store.get(42), store.dialogue(42)) == before
        assert store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0] == 0
        assert store.db.execute('SELECT COUNT(*) FROM outbound_events').fetchone()[0] == 0
        assert not store._inbound and not store.db.in_transaction
    finally:
        store.db.close()


def test_crm_await_is_outside_transaction_and_uncertain_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    monkeypatch.setenv('CRM_ENABLED', 'true')
    monkeypatch.setattr('crm._config', lambda city: ('url','email','key','1',''))
    app = create_application(tmp_path)
    app.STORE.ensure(42)
    ConversationState(app.STORE).update(42, submission_pending=1, crm_consent=1, wants_trial=1,
                                        name='Parent',phone='+79991234567',city='Александров')
    async def create(contact):
        assert not app.STORE.db.in_transaction
        await asyncio.sleep(0)
        raise TimeoutError('unknown remote result')
    async def notify(*args):
        assert not app.STORE.db.in_transaction
    app.create_lead = AsyncMock(side_effect=create)
    app.deliver_lead = AsyncMock(side_effect=notify)
    try:
        asyncio.run(app.maybe_crm(None,42))
        asyncio.run(app.maybe_crm(None,42))
        assert app.create_lead.await_count == 1
        assert app.STORE.dialogue(42)['application_status'] == 'uncertain'
        assert app.STORE.db.execute('SELECT state FROM effects').fetchone()[0] == 'uncertain'
    finally:
        app.close()


def test_active_send_context_cannot_authorize_another_application(tmp_path, monkeypatch):
    from telethon import TelegramClient, types
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    a, b = create_application(tmp_path/'a'), create_application(tmp_path/'b')
    raw = AsyncMock()
    monkeypatch.setattr(TelegramClient, 'send_message', raw)
    client = GuardedTelegramClient(None,123,'0'*32)
    client.configure_delivery(b.STORE, set(), b.M)
    client.get_input_entity = AsyncMock(return_value=types.InputPeerUser(42,1))
    a.STORE.ensure(42)
    ConversationState(a.STORE).claim_inbound(42,1,'hello')
    ledger = Ledger(a.STORE)
    ledger.prepare('same',42,'reply','test'); ledger.validate('same'); ledger.approve('same')
    async def send():
        assert not a.STORE.db.in_transaction
        with pytest.raises(PolicyBlocked):
            await client.send_message(42,'hello')
    try:
        asyncio.run(ledger.deliver('same',send,safe=False))
        raw.assert_not_called()
    finally:
        a.close(); b.close()


def test_replaced_test_session_and_failed_construction_close_resources(tmp_path, monkeypatch):
    import application
    app = create_application(tmp_path/'app')
    app.reset_test(42)
    previous = app.TEST_SESSIONS[42]
    app.reset_test(42)
    for resource in previous:
        with pytest.raises(sqlite3.ProgrammingError):
            resource.db.execute('SELECT 1')
    app.end_test(42)
    assert not app.TEST_SESSIONS and not app.TESTERS
    app.close()
    opened = []
    def store(*args):
        resource = Store(*args)
        opened.append(resource)
        return resource
    def fail(*args):
        raise RuntimeError('constructor failed')
    monkeypatch.setattr(application,'Store',store)
    monkeypatch.setattr(application,'Manager',fail)
    with pytest.raises(RuntimeError):
        create_application(tmp_path/'failed')
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].db.execute('SELECT 1')


def test_respond_never_opens_a_default_database(monkeypatch):
    from brain import respond, SCHEMA
    response = dict.fromkeys(SCHEMA['required'])
    response.update(reply='Ответ',handoff=False,wants_trial=False,next_action='answer')
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
        status='completed',usage=None,output_text=json.dumps(response)))),close=AsyncMock())
    monkeypatch.setattr('brain.AsyncOpenAI',lambda **kwargs: client)
    def forbidden(*args, **kwargs):
        raise AssertionError('respond opened a database')
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    manager = SimpleNamespace(slots=lambda: [],llm_request=lambda model: None)
    assert asyncio.run(respond([],manager=manager))['reply'] == 'Ответ'
    client.close.assert_awaited_once()


def test_entry_point_closes_resources_on_failure(monkeypatch):
    import main
    from unittest.mock import Mock
    app = SimpleNamespace(main=AsyncMock(side_effect=RuntimeError('startup failed')),close=Mock())
    monkeypatch.setattr(main,'create_application',lambda: app)
    monkeypatch.setattr(main,'load_dotenv',lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(main.main())
    app.close.assert_called_once()


def test_crm_receipt_rolls_back_if_last_local_write_fails(monkeypatch):
    store = Store(':memory:')
    try:
        store.ensure(42)
        store.update(42,submission_pending=1)
        store.claim_effect('crm:test')
        def fail(*args):
            raise RuntimeError('last local write failed')
        monkeypatch.setattr(store,'_save_dialogue',fail)
        with pytest.raises(RuntimeError):
            ConversationState(store).submitted(42,123,'crm:test')
        assert store.get(42)['crm_id'] is None
        assert store.get(42)['submission_pending'] == 1
        assert store.db.execute("SELECT state FROM effects WHERE effect_key='crm:test'").fetchone()[0] == 'sending'
        assert store.db.execute("SELECT COUNT(*) FROM funnel_events WHERE event='lead_created'").fetchone()[0] == 0
        assert not store.db.in_transaction
    finally:
        store.db.close()
