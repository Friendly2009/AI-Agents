"""Consultation use case with explicit application dependencies and value input."""
from __future__ import annotations
import asyncio
import json
import logging
import os
from pathlib import Path
import re
from conversation_style import preference_updates, read_preferences, turn_state
from sales_context import guard_sales_action
from handoff_flow import requests_handoff, transfer_to_managers, reply_to_handoff
from policy import is_stop, is_yes, is_ai_decline
from dialogue_state import guard_model, safe_reply, question_expired
from conversation_logic import should_ask_recording, RECORDING_QUESTION, recording_decision, ready_for_recording, known_context
from app_settings import PERMISSION, is_greeting, safe_test_mode
from conversation_service import ConversationState
from handoff_ack import confirm_privacy

LOG = logging.getLogger('softskills')


async def _handle_dm(app, client, event):
    user_id = event.sender_id
    testing = user_id in app.TESTERS
    STORE, M = app.TEST_SESSIONS[user_id] if testing else (app.STORE, app.M)
    M.bind(STORE)
    async def notice_owner(client, message):
        if not testing:
            return await app.notice_owner(client, message)
    sender = event.sender
    if getattr(sender, 'bot', False):
        return
    text = (event.raw_text or '').strip()
    if not text:
        return
    lock = app.CHAT_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        STORE.ensure(user_id, username=getattr(sender, 'username', '') or '', source='Входящий Telegram')
        if not ConversationState(STORE).claim_inbound(user_id, event.id, text):
            return
        STORE.ensure(user_id, username=getattr(sender, 'username', '') or '', source='Входящий Telegram')
        contact = STORE.get(user_id)
        if testing and text.startswith('/'):
            await event.reply('В тесте проверяем обычный диалог. Начать заново: /диалог_тест. Выйти: /конец_теста.')
            return
        if text.lower().strip('.! ') in {'/продолжить', 'продолжим'}:
            STORE.control.resume(user_id, f'telegram:{user_id}:{event.id}')
            await event.reply('Я на связи 🙂 Что хотели бы узнать?')
            return
        if text.lower() in {'/удалить', '/delete'}:
            app.PENDING_QUESTIONS.pop(user_id, None)
            if not testing:
                app.RADAR.remove(user_id)
                app.LEARNING.erase(user_id)
            STORE.erase(user_id, receipt=f'privacy:{user_id}:delete:{event.id}')
            M.erase(user_id)
            await confirm_privacy(STORE, user_id, event.id, 'delete', event.reply, safe=safe_test_mode())
            return
        if text.lower() in {'/стоп', '/stop'} or is_stop(text):
            app.PENDING_QUESTIONS.pop(user_id, None)
            if not testing:
                app.RADAR.stop(user_id)
                app.LEARNING.mark_user(user_id, 'lost')
            STORE.erase(user_id, receipt=f'privacy:{user_id}:stop:{event.id}')
            from funnel import Funnel
            with STORE.db:
                Funnel(STORE).emit(user_id,'client_declined',reliability='intent',confirmation='explicit_stop',details={'reason':'refusal'})
            STORE.set_dialogue(user_id, stage='declined', declined=True)
            M.erase(user_id)
            await confirm_privacy(STORE, user_id, event.id, 'stop', event.reply, safe=safe_test_mode())
            return
        # Ownership applies before family/booking commands can mutate or answer.
        # Explicit repeated handoff requests may recover an unsent receipt below.
        if STORE.control.blocked(user_id) and not requests_handoff(text, STORE.history(user_id)):
            return
        if text.lower() == '/возобновить_семью':
            if os.getenv('FAMILY_RADAR_ENABLED','false').lower() != 'true':
                await event.reply('Регистрация семей пока выключена.')
                return
            if not os.getenv('PRIVACY_POLICY_URL','').strip().startswith('https://'):
                await event.reply('Сейчас регистрация не настроена: нет ссылки на политику обработки персональных данных.')
                return
            app.RADAR.remove(user_id)
            ConversationState(STORE).update(user_id, stopped=0)
            await event.reply(app.RADAR.begin(user_id, getattr(sender,'username','') or '', getattr(sender,'first_name','') or ''))
            return
        if text.lower().startswith('/семья'):
            if contact.get('stopped'):
                await event.reply('Вы ранее остановили общение. Если хотите зарегистрироваться заново, напишите /возобновить_семью.')
                return
            if os.getenv('FAMILY_RADAR_ENABLED','false').lower() != 'true':
                await event.reply('Регистрация семей пока выключена. Организатор Soft Skills включит её после настройки.')
                return
            if not os.getenv('PRIVACY_POLICY_URL','').strip().startswith('https://'):
                await event.reply('Регистрация недоступна: сначала требуется настроить HTTPS-ссылку на действующую политику конфиденциальности.')
                return
            parts = text.split(maxsplit=1)
            source = 'telegram_inbound'
            if len(parts) > 1 and re.fullmatch(r'[a-z0-9_-]{2,24}', parts[1]):
                source = 'telegram_' + parts[1]
            await event.reply(app.RADAR.begin(user_id, getattr(sender,'username','') or '', getattr(sender,'first_name','') or '',source=source))
            return
        family = None if testing else app.RADAR.get(user_id)
        if family and family['stage'] in {'adult','city','age','data_consent','marketing'}:
            await event.reply(app.RADAR.step(user_id, text, os.getenv('PRIVACY_POLICY_URL','')))
            return
        if contact['stopped']:
            return
        if requests_handoff(text, [] if question_expired(STORE.dialogue(user_id)) else STORE.history(user_id)):
            reply = await transfer_to_managers(client, notice_owner, M, user_id, contact, text, STORE.history(user_id), testing, store=STORE)
            ConversationState(STORE).add_turn(user_id, 'user', text)
            if await reply_to_handoff(STORE, user_id, reply, event.reply, testing):
                ConversationState(STORE).add_turn(user_id, 'assistant', reply)
            return
        if not testing and await app.manager_commands(client,event,STORE, manager=M, learning=app.LEARNING):
            return
        if not testing and STORE.configuration().resolve(user_id)['values']['mode'] == 'paused':
            return
        if M.paused(user_id):
            ConversationState(STORE).add_turn(user_id, 'user', text)
            return
        if not testing and not safe_test_mode():
            app.LEARNING.mark_engaged(user_id, text)
        # Finish an outstanding question from older installations only if answered.
        prior = STORE.history(user_id)
        if prior and prior[-1]['content'] == PERMISSION and prior[-1]['role'] == 'assistant':
            if is_ai_decline(text):
                await event.reply('Можно связаться с менеджером Soft Skills: /менеджер.')
                return
            if is_yes(text):
                text = next((m['content'] for m in reversed(prior) if m['role'] == 'user'), text)
        from conversation_info import direct_answer
        answer = direct_answer(text)
        if answer:
            ConversationState(STORE).add_turn(user_id, 'user', text)
            await event.reply(answer)
            ConversationState(STORE).add_turn(user_id, 'assistant', answer)
            return

        decision = recording_decision([] if question_expired(STORE.dialogue(user_id)) else STORE.history(user_id), text)
        dialogue = ConversationState(STORE).observe(user_id, text)
        contact = STORE.get(user_id)
        if decision is True and not ready_for_recording(contact):
            decision = None
        if decision is not None:
            ConversationState(STORE).recording_consent(user_id, decision)
            ConversationState(STORE).add_turn(user_id, 'user', text)
            reply = ('Спасибо! Данные для заявки сохранены. Дату и время занятия ещё нужно подтвердить.'
                     if decision else 'Хорошо, заявку в систему записи не передаю. Можем продолжить консультацию здесь.')
            if (safe_test_mode() or testing) and decision:
                reply = 'Тест: согласие сохранено, реальная заявка не отправлялась. Дата и время занятия не подтверждены.'
            await event.reply(reply)
            ConversationState(STORE).add_turn(user_id, 'assistant', reply)
            if decision:
                result = await app.maybe_crm(client, user_id)
                if result and result.get('crm_id'):
                    receipt = f"В CRM подтверждена карточка обращения #{result['crm_id']}. Дата и время занятия ещё не подтверждены."
                    await event.reply(receipt)
                    ConversationState(STORE).add_turn(user_id, 'assistant', receipt)
            return

        if is_greeting(text) and not STORE.history(user_id):
            reply = 'Здравствуйте! Чем могу помочь?'
            ConversationState(STORE).add_turn(user_id, 'user', text)
            await event.reply(reply)
            ConversationState(STORE).add_turn(user_id, 'assistant', reply)
            return
        preferences = read_preferences(contact.get('speech_preferences'))
        preferences.update(preference_updates(text))
        if preferences:
            ConversationState(STORE).update(user_id, speech_preferences=json.dumps(preferences, ensure_ascii=False))
            contact = STORE.get(user_id)
        ConversationState(STORE).add_turn(user_id, 'user', text)
        response_started = __import__('time').monotonic()
        ownership_revision = STORE.control.get(user_id)['revision']
        # Recording consent is accepted only as an answer to the explicit recording question.
        try:
            # Telegram shows a genuine typing indicator while GPT prepares the response.
            async with client.action(event.chat_id, 'typing'):
                history_for_ai = STORE.history(user_id, limit=30)
                learning_bucket = app.LEARNING.bucket(text, contact)
                learning_selection = (learning_bucket, app.LEARNING.choose(learning_bucket, contact.get('source') or 'unknown'))
                learning_note = app.LEARNING.prompt_hint(text, contact, history_for_ai, selection=learning_selection) if not testing else ''
                parsed = await asyncio.wait_for(app.respond(history_for_ai, campaign=app.load_active(), learning_note=learning_note, manager=M, client_context=known_context(contact, dialogue), configuration=app.STORE.configuration().resolve(user_id)),timeout=45)
        except Exception as exc:
            if (STORE.control.get(user_id)['revision'] != ownership_revision
                    or M.paused(user_id) or (STORE.get(user_id) or {}).get('stopped')):
                return
            from funnel import Funnel
            Funnel(STORE).metric('llm_error')
            LOG.error('Ошибка API GPT: %s', type(exc).__name__)
            if not testing and STORE.configuration().resolve(user_id)['values']['mode'] == 'paused':
                return
            reply = await transfer_to_managers(client, notice_owner, M, user_id, contact, 'Не удалось подготовить ответ: ' + text, STORE.history(user_id), testing, store=STORE)
            if await reply_to_handoff(STORE, user_id, reply, event.reply, testing):
                ConversationState(STORE).add_turn(user_id, 'assistant', reply)
            return
        parsed = guard_sales_action(parsed, text)
        state = turn_state(history_for_ai, preferences=contact.get('speech_preferences'))
        if state['suppress_sales']:
            parsed['wants_trial'] = False
            parsed['trial_decision'] = 'no' if state['mood'] == 'ending' else None
            if not parsed.get('handoff'):
                parsed['next_action'] = 'pause' if state['mood'] == 'ending' else 'answer'
        parsed = guard_model(parsed, dialogue)
        if (dialogue.get('request_active') and dialogue.get('intent') in {'request', 'engage'}
                and not dialogue.get('family_ambiguous') and not dialogue.get('conflicts')
                and ready_for_recording(STORE.get(user_id))):
            parsed['next_action'] = 'recording'
        # Model suggestions never write permanent facts or grant consent.
        if STORE.control.get(user_id)['revision'] != ownership_revision or M.paused(user_id) or (STORE.get(user_id) or {}).get('stopped') or (not testing and STORE.configuration().resolve(user_id)['values']['mode'] == 'paused'):
            return  # A manager took over while the model was running.
        if parsed.get('handoff'):
            reply = await transfer_to_managers(client, notice_owner, M, user_id, STORE.get(user_id), text, STORE.history(user_id), testing, store=STORE)
            if await reply_to_handoff(STORE, user_id, reply, event.reply, testing):
                ConversationState(STORE).add_turn(user_id, 'assistant', reply)
            return
        reply = safe_reply(str(parsed['reply']), dialogue)
        current = STORE.get(user_id)
        if should_ask_recording(contact, current, parsed, STORE.history(user_id, limit=30)):
            reply = RECORDING_QUESTION
        await client.send_message(event.chat_id, reply)
        from funnel import Funnel
        Funnel(STORE).metric('response_seconds', __import__('time').monotonic()-response_started)
        ConversationState(STORE).add_turn(user_id, 'assistant', reply)
        if not testing and not safe_test_mode():
            if parsed.get('trial_decision') == 'yes':
                app.LEARNING.mark_user(user_id, 'trial_interest')
            elif parsed.get('trial_decision') == 'no':
                app.LEARNING.mark_user(user_id, 'paused')
            app.LEARNING.record(user_id, text, parsed, reply, current, selection=learning_selection)


