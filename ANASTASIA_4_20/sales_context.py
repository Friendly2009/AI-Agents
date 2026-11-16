"""Small dialogue safeguards; no fabricated facts or automatic sales claims."""
import re

PRESSURE = re.compile(r'\b(давите|давишь|навязываете|навязываешь)\b|странно\s+(?:ты\s+)?отвеча|на\s+какое\s+(?:пробное|занятие)', re.I)
PAUSE = re.compile(r'\b(?:я\s+)?подумаю\b|\bне\s+сейчас\b|\bпока\s+не\s+(?:готов|готова|хочу|будем)\b|\bне\s+записывайте\b', re.I)
CONCERN = re.compile(r'\bсомневаюсь\b|\bсомнения\b|\bбоюсь\b|\bдорого\b|\bне\s+уверен[а]?\b|\bне\s+знаю.{0,25}(?:интересно|понравится)|\bне\s+понравится\b', re.I)
INVITE = re.compile(r'хотите.{0,35}(?:прийти|попробовать|записаться)|(?:вам\s+)?подходит.{0,25}(?:время|воскресенье)|отправ.{0,20}заявк', re.I)

def current_need(text):
    if PRESSURE.search(text): return 'repair'
    if PAUSE.search(text): return 'pause'
    if CONCERN.search(text): return 'concern'
    return 'open'

def coaching_context(history):
    latest = next((t.get('content','') for t in reversed(history) if t.get('role')=='user'), '')
    need = current_need(latest)
    guide = {
        'repair': 'Сейчас восстанови доверие: признай конкретную ошибку и объясни суть. Никакой записи и повторного предложения в этом ответе.',
        'pause': 'Родитель откладывает решение. Кратко прими паузу. Не спрашивай телефон, не приглашай снова, не назначай напоминание.',
        'concern': 'Сначала разберись с сомнением. Используй известный интерес, один довод или один нужный вопрос. Не переходи к оформлению в этом ответе.',
        'open': 'Ответь на текущий запрос. При явной готовности помоги оформить, иначе сначала объясни подходящий результат занятия.'
    }[need]
    if re.search(r'зачем|чему.{0,20}науч|чем.{0,25}отлич|какая.{0,15}польз|ии.{0,20}(?:пишет|сделает|заменит)', latest, re.I):
        guide += ' Это содержательный вопрос об обучении: объясни принцип и практический смысл. Разделяй общие знания и подтверждённую программу школы. Не подменяй объяснение приглашением.'
    if re.search(r'(?:второ[йг]|дво[еи]|два ребён|два ребен|двое детей|дочк.{0,20}сын|сын.{0,20}дочк)', latest, re.I):
        guide += ' Возможно, речь о нескольких детях: не смешивай их возраст и интересы. Пока не определено, кому подбираем занятие, не перезаписывай child_age и не оформляй общую заявку как на одного ребёнка.'
    recent = [t.get('content','') for t in history if t.get('role')=='assistant'][-3:]
    if any(INVITE.search(t) for t in recent):
        guide += ' Недавно уже было приглашение: не повторяй его на вопрос или сомнение. При согласии переходи к недостающим данным, при вопросе ответь по существу.'
    return 'Подсказка по текущему ходу (это эвристика, учитывай полный контекст): ' + guide

def guard_sales_action(parsed, text):
    """A concern/deferral must never be converted into recording by model output."""
    result = dict(parsed)
    need = current_need(text)
    if need != 'open' and not result.get('handoff'):
        result['wants_trial'] = False
        result['trial_decision'] = 'no' if need=='pause' else None
        result['next_action'] = 'pause' if need=='pause' else 'answer'
    return result
