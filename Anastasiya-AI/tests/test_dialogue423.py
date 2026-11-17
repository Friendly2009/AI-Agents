"""State, evidence, restart and handler regressions; all external calls mocked."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from dialogue_state import initial_state, after_reply, reduce_user, guard_model, safe_reply
from storage import Store
from conversation_logic import RECORDING_QUESTION, recording_decision, known_context
from handoff_flow import original_question, transfer_to_managers


@pytest.mark.parametrize('question,text,intent', [
    ('Рассказать о пробном?', 'да', 'explain'),
    ('Рассказать о пробном?', 'хорошо', 'explain'),
    ('Рассказать о пробном?', 'можно', 'explain'),
    ('Рассказать о пробном?', 'давайте', 'explain'),
    ('Передать вопрос менеджеру?', 'да', 'handoff'),
    ('Хотите записаться?', 'да', 'request'),
    ('Хотите прийти?', 'да', 'interest'),
    ('Какой город?', 'да', 'clarify'),
    ('', 'да', 'clarify'),
    ('Хотите записаться?', 'нет', 'decline_request'),
    ('Рассказать о пробном?', 'нет', 'negative'),
    ('Передать вопрос менеджеру?', 'нет', 'negative'),
    (RECORDING_QUESTION, 'да', 'consent'),
    ('', 'позже', 'pause'),
    ('', 'я подумаю', 'pause'),
    ('', 'подумаю', 'pause'),
    ('', 'не сейчас', 'pause'),
    ('', 'интересно', 'interest'),
    ('', 'Мне интересно, сколько это стоит', 'information'),
    ('', 'А сколько стоит?', 'information'),
    ('', 'Запишите нас', 'request'),
    ('', 'Хочу записаться', 'request'),
    ('', 'Не пишите мне', 'decline'),
    ('', 'Не хочу записываться', 'decline'),
    ('', 'Дорого', 'concern'),
    ('', 'Нет опыта', 'concern'),
    ('', 'Ребёнок не любит программирование', 'concern'),
    ('', 'Не подходит время', 'concern'),
    ('', 'Мы посещаем другие курсы', 'concern'),
    ('', 'Не доверяю незнакомому контакту', 'concern'),
    ('', 'Боюсь, не понравится', 'concern'),
])
def test_intent(question, text, intent):
    state, updates = reduce_user(after_reply(initial_state(), question), text, {})
    assert state['intent'] == intent
    if intent != 'request':
        assert not updates.get('wants_trial')
    parsed = guard_model(dict(wants_trial=True, trial_decision='yes', next_action='recording'), state)
    if intent in {'information','explain','interest','clarify','concern','pause','decline','negative'}:
        assert parsed['next_action'] != 'recording'


@pytest.fixture
def store():
    s = Store(':memory:')
    s.ensure(42)
    yield s
    s.db.close()


def test_known_facts_not_asked_again(store):
    ConversationState(store).observe(42, 'Сыну 10 лет, мы из Александрова, он любит компьютерные игры')
    ConversationState(store).observe(42, 'Меня зовут Анна, телефон +7 999 123 45 67, интересует ИИ')
    state = ConversationState(store).observe(42, 'Запишите нас')
    assert store.get(42)['child_age'] == 10
    assert store.get(42)['city'] == 'Александров'
    assert state['interests'] == ['игры']
    assert state['stage'] == 'trial_request'
    assert known_context(store.get(42), state)['dialogue']['children']['son']['age'] == 10


@pytest.mark.parametrize('question,text,key,value', [
    ('Сколько лет ребёнку?', '10', 'child_age', 10),
    ('В каком городе?', 'Ярославль', 'city', 'Ярославль'),
    ('Как к вам обращаться?', 'Анна', 'name', 'Анна'),
    ('Какое направление?', 'Python', 'course', 'Python'),
    ('Какой телефон?', '8 (999) 123-45-67', 'phone', '+79991234567'),
    ('', 'Удобно по вечерам', 'wanted_time', 'по вечерам'),
])
def test_contextual_fact(question,text,key,value,store):
    ConversationState(store).add_turn(42, 'assistant', question)
    ConversationState(store).observe(42, text)
    assert store.get(42)[key] == value


def test_guess_not_fact(store):
    ConversationState(store).observe(42, 'Наверное, в Александрове есть курсы для детей 10 лет?')
    assert not store.get(42)['city']
    assert store.get(42)['child_age'] is None


def test_conflict_not_overwritten(store):
    ConversationState(store).observe(42, 'Мы из Александрова')
    ConversationState(store).observe(42, 'Мы из Ярославля')
    assert store.get(42)['city'] == 'Александров'
    assert store.dialogue(42)['conflicts']['city'] == 'Ярославль'


def test_explicit_correction(store):
    ConversationState(store).observe(42, 'Мы из Александрова')
    ConversationState(store).observe(42, 'Исправление: мы из Ярославля')
    assert store.get(42)['city'] == 'Ярославль'
    assert not store.dialogue(42)['conflicts']


def test_two_children(store):
    ConversationState(store).observe(42, 'Сыну 10 лет, дочке 7 лет')
    state = ConversationState(store).observe(42, 'Запишите нас')
    assert state['children']['son']['age'] == 10
    assert state['children']['daughter']['age'] == 7
    assert store.get(42)['child_age'] is None
    assert 'какого ребёнка' in safe_reply('Какой возраст?', state)


def test_second_child_later(store):
    ConversationState(store).observe(42, 'Сыну 10 лет, любит игры')
    state = ConversationState(store).observe(42, 'Дочке 7 лет')
    assert state['family_ambiguous']
    assert not state['interests']
    assert state['children']['son']['interests'] == ['игры']


def test_select_child(store):
    ConversationState(store).observe(42, 'Сыну 10 лет, дочке 7 лет')
    state = ConversationState(store).observe(42, 'Запишите сына')
    assert state['active_child'] == 'son'
    assert not state['family_ambiguous']
    assert store.get(42)['child_age'] == 10


def test_selected_child_survives_followup(store):
    ConversationState(store).observe(42, 'Сыну 10 лет, дочке 7 лет')
    ConversationState(store).observe(42, 'Запишите сына')
    state=ConversationState(store).observe(42, 'Меня зовут Анна')
    assert state['active_child']=='son' and not state['family_ambiguous']
    assert store.get(42)['child_age']==10


def test_age_conflict_and_correction_survive(store):
    ConversationState(store).observe(42,'Сыну 10 лет')
    state=ConversationState(store).observe(42,'Сыну 11 лет')
    assert state['conflicts']['child_age']==11
    assert store.get(42)['child_age']==10
    ConversationState(store).observe(42,'Исправление: 11')
    ConversationState(store).observe(42,'Спасибо')
    assert store.get(42)['child_age']==11
    assert store.dialogue(42)['children']['son']['age']==11


def test_same_gender_siblings(store):
    ConversationState(store).observe(42,'Сыну 10 лет')
    state=ConversationState(store).observe(42,'Второму сыну 7 лет')
    assert state['children']['son']['age']==10
    assert state['children']['son_2']['age']==7
    assert state['family_ambiguous']


def test_short_city_correction(store):
    ConversationState(store).observe(42,'Мы из Александрова')
    ConversationState(store).observe(42,'Мы из Ярославля')
    ConversationState(store).observe(42,'Исправление: Ярославль')
    assert store.get(42)['city']=='Ярославль'


def test_recovery_report_preserves_uncertainty(store):
    from recovery import report
    store.claim_effect('crm:unknown')
    ConversationState(store).claim_inbound(42,9,'Сколько стоит?')
    data=report(store)
    assert data['effects_require_review'][0]['state']=='sending'
    assert data['inbound_requires_review'][0]['content']=='Сколько стоит?'


def test_invalid_age(store):
    ConversationState(store).observe(42, 'Сыну 99 лет')
    assert store.get(42)['child_age'] is None


def test_missing_answer_consumes_question(store):
    ConversationState(store).add_turn(42, 'assistant', 'Хотите записаться?')
    ConversationState(store).observe(42, 'А сколько стоит?')
    state = ConversationState(store).observe(42, 'да')
    assert state['intent'] == 'clarify'
    assert not state['request_active']


def test_late_yes_is_not_authorization(store):
    ConversationState(store).add_turn(42, 'assistant', 'Хотите записаться?')
    store.set_dialogue(42, updated_at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat())
    assert ConversationState(store).observe(42, 'да')['intent'] == 'clarify'


def test_restart_state_and_event(tmp_path):
    path = str(tmp_path/'db')
    a = Store(path); a.ensure(42)
    ConversationState(a).claim_inbound(42, 1, 'Сыну 10 лет')
    ConversationState(a).observe(42, 'Сыну 10 лет'); ConversationState(a).add_turn(42, 'assistant', 'Рассказать о занятии?')
    a.db.close()
    b = Store(path)
    assert b.get(42)['child_age'] == 10
    assert ConversationState(b).observe(42, 'да')['intent'] == 'explain'
    assert not ConversationState(b).claim_inbound(42, 1, 'Сыну 10 лет')
    b.db.close()


def test_interrupted_event_is_recoverable(tmp_path):
    path = str(tmp_path/'db'); a = Store(path)
    ConversationState(a).claim_inbound(42, 3, 'важный вопрос'); a.db.close()
    b = Store(path)
    assert b.pending_inbound()[0]['content'] == 'важный вопрос'
    b.db.close()


def test_atomic_fact_state_rollback(store):
    with patch.object(store, '_save_dialogue', side_effect=sqlite3.OperationalError('disk full')):
        with pytest.raises(sqlite3.OperationalError):
            ConversationState(store).observe(42, 'Мы из Александрова')
    assert not store.get(42)['city']


def test_two_connections_claim_once(tmp_path):
    a=Store(str(tmp_path/'db')); b=Store(str(tmp_path/'db'))
    assert ConversationState(a).claim_inbound(42, 1, 'вопрос')
    assert not ConversationState(b).claim_inbound(42, 1, 'вопрос')
    a.db.close(); b.db.close()


def test_pause_revokes_pending_submission(store):
    ConversationState(store).update(42, wants_trial=1, crm_consent=1, submission_pending=1)
    state=ConversationState(store).observe(42, 'подумаю')
    assert not store.get(42)['submission_pending']
    assert not store.get(42)['crm_consent']
    assert state['paused']


def test_resume_is_not_new_request(store):
    ConversationState(store).observe(42, 'Запишите нас'); ConversationState(store).observe(42, 'позже')
    state=ConversationState(store).observe(42, 'Сколько стоит?')
    assert state['stage']=='consultation'
    assert not state['request_active']


def test_only_missing_field_requested(store):
    ConversationState(store).observe(42, 'Меня зовут Анна, мы из Александрова, интересует ИИ')
    state=ConversationState(store).observe(42, 'Запишите нас')
    assert safe_reply('Сколько лет и какой город?', state)=='Какой телефон указать для заявки?'


def test_manager_question_not_yes():
    history=[{'role':'user','content':'А родители могут присутствовать?'},
             {'role':'assistant','content':'Передать вопрос менеджеру?'}]
    assert original_question('Да',history)=='А родители могут присутствовать?'


@pytest.mark.parametrize('delivered', [0,1])
def test_handoff_delivery(delivered):
    from manager import Manager
    manager=Manager(':memory:').bind(Store(':memory:'))
    notify=AsyncMock(return_value=delivered)
    history=[{'role':'user','content':'А родители могут присутствовать?'}]
    reply=asyncio.run(transfer_to_managers(None,notify,manager,42,{'city':'Александров'},'Да',history))
    message=notify.call_args.args[1]
    assert 'Вопрос: А родители могут присутствовать?' in message
    assert 'Александров' in message
    assert ('Передала вопрос' in reply) == bool(delivered)


def test_interest_does_not_grant_consent():
    assert recording_decision([{'role':'assistant','content':'Хотите прийти?'}], 'да') is None


def test_atomic_submission(store):
    store.claim_effect('crm:42'); ConversationState(store).submitted(42, 101, 'crm:42')
    assert store.get(42)['crm_id']==101
    assert store.dialogue(42)['stage']=='lead_submitted'
    assert not store.claim_effect('crm:42')


def test_llm_cannot_claim_booking():
    assert 'подтверждения' in safe_reply('Вы записаны!', initial_state()).lower()


def test_delete_memory(store):
    ConversationState(store).observe(42, 'Сыну 10 лет'); ConversationState(store).claim_inbound(42,1,'private')
    store.erase(42)
    assert not store.dialogue(42)['children']
    assert not store.pending_inbound()


def test_handler_serializes_and_ignores_model_guesses(store):
    from app_factory import make_app
    main = make_app()
    from manager import Manager
    from brain import SCHEMA
    @asynccontextmanager
    async def action(*args): yield
    model_data={k:None for k in SCHEMA['required']}
    model_data.update(reply='Объясняю занятие.',wants_trial=True,handoff=False,next_action='recording',
                      trial_decision='yes',city='Ярославль',child_age=15,parent_name='Выдумка')
    model=AsyncMock(return_value=model_data)
    manager=Manager(':memory:').bind(Store(':memory:'))
    ConversationState(store).update(42,ai_consent=1,ai_notice_shown=1)
    ConversationState(store).add_turn(42,'assistant','Рассказать о пробном?')
    event=SimpleNamespace(sender_id=42,chat_id=42,id=1,raw_text='да',reply=AsyncMock(),
                          get_sender=AsyncMock(return_value=SimpleNamespace(bot=False,username='parent')))
    client=SimpleNamespace(action=action,send_message=AsyncMock())
    async def run(): await asyncio.gather(main.handle_dm(client,event), main.handle_dm(client,event))
    with patch.multiple(main,STORE=store,M=manager,TESTERS=set(),CHAT_LOCKS={}), \
         patch.object(main,'respond',model),patch.object(main,'manager_commands',AsyncMock(return_value=False)), \
         patch.object(main.RADAR,'get',return_value=None),patch.object(main.LEARNING,'record'), \
         patch.object(main.LEARNING,'mark_engaged'),patch.object(main.LEARNING,'mark_user'):
        asyncio.run(run())
    model.assert_awaited_once()
    assert not store.get(42)['city'] and store.get(42)['child_age'] is None
    assert store.get(42)['name'] is None and not store.get(42)['wants_trial']
    assert client.send_message.call_args.args[1]=='Объясняю занятие.'
    manager.db.close()


@pytest.mark.parametrize('scenario', ['consent', 'parallel', 'takeover', 'send_failure', 'first_question'])
def test_handler_lifecycle(store, scenario):
    from app_factory import make_app
    main = make_app()
    from manager import Manager
    from brain import SCHEMA
    manager=Manager(':memory:').bind(Store(':memory:'))
    @asynccontextmanager
    async def action(*args): yield
    data={k:None for k in SCHEMA['required']}
    data.update(reply='Ответ по существу.',wants_trial=False,handoff=False,next_action='answer')
    active=0; maximum=0
    async def model(*args, **kwargs):
        nonlocal active, maximum
        active+=1; maximum=max(maximum,active)
        await asyncio.sleep(0)
        if scenario=='takeover': manager.handoff(42)
        active-=1
        return dict(data)
    model_mock=AsyncMock(side_effect=model)
    client=SimpleNamespace(action=action,send_message=AsyncMock())
    def event(text, mid):
        return SimpleNamespace(sender_id=42,chat_id=42,id=mid,raw_text=text,reply=AsyncMock(),
            get_sender=AsyncMock(return_value=SimpleNamespace(bot=False,username='parent')))
    ConversationState(store).update(42,ai_consent=int(scenario!='first_question'),ai_notice_shown=1)
    async def run():
        if scenario=='consent':
            ConversationState(store).observe(42,'Меня зовут Анна, сыну 10 лет, мы из Александрова, интересует ИИ, телефон +79991234567')
            await main.handle_dm(client,event('Запишите нас',1))
            assert client.send_message.call_args.args[1]==RECORDING_QUESTION
            assert not store.get(42)['crm_consent']
            await main.handle_dm(client,event('да',2))
            assert store.get(42)['crm_consent']
            assert store.get(42)['crm_id'] is None  # Safe mode cannot submit.
        elif scenario=='parallel':
            await asyncio.gather(main.handle_dm(client,event('Мы из Александрова',1)),
                                 main.handle_dm(client,event('Сыну 10 лет',2)))
            assert maximum==1 and model_mock.await_count==2
            assert store.get(42)['city']=='Александров' and store.get(42)['child_age']==10
        elif scenario=='takeover':
            await main.handle_dm(client,event('Сколько стоит?',1))
            client.send_message.assert_not_awaited()
        elif scenario=='send_failure':
            client.send_message.side_effect=RuntimeError('delivery failed')
            with pytest.raises(RuntimeError):
                await main.handle_dm(client,event('Сколько стоит?',1))
            assert store.pending_inbound()[0]['content']=='Сколько стоит?'
        else:
            await main.handle_dm(client,event('Сколько стоит?',1))
            model_mock.assert_not_awaited()
            await main.handle_dm(client,event('да',2))
            model_mock.assert_awaited_once()
            assert model_mock.call_args.args[0][-1]['content']=='Сколько стоит?'
    with patch.multiple(main,STORE=store,M=manager,TESTERS=set(),CHAT_LOCKS={}), \
         patch.object(main,'respond',model_mock),patch.object(main,'manager_commands',AsyncMock(return_value=False)), \
         patch.object(main.RADAR,'get',return_value=None),patch.object(main.LEARNING,'record'), \
         patch.object(main.LEARNING,'mark_engaged'),patch.object(main.LEARNING,'mark_user'), \
         patch.dict('os.environ',{'SAFE_TEST_MODE':'true'}):
        asyncio.run(run())
    manager.db.close()

from conversation_service import ConversationState
