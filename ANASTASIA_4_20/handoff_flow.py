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


async def transfer_to_managers(client, notify, manager, uid, contact, text, history, testing=False):
    if testing:
        return 'Тест: здесь я передала бы вопрос менеджеру. Уведомления отключены, можем продолжать проверку.'
    username = contact.get('username') or ''
    link = 'https://t.me/' + username if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username) else f'Telegram ID: {uid}'
    context = '\n'.join(f"{m['role']}: {m['content']}" for m in history[-6:])[-2200:]
    message = f'Нужен менеджер\n{link}\nID: {uid}\nВопрос: {text[:600]}\nКонтекст:\n{context}\nВернуть помощника: /вернуть_ии {uid}'
    try:
        count = await notify(client, message)
    except Exception:
        count = 0
    if count:
        manager.handoff(uid)
        return 'Передала вопрос менеджеру вместе с нашей перепиской. Он сможет продолжить здесь. Время ответа пока не подтверждено.'
    return 'Не удалось доставить уведомление менеджеру. Запрос пока не передан. Можно попробовать ещё раз: «позовите менеджера». Я остаюсь на связи.'
