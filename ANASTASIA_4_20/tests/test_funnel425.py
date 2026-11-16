import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from storage import Store
from funnel import Funnel, EVENTS
from sales_learning import SalesLearning
from manager import Manager
from crm import create_lead, reconcile_lead, _create, normalized_phone
from lead_delivery import deliver_lead, lead_summary
from objections import classify_all

@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','false')
    monkeypatch.setenv('ADMIN_OWNER_TG_ID','101')
    # The actual admin registry uses director_orders; no privileged test bypass.
    monkeypatch.setenv('ADMIN_WIFE_TG_ID','202')
    from director_orders import admin_ids
    assert 101 in admin_ids(0)
    s=Store(':memory:'); s.ensure(7,source='telegram_school'); yield s
    s.db.close()

def test_zero_report(live):
    assert all(v==0 for v in Funnel(live).counts().values())
    assert 'нет данных' in Funnel(live).report()
    assert 'не рассчитано' in Manager(':memory:').report(live)

def test_intent_is_not_attendance(live):
    f=Funnel(live)
    with live.db:
        f.emit(7,'trial_interested',reliability='intent',confirmation='llm')
        f.emit(7,'trial_interested',reliability='intent',confirmation='llm')
    assert f.counts()['trial_interested']==1
    assert f.counts()['trial_attended']==0
    for evidence in ['intent','program']:
        with pytest.raises(ValueError): f.emit(7,'trial_attended',reliability=evidence)

def test_confirmation_auth_idempotency_conflict(live):
    f=Funnel(live)
    with pytest.raises(PermissionError): f.confirm(7,'trial_attended',303,'lesson:1')
    f.confirm(7,'trial_attended',101,'lesson:1')
    f.confirm(7,'trial_attended',101,'lesson:1')
    row=live.db.execute('SELECT * FROM funnel_events').fetchone()
    assert row['actor_id']==101 and row['external_id']=='lesson:1'
    assert f.counts()['trial_attended']==1
    with pytest.raises(ValueError): f.confirm(7,'trial_missed',101,'lesson:1')

def test_test_events_excluded(live,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','true')
    Funnel(live).confirm(7,'trial_attended',101,'lesson:1')
    assert Funnel(live).counts()['trial_attended']==0

def test_events_full_schema(live):
    for e in EVENTS:
        with live.db:
            Funnel(live).emit(7,e,reliability='human',actor_id=101,confirmation='admin',external_id='ref')
    assert all(v==1 for v in Funnel(live).counts().values())
    assert live.db.execute('SELECT COUNT(*) FROM funnel_events WHERE occurred_at IS NULL OR source IS NULL').fetchone()[0]==0

def test_contact_lifecycle_and_delete(live):
    live.update(7,ai_consent=1)
    assert live.claim_inbound(7,1,'Вопрос')
    assert not live.claim_inbound(7,1,'Вопрос')
    live.add_turn(7,'assistant','Ответ')
    live.claim_inbound(7,2,'Спасибо')
    live.recording_consent(7,True)
    live.submitted(7,55,'key')
    c=Funnel(live).counts()
    assert all(c[e]==1 for e in ('contact_authorized','conversation_started','client_replied','trial_requested','lead_created'))
    live.erase(7)
    assert not any(Funnel(live).counts().values())

@pytest.mark.parametrize('failures', [0,1,2])
def test_independent_directors_and_dedup(live,failures):
    client=SimpleNamespace(send_message=AsyncMock(side_effect=[TimeoutError() if i<failures else None for i in range(2)]))
    result=asyncio.run(deliver_lead(client,live,{'user_id':7},[101,202],'CRM unknown'))
    asyncio.run(deliver_lead(client,live,{'user_id':7},[101,202],'CRM unknown'))
    assert client.send_message.await_count==2
    assert sum(v=='sent' for v in result.values())==2-failures
    assert Funnel(live).counts()['lead_delivered']==int(failures==0)
    assert 'Telegram ID: 7' in lead_summary({'user_id':7},'unknown')
    assert 'TG-7' in lead_summary({'user_id':7},'unknown')

def test_safe_mode_no_effects(live,monkeypatch):
    monkeypatch.setenv('SAFE_TEST_MODE','true')
    client=SimpleNamespace(send_message=AsyncMock())
    with patch('crm._post') as post:
        assert asyncio.run(create_lead({'user_id':7})) is None
        assert asyncio.run(reconcile_lead({'user_id':7})) is None
        asyncio.run(deliver_lead(client,live,{'user_id':7},[101,202],'test'))
        post.assert_not_called()
    client.send_message.assert_not_called()

def customer():
    return dict(user_id=7,city='Ярославль',name='Родитель',phone='8 (999) 999-99-99',crm_consent=1,wants_trial=1)

def test_no_consent_no_network(live):
    with patch('crm._post') as post:
        with pytest.raises(ValueError): asyncio.run(create_lead(dict(customer(),crm_consent=0)))
        post.assert_not_called()

def test_branch_payload_and_existing(live,monkeypatch):
    for key,val in {'URL':'https://example.invalid','EMAIL':'test','API_KEY':'test','BRANCH':'24'}.items():
        monkeypatch.setenv('ALFACRM_YARO_'+key,val)
    with patch('crm._post',side_effect=[{'token':'test'},{'items':[]},{'success':True,'model':{'id':88}}]) as post:
        assert asyncio.run(create_lead(customer()))==88
        assert post.call_args.args[0].endswith('/24/customer/create')
        assert post.call_args.args[1]['phone']==['79999999999']
        assert post.call_args.args[1]['name']=='Родитель'
    with patch('crm._post',side_effect=[{'token':'test'},{'items':[{'id':88,'phone':['+79999999999']}]}]) as post:
        assert asyncio.run(create_lead(customer()))==88
        assert all('/create' not in c.args[0] for c in post.call_args_list)

def test_reconciliation_never_creates(live):
    with patch('crm._config',return_value=('https://example.invalid','e','k','24','')):
        with patch('crm._post',side_effect=[{'token':'test'},{'items':[]}]) as post:
            assert asyncio.run(reconcile_lead(customer())) is None
            assert all('/create' not in c.args[0] for c in post.call_args_list)
        with patch('crm._post',side_effect=[{'token':'test'},{'items':[{'id':88,'phone':['89999999999']}]}]):
            assert asyncio.run(reconcile_lead(customer()))==88
        with patch('crm._post',side_effect=TimeoutError):
            with pytest.raises(TimeoutError): asyncio.run(create_lead(customer()))

def test_small_samples_sources_and_no_automatic_attendance(live):
    l=SalesLearning(':memory:')
    n=l.record(7,'игры',{},'Ответ',{'source':'A'},selection=('gaming','detailed'))
    l.mark_engaged(7)
    with pytest.raises(PermissionError): l.mark(n,'attended')
    l.mark(n,'attended',101)
    l.mark(n,'attended',101)
    assert l.strategy_stats('gaming','A')=={'detailed':(1,1)}
    assert not l.strategy_stats('gaming','B')
    assert l.choose('gaming','A')=='interest_bridge'
    assert 'малая выборка' in l.report()
    l.erase(7); assert not l.strategy_stats('gaming','A')

@pytest.mark.parametrize('text,category', [('дорого','price'),('нет времени','time'),('слишком мал','age'),('нет опыта','experience'),('неинтересно','interest'),('не доверяю','trust'),('сомневаюсь в программе','program'),('другая школа','competitor'),('подумаю','think'),('не пишите','refusal')])
def test_objections(text,category):
    assert category in classify_all(text)

def test_cost_coverage_and_invalid_tariffs(live,monkeypatch):
    m=Manager(':memory:')
    monkeypatch.setenv('API_INPUT_USD_PER_MILLION','bad')
    monkeypatch.setenv('API_OUTPUT_USD_PER_MILLION','2')
    m.llm_request('gpt-5-mini'); m.usage('gpt-5-mini',100,50)
    assert m.db.execute('SELECT cost FROM usage').fetchone()[0] is None
    assert 'не рассчитано' in m.report(live)
    monkeypatch.setenv('API_INPUT_USD_PER_MILLION','1')
    m=Manager(':memory:');m.llm_request('gpt-5-mini');m.usage('gpt-5-mini',100,50)
    Funnel(live).confirm(7,'trial_attended',101,'lesson:1')
    assert '$0.0002' in m.report(live)
    m.llm_request('gpt-5-mini')
    assert 'не рассчитано' in m.report(live)

def test_admin_report_denied(live):
    from manager import commands
    event=SimpleNamespace(raw_text='/воронка',sender_id=303,reply=AsyncMock())
    assert not asyncio.run(commands(None,event,live,admin=False))
    event.reply.assert_not_called()

def test_phone_exact_normalization():
    assert normalized_phone('8 (999) 999-99-99')==normalized_phone('+7 999 999 99 99')
    assert normalized_phone('+1 999 999 9999')!=normalized_phone('+7 999 999 9999')

def test_success_not_reported_as_telegram_error(live):
    from delivery import Ledger
    ledger=Ledger(live)
    ledger.prepare('test',7,'reply','source')
    with live.db:
        live.db.execute("UPDATE outbound_operations SET state='sent',error='' WHERE id='test'")
    assert 'Telegram: ошибки=0' in Funnel(live).report()

def test_conversion_uses_same_clients_and_time(live):
    live.ensure(8)
    with live.db:
        Funnel(live).emit(7,'trial_confirmed',reliability='human',actor_id=101)
        Funnel(live).emit(8,'trial_attended',reliability='human',actor_id=101)
    assert 'trial_confirmed → trial_attended: 0.0% (0/1)' in Funnel(live).report()
