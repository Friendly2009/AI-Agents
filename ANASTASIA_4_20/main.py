"""Soft Skills Telegram Outreach MVP.

Modes: listen, send-optins, discover, status.
Outgoing private contact ONLY with individually documented prior permission.
Group monitoring ONLY for explicitly addressed requests in admin-approved groups.
"""
from __future__ import annotations
import argparse
import asyncio
import csv
from datetime import date
import json
import logging
import os
from pathlib import Path
import re
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError, RPCError

from brain import respond
from invitations import Invitations
from outgoing import send_once
from manual_dialogue import remember_opening, recover_opening
from manager import Manager, M, commands as manager_commands
from director_orders import (interpret as interpret_director_order, add_pending as add_director_pending,
    preview as director_preview, approve as approve_director_order, reject as reject_director_order,
    summary as director_summary, admin_ids as director_admin_ids, is_admin as is_director_admin)
from campaigns import (load_active, load_draft, compose_first_message, draft_from_owner_idea,
                       activate_draft, pause_campaign, resume_campaign)
from crm import create_lead
from lead_delivery import deliver_lead
from sales_context import guard_sales_action
from sales_learning import LEARNING
from handoff_flow import requests_handoff, transfer_to_managers
from policy import can_outreach, group_trigger, is_stop, is_yes, is_ai_decline, is_crm_consent
from storage import Store
from family_radar import FamilyRadar, campaign_key
from verified_leads import import_verified_csv, source_breakdown, format_pipeline
from community_inspector import (normalize_public_community, inspect_community, save_scan, read_scans,
                                 format_scan, SEED_COMMUNITIES)
from city_sources import (CATEGORIES, init_catalog, read_catalog, catalog_report, mark_local,
                          search_telegram_sources, export_catalog_csv, resolve_category, upsert_sources, locality_signal)
from autojoin import (run_auto_join, refresh_pending, status_report as join_status_report,
                      enable as join_enable, normalize_username as normalize_join_username)

load_dotenv()
LOG = logging.getLogger('softskills')
STORE = Store()
RADAR = FamilyRadar()
BASE = Path(__file__).resolve().parent
from conversation_logic import should_ask_recording, RECORDING_QUESTION, recording_decision, ready_for_recording, known_context, recording_refused

CHAT_LOCKS = {}
# Kept only in RAM until AI consent, never sent to GPT before consent.
PENDING_QUESTIONS = {}
GROUP_REPLIES = set()
TESTERS = set()
TEST_SESSIONS = {}

def reset_test(user_id):
    TEST_SESSIONS[user_id] = (Store(":memory:"), Manager(":memory:"))
    TESTERS.add(user_id)

def is_greeting(text):
    return re.sub(r"[^а-яa-z]", "", text.lower()) in {"привет", "здравствуйте", "добрыйдень", "доброеутро", "добрыйвечер", "здравствуй"}

PERMISSION = ('Здравствуйте! Я Анастасия, ассистент Soft Skills. '
              'Можно обработать ваш вопрос с помощью ИИ? Ответьте «да» или «нет».')



def safe_test_mode() -> bool:
    """Fail closed: no outgoing contact, joining, discovery, or inbound AI replies."""
    return os.getenv('SAFE_TEST_MODE', 'true').strip().lower() != 'false'


def session_path() -> Path:
    """Create a fresh Telethon session. Do not reuse a vendor's TData/Auth Key."""
    label = os.getenv('TG_SESSION_NAME', 'anastasia_own_login').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', label):
        raise SystemExit('TG_SESSION_NAME: только буквы, цифры, - и _, от 3 до 40 символов.')
    folder = BASE / 'data' / 'sessions'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / label

def settings():
    aid = os.getenv('TG_API_ID', '').strip()
    ahash = os.getenv('TG_API_HASH', '').strip()
    if not aid.isdigit() or not ahash:
        raise SystemExit('Укажите TG_API_ID и TG_API_HASH в .env (my.telegram.org).')
    return int(aid), ahash


def allowed_groups():
    path = BASE / 'approved_groups.json'
    groups = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    return [g for g in groups if g.get('admin_permission') is True and g.get('chat') and g.get('marker')]


async def director_reply(client, event, text):
    if event.chat_id == (await client.get_me()).id:
        await notice_owner(client, text)
    else:
        await event.reply(text[:4000])


async def director_message(client, event, owner_id):
    """Run ONLY explicit director commands from pinned numeric Telegram IDs."""
    sender_id = event.sender_id
    message = (event.raw_text or '').strip()
    if message == '/мой_id' and event.is_private:
        # Displays the requester's own ID only, not admin rights.
        await event.reply(f'Ваш Telegram ID: {sender_id}. Для назначения прав директору нужно указать этот ID в локальном .env.')
        return True
    is_voice_order = bool(getattr(event.message, 'voice', False)) and (
        not message or message.lower().startswith('/приказ'))
    is_order = (message.lower().startswith('/приказ ') or is_voice_order or
         message.startswith('/утвердить_приказ ') or
         message.startswith('/отклонить_приказ ') or
         message in {'/распоряжения','/админы','/помощь_директору'})
    if not is_order:
        return False
    if not is_director_admin(sender_id,owner_id):
        await event.reply('Команда недоступна. Сначала настройте ADMIN_OWNER_TG_ID и ADMIN_WIFE_TG_ID в .env или обратитесь к владельцу.')
        return True
    try:
        if message in {'/админы','/помощь_директору'}:
            await director_reply(client,event,
                'Оба администратора имеют одинаковые права, изменения подтверждает любой из них.\n'
                'Текст: /приказ Лагерь 16, 18, 19 и 20 ноября 2026, цену уточнить.\n'
                'Голос: отправьте голосовое в этот личный чат; из Избранного владельца — туда.\n'
                '/распоряжения — утверждённые предложения.\n'
                '/утвердить_приказ НОМЕР и /отклонить_приказ НОМЕР.\n'
                'Команда /мой_id от каждого из вас покажет цифровой Telegram ID.')
        elif message == '/распоряжения':
            await director_reply(client,event,director_summary())
        elif message.startswith('/утвердить_приказ '):
            num=int(message.split(' ',1)[1])
            description=approve_director_order(num,sender_id)
            await director_reply(client,event,f'✅ Приказ №{num} утверждён.\n{description[:450]}\n'
                'До выключения SAFE_TEST_MODE=true это не запускает работу с реальными клиентами.')
        elif message.startswith('/отклонить_приказ '):
            num=int(message.split(' ',1)[1])
            reject_director_order(num,sender_id)
            await director_reply(client,event,f'Черновик приказа №{num} отменён.')
        else:
            if is_voice_order:
                from openai import AsyncOpenAI
                from tempfile import TemporaryDirectory
                from pathlib import Path
                if getattr(event.message.file, 'size',0)>15*1024*1024:
                    raise ValueError('Слишком большое голосовое сообщение (максимум 15 МБ).')
                if not os.getenv('OPENAI_API_KEY','').strip():
                    raise ValueError('Чтобы распознать голос, заполните OPENAI_API_KEY в .env.')
                with TemporaryDirectory() as td:
                    target=await event.message.download_media(file=td + '/order.ogg')
                    if not target: raise ValueError('Не удалось скачать голосовое сообщение')
                    with open(target,'rb') as media:
                        transcript=await AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'],timeout=60).audio.transcriptions.create(
                            model=os.getenv('VOICE_TRANSCRIPTION_MODEL','whisper-1'), file=media,
                            language='ru')
                source=(transcript.text or '').strip()
                if not source: raise ValueError('Не удалось распознать текст распоряжения')
            else:
                source=message[len('/приказ '):].strip()
            if not 8<=len(source)<=4000:
                raise ValueError('Распоряжение должно содержать 8–4000 символов.')
            await director_reply(client,event,'🧠 Распоряжение получено. Готовлю черновик для подтверждения...')
            order=await interpret_director_order(source)
            identifier=add_director_pending(order,sender_id,source)
            await director_reply(client,event,director_preview(order,identifier))
    except (ValueError,KeyError) as exc:
        await director_reply(client,event,'⚠️ ' + str(exc)[:350])
    except Exception as exc:
        LOG.exception('Ошибка команды директора')
        await director_reply(client,event,'⚠️ Не удалось выполнить команду. Проверьте OPENAI_API_KEY и журнал программы. Тип ошибки: '+type(exc).__name__)
    return True


async def notice_owner(client, text):
    recipients = director_admin_ids(0)
    delivered = 0
    for recipient in recipients or {'me'}:
        try:
            await client.send_message(recipient, text, parse_mode=None)
            delivered += 1
        except Exception:
            LOG.exception('Уведомление руководителю не доставлено')
    return delivered


async def maybe_crm(client, user_id):
    if safe_test_mode() or user_id in TESTERS:
        return
    contact = STORE.get(user_id)
    if not contact or not contact.get('submission_pending') or contact.get('stopped') or not contact.get('wants_trial'):
        return
    if not (contact.get('crm_consent') and contact.get('name') and contact.get('phone') and contact.get('city')):
        return
    status = 'отключена; заявка сохранена локально'
    if contact.get('crm_id'):
        status = 'карточка #' + str(contact['crm_id'])
    elif os.getenv('CRM_ENABLED','false').lower() == 'true':
        from crm import _config
        import hashlib
        crm_key = "crm:" + hashlib.sha256((contact["city"] + ":" + contact["phone"]).encode()).hexdigest()
        try:
            configured = _config(contact['city'])
        except ValueError:
            configured = None
        if not configured:
            status = 'не настроена для города; заявка сохранена локально'
        elif not STORE.claim_effect(crm_key):
            return  # Another attempt owns delivery; ambiguous attempts need manual reconciliation.
        else:
            try:
                crm_id = await create_lead(contact)
                if crm_id is None:
                    raise RuntimeError('CRM configuration changed during submission')
                STORE.update(user_id, crm_id=crm_id)
                STORE.finish_effect(crm_key, 'sent')
                status = 'карточка #' + str(crm_id)
            except (Exception, asyncio.CancelledError) as exc:
                STORE.finish_effect(crm_key, 'uncertain')
                LOG.error('AlfaCRM: %s; результат требует сверки', type(exc).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                status = 'результат неизвестен; нужна ручная сверка, автоповтор отключён'
    await deliver_lead(client, STORE, contact, director_admin_ids(0), status)


async def handle_dm(client, event):
    user_id = event.sender_id
    testing = user_id in TESTERS
    STORE, M = TEST_SESSIONS[user_id] if testing else (globals()['STORE'], globals()['M'])
    async def notice_owner(client, message):
        if not testing:
            return await globals()['notice_owner'](client, message)
    sender = await event.get_sender()
    if getattr(sender, 'bot', False):
        return
    text = (event.raw_text or '').strip()
    if not text:
        return
    lock = CHAT_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        if not M.claim(user_id,event.id):
            return
        STORE.ensure(user_id, username=getattr(sender, 'username', '') or '', source='Входящий Telegram')
        contact = STORE.get(user_id)
        if testing and text.startswith('/'):
            await event.reply('В тесте проверяем обычный диалог. Начать заново: /диалог_тест. Выйти: /конец_теста.')
            return
        if text.lower() == '/продолжить':
            STORE.update(user_id, stopped=0)
            M.handoff(user_id, False)
            await event.reply('Я на связи 🙂 Что хотели бы узнать?')
            return
        if text.lower() in {'/удалить', '/delete'}:
            PENDING_QUESTIONS.pop(user_id, None)
            if not testing:
                RADAR.remove(user_id)
                LEARNING.erase(user_id)
            STORE.erase(user_id)
            M.erase(user_id)
            await event.reply('Удалены регистрация семьи и история локальной переписки. Оставлен только технический запрет на повторное обращение. Если данные ранее передавались в AlfaCRM, для удаления оттуда обратитесь к Soft Skills.')
            return
        if text.lower() in {'/стоп', '/stop'} or is_stop(text):
            PENDING_QUESTIONS.pop(user_id, None)
            if not testing:
                RADAR.stop(user_id)
                LEARNING.mark_user(user_id, 'lost')
            STORE.erase(user_id)
            M.erase(user_id)
            await event.reply('Общение и приглашения остановлены. Удалить регистрационные данные: /удалить.')
            return
        if text.lower() == '/возобновить_семью':
            if os.getenv('FAMILY_RADAR_ENABLED','false').lower() != 'true':
                await event.reply('Регистрация семей пока выключена.')
                return
            if not os.getenv('PRIVACY_POLICY_URL','').strip().startswith('https://'):
                await event.reply('Сейчас регистрация не настроена: нет ссылки на политику обработки персональных данных.')
                return
            RADAR.remove(user_id)
            STORE.update(user_id, stopped=0)
            await event.reply(RADAR.begin(user_id, getattr(sender,'username','') or '', getattr(sender,'first_name','') or ''))
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
            await event.reply(RADAR.begin(user_id, getattr(sender,'username','') or '', getattr(sender,'first_name','') or '',source=source))
            return
        family = None if testing else RADAR.get(user_id)
        if family and family['stage'] in {'adult','city','age','data_consent','marketing'}:
            await event.reply(RADAR.step(user_id, text, os.getenv('PRIVACY_POLICY_URL','')))
            return
        if contact['stopped']:
            return
        if requests_handoff(text, STORE.history(user_id)):
            reply = await transfer_to_managers(client, notice_owner, M, user_id, contact, text, STORE.history(user_id), testing)
            STORE.add_turn(user_id, 'user', text)
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            return
        if not testing and await manager_commands(client,event,STORE):
            return
        if M.paused(user_id):
            STORE.add_turn(user_id, 'user', text)
            reply = 'Ваш вопрос ожидает ответа менеджера. Чтобы продолжить со мной, напишите «продолжим».'
            if text.lower().strip('.! ') == 'продолжим':
                M.handoff(user_id, False)
                reply = 'Продолжим 🙂 Что хотите уточнить?'
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            return
        if not testing:
            LEARNING.mark_engaged(user_id, text)
        if not contact.get('ai_consent'):
            prior = STORE.history(user_id)
            asked = bool(prior and prior[-1]['role'] == 'assistant' and prior[-1]['content'] == PERMISSION)
            if asked and is_yes(text):
                STORE.update(user_id, ai_consent=1, ai_notice_shown=1)
                STORE.add_turn(user_id, 'user', text)
                reply = 'Спасибо. Какой вопрос о занятиях хотите обсудить?'
                STORE.add_turn(user_id, 'assistant', reply)
                await event.reply(reply)
            elif is_ai_decline(text):
                await event.reply('Можно продолжить с менеджером Soft Skills: напишите /менеджер.')
            else:
                STORE.add_turn(user_id, 'assistant', PERMISSION)
                await event.reply(PERMISSION)
            return
        if not contact.get('ai_notice_shown'):
            if is_ai_decline(text):
                await event.reply('Можно продолжить с менеджером Soft Skills: напишите /менеджер.')
                return
            intro = 'Я Анастасия, ассистент Soft Skills 🙂 Помогу разобраться с занятиями.'
            if is_greeting(text):
                intro += ' Что хотели бы узнать?'
            await client.send_message(event.chat_id, intro)
            STORE.update(user_id, ai_notice_shown=1)
            contact = STORE.get(user_id)
            if is_greeting(text):
                STORE.add_turn(user_id, 'user', text)
                STORE.add_turn(user_id, 'assistant', intro)
                return
            pending = PENDING_QUESTIONS.pop(user_id, [])
            if pending:
                text = '\n'.join(pending + [text])


        decision = recording_decision(STORE.history(user_id), text)
        if decision is not None:
            STORE.update(user_id, crm_consent=int(decision), submission_pending=int(decision))
            STORE.add_turn(user_id, 'user', text)
            reply = ('Спасибо! Данные для заявки сохранены. Дату и время занятия ещё нужно подтвердить.'
                     if decision else 'Хорошо, заявку в систему записи не передаю. Можем продолжить консультацию здесь.')
            if (safe_test_mode() or testing) and decision:
                reply = 'Тест: согласие сохранено, реальная заявка не отправлялась. Дата и время занятия не подтверждены.'
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            if decision:
                await maybe_crm(client, user_id)
                if not testing:
                    LEARNING.mark_user(user_id, 'lead')
            return

        if is_greeting(text) and not STORE.history(user_id):
            reply = 'Привет 🙂 Что хотели бы узнать о занятиях?'
            STORE.add_turn(user_id, 'user', text)
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            return
        STORE.add_turn(user_id, 'user', text)
        # Recording consent is accepted only as an answer to the explicit recording question.
        try:
            # Telegram shows a genuine typing indicator while GPT prepares the response.
            async with client.action(event.chat_id, 'typing'):
                history_for_ai = STORE.history(user_id, limit=30)
                learning_bucket = LEARNING.bucket(text, contact)
                learning_selection = (learning_bucket, LEARNING.choose(learning_bucket))
                learning_note = LEARNING.prompt_hint(text, contact, history_for_ai, selection=learning_selection) if not testing else ''
                parsed = await asyncio.wait_for(respond([{'role':'assistant','content':'Ранее известные сведения клиента: '+json.dumps(known_context(contact),ensure_ascii=False)}]+history_for_ai, campaign=load_active(), learning_note=learning_note, manager=M),timeout=45)
        except Exception as exc:
            LOG.error('Ошибка API GPT: %s', type(exc).__name__)
            reply = await transfer_to_managers(client, notice_owner, M, user_id, contact, 'Не удалось подготовить ответ: ' + text, STORE.history(user_id), testing)
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            return
        parsed = guard_sales_action(parsed, text)
        updates = {}
        for key in ('city', 'course', 'parent_name', 'phone', 'child_age', 'wanted_time'):
            val = parsed.get(key)
            dest = 'name' if key == 'parent_name' else key
            if val is None or val == '':
                continue
            if key == 'city' and val not in {'Александров', 'Ярославль'}:
                continue
            if key == 'phone':
                digits = ''.join(x for x in str(val) if x.isdigit())
                if len(digits) != 11 or digits[0] not in {'7','8'}:
                    continue
                val = '+7' + digits[1:]
            if key == 'child_age':
                if not isinstance(val, int) or not 0 < val < 19:
                    continue
            updates[dest] = val if key == 'child_age' else str(val)[:100]
        if parsed.get('wants_trial') is True:
            updates['wants_trial'] = 1
            if not parsed.get('course') and not contact.get('course'):
                updates['course'] = load_active().get('title', 'Заявка')[:100]
        if parsed.get('trial_decision') in {'yes', 'no'}:
            updates['wants_trial'] = int(parsed['trial_decision'] == 'yes')
        if parsed.get('trial_decision') == 'yes' and not contact.get('submission_pending'):
            updates['crm_consent'] = 0  # Legacy consent cannot authorize a new request.
        if isinstance(parsed.get('consultation_notes'), str):
            updates['consultation_notes'] = parsed['consultation_notes'][:1000]
        if updates:
            STORE.update(user_id, **updates)
        if parsed.get('handoff'):
            reply = await transfer_to_managers(client, notice_owner, M, user_id, STORE.get(user_id), text, STORE.history(user_id), testing)
            STORE.add_turn(user_id, 'assistant', reply)
            await event.reply(reply)
            return
        reply = str(parsed['reply'])
        current = STORE.get(user_id)
        if should_ask_recording(contact, current, parsed, STORE.history(user_id, limit=30)):
            reply = RECORDING_QUESTION
        await client.send_message(event.chat_id, reply)
        STORE.add_turn(user_id, 'assistant', reply)
        if not testing:
            if parsed.get('trial_decision') == 'yes':
                LEARNING.mark_user(user_id, 'trial_interest')
            elif parsed.get('trial_decision') == 'no':
                LEARNING.mark_user(user_id, 'paused')
            LEARNING.record(user_id, text, parsed, reply, current, selection=learning_selection)


async def handle_groups(client, event, groups):
    if not event.is_group or event.out:
        return
    for group in groups:
        try:
            group_entity = await client.get_entity(group['chat'])
        except (ValueError, RPCError):
            continue
        from telethon import utils
        if event.chat_id != utils.get_peer_id(group_entity):
            continue
        if not group_trigger(event.raw_text or '', group['marker']):
            return
        sender = await event.get_sender()
        if getattr(sender, 'bot', False):
            return
        # No group post content is stored or submitted to a third-party model.
        key = (event.chat_id, event.sender_id)
        if key in GROUP_REPLIES:
            return
        GROUP_REPLIES.add(key)
        await event.reply('Здравствуйте! На связи Анастасия Владимировна, менеджер образовательного проекта Soft Skills. Могу рассказать о наших занятиях и бесплатных мероприятиях. Напишите нам в личные сообщения, если это актуально.')
        return


async def listen(client):
    groups = allowed_groups()
    init_catalog()
    STORE.purge_old_turns(30)
    LOG.info('Режим слушателя активен. Разрешённых групп: %s', len(groups))
    async def outreach_loop():
        while True:
            if os.getenv('OUTREACH_ENABLED', 'false').lower() == 'true':
                try:
                    await send_optins(client)
                except Exception:
                    LOG.exception('Ошибка разрешённой исходящей кампании')
            await asyncio.sleep(max(15, int(os.getenv('OUTREACH_POLL_MINUTES','60'))) * 60)
    async def discover_loop():
        while True:
            if os.getenv('AUTO_DISCOVER_GROUPS', 'true').lower() == 'true':
                try:
                    await discover(client)
                except Exception:
                    LOG.exception('Ошибка поиска тематических групп')
            await asyncio.sleep(24 * 3600)
    async def auto_join_loop():
        # For owner-controlled sources only; disabled until /автовступление вкл.
        while True:
            try:
                if not STORE.outbound_blocked() and os.getenv('AUTO_JOIN_ENABLED', 'false').lower() == 'true':
                    outcome = await run_auto_join(client)
                    if outcome['attempts'] or outcome['preview']:
                        LOG.info('AUTO JOIN: %s', outcome)
                    await refresh_pending(client)
            except FloodWaitError as e:
                LOG.warning('AUTO JOIN: FloodWait %s секунд, без обхода', e.seconds)
            except Exception:
                LOG.exception('Ошибка AUTO JOIN')
            await asyncio.sleep(max(120, int(os.getenv('AUTO_JOIN_POLL_MINUTES', '240'))) * 60)

    async def family_radar_loop():
        while True:
            if os.getenv('FAMILY_RADAR_OUTREACH_ENABLED','false').lower() == 'true':
                try:
                    await send_radar(client)
                except Exception:
                    LOG.exception('Ошибка отправки по зарегистрированным семьям')
            await asyncio.sleep(max(15,int(os.getenv('OUTREACH_POLL_MINUTES','60'))) * 60)
    owner_id = (await client.get_me()).id

    def invitation_consent(uid):
        rec = RADAR.get(uid)
        return bool(rec and RADAR.can_send(rec, {'paused': False, 'title': 'director_greeting'})[0])
    invitations = Invitations(client, STORE, safe_test_mode,
                              director_admin_ids(owner_id) | {owner_id}, invitation_consent)

    async def admin_commands(event):
        """All earlier commands are also available to either approved personal admin ID."""
        message = (event.raw_text or '').strip()
        low = message.lower().strip(' !?.')
        if low in {'ты тут', 'ты работаешь', 'проверь, работаешь ли ты', '/статус'}:
            await event.reply('На связи 🙂 Режим: ' + ('SAFE TEST' if safe_test_mode() else 'рабочий') + '. Для разговора как клиент: /диалог_тест. Управление: /помощь.')
            return
        if low == 'останови приглашения':
            invitations.stop.set()
            await event.reply('Приглашения остановлены. Входящие диалоги продолжаются.')
            return
        if low == 'покажи заявки':
            rows = STORE.db.execute('SELECT user_id,city,course,wants_trial,crm_id FROM contacts WHERE wants_trial=1 AND stopped=0 ORDER BY updated_at DESC LIMIT 15').fetchall()
            await event.reply('Заявок пока нет.' if not rows else '\n'.join(f'ID {r[0]} · {r[1] or "город не указан"} · {r[2] or "курс не выбран"} · ' + ('заявка в CRM' if r[4] else 'ждёт подтверждения менеджера') for r in rows))
            return
        if low in {'/обучение', 'обучение', '/обучение отчёт'}:
            await event.reply(LEARNING.report())
            return
        if low in {'/обучение пауза', 'обучение пауза'}:
            LEARNING.set_enabled(False)
            await event.reply('Обучение менеджера поставлено на паузу. Диалоги продолжаются по утверждённой политике.')
            return
        if low in {'/обучение вкл', '/обучение включить', 'обучение вкл'}:
            LEARNING.set_enabled(True)
            await event.reply('Обучение менеджера включено. Новые подходы будут сравниваться по результатам диалогов.')
            return
        if low.startswith('/обучение успех ') or low.startswith('/обучение посещение ') or low.startswith('/обучение провал '):
            parts = message.split()
            if len(parts) != 3 or not parts[2].isdigit():
                await event.reply('Формат: /обучение успех ID')
                return
            label = {'успех': 'lead', 'посещение': 'attended', 'провал': 'lost'}[parts[1].lower()]
            ok = LEARNING.mark(int(parts[2]), label, event.sender_id)
            await event.reply('Исход сохранён. Менеджер учтёт его в сравнении сценариев.' if ok else 'Такой ID обучения не найден.')
            return
        if await invitations.handle(event):
            return
        if await director_message(client,event,owner_id):
            return
        if await manager_commands(client,event,STORE,admin=True):
            return
        try:
            if safe_test_mode():
                if message in {'/тест', '/проверка', '/помощь'}:
                    await director_reply(client,event,
                        '🔒 ANASTASIA SAFE TEST: включён.\n'
                        'Не отправляю сообщения людям, не отвечаю на входящие, не вступаю ' 
                        'в группы, не запускаю поиск и не передаю данные в CRM.\n'
                        'Команды: /тест GPT Привет; /каталог; /проверка.\n'
                        'Все ответы теста публикуются только здесь, в Избранном.')
                elif message.lower().startswith('/тест gpt '):
                    probe = message[len('/тест GPT '):].strip()[:350]
                    if not os.getenv('OPENAI_API_KEY','').strip():
                        await director_reply(client,event, 'Нет OPENAI_API_KEY в локальном .env. Telegram подключён, но GPT пока не активен.')
                    elif not probe:
                        await director_reply(client,event, 'Напиши /тест GPT Здравствуйте')
                    else:
                        answer = await respond([{'role': 'user', 'content': probe}], campaign=load_active(), manager=Manager(':memory:'))
                        await director_reply(client,event, '🧪 Ответ Анастасии (только Избранное):\n' + str(answer.get('reply',''))[:500])
                elif message == '/каталог':
                    await director_reply(client,event, catalog_report('все'))
                elif message.startswith('/'):
                    await director_reply(client,event, '🔒 Команда отключена в SAFE_TEST_MODE=true. Для проверки: /проверка')
                return
            if message in {'/автовступление', '/вступления', '/заявки'}:
                if message == '/заявки':
                    result = await refresh_pending(client)
                    await director_reply(client,event, f'Обновление заявок: проверено {result["checked"]}, новых одобрений {result["joined"]}.')
                await director_reply(client,event, join_status_report())
            elif message in {'/автовступление вкл', '/автовступление выкл'}:
                new_state = message.endswith('вкл')
                join_enable(new_state)
                await director_reply(client,event, ('✅ Автовступление включено.' if new_state else '⏸ Автовступление выключено.') +
                    '\n' + join_status_report() +
                    '\nЧтобы реальное вступление работало, в .env нужны AUTO_JOIN_ENABLED=true и AUTO_JOIN_DRY_RUN=false.')
            elif message in {'/вступить все', '/вступить всё'} or message.startswith('/вступить '):
                arg = message[len('/вступить '):].strip()
                if arg in {'все', 'всё'}:
                    targets = None
                else:
                    try:
                        targets = [normalize_join_username(arg)]
                    except ValueError as e:
                        await director_reply(client,event, str(e))
                        targets = []
                if targets != []:
                    result = await run_auto_join(client, selected=targets)
                    summary = (f'📡 {result["message"]}\n'
                               f'Попыток: {result["attempts"]}; вступлений: {result["joined"]}; '
                               f'заявок: {result["pending"]}.')
                    if result['preview']:
                        summary += '\nБудут проверены: ' + ', '.join(result['preview'])
                    await director_reply(client,event, summary)
            elif message.startswith('/идея '):
                draft = await draft_from_owner_idea(message[len('/идея '):])
                await director_reply(client,event, '📝 Черновик готов. Ничего не отправлено.\n\n' +
                    compose_first_message('Ирина', draft) +
                    '\n\nДля включения напишите /утвердить, для проверки /показать.')
            elif message == '/категории':
                await director_reply(client,event, '🗂 Анастасия ищет публичные Telegram-группы И КАНАЛЫ Александрова по категориям:\n' +
                    ', '.join(CATEGORIES) +
                    '\n\nПримеры: /найти библиотеки; /найти кафе; /найти школы; /найти все. '
                    'Команда /найти ещё продолжает общий поиск с другого набора запросов.')
            elif message == '/найти ещё' or message == '/найти еще':
                await director_reply(client,event, '🔎 Продолжаю по следующей пачке запросов...')
                stats = await search_telegram_sources(client, 'все', query_limit=9)
                await director_reply(client,event, f'✅ Запросов {stats["queries"]}; новых площадок {stats["new"]}; '
                                           f'всего в каталоге {stats["total"]}. /каталог')
            elif message == '/найти' or message.startswith('/найти '):
                arg = message[len('/найти'):].strip() or 'все'
                category = resolve_category(arg)
                if category not in CATEGORIES and category != 'все':
                    await director_reply(client,event, 'Неизвестная категория: ' + arg + '. Посмотри /категории.')
                else:
                    await director_reply(client,event, '🔎 Проверяю публичные названия Telegram-сообществ: ' + category +
                                       '. Скрытые подписчики и личные аккаунты не запрашиваются.')
                    limit = 9 if category == 'все' else 6
                    try:
                        stats = await search_telegram_sources(client, category, query_limit=limit)
                        await director_reply(client,event,
                            f'✅ Поиск завершён: запросов {stats["queries"]}; публичных площадок найдено в этом проходе '
                            f'{stats["public_communities_this_scan"]}; новых для каталога {stats["new"]}; '
                            f'обновлено {stats["updated"]}; всего площадок {stats["total"]}. '
                            'Списки участников не выгружались.\n' +
                            ('Есть ошибки API: ' + str(stats['errors']) + '.\n' if stats['errors'] else '') +
                            'Результат: /каталог ' + category + '. Дополнительный проход: /найти ещё.')
                    except Exception:
                        LOG.exception('Ошибка CITY RADAR')
                        await director_reply(client,event, '⚠️ Не удалось завершить поиск площадок. Проверь журнал.')
            elif message == '/каталог' or message.startswith('/каталог '):
                arg = message[len('/каталог'):].strip() or 'все'
                try:
                    await director_reply(client,event, catalog_report(arg))
                except ValueError:
                    await director_reply(client,event, 'Неизвестная категория. Напиши /категории.')
            elif message.startswith('/подтвердить_город '):
                source = message.split(maxsplit=1)[1]
                if mark_local(source):
                    await director_reply(client,event, '✅ Местоположение сообщества подтверждено тобой. /каталог')
                else:
                    await director_reply(client,event, 'Такого сообщества нет в каталоге. Сначала /скан или /найти.')
            elif message == '/экспорт_площадок':
                file = export_catalog_csv()
                await client.send_file(event.chat_id, str(file), caption='Каталог только публичных Telegram-сообществ Александрова. Без данных подписчиков.')
            elif message.startswith('/скан ') or message.startswith('/парсинг '):
                # Sends back community-level metadata only. No hidden-member extraction.
                args = message.split(maxsplit=1)[1].split()
                if len(args) == 0 or len(args) > 5:
                    await director_reply(client,event, 'Укажите 1–5 ссылок: /скан https://t.me/alexautolive')
                else:
                    responses=[]
                    for arg in args:
                        try:
                            channel = normalize_public_community(arg)
                            outcome = await inspect_community(client, channel)
                            save_scan(outcome)
                            if outcome.get('type') in {'группа', 'канал'}:
                                status, evidence = locality_signal(outcome.get('title', ''), username=channel)
                                upsert_sources([{
                                    'username': channel, 'title': outcome.get('title', ''),
                                    'category': 'город', 'type': outcome['type'],
                                    'city_status': status, 'city_evidence': evidence,
                                    'member_count': outcome.get('members_or_subscribers'),
                                    'query': 'ручная команда /скан',
                                    'last_checked': outcome.get('checked_at_utc')
                                }])
                            responses.append(format_scan(outcome))
                        except ValueError as exc:
                            responses.append(f'⚠️ Неподходящая ссылка {arg[:64]}: {exc}')
                        except RPCError as exc:
                            responses.append(f'⚠️ Не удалось проверить {arg[:64]}: {type(exc).__name__}. Доступ ограничен Telegram.')
                        except Exception:
                            LOG.exception('Ошибка проверки сообщества')
                            responses.append('⚠️ Ошибка проверки сообщества. Подробности в журнале программы.')
                    for r in responses:
                        await director_reply(client,event, r)
            elif message == '/источники':
                records = read_scans()
                if not records:
                    await director_reply(client,event, 'Пока нет проверок. Отправьте: /скан ' + ' '.join(SEED_COMMUNITIES))
                else:
                    lines=['📚 Проверенные сообщества (публичная статистика):']
                    for r in records[-20:]:
                        count=r.get('members_or_subscribers')
                        lines.append(f'• {r["username"]} — {r["type"]}; ' + (str(count) if count is not None else 'без числа'))
                    await director_reply(client,event, '\n'.join(lines))
            elif message in {'/лиды', '/клиенты'}:
                leads = RADAR.candidates()
                if not leads:
                    await director_reply(client,event,'Пока нет проверенных родителей с согласием на приглашения. Команда /воронка покажет источники.')
                else:
                    lines=['👨‍👩‍👧‍👦 Семьи для дальнейшей работы:']
                    for rec in leads[:15]:
                        first = (rec.get('first_name') or 'Родитель')[:40]
                        lines.append(f'• {first} · возраст: {rec.get("child_ages") or "неизвестен"} · {rec.get("source") or "без источника"} · ID {rec["user_id"]}')
                    lines.append('Доступны лишь семьи с подтверждёнными сведениями и согласием на приглашения.')
                    await director_reply(client,event,'\n'.join(lines))
            elif message == '/воронка':
                await director_reply(client,event, format_pipeline(source_breakdown(RADAR)))
            elif message == '/радар':
                metrics = RADAR.summary()
                await director_reply(client,event, ('📡 FAMILY RADAR\n'
                    f'Начали регистрацию: {metrics["registered"]}\n'
                    f'Подтверждённые семьи Александрова: {metrics["verified_families"]}\n'
                    f'Разрешили получать приглашения: {metrics["allowed_outreach"]}\n'
                    f'Отписались: {metrics["stopped"]}\n'
                    f'Приглашений сегодня: {metrics["sent_today"]}'))
            elif message == '/показать':
                active = load_active()
                draft = load_draft()
                info = 'Активная идея: ' + active['title'] + (' (на паузе)' if active.get('paused', True) else ' (готова)')
                if draft:
                    info += '\n\nЧерновик: ' + draft['title'] + '\n\n' + compose_first_message('Ирина', draft)
                await director_reply(client,event, info)
            elif message == '/утвердить':
                active = activate_draft()
                await director_reply(client,event, f'✅ Идея «{active["title"]}» активирована. Отправка всё ещё зависит от OUTREACH_ENABLED и DRY_RUN.')
            elif message == '/пауза':
                pause_campaign()
                await director_reply(client,event, '⏸ Текущая исходящая кампания поставлена на паузу. Входящие ответы продолжаются.')
            elif message == '/возобновить':
                resume_campaign()
                await director_reply(client,event, '▶️ Кампания возобновлена. Исходящие всё равно требуют настроенного разрешённого списка и OUTREACH_ENABLED=true.')
            else:
                await event.reply('Это директорский чат. Для проверки консультации: /диалог_тест. Можно написать «покажи заявки», «ты тут?» или «останови приглашения». Все команды: /помощь.')
        except Exception as exc:
            LOG.exception('Команда владельца не выполнена')
            await director_reply(client,event, '⚠️ Не удалось выполнить команду: ' + type(exc).__name__)

    @client.on(events.NewMessage(outgoing=True))
    async def owner_command(event):
        # Existing administrative Saved Messages support is kept for the service account.
        if event.is_private and event.chat_id == owner_id:
            await admin_commands(event)

    @client.on(events.NewMessage(outgoing=True))
    async def manual_opening(event):
        if safe_test_mode() or not event.is_private or event.chat_id == owner_id:
            return
        uid = event.chat_id
        if is_director_admin(uid, owner_id) or uid == 777000:
            return
        try:
            recipient = await event.get_chat()
            if not isinstance(recipient, types.User) or recipient.bot or recipient.deleted:
                return
            async with CHAT_LOCKS.setdefault(uid, asyncio.Lock()):
                remembered = remember_opening(STORE, M, uid, event.raw_text or '',
                                              getattr(recipient, 'username', '') or '')
                if remembered:
                    LOG.info('Ручной диалог подключён: ID %s; ожидаем ответ', uid)
        except Exception:
            LOG.exception('Не удалось сохранить начало ручного диалога')

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if event.is_private:
            # Both personal administrators can issue the entire set of administrative commands.
            if is_director_admin(event.sender_id,owner_id):
                if event.sender_id in invitations.collecting and not (event.raw_text or '').startswith('/'):
                    await invitations.handle(event)
                    return
                if event.raw_text == '/диалог_тест':
                    reset_test(event.sender_id)
                    await event.reply('Новый тестовый диалог: память чистая. Реальные заявки, CRM и уведомления руководителям отключены независимо от рабочего режима. Выход: /конец_теста')
                    return
                if event.raw_text == '/конец_теста':
                    TESTERS.discard(event.sender_id)
                    await event.reply('Тест завершён')
                    return
                if event.sender_id in TESTERS and (not (event.raw_text or '').startswith('/') or (event.raw_text or '').split()[0] in {'/менеджер','/бронь','/отмена','/напомнить','/согласие_на_запись','/стоп','/удалить','/расписание'}):
                    await handle_dm(client,event)
                else:
                    await admin_commands(event)
                return
            # /мой_id is available before the two IDs are configured; it grants no rights.
            if await director_message(client,event,owner_id):
                return
        if safe_test_mode():
            return  # No processing, saving or answering messages from other people.
        try:
            if event.is_private:
                try:
                    async with CHAT_LOCKS.setdefault(event.sender_id, asyncio.Lock()):
                        await recover_opening(client, event, STORE, M, excluded=director_admin_ids(owner_id) | {owner_id})
                except Exception:
                    LOG.exception('Не удалось восстановить ручное начало диалога')
                await handle_dm(client, event)
            elif groups and event.is_group:
                await handle_groups(client, event, groups)
        except FloodWaitError as e:
            LOG.warning('Telegram ограничил частоту: %s секунд. Без обхода ограничений.', e.seconds)
        except Exception:
            LOG.exception('Ошибка обработки сообщения')
    async def reminders_loop():
        while True:
            STORE.purge_old_turns(int(os.getenv('HISTORY_DAYS','30')))
            if os.getenv('REMINDERS_ENABLED','false').lower()=='true':
                for b in M.due():
                    contact=STORE.get(b['uid']) or {}
                    if contact.get('stopped') or safe_test_mode() or STORE.outbound_blocked(): continue
                    # Mark before send: ambiguous delivery never triggers duplicate automatic reminders.
                    with M.db: M.db.execute('UPDATE bookings SET sent=1 WHERE id=?',(b['id'],))
                    try:
                        await send_once(STORE, f"reminder:{b['id']}", b['uid'],
                            lambda: client.send_message(b['uid'],f"Напоминаем: {b['course']}, {b['starts']} (UTC). Бронь #{b['id']}. Отмена: /отмена {b['id']}",parse_mode=None))
                    except Exception:
                        await notice_owner(client,f"Не удалось доставить напоминание по брони #{b['id']}. Проверьте вручную.")
            await asyncio.sleep(60)
    tasks = [] if safe_test_mode() else [asyncio.create_task(reminders_loop()),
        asyncio.create_task(discover_loop()), asyncio.create_task(family_radar_loop()),
        asyncio.create_task(auto_join_loop())]
    if safe_test_mode():
        LOG.info('🔒 SAFE_TEST_MODE: фоновые действия отключены, только команды из Избранного')
    try:
        await client.run_until_disconnected()
    finally:
        if invitations.task:
            tasks.append(invitations.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def send_optins(client):
    print('Режим CSV-аутрича из v2 в FAMILY RADAR v3 отключён: он не подтверждает возраст ребёнка. Используйте регистрацию /семья и send-radar.')
    return


async def send_radar(client):
    """Campaign follow-ups ONLY to consenting adults who initiated contact."""
    if safe_test_mode() or STORE.outbound_blocked():
        return
    if os.getenv('FAMILY_RADAR_OUTREACH_ENABLED','false').lower() != 'true':
        print('FAMILY_RADAR_OUTREACH_ENABLED=false: рассылка отключена')
        return
    campaign = load_active()
    if campaign.get('paused',True):
        print('Кампания на паузе. В Избранном: /показать, /возобновить')
        return
    dry_run = os.getenv('DRY_RUN','true').lower() == 'true'
    daily_limit = max(1,min(int(os.getenv('DAILY_LIMIT','10')),20))
    interval = max(60,int(os.getenv('SEND_INTERVAL_SECONDS','120')))
    for rec in RADAR.candidates():
        if safe_test_mode() or STORE.outbound_blocked():
            break
        if STORE.sent_today() + RADAR.sent_today() >= daily_limit:
            print('Достигнут суточный лимит отправок')
            return
        ok, why = RADAR.can_send(rec,campaign)
        if not ok:
            print(f'Пропущен Telegram ID {rec["user_id"]}: {why}')
            continue
        if (STORE.get(rec['user_id']) or {}).get('stopped'):
            print(f'Пропуск {rec["user_id"]}: отказ от общения')
            continue
        # Names from profile are optional; skip ambiguous/non-word profile names.
        first = (rec.get('first_name') or '').strip()
        if not re.fullmatch(r'[А-Яа-яЁёA-Za-z-]{2,30}', first):
            first = ''
        message = compose_first_message(first, campaign)
        if dry_run:
            print(f'[БЕЗ ОТПРАВКИ] ID {rec["user_id"]}: {message}')
            continue
        try:
            # Reserve campaign/cooldown before the network; never retry uncertain delivery.
            RADAR.mark_sent(rec['user_id'],campaign)
            if not await send_once(STORE, f'radar:{rec["user_id"]}:{campaign_key(campaign)}',
                    rec['user_id'], lambda: client.send_message(rec['user_id'],message,parse_mode=None)):
                continue
            print(f'Приглашение отправлено существующему подписчику {rec["user_id"]}')
            await asyncio.sleep(interval)
        except FloodWaitError as e:
            print(f'Ограничение Telegram: остановка отправки на {e.seconds} секунд')
            break
        except Exception as ex:
            print(f'Отправки остановлены: {type(ex).__name__}; результат требует проверки')
            break


async def discover(client):
    """Daily rotating search across Alexandrov categories. Community metadata only."""
    init_catalog()
    limit = max(1, min(12, int(os.getenv('CITY_RADAR_QUERIES_PER_RUN','8'))))
    stats = await search_telegram_sources(client, 'все', query_limit=limit)
    LOG.info('CITY RADAR: queries=%s new=%s total=%s errors=%s',
             stats['queries'], stats['new'], stats['total'], stats['errors'])
    return stats


async def retry_crm(client):
    for contact in STORE.list_ready():
        await maybe_crm(client, contact['user_id'])
        await asyncio.sleep(1)


async def main():
    parser = argparse.ArgumentParser(description='Soft Skills Telegram Outreach')
    parser.add_argument('mode', choices=['listen','send-optins','send-radar','discover','retry-crm','status','preview','radar-preview','import-verified','lead-report','join-status','join-cycle'])
    parser.add_argument('--file', default='', help='CSV с документированными согласиями для import-verified')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    if args.mode == 'import-verified':
        if not args.file:
            raise SystemExit('Укажите --file путь_к_csv')
        stats = import_verified_csv(args.file, RADAR)
        print('Результат импорта:', json.dumps(stats, ensure_ascii=False, indent=2))
        print(format_pipeline(source_breakdown(RADAR)))
        return
    if args.mode == 'join-status':
        print(join_status_report())
        return
    if args.mode == 'lead-report':
        print(format_pipeline(source_breakdown(RADAR)))
        return
    if args.mode == 'preview':
        camp = load_active()
        print('Идея:', camp['title'], '| пауза:', camp.get('paused', True))
        print(compose_first_message('Ирина', camp))
        return
    if args.mode == 'status':
        print(f'Разрешённых первых обращений отправлено сегодня: {STORE.sent_today()}')
        print(f'Заявок, ожидающих отправки в CRM: {len(STORE.list_ready())}')
        print('FAMILY RADAR:', json.dumps(RADAR.summary(), ensure_ascii=False))
        print('CITY RADAR: public community sources=', len(init_catalog()))
        return
    if args.mode == 'radar-preview':
        campaign = load_active()
        print('Кампания:',campaign['title'],'| пауза:',campaign.get('paused',True))
        for rec in RADAR.candidates():
            eligible, reason = RADAR.can_send(rec,campaign)
            print('Кандидат:',rec['user_id'],'| можно связаться:',eligible,'|',reason)
        print('Результаты:',json.dumps(RADAR.summary(),ensure_ascii=False))
        return
    if safe_test_mode() and args.mode not in {'listen'}:
        raise SystemExit('SAFE_TEST_MODE=true: для безопасности доступен только режим listen (и офлайн-отчёты).')
    import sqlite3
    process_lock=sqlite3.connect(str(BASE/'data'/'running.lock.db'),timeout=0)
    try:
        process_lock.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError:
        raise SystemExit('Анастасия уже запущена из этой папки. Закройте первое окно.')
    aid, ahash = settings()
    client = TelegramClient(str(session_path()), aid, ahash, flood_sleep_threshold=0,
                            request_retries=0, raise_last_call_error=True)
    try:
        await client.start(phone=os.getenv('TG_PHONE', '').strip() or None)
        if args.mode == 'listen':
            await listen(client)
        elif args.mode == 'send-optins':
            await send_optins(client)
        elif args.mode == 'send-radar':
            await send_radar(client)
        elif args.mode == 'discover':
            await discover(client)
        elif args.mode == 'join-cycle':
            result = await run_auto_join(client)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(join_status_report())
        elif args.mode == 'retry-crm':
            await retry_crm(client)
    finally:
        try:
            await client.disconnect()
        finally:
            process_lock.close()


if __name__ == '__main__':
    asyncio.run(main())
