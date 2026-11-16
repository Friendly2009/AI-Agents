"""Evidence-based dialogue reducer. No model output can grant an action or a fact."""
import re
from copy import deepcopy
from datetime import datetime, timezone, timedelta

STAGES = {'initial_contact', 'engaged', 'discovery', 'consultation',
          'objection_handling', 'trial_interest', 'trial_request', 'details_pending',
          'consent_pending', 'lead_submitted', 'manager_handoff', 'visit_confirmed',
          'attended', 'declined', 'paused'}
YES = {'да', 'хорошо', 'можно', 'давайте', 'да пожалуйста', 'да, пожалуйста', 'ок'}
CORRECTION = re.compile(r'ошиб|исправ|на самом деле|теперь|переехали|вернее|не .{1,30},? а ', re.I)


def initial_state():
    return dict(stage='initial_contact', topic=None, last_question=None,
                question_kind=None, intent=None, expected_action=None,
                handoff_status='none', original_question=None, paused=False,
                declined=False, request_active=False, children={}, active_child=None,
                family_ambiguous=False, conflicts={}, evidence={}, interests=[], concerns=[],
                application_status='none', updated_at=None)


def question_kind(reply):
    if '?' not in reply:
        return None
    from conversation_logic import RECORDING_QUESTION
    if reply == RECORDING_QUESTION:
        return 'consent'
    if re.search(r'(?:переда|связ|подключ).{0,140}менеджер', reply, re.I):
        return 'handoff'
    if re.search(r'рассказать|объяснить|узнать подробнее', reply, re.I):
        return 'explain'
    if re.search(r'оформить.{0,30}заявк|хотите.{0,25}записаться|записать вас', reply, re.I):
        return 'request'
    if re.search(r'хотите.{0,30}(?:прийти|попробовать)', reply, re.I):
        return 'interest'
    for key, pattern in [('child_age', r'сколько.{0,15}лет|возраст'),
                         ('city', r'како[мй].{0,10}город|откуда'),
                         ('name', r'как.{0,10}(?:зовут|обращаться)'),
                         ('phone', r'телефон'), ('course', r'направлен|курс'),
                         ('wanted_time', r'когда|время|день')]:
        if re.search(pattern, reply, re.I):
            return key
    return 'other'


def after_reply(state, reply):
    state = deepcopy(state)
    kind = question_kind(reply)
    state.update(last_question=reply if kind else None, question_kind=kind,
                 expected_action=kind, updated_at=datetime.now(timezone.utc).isoformat())
    if kind == 'consent':
        state['stage'] = 'consent_pending'
    return state


def question_expired(state, now=None):
    stamp = state.get('updated_at')
    return bool(stamp and (now or datetime.now(timezone.utc)) - datetime.fromisoformat(stamp) > timedelta(days=7))


def _put(updates, state, contact, key, value, text):
    old = contact.get(key)
    if old and old != value and not CORRECTION.search(text):
        state['conflicts'][key] = value
        return
    updates[key] = value
    state['conflicts'].pop(key, None)
    state['evidence'][key] = text[:400]


def extract_facts(text, contact, state):
    """Conservative Russian grammar; uncertain values remain pending, never guessed."""
    updates = {}
    if re.search(r'\b(?:например|предположим|возможно|наверное|если|цитата)\b', text, re.I):
        return updates
    kind = state.get('question_kind')
    low = text.lower()
    multi = bool(re.search(r'двое|два ребён|два ребен|двое детей|второй|вторая', low))
    labels = []
    for label, pattern in [('son', r'сын(?:у|а)?'), ('daughter', r'доч(?:ь|ке|ери|ка)'),
                           ('older', r'старш(?:ему|ей|ий|ая)'), ('younger', r'младш(?:ему|ей|ий|ая)')]:
        matches = list(re.finditer(r'\b' + pattern + r'\s*(?:уже\s*)?(\d{1,2})(?:\s*(?:лет|год))?', low))
        for match in matches:
            age = int(match[1])
            if not 0 < age < 19:
                continue
            child_label = label
            if re.search(r'втор(?:ому|ой|ая|ую)\s*$', low[max(0,match.start()-12):match.start()]):
                child_label = label + '_2'
            if child_label in labels:
                child_label = label + '_2'
            labels.append(child_label)
            child = state['children'].setdefault(child_label, {})
            if child.get('age') and child['age'] != age and not CORRECTION.search(text):
                state['conflicts']['child_age'] = age
            else:
                child.update(age=age, evidence=match[0])
    if multi or (len(state['children']) > 1 and (not state.get('active_child') or any(label != state['active_child'] for label in labels))):
        state['family_ambiguous'] = True
        state['active_child'] = None
        updates['child_age'] = None
        state['interests'] = []
    selection = re.search(r'(?:записать|запишите|занятие для|подбираем для)\s+(сына|дочери|дочку|старшего|младшего)', low)
    if selection:
        chosen = {'сына':'son', 'дочери':'daughter', 'дочку':'daughter', 'старшего':'older', 'младшего':'younger'}[selection[1]]
        if chosen in state['children']:
            state['active_child'] = chosen
            state['family_ambiguous'] = False
            updates['child_age'] = state['children'][chosen].get('age')
            state['interests'] = list(state['children'][chosen].get('interests', []))
    elif len(state['children']) == 1 and not state['family_ambiguous']:
        state['active_child'] = next(iter(state['children']))
        if labels and 'child_age' not in state['conflicts']:
            _put(updates, state, contact, 'child_age', state['children'][state['active_child']]['age'], text)
    age = re.search(r'(?:реб[её]нку\s+)(\d{1,2})\s*(?:лет|год)', low)
    if not age and (kind == 'child_age' or ('child_age' in state['conflicts'] and CORRECTION.search(text))):
        age = re.fullmatch(r'\s*(?:исправление[: ]*)?(\d{1,2})(?:\s*(?:лет|года?|ему|ей))?[.! ]*', low)
    if age and not state['family_ambiguous'] and 0 < int(age[1]) < 19:
        _put(updates, state, contact, 'child_age', int(age[1]), text)
        if updates.get('child_age') == int(age[1]) and state.get('active_child'):
            state['children'][state['active_child']].update(age=int(age[1]), evidence=text[:400])
    city = re.search(r'(?:мы\s+из|жив[её]м\s+в|переехали\s+в|город\s*[:—-]?)\s*(Александров[ае]?|Ярославл[ьея])\b', text, re.I)
    if not city and (kind == 'city' or ('city' in state['conflicts'] and CORRECTION.search(text))):
        city = re.fullmatch(r'\s*(?:исправление[: ]*)?(Александров[ае]?|Ярославл[ьея])[.! ]*', text, re.I)
    if city:
        _put(updates, state, contact, 'city', 'Александров' if city[1].lower().startswith('алекс') else 'Ярославль', text)
    name = re.search(r'меня зовут\s+([А-ЯЁа-яё-]{2,40})', text, re.I)
    if not name and kind == 'name':
        name = re.fullmatch(r'\s*([А-ЯЁ][а-яё-]{1,39})[.! ]*', text)
    if name:
        _put(updates, state, contact, 'name', name[1].capitalize(), text)
    phone = re.search(r'(?<!\d)([+78][\d ()-]{9,22}\d)(?!\d)', text)
    if phone:
        digits = re.sub(r'\D', '', phone[1])
        if len(digits) == 11 and digits[0] in '78':
            _put(updates, state, contact, 'phone', '+7' + digits[1:], text)
    course = re.search(r'(?:интересует|хотим изучать|курс|направление)\s*[:—-]?\s*(программирование|робототехника|искусственный интеллект|ИИ|дизайн|Python|Scratch)\b', text, re.I)
    if not course and kind == 'course':
        course = re.fullmatch(r'\s*(программирование|робототехника|искусственный интеллект|ИИ|дизайн|Python|Scratch)[.! ]*', text, re.I)
    if course:
        _put(updates, state, contact, 'course', course[1], text)
    time = re.search(r'(?:удобно|подходит|можем)\s+((?:по\s+)?(?:вечерам|выходным|субботам|воскресеньям)|после\s+\d{1,2}(?::\d{2})?)', text, re.I)
    if time:
        _put(updates, state, contact, 'wanted_time', time[1], text)
    if re.search(r'любит|интересуется|увлекается', low):
        for interest, pattern in [('игры', r'игр'), ('рисование', r'рисов'), ('роботы', r'робот')]:
            if re.search(pattern, low) and interest not in state['interests'] and not state['family_ambiguous']:
                state['interests'].append(interest)
                child = state['children'].get(state.get('active_child'))
                if child is not None:
                    child.setdefault('interests', [])
                    if interest not in child['interests']:
                        child['interests'].append(interest)
    return updates


def reduce_user(state, text, contact):
    from policy import is_stop
    from sales_context import current_need
    state = deepcopy(state or initial_state())
    if question_expired(state):
        state.update(question_kind=None, last_question=None, expected_action=None)
    low = text.lower().strip(' .!?')
    previous_kind = state.get('question_kind')
    updates = extract_facts(text, contact, state)
    question = '?' in text or bool(re.search(r'сколько|стоим|цен[аыу]|можно ли|могут ли|а родители', low))
    need = current_need(text)
    intent = 'information' if question else 'engage'
    if is_stop(text) or low in {'не хочу записываться', 'отказываюсь', 'нет спасибо', 'нет, спасибо'}:
        intent = 'decline'
    elif need == 'pause' or low in {'позже', 'потом', 'я подумаю', 'подумаю'}:
        intent = 'pause'
    elif low in YES:
        intent = {'request':'request', 'interest':'interest', 'explain':'explain',
                  'handoff':'handoff', 'consent':'consent'}.get(previous_kind, 'clarify')
    elif low == 'нет':
        intent = 'decline_request' if previous_kind in {'request','interest'} else 'negative'
    elif not question and re.search(r'\b(?:запишите|хочу записаться|хотим записаться|оформите заявку|давайте запишемся)\b', low):
        intent = 'request'
    elif need in {'concern', 'repair'} or re.search(r'не любит программ|только игр|нет опыта|не подходит время|другие курсы|не доверя', low):
        intent = 'concern'
    elif not question and re.search(r'интересно|интересует пробное|хочу попробовать', low):
        intent = 'interest'
    if question:
        state['original_question'] = text[:1000]
    state['intent'] = intent
    if intent == 'concern' and text[:400] not in state['concerns']:
        state['concerns'] = (state['concerns'] + [text[:400]])[-10:]
    state['topic'] = 'price' if re.search(r'стоим|цен[аыу]|сколько стоит', low) else ('trial' if intent in {'request','interest'} else state.get('topic'))
    if intent in {'decline', 'decline_request', 'pause'}:
        state.update(stage='paused' if intent == 'pause' else 'declined',
                     paused=intent == 'pause', declined=intent != 'pause', request_active=False,
                     expected_action=None)
        updates.update(wants_trial=0, submission_pending=0, crm_consent=0)
    else:
        if intent not in {'clarify','negative'}:
            state.update(paused=False, declined=False)
        stages = {'information':'consultation', 'explain':'consultation', 'concern':'objection_handling',
                  'interest':'trial_interest', 'request':'trial_request', 'engage':'engaged'}
        state['stage'] = stages.get(intent, state['stage'])
        if updates and intent == 'engage':
            state['stage'] = 'discovery'
        if intent == 'request':
            state['request_active'] = True
            updates['wants_trial'] = 1
            if not contact.get('submission_pending'):
                updates['crm_consent'] = 0
        if state['request_active'] and intent in {'request','engage'}:
            merged = dict(contact, **updates)
            missing = [k for k in ('name','phone','city','course') if not merged.get(k)]
            if missing or state['family_ambiguous'] or state['conflicts']:
                state.update(stage='details_pending', expected_action='clarify_child' if state['family_ambiguous'] else (missing[0] if missing else 'resolve_conflict'))
    # A question is consumed by one reply. Stale yes after a pause cannot authorize anything.
    state.update(last_question=None, question_kind=None, updated_at=datetime.now(timezone.utc).isoformat())
    return state, updates


def guard_model(parsed, state):
    result = dict(parsed)
    active = state.get('request_active') and not state.get('paused') and not state.get('declined')
    result['wants_trial'] = bool(active)
    result['trial_decision'] = 'yes' if state.get('intent') == 'request' else ('no' if state.get('intent') in {'pause','decline','decline_request'} else None)
    if not active or state.get('intent') in {'information','explain','concern','clarify','negative','interest'} or state.get('family_ambiguous') or state.get('conflicts'):
        if result.get('next_action') == 'recording':
            result['next_action'] = 'answer'
    return result


def safe_reply(reply, state):
    if state.get('intent') == 'pause':
        if re.fullmatch(r'(?:Хорошо|Поняла|Спасибо)[.! ]*', reply, re.I):
            return reply
        return 'Хорошо, возьмите время подумать. Если захотите продолжить, напишите.'
    if state.get('intent') in {'decline','decline_request'}:
        return 'Хорошо, оформление и приглашения остановлены.'
    if state.get('intent') == 'clarify':
        return 'Уточните, пожалуйста, к чему относится ваш ответ?'
    if state.get('family_ambiguous') and state.get('request_active'):
        return 'Для какого ребёнка подбираем занятие? Данные детей сохранены отдельно.'
    if state.get('conflicts') and state.get('intent') not in {'information','concern','explain'}:
        labels = {'city':'город', 'child_age':'возраст ребёнка', 'name':'имя', 'phone':'телефон', 'course':'направление', 'wanted_time':'удобное время'}
        field = next(iter(state['conflicts']))
        return f"Уточните, пожалуйста, {labels.get(field, field)}. Это исправление ранее указанных сведений? Напишите «исправление» и верное значение."
    if state.get('stage') == 'details_pending' and state.get('intent') in {'request','engage'}:
        questions = {'name':'Как к вам обращаться?', 'phone':'Какой телефон указать для заявки?',
                     'city':'В каком городе подбираем занятие?', 'course':'Какое направление вас интересует?'}
        if state.get('expected_action') in questions:
            return questions[state['expected_action']]
    if re.search(r'записала вас|вы записаны|заявка (?:создана|отправлена)|бронь подтверждена|передала.{0,30}менеджеру', reply, re.I):
        return 'Подтверждения записи пока нет. Дату и время должен подтвердить сотрудник школы.'
    return reply
