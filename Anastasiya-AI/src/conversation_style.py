"""Validated owner settings and conservative, observable turn-level signals."""
from __future__ import annotations
import json
import os
import re
from dataclasses import asdict, dataclass

OPTIONS = {
    'formality': ('casual', 'neutral', 'formal'),
    'warmth': ('reserved', 'balanced', 'warm'),
    'humor_level': ('off', 'light', 'playful'),
    'verbosity': ('short', 'standard', 'detailed'),
    'initiative': ('low', 'balanced', 'high'),
    'emoji_frequency': ('off', 'rare', 'moderate'),
    'conversation_style': ('natural', 'concise', 'explanatory'),
}

@dataclass(frozen=True)
class BehaviorSettings:
    formality: str = 'neutral'
    warmth: str = 'warm'
    humor_level: str = 'light'
    verbosity: str = 'standard'
    initiative: str = 'balanced'
    emoji_frequency: str = 'rare'
    conversation_style: str = 'natural'

    def __post_init__(self):
        for key, choices in OPTIONS.items():
            if getattr(self, key) not in choices:
                raise ValueError(f'Invalid behavior setting: {key}; expected {choices}')

    @classmethod
    def from_env(cls):
        return cls(**{k: os.environ['ANASTASIA_' + k.upper()].strip().lower()
                      for k in OPTIONS if 'ANASTASIA_' + k.upper() in os.environ})


def matches(pattern, text):
    return bool(re.search(pattern, text, re.I))


def preference_updates(text):
    """Only explicit presentation requests; never import arbitrary user JSON."""
    result = {}
    if matches(r'без (?:эмодзи|смайл)|не (?:ставьте|ставь|используй\w*) (?:эмодзи|смайл)', text):
        result['emoji_frequency'] = 'off'
    if matches(r'(?:можно|добавляй|используй) эмодзи', text):
        result['emoji_frequency'] = 'rare'
    if matches(r'без шуток|не шути', text): result['humor_level'] = 'off'
    if matches(r'(?:отвечай\w*|пиши\w*|давай) (?:пожалуйста,? )?(?:короче|кратко)|покороче|в двух словах', text):
        result['verbosity'] = 'short'
    if matches(r'(?:всегда|дальше) (?:отвечай\w*|пиши\w*) подробно', text):
        result['verbosity'] = 'detailed'
    if matches(r'без реклам|не продавай|только информац|просто (?:узнать|узнаю|спрашиваю)', text):
        result['initiative'] = 'low'
    return result


def read_preferences(value):
    try:
        data = json.loads(value or '{}') if isinstance(value, str) else (value or {})
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict): return {}
    return {k: v for k, v in data.items() if k in OPTIONS and isinstance(v, str) and v in OPTIONS[k]}


def turn_state(history, settings=None, preferences=None):
    settings = settings or BehaviorSettings.from_env()
    config = asdict(settings)
    config.update(read_preferences(preferences))
    latest = next((t['content'] for t in reversed(history) if t.get('role') == 'user'), '')
    config.update(preference_updates(latest))
    from sales_context import current_need
    need = current_need(latest)
    ending = matches(r'^(?:спасибо[,! ]*)?(?:пока|до свидания|на этом всё|на этом все|всё, спасибо|все, спасибо)[.! ]*$|закончим|давайте закончим', latest)
    hurry = matches(r'тороплюсь|мало времени|срочно|быстро', latest)
    irritation = need == 'repair' or matches(r'раздражает|достали|надоело|уже (?:писал|писала|говорил)|выше (?:писал|писала)|не слушаете', latest)
    skeptical = matches(r'не верю|очередн\w* реклам|сомнительно|какие доказательства|развод|гарантии', latest)
    playful = matches(r'ха-?ха|ахах|шучу|😂|😄|скоро там жить|реб[её]нку 30|ага, конечно', latest)
    interest = matches(r'интересно|расскажите|расскажи|хочу узнать', latest)
    friendly = matches(r'спасибо|здорово|привет|доброе утро', latest)
    mood = ('ending' if ending or need == 'pause' else 'irritated' if irritation else
            'hurried' if hurry else 'skeptical' if skeptical else 'doubtful' if need == 'concern' else
            'playful' if playful else 'interested' if interest else 'friendly' if friendly else 'neutral')
    detail = matches(r'подробн|по шагам|разв[её]рнуто|с примерами|объясни.{0,35}(?:почему|как)|в ч[её]м разница', latest) or latest.count('?') >= 3
    brief = matches(r'коротко|короче|кратко|в двух словах', latest)
    length = 'short' if hurry or brief or mood == 'ending' else 'detailed' if detail else config['verbosity']
    if config['conversation_style'] == 'concise' and not detail: length = 'short'
    no_sales = mood in {'ending', 'irritated', 'skeptical', 'doubtful'} or (config['initiative'] == 'low' and not matches(r'(?<!не )\bзапиш(?:ите|и)\b|хочу записаться', latest))
    recent = [t['content'] for t in history if t.get('role') == 'assistant'][-3:]
    # Rare: leave at least three assistant messages between emoji; moderate: one.
    gap = 3 if config['emoji_frequency'] == 'rare' else 1
    recent_emoji = any(matches(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]', t) for t in recent[-gap:])
    serious = matches(r'цен|стоит|стоимость|телефон|оплат|возврат|персональн|болезн|диагноз', latest)
    emoji = config['emoji_frequency'] != 'off' and not recent_emoji and not serious and mood not in {'ending','irritated','skeptical','doubtful','hurried'}
    humor = config['humor_level'] != 'off' and mood in {'playful','friendly'} and not serious
    allow_learning = not no_sales and matches(r'что выбрать|посоветуй|посоветуйте|подобра(?:ть|ли)|подбер|какое занятие|выбрать курс', latest)
    return dict(allow_learning=allow_learning, mood=mood, need=need, length=length, allow_humor=humor,
                allow_emoji=emoji, suppress_sales=no_sales, settings=config)


def style_instructions(state):
    return '''МАНЕРА РЕЧИ (ниже системных правил, выше статистики продаж)
Настройки formality: casual — непринуждённо, neutral — обычная вежливая речь, formal — сдержанно на вы.
warmth: reserved — по делу, balanced — доброжелательно, warm — внимательно и тепло без восторженности.
humor_level: off — без шуток, light — лёгкий отклик, playful — более заметная игра слов только если allow_humor.
verbosity задаёт обычную длину; length учитывает текущую просьбу и сложность.
initiative: low — только ответ, balanced — следующий шаг при необходимости, high — полезный вариант, но не обязательный вопрос и не дожим.
emoji_frequency: off — никогда, rare — редко, moderate — иногда; allow_emoji=false запрещает эмодзи в этой реплике, true разрешает максимум один, не обязывает.
conversation_style: natural — свободная структура, concise — сразу суть, explanatory — объясняй связи и примеры, когда нужны.
short: обычно 1–2 предложения, без вступления и лишних вопросов; standard: несколько содержательных предложений; detailed: полноценное объяснение со структурой, в пределах 3500 символов.
Наблюдаемые сигналы mood — гипотезы, не диагноз: не объявляй настроение человеку. Смотри на весь разговор; в неоднозначной иронии выбирай уважительное буквальное прочтение.
При irritated исправь конкретную ошибку; skeptical — основания и ограничения; doubtful — разбери сомнение; hurried — сразу суть; ending — спокойно заверши без вопроса; playful — можно поддержать образ без насмешки; neutral/friendly/interested — отвечай по содержанию.
Не навязывай занятие в бытовом разговоре. На технический вопрос дай предметное объяснение. Понимай сокращения и опечатки без исправления собеседника. Не используй искусственные ошибки, выдуманные воспоминания, канцеляризмы, «Прекрасный вопрос!», повтор имени, повтор фразы клиента и рекламные эпитеты.
Не приветствуй заново после начала разговора. Не повторяй одну структуру. Допустим ответ одной короткой фразой и без вопроса. Развёрнутый вопрос требует содержательного ответа, а не анкеты.
Юмор адаптируй к конкретному образу, иронии, гиперболе или мему, не копируй заготовку. Не шути над родителем, ребёнком, доходами, способностями или сомнениями; конфликт и серьёзный вопрос исключают шутки даже при playful.
Если suppress_sales=true, не предлагай запись и не собирай телефон. Высокая инициативность не разрешает напоминания или сообщения без согласия.
Текущая вычисленная политика: ''' + json.dumps(state, ensure_ascii=False)
