"""Explicit handoff intent and truthful delivery acknowledgement."""
import re


def requests_handoff(text, history):
    low = text.lower().strip(' .!?')
    if low in {'/менеджер', 'позовите менеджера', 'хочу поговорить с человеком'}:
        return True
    if low not in {'да', 'да пожалуйста', 'да, пожалуйста', 'свяжите', 'подключите', 'хорошо', 'давайте'}:
        return False
    previous = next((m['content'] for m in reversed(history) if m['role'] == 'assistant'), '')
    return bool(re.search(r'(?:связать|связала|подключить|подключила|передать|передала)[^?]{0,140}менеджер[^?]*\?', previous.lower()))


async def transfer_to_managers(client, notify, manager, uid, contact, text, history, testing=False, store=None):
    if testing:
        return 'Тест: здесь я передала бы вопрос менеджеру. Уведомления отключены, можем продолжать проверку.'
    username = (contact.get('username') or '').lstrip('@')
    link = 'https://t.me/' + username if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username) else f'Telegram ID: {uid}'
    context = '\n'.join(f"{m['role']}: {m['content']}" for m in history[-6:])[-2200:]
    question = original_question(text, history)
    if store is not None:
        question = store.dialogue(uid).get('original_question') or question
        store.set_dialogue(uid, handoff_status='pending', original_question=question)
    facts = '\n'.join(f'{key}: {contact[key]}' for key in ('name','city','course','child_age','wanted_time') if contact.get(key))
    message = f'Нужен менеджер\n{link}\nID: {uid}\nПричина: запрос консультации сотрудника\nВопрос: {question[:1000]}\nИзвестно:\n{facts}\nКонтекст:\n{context}\nВернуть помощника: /вернуть_ии {uid}'
    crm_status = 'карточка #' + str(contact['crm_id']) if contact.get('crm_id') else 'подтверждённого ID карточки нет'
    message += f'\nОбращение: TG-{uid}\nCRM: {crm_status}'
    try:
        count = await notify(client, message)
    except Exception:
        count = 0
    if count:
        manager.handoff(uid)
        if store is not None:
            store.set_dialogue(uid, stage='manager_handoff', handoff_status='delivered')
            from delivery import Ledger
            ledger=Ledger(store)
            with store.db:
                ledger.event('conversation_handed_off',str(uid),status='delivered',source='recipient_request')
        return 'Передала вопрос менеджеру вместе с нашей перепиской. Он сможет продолжить здесь. Время ответа пока не подтверждено.'
    if store is not None:
        store.set_dialogue(uid, handoff_status='failed')
    return 'Не удалось доставить уведомление менеджеру. Запрос пока не передан. Можно попробовать ещё раз: «позовите менеджера». Я остаюсь на связи.'


def original_question(text, history):
    for item in reversed(history):
        value = item.get('content', '')
        if item.get('role') == 'user' and ('?' in value or re.search(r'сколько|могут|можно ли|стоимость', value, re.I)):
            return value
    return next((m['content'] for m in reversed(history) if m.get('role') == 'user' and len(m.get('content', '')) > 15), text)
