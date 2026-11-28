"""Anastasia Vladimirovna: flexible AI manager for approved adult contacts."""
from __future__ import annotations
import json
import os
from openai import AsyncOpenAI
from director_orders import public_context
from manager import knowledge

from prompt_builder import build_prompt
from trial_offer import offer_context
SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "city": {"type": ["string", "null"], "enum": ["Александров", "Ярославль", None]},
        "course": {"type": ["string", "null"]},
        "child_age": {"type": ["integer", "null"]},
        "parent_name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "wanted_time": {"type": ["string", "null"]},
        "wants_trial": {"type": "boolean"},
        "handoff": {"type": "boolean"}
    },
    "required": ["reply", "city", "course", "child_age", "parent_name", "phone", "wanted_time", "wants_trial", "handoff"],
    "additionalProperties": False
}

SCHEMA['properties'].update({
    'consultation_notes': {'type': ['string', 'null']},
    'next_action': {'type': 'string', 'enum': ['answer','clarify','recording','pause','handoff']},
    'trial_decision': {'type': ['string','null'], 'enum': ['yes','no',None]},
})
SCHEMA['required'] += ['consultation_notes','next_action','trial_decision']


async def respond(history: list[dict], campaign: dict | None = None, learning_note: str = "", manager=None, client_context=None, settings=None, configuration=None):
    if manager is None:
        raise ValueError('respond requires an explicit manager')
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    if configuration is None:
        from agent_config import defaults
        configuration = {'values': defaults(), 'prompts': []}
    model = configuration['values']['model']
    # Use only the owner-approved active offer; do not invent event details.
    campaign = campaign or {}
    instructions, inputs, state = build_prompt(
        history, facts=knowledge(), orders=public_context(), slots=manager.slots(),
        offer=offer_context(), campaign=campaign, client_context=client_context,
        learning_note=learning_note, settings=settings, configuration=configuration)
    language = configuration['values'].get('response_language', 'ru')
    instructions += f"\nWrite the entire user-facing reply in {language}, regardless of the input language. Answer the question directly. Never ask for permission to use AI."
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30)
    try:
        manager.llm_request(model)
        response = await client.responses.create(
            model=model,
            instructions=instructions,
            input=inputs,
            max_output_tokens=4000,
            **({"reasoning": {"effort": "low"}} if model.startswith("gpt-5") else {}),
            text={"format": {"type": "json_schema", "name": "softskills_consultation", "strict": True, "schema": SCHEMA}},
        )
    finally:
        await client.close()
    usage = response.usage
    if usage:
        manager.usage(model,usage.input_tokens,usage.output_tokens)
    status = getattr(response, "status", None)
    if status != "completed":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", "unknown")
        raise ValueError(f"AI response not completed: status={status}, reason={reason}")
    raw = (response.output_text or "").strip()
    if not raw:
        raise ValueError("AI returned no text (empty output or refusal)")
    if raw.startswith('```'):
        raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("reply"):
        raise ValueError("AI не вернул корректный ответ")
    validate_response(data)
    return data


def validate_response(data):
    if set(data) != set(SCHEMA['required']):
        raise ValueError('Invalid model fields')
    types = {'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    for name, spec in SCHEMA['properties'].items():
        kinds = spec['type'] if isinstance(spec['type'], list) else [spec['type']]
        value = data[name]
        if type(value) not in [types[k] for k in kinds]:
            raise ValueError('Invalid model field type: ' + name)
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError('Invalid model enum: ' + name)
    if not data['reply'].strip() or len(data['reply']) > 3500:
        raise ValueError('Invalid reply length')
