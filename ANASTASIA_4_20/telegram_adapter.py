"""Telegram event conversion, command routing and background task lifecycle."""
from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from telethon import events, types
from telethon.errors import FloodWaitError, RPCError
from invitations import Invitations
from outgoing import send_once
from manual_dialogue import remember_opening, recover_opening
from manager import Manager
from director_orders import interpret as interpret_director_order, add_pending as add_director_pending, preview as director_preview, approve as approve_director_order, reject as reject_director_order, summary as director_summary, is_admin as is_director_admin
from campaigns import load_draft, compose_first_message, draft_from_owner_idea, activate_draft, pause_campaign, resume_campaign
from policy import group_trigger
from verified_leads import source_breakdown, format_pipeline
from community_inspector import normalize_public_community, inspect_community, save_scan, read_scans, format_scan, SEED_COMMUNITIES
from city_sources import CATEGORIES, init_catalog, catalog_report, mark_local, search_telegram_sources, export_catalog_csv, resolve_category, upsert_sources, locality_signal
from autojoin import run_auto_join, refresh_pending, status_report as join_status_report, enable as join_enable, normalize_username as normalize_join_username
from app_settings import allowed_groups, safe_test_mode
from consultation_input import ConsultationInput

LOG = logging.getLogger('softskills')


async def director_reply(app, client, event, text):
    if event.chat_id == (await client.get_me()).id:
        await app.notice_owner(client, text)
    else:
        await event.reply(text[:4000])


async def director_message(app, client, event, owner_id):
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
            await app.director_reply(client,event,
                'Оба администратора имеют одинаковые права, изменения подтверждает любой из них.\n'
                'Текст: /приказ Лагерь 16, 18, 19 и 20 ноября 2026, цену уточнить.\n'
                'Голос: отправьте голосовое в этот личный чат; из Избранного владельца — туда.\n'
                '/распоряжения — утверждённые предложения.\n'
                '/утвердить_приказ НОМЕР и /отклонить_приказ НОМЕР.\n'
                'Команда /мой_id от каждого из вас покажет цифровой Telegram ID.')
        elif message == '/распоряжения':
            await app.director_reply(client,event,director_summary())
        elif message.startswith('/утвердить_приказ '):
            num=int(message.split(' ',1)[1])
            description=approve_director_order(num,sender_id)
            await app.director_reply(client,event,f'✅ Приказ №{num} утверждён.\n{description[:450]}\n'
                'До выключения SAFE_TEST_MODE=true это не запускает работу с реальными клиентами.')
        elif message.startswith('/отклонить_приказ '):
            num=int(message.split(' ',1)[1])
            reject_director_order(num,sender_id)
            await app.director_reply(client,event,f'Черновик приказа №{num} отменён.')
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
            await app.director_reply(client,event,'🧠 Распоряжение получено. Готовлю черновик для подтверждения...')
            order=await interpret_director_order(source, manager=app.M)
            identifier=add_director_pending(order,sender_id,source)
            await app.director_reply(client,event,director_preview(order,identifier))
    except (ValueError,KeyError) as exc:
        await app.director_reply(client,event,'⚠️ ' + str(exc)[:350])
    except Exception as exc:
        LOG.exception('Ошибка команды директора')
        await app.director_reply(client,event,'⚠️ Не удалось выполнить команду. Проверьте OPENAI_API_KEY и журнал программы. Тип ошибки: '+type(exc).__name__)
    return True


async def notice_owner(app, client, text):
    recipients = app.director_admin_ids(0)
    delivered = 0
    uncertain = False
    for recipient in recipients or {'me'}:
        try:
            await client.send_message(recipient, text, parse_mode=None)
            delivered += 1
        except Exception as exc:
            uncertain |= not isinstance(exc, RPCError) or (getattr(exc, 'code', None) or 500) >= 500
            LOG.exception('Уведомление руководителю не доставлено')
    if not delivered and uncertain:
        raise RuntimeError('Manager notification unconfirmed; reconciliation required')
    return delivered


async def handle_dm(app, client, event):
    store = app.TEST_SESSIONS[event.sender_id][0] if event.sender_id in app.TESTERS else app.STORE
    try:
        await app._handle_dm(client, ConsultationInput(event.sender_id, event.chat_id, event.id, event.raw_text, await event.get_sender(), event.reply))
        store.complete_inbound(event.sender_id, event.id)
    finally:
        if store._inbound.get(event.sender_id) == event.id:
            store._inbound.pop(event.sender_id, None)


async def handle_groups(app, client, event, groups):
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
        if key in app.GROUP_REPLIES:
            return
        app.GROUP_REPLIES.add(key)
        await event.reply('Здравствуйте! На связи Анастасия Владимировна, менеджер образовательного проекта Soft Skills. Могу рассказать о наших занятиях и бесплатных мероприятиях. Напишите нам в личные сообщения, если это актуально.')
        return


async def listen(app, client):
    groups = allowed_groups()
    init_catalog()
    app.STORE.purge_old_turns(30)
    LOG.info('Режим слушателя активен. Разрешённых групп: %s', len(groups))
    async def outreach_loop():
        while True:
            if os.getenv('OUTREACH_ENABLED', 'false').lower() == 'true':
                try:
                    await app.send_optins(client)
                except Exception:
                    LOG.exception('Ошибка разрешённой исходящей кампании')
            await asyncio.sleep(max(15, int(os.getenv('OUTREACH_POLL_MINUTES','60'))) * 60)
    async def discover_loop():
        while True:
            if os.getenv('AUTO_DISCOVER_GROUPS', 'true').lower() == 'true':
                try:
                    await app.discover(client)
                except Exception:
                    LOG.exception('Ошибка поиска тематических групп')
            await asyncio.sleep(24 * 3600)
    async def auto_join_loop():
        # For owner-controlled sources only; disabled until /автовступление вкл.
        while True:
            try:
                if not app.STORE.outbound_blocked() and os.getenv('AUTO_JOIN_ENABLED', 'false').lower() == 'true':
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
                    await app.send_radar(client)
                except Exception:
                    LOG.exception('Ошибка отправки по зарегистрированным семьям')
            await asyncio.sleep(max(15,int(os.getenv('OUTREACH_POLL_MINUTES','60'))) * 60)
    owner_id = (await client.get_me()).id

    def invitation_consent(uid):
        rec = app.RADAR.get(uid)
        return bool(rec and app.RADAR.can_send(rec, {'paused': False, 'title': 'director_greeting'})[0])
    invitations = Invitations(client, app.STORE, safe_test_mode,
                              app.director_admin_ids(owner_id) | {owner_id}, invitation_consent)

    async def admin_commands(event):
        """All earlier commands are also available to either approved personal admin ID."""
        message = (event.raw_text or '').strip()
        low = message.lower().strip(' !?.')
        if low == '/панель':
            from agent_config import require_admin
            require_admin(event.sender_id)
            code = app.STORE.configuration().issue_login(event.sender_id)
            await event.reply('Панель на компьютере агента: http://127.0.0.1:8765\nОдноразовый код на 5 минут:\n' + code)
            return
        if low in {'ты тут', 'ты работаешь', 'проверь, работаешь ли ты', '/статус'}:
            await event.reply('На связи 🙂 Режим: ' + ('SAFE TEST' if safe_test_mode() else 'рабочий') + '. Для разговора как клиент: /диалог_тест. Управление: /помощь.')
            return
        if low == 'останови приглашения':
            invitations.stop.set()
            await event.reply('Приглашения остановлены. Входящие диалоги продолжаются.')
            return
        if low == 'покажи заявки':
            rows = app.STORE.db.execute('SELECT user_id,city,course,wants_trial,crm_id FROM contacts WHERE wants_trial=1 AND stopped=0 ORDER BY updated_at DESC LIMIT 15').fetchall()
            await event.reply('Заявок пока нет.' if not rows else '\n'.join(f'ID {r[0]} · {r[1] or "город не указан"} · {r[2] or "курс не выбран"} · ' + ('заявка в CRM' if r[4] else 'ждёт подтверждения менеджера') for r in rows))
            return
        if low in {'/обучение', 'обучение', '/обучение отчёт'}:
            await event.reply(app.LEARNING.report())
            return
        if low in {'/обучение пауза', 'обучение пауза'}:
            app.LEARNING.set_enabled(False)
            await event.reply('Обучение менеджера поставлено на паузу. Диалоги продолжаются по утверждённой политике.')
            return
        if low in {'/обучение вкл', '/обучение включить', 'обучение вкл'}:
            app.LEARNING.set_enabled(True)
            await event.reply('Обучение менеджера включено. Новые подходы будут сравниваться по результатам диалогов.')
            return
        if low.startswith('/обучение успех ') or low.startswith('/обучение посещение ') or low.startswith('/обучение провал '):
            parts = message.split()
            if len(parts) != 3 or not parts[2].isdigit():
                await event.reply('Формат: /обучение успех ID')
                return
            label = {'успех': 'lead', 'посещение': 'attended', 'провал': 'lost'}[parts[1].lower()]
            if safe_test_mode():
                await event.reply('SAFE TEST: разметка рабочих результатов отключена.')
                return
            row = app.LEARNING.db.execute('SELECT user_id FROM interactions WHERE id=?',(int(parts[2]),)).fetchone()
            if row and label == 'attended':
                from funnel import Funnel
                Funnel(app.STORE).confirm(row[0],'trial_attended',event.sender_id,'learning:'+parts[2])
            ok = app.LEARNING.mark(int(parts[2]), label, event.sender_id)
            await event.reply('Исход сохранён. Менеджер учтёт его в сравнении сценариев.' if ok else 'Такой ID обучения не найден.')
            return
        if await invitations.handle(event):
            return
        if await app.director_message(client,event,owner_id):
            return
        if await app.manager_commands(client,event,app.STORE,admin=True, manager=app.M, learning=app.LEARNING):
            return
        try:
            if safe_test_mode():
                if message in {'/тест', '/проверка', '/помощь'}:
                    await app.director_reply(client,event,
                        '🔒 ANASTASIA SAFE TEST: включён.\n'
                        'Не отправляю сообщения людям, не отвечаю на входящие, не вступаю ' 
                        'в группы, не запускаю поиск и не передаю данные в CRM.\n'
                        'Команды: /тест GPT Привет; /каталог; /проверка.\n'
                        'Все ответы теста публикуются только здесь, в Избранном.')
                elif message.lower().startswith('/тест gpt '):
                    probe = message[len('/тест GPT '):].strip()[:350]
                    if not os.getenv('OPENAI_API_KEY','').strip():
                        await app.director_reply(client,event, 'Нет OPENAI_API_KEY в локальном .env. Telegram подключён, но GPT пока не активен.')
                    elif not probe:
                        await app.director_reply(client,event, 'Напиши /тест GPT Здравствуйте')
                    else:
                        probe_manager = Manager(':memory:')
                        try:
                            answer = await app.respond([{'role': 'user', 'content': probe}], campaign=app.load_active(), manager=probe_manager, configuration=app.STORE.configuration().resolve())
                        finally:
                            probe_manager.db.close()
                        await app.director_reply(client,event, '🧪 Ответ Анастасии (только Избранное):\n' + str(answer.get('reply',''))[:500])
                elif message == '/каталог':
                    await app.director_reply(client,event, catalog_report('все'))
                elif message.startswith('/'):
                    await app.director_reply(client,event, '🔒 Команда отключена в SAFE_TEST_MODE=true. Для проверки: /проверка')
                return
            if message in {'/автовступление', '/вступления', '/заявки'}:
                if message == '/заявки':
                    result = await refresh_pending(client)
                    await app.director_reply(client,event, f'Обновление заявок: проверено {result["checked"]}, новых одобрений {result["joined"]}.')
                await app.director_reply(client,event, join_status_report())
            elif message in {'/автовступление вкл', '/автовступление выкл'}:
                new_state = message.endswith('вкл')
                join_enable(new_state)
                await app.director_reply(client,event, ('✅ Автовступление включено.' if new_state else '⏸ Автовступление выключено.') +
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
                        await app.director_reply(client,event, str(e))
                        targets = []
                if targets != []:
                    result = await run_auto_join(client, selected=targets)
                    summary = (f'📡 {result["message"]}\n'
                               f'Попыток: {result["attempts"]}; вступлений: {result["joined"]}; '
                               f'заявок: {result["pending"]}.')
                    if result['preview']:
                        summary += '\nБудут проверены: ' + ', '.join(result['preview'])
                    await app.director_reply(client,event, summary)
            elif message.startswith('/идея '):
                draft = await draft_from_owner_idea(message[len('/идея '):], manager=app.M)
                await app.director_reply(client,event, '📝 Черновик готов. Ничего не отправлено.\n\n' +
                    compose_first_message('Ирина', draft) +
                    '\n\nДля включения напишите /утвердить, для проверки /показать.')
            elif message == '/категории':
                await app.director_reply(client,event, '🗂 Анастасия ищет публичные Telegram-группы И КАНАЛЫ Александрова по категориям:\n' +
                    ', '.join(CATEGORIES) +
                    '\n\nПримеры: /найти библиотеки; /найти кафе; /найти школы; /найти все. '
                    'Команда /найти ещё продолжает общий поиск с другого набора запросов.')
            elif message == '/найти ещё' or message == '/найти еще':
                await app.director_reply(client,event, '🔎 Продолжаю по следующей пачке запросов...')
                stats = await search_telegram_sources(client, 'все', query_limit=9)
                await app.director_reply(client,event, f'✅ Запросов {stats["queries"]}; новых площадок {stats["new"]}; '
                                           f'всего в каталоге {stats["total"]}. /каталог')
            elif message == '/найти' or message.startswith('/найти '):
                arg = message[len('/найти'):].strip() or 'все'
                category = resolve_category(arg)
                if category not in CATEGORIES and category != 'все':
                    await app.director_reply(client,event, 'Неизвестная категория: ' + arg + '. Посмотри /категории.')
                else:
                    await app.director_reply(client,event, '🔎 Проверяю публичные названия Telegram-сообществ: ' + category +
                                       '. Скрытые подписчики и личные аккаунты не запрашиваются.')
                    limit = 9 if category == 'все' else 6
                    try:
                        stats = await search_telegram_sources(client, category, query_limit=limit)
                        await app.director_reply(client,event,
                            f'✅ Поиск завершён: запросов {stats["queries"]}; публичных площадок найдено в этом проходе '
                            f'{stats["public_communities_this_scan"]}; новых для каталога {stats["new"]}; '
                            f'обновлено {stats["updated"]}; всего площадок {stats["total"]}. '
                            'Списки участников не выгружались.\n' +
                            ('Есть ошибки API: ' + str(stats['errors']) + '.\n' if stats['errors'] else '') +
                            'Результат: /каталог ' + category + '. Дополнительный проход: /найти ещё.')
                    except Exception:
                        LOG.exception('Ошибка CITY RADAR')
                        await app.director_reply(client,event, '⚠️ Не удалось завершить поиск площадок. Проверь журнал.')
            elif message == '/каталог' or message.startswith('/каталог '):
                arg = message[len('/каталог'):].strip() or 'все'
                try:
                    await app.director_reply(client,event, catalog_report(arg))
                except ValueError:
                    await app.director_reply(client,event, 'Неизвестная категория. Напиши /категории.')
            elif message.startswith('/подтвердить_город '):
                source = message.split(maxsplit=1)[1]
                if mark_local(source):
                    await app.director_reply(client,event, '✅ Местоположение сообщества подтверждено тобой. /каталог')
                else:
                    await app.director_reply(client,event, 'Такого сообщества нет в каталоге. Сначала /скан или /найти.')
            elif message == '/экспорт_площадок':
                file = export_catalog_csv()
                await client.send_file(event.chat_id, str(file), caption='Каталог только публичных Telegram-сообществ Александрова. Без данных подписчиков.')
            elif message.startswith('/скан ') or message.startswith('/парсинг '):
                # Sends back community-level metadata only. No hidden-member extraction.
                args = message.split(maxsplit=1)[1].split()
                if len(args) == 0 or len(args) > 5:
                    await app.director_reply(client,event, 'Укажите 1–5 ссылок: /скан https://t.me/alexautolive')
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
                        await app.director_reply(client,event, r)
            elif message == '/источники':
                records = read_scans()
                if not records:
                    await app.director_reply(client,event, 'Пока нет проверок. Отправьте: /скан ' + ' '.join(SEED_COMMUNITIES))
                else:
                    lines=['📚 Проверенные сообщества (публичная статистика):']
                    for r in records[-20:]:
                        count=r.get('members_or_subscribers')
                        lines.append(f'• {r["username"]} — {r["type"]}; ' + (str(count) if count is not None else 'без числа'))
                    await app.director_reply(client,event, '\n'.join(lines))
            elif message in {'/лиды', '/клиенты'}:
                leads = app.RADAR.candidates()
                if not leads:
                    await app.director_reply(client,event,'Пока нет проверенных родителей с согласием на приглашения. Команда /воронка покажет источники.')
                else:
                    lines=['👨‍👩‍👧‍👦 Семьи для дальнейшей работы:']
                    for rec in leads[:15]:
                        first = (rec.get('first_name') or 'Родитель')[:40]
                        lines.append(f'• {first} · возраст: {rec.get("child_ages") or "неизвестен"} · {rec.get("source") or "без источника"} · ID {rec["user_id"]}')
                    lines.append('Доступны лишь семьи с подтверждёнными сведениями и согласием на приглашения.')
                    await app.director_reply(client,event,'\n'.join(lines))
            elif message == '/воронка':
                await app.director_reply(client,event, format_pipeline(source_breakdown(app.RADAR)))
            elif message == '/радар':
                metrics = app.RADAR.summary()
                await app.director_reply(client,event, ('📡 FAMILY RADAR\n'
                    f'Начали регистрацию: {metrics["registered"]}\n'
                    f'Подтверждённые семьи Александрова: {metrics["verified_families"]}\n'
                    f'Разрешили получать приглашения: {metrics["allowed_outreach"]}\n'
                    f'Отписались: {metrics["stopped"]}\n'
                    f'Приглашений сегодня: {metrics["sent_today"]}'))
            elif message == '/показать':
                active = app.load_active()
                draft = load_draft()
                info = 'Активная идея: ' + active['title'] + (' (на паузе)' if active.get('paused', True) else ' (готова)')
                if draft:
                    info += '\n\nЧерновик: ' + draft['title'] + '\n\n' + compose_first_message('Ирина', draft)
                await app.director_reply(client,event, info)
            elif message == '/утвердить':
                active = activate_draft()
                await app.director_reply(client,event, f'✅ Идея «{active["title"]}» активирована. Отправка всё ещё зависит от OUTREACH_ENABLED и DRY_RUN.')
            elif message == '/пауза':
                pause_campaign()
                await app.director_reply(client,event, '⏸ Текущая исходящая кампания поставлена на паузу. Входящие ответы продолжаются.')
            elif message == '/возобновить':
                resume_campaign()
                await app.director_reply(client,event, '▶️ Кампания возобновлена. Исходящие всё равно требуют настроенного разрешённого списка и OUTREACH_ENABLED=true.')
            else:
                await event.reply('Это директорский чат. Для проверки консультации: /диалог_тест. Можно написать «покажи заявки», «ты тут?» или «останови приглашения». Все команды: /помощь.')
        except Exception as exc:
            LOG.exception('Команда владельца не выполнена')
            await app.director_reply(client,event, '⚠️ Не удалось выполнить команду: ' + type(exc).__name__)

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
            async with app.CHAT_LOCKS.setdefault(uid, asyncio.Lock()):
                remembered = remember_opening(app.STORE, app.M, uid, event.raw_text or '',
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
                    app.reset_test(event.sender_id)
                    await event.reply('Новый тестовый диалог: память чистая. Реальные заявки, CRM и уведомления руководителям отключены независимо от рабочего режима. Выход: /конец_теста')
                    return
                if event.raw_text == '/конец_теста':
                    app.end_test(event.sender_id)
                    await event.reply('Тест завершён')
                    return
                if event.sender_id in app.TESTERS and (not (event.raw_text or '').startswith('/') or (event.raw_text or '').split()[0] in {'/менеджер','/бронь','/отмена','/напомнить','/согласие_на_запись','/стоп','/удалить','/расписание'}):
                    await app.handle_dm(client,event)
                else:
                    await admin_commands(event)
                return
            # /мой_id is available before the two IDs are configured; it grants no rights.
            if await app.director_message(client,event,owner_id):
                return
        if safe_test_mode():
            return  # No processing, saving or answering messages from other people.
        try:
            if event.is_private:
                try:
                    async with app.CHAT_LOCKS.setdefault(event.sender_id, asyncio.Lock()):
                        await recover_opening(client, event, app.STORE, app.M, excluded=app.director_admin_ids(owner_id) | {owner_id})
                except Exception:
                    LOG.exception('Не удалось восстановить ручное начало диалога')
                await app.handle_dm(client, event)
            elif groups and event.is_group:
                await app.handle_groups(client, event, groups)
        except FloodWaitError as e:
            LOG.warning('Telegram ограничил частоту: %s секунд. Без обхода ограничений.', e.seconds)
        except Exception:
            LOG.exception('Ошибка обработки сообщения')
    async def reminders_loop():
        while True:
            app.STORE.purge_old_turns(int(os.getenv('HISTORY_DAYS','30')))
            if os.getenv('REMINDERS_ENABLED','false').lower()=='true':
                for b in app.M.due():
                    contact=app.STORE.get(b['uid']) or {}
                    if contact.get('stopped') or safe_test_mode() or app.STORE.outbound_blocked(): continue
                    # Mark before send: ambiguous delivery never triggers duplicate automatic reminders.
                    try:
                        confirmed = await send_once(app.STORE, f"reminder:{b['id']}", b['uid'],
                            lambda: client.send_message(b['uid'],f"Напоминаем: {b['course']}, {b['starts']} (UTC). Бронь #{b['id']}. Отмена: /отмена {b['id']}",parse_mode=None))
                        if confirmed:
                            with app.M.db: app.M.db.execute('UPDATE bookings SET sent=1 WHERE id=?',(b['id'],))
                        else:
                            await app.notice_owner(client, f"Напоминание #{b['id']} заблокировано; см. outbound_operations.")
                    except Exception:
                        await app.notice_owner(client,f"Не удалось доставить напоминание по брони #{b['id']}. Проверьте вручную.")
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


