"""Campaign briefs and staged owner approval for Anastasia Vladimirovna."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
ACTIVE = DATA / 'active_campaign.json'
DRAFT = DATA / 'draft_campaign.json'

DEFAULT = {
    'title': 'Интерактив ко Дню матери',
    'message_body': ('Ко Дню матери мы готовим бесплатный интерактив для детей: ребята смогут '
                     'создать открытку с помощью искусственного интеллекта и записать голосовое '
                     'поздравление, которое мама откроет по QR-коду. 🎁'),
    'question': 'Подскажите, вам было бы это актуально?',
    'paused': True,
    'organizer': 'Soft Skills',
}

CAMPAIGN_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'message_body': {'type': 'string'},
        'question': {'type': 'string'},
    },
    'required': ['title', 'message_body', 'question'],
    'additionalProperties': False,
}


def clean_campaign(data: dict, *, paused: bool = True) -> dict:
    result = {}
    for name, max_len in [('title', 100), ('message_body', 700), ('question', 160)]:
        value = str(data.get(name, '')).strip()[:max_len]
        if not value:
            raise ValueError('Не хватает поля ' + name)
        result[name] = value
    result['organizer'] = 'Soft Skills'
    result['paused'] = bool(paused)
    result['updated_utc'] = datetime.now(timezone.utc).isoformat()
    return result


def load_active() -> dict:
    if ACTIVE.exists():
        return json.loads(ACTIVE.read_text(encoding='utf-8'))
    return dict(DEFAULT)


def load_draft() -> dict | None:
    return json.loads(DRAFT.read_text(encoding='utf-8')) if DRAFT.exists() else None


def _save(path: Path, data: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    # Write temporary file and replace atomically.
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def save_draft(data: dict):
    _save(DRAFT, clean_campaign(data))


def activate_draft() -> dict:
    draft = load_draft()
    if not draft:
        raise ValueError('Нет подготовленной идеи. Сначала отправьте /идея ...')
    current = clean_campaign(draft, paused=False)
    _save(ACTIVE, current)
    return current


def pause_campaign() -> dict:
    current = load_active()
    current['paused'] = True
    _save(ACTIVE, current)
    return current


def resume_campaign() -> dict:
    current = load_active()
    current['paused'] = False
    _save(ACTIVE, current)
    return current


def compose_first_message(first_name: str = '', campaign: dict | None = None, agent_name: str | None = None):
    campaign = campaign or load_active()
    agent_name = agent_name or os.getenv('AGENT_NAME', 'Анастасия Владимировна')
    name = first_name.strip()[:40]
    # Names only from independently provided, appropriate adult-contact records.
    greeting = f'Здравствуйте, {name}! 🌷' if name else 'Здравствуйте! 🌷'
    return (f'{greeting}\n\nНа связи {agent_name}, менеджер образовательных проектов. '
            f'Организатор — {campaign.get("organizer", "Soft Skills")}.\n\n'
            f'{campaign["message_body"].strip()}\n\n{campaign["question"].strip()}\n\n'
            'Если неактуально, напишите «неинтересно», и мы больше не будем обращаться.')


async def draft_from_owner_idea(brief: str) -> dict:
    brief = brief.strip()
    if not 15 <= len(brief) <= 2400:
        raise ValueError('Описание идеи должно содержать от 15 до 2400 символов.')
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=30)
    resp = await client.responses.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-5-mini'),
        instructions=(
            'Ты редактор приглашений детских образовательных мероприятий Soft Skills. '
            'Из инструкции владельца составь лаконичное, доброжелательное приглашение для родителей. '
            'Без утверждений о детях адресата, без выдуманных дат, цен, обещаний или свободных мест. '
            'Если мероприятие лишь планируется, используй "планируем", не утверждай, что оно уже запущено. '
            'Не спрашивай разрешение прислать рекламу: контакты для исходящих выбираются отдельной программой '
            'только среди тех, кто заранее разрешил связь. Заканчивай вопросом об актуальности. '
            'Не называй менеджера человеком или виртуальным. Не скрывай организатора Soft Skills. '
            'Верни только JSON по схеме. Тело 1–3 коротких предложения.'
        ),
        input=brief,
        max_output_tokens=400,
        text={'format': {'type': 'json_schema', 'name': 'outreach_campaign', 'strict': True,
                         'schema': CAMPAIGN_SCHEMA}},
    )
    parsed = json.loads(resp.output_text.strip())
    campaign = clean_campaign(parsed)
    save_draft(campaign)
    return campaign
