"""Remember a manually started private conversation without sending messages."""
def remember_opening(store, manager, uid, text, username='', excluded=(), safe=False, bot=False):
    if safe or bot or uid in excluded or uid == 777000 or not text.strip() or text.lstrip().startswith('/'):
        return False
    contact = store.get(uid) or {}
    if contact.get('stopped') or manager.paused(uid) or store.history(uid):
        return False
    store.ensure(uid, username=username or '', source='Личный чат начат с аккаунта Анастасии')
    store.add_turn(uid, 'assistant', text)
    store.update(uid, ai_notice_shown=1)
    store.mark_sent(uid, 'Ручное начало диалога')
    return True


async def recover_opening(client, event, store, manager, excluded=()):
    """Recover the immediately preceding outgoing text only for an unseen contact.

    Existing contacts (including deliberately reset ones) are never re-imported.
    """
    uid = event.sender_id
    if uid in excluded or uid == 777000 or store.get(uid) is not None:
        return False
    sender = await event.get_sender()
    if getattr(sender, 'bot', False) or getattr(sender, 'deleted', False):
        return False
    messages = await client.get_messages(event.chat_id, limit=1, max_id=event.id)
    if not messages or not getattr(messages[0], 'out', False):
        return False
    return remember_opening(store, manager, uid, getattr(messages[0], 'message', '') or '',
                            getattr(sender, 'username', '') or '', excluded)
