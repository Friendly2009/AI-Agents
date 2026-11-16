"""Deterministic dialogue state; a general yes never grants recording consent."""
RECORDING_QUESTION = 'Можно использовать ваши имя и телефон для оформления заявки в Soft Skills и передать их в систему записи школы? Ответьте «да» или «нет».'

def recording_decision(history, text):
    if not history or history[-1].get('role') != 'assistant' or history[-1].get('content') != RECORDING_QUESTION:
        return None
    answer = text.strip().lower().rstrip('.! ')
    if answer in {'да', 'согласен', 'согласна', 'разрешаю', 'да, согласен', 'да, согласна'}:
        return True
    if answer in {'нет', 'не согласен', 'не согласна', 'не передавать'}:
        return False
    return None

def ready_for_recording(contact):
    return bool(contact.get('wants_trial') and all(contact.get(k) for k in ('name','phone','city','course')))

def known_context(contact):
    return {k: contact.get(k) for k in ('name','phone','city','course','child_age','wanted_time','wants_trial','crm_consent','crm_id','consultation_notes','speech_preferences')}

def recording_refused(history):
    for index in range(len(history)-1, 0, -1):
        if history[index].get('role') == 'user':
            decision = recording_decision(history[:index], history[index]['content'])
            if decision is not None:
                return decision is False
    return False


def should_ask_recording(before, after, parsed, history):
    # Never replace a substantive answer with the consent prompt.
    return (ready_for_recording(after) and not after.get('crm_consent')
            and parsed.get('next_action') == 'recording'
            and not parsed.get('handoff') and not recording_refused(history))
