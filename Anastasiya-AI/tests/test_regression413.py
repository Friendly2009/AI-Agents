import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import re
from storage import Store
from manager import Manager

SOURCE = (Path(__file__).resolve().parents[1] / 'src') / 'consultation_service.py'
def funcs(**values):
    from app_factory import make_app
    app = make_app()
    for key,value in values.items():
        setattr(app,key,value)
    return dict(maybe_crm=app.maybe_crm, reset_test=app.reset_test,
                is_greeting=app.is_greeting, TEST_SESSIONS=app.TEST_SESSIONS)

def test_test_mode_blocks_even_when_live_enabled(monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    store=Store(':memory:'); store.ensure(1);ConversationState(store).update(1,submission_pending=1,crm_consent=1,wants_trial=1,name='Test',phone='1',city='Александров')
    delivery=AsyncMock();create=AsyncMock()
    f=funcs(STORE=store,TESTERS={1},safe_test_mode=lambda:False,deliver_lead=delivery,create_lead=create)
    asyncio.run(f['maybe_crm'](None,1))
    delivery.assert_not_called();create.assert_not_called()

def test_legacy_consent_cannot_submit(monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE', 'false')
    store=Store(':memory:');store.ensure(1);ConversationState(store).update(1,crm_consent=1,wants_trial=1,name='Test',phone='1',city='Александров')
    delivery=AsyncMock();create=AsyncMock()
    f=funcs(STORE=store,safe_test_mode=lambda:False,deliver_lead=delivery,create_lead=create)
    asyncio.run(f['maybe_crm'](None,1))
    delivery.assert_not_called();create.assert_not_called()

def test_reset_has_separate_clean_store():
    real=Store(':memory:');real.ensure(1);ConversationState(real).update(1,child_age=15,crm_consent=1)
    f=funcs(STORE=real);f['reset_test'](1)
    teststore=f['TEST_SESSIONS'][1][0];teststore.ensure(1);ConversationState(teststore).update(1,child_age=10)
    f['reset_test'](1)
    assert f['TEST_SESSIONS'][1][0].get(1) is None
    assert real.get(1)['child_age']==15

def test_plain_greeting():
    f=funcs()
    assert f['is_greeting']('Привет 👋')
    assert not f['is_greeting']('Привет! Хочу записаться')

def test_dm_does_not_submit_after_every_reply():
    tree=ast.parse(SOURCE.read_text());fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_handle_dm')
    calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='maybe_crm']
    assert len(calls)==1

from conversation_service import ConversationState
