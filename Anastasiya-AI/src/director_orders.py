"""Director Orders 4.4: two equal Telegram ID admins, approval, date-aware offers.

No outreach, no raw member data. Default is admin-only SAFE_TEST_MODE.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / 'data' / 'director_orders.json'
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')

ACTION_SCHEMA = {
    'type':'object', 'additionalProperties':False,
    'properties': {
        'name':{'type':'string'},
        'kind':{'type':['string','null']},
        'city':{'type':['string','null']},
        'dates':{'type':['array','null'],'items':{'type':'string'}},
        'price_rub':{'type':['integer','null']},
        'priority':{'type':['integer','null']},
        'priority_until':{'type':['string','null']},
        'notes':{'type':['string','null']},
        'status':{'type':['string','null']},
    },
    'required':['name','kind','city','dates','price_rub','priority','priority_until','notes','status'],
}
ORDER_SCHEMA = {'type':'object', 'additionalProperties':False,
    'properties':{
        'summary':{'type':'string'},
        'actions':{'type':'array','items':ACTION_SCHEMA},
        'questions':{'type':'array','items':{'type':'string'}},
    },
    'required':['summary','actions','questions'],
}


def _when() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _default():
    return {'revision':0,'next_id':1,'pending':{},'offers':{},'audit':[]}


def _read():
    if not STATE_FILE.exists():
        return _default()
    state=json.loads(STATE_FILE.read_text(encoding='utf-8'))
    if not isinstance(state,dict) or 'revision' not in state:
        raise ValueError('Повреждён файл распоряжений, сохраните копию и остановите программу')
    return state


def _write(state):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(STATE_FILE)


def _date(value):
    if not isinstance(value,str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError('Дата должна быть в формате ГГГГ-ММ-ДД')
    date.fromisoformat(value)
    return value


def _normalize_key(name:str,city:str|None):
    return (str(city or 'любой').casefold().strip()+'::'+str(name).casefold().strip())


def _check_action(action:dict):
    if not isinstance(action,dict) or not (2 <= len(str(action.get('name','')).strip())<=100):
        raise ValueError('Название события отсутствует или слишком длинное')
    name=action['name'].strip()
    if not re.search(r'[А-Яа-яA-Za-z]',name):
        raise ValueError('Название события должно содержать буквы')
    city=action.get('city')
    if city is not None and city not in ('Александров','Ярославль','оба города'):
        raise ValueError('Неподдерживаемый город')
    dates=action.get('dates')
    if dates is not None:
        if not isinstance(dates,list) or not (1<=len(dates)<=30):
            raise ValueError('Укажите от 1 до 30 отдельных дат или не меняйте даты')
        if len(dates)!=len(set(dates)):
            raise ValueError('Даты повторяются')
        for d in dates: _date(d)
    price=action.get('price_rub')
    if price is not None and (type(price)!=int or price<0 or price>5_000_000):
        raise ValueError('Цена вне допустимого диапазона')
    priority=action.get('priority')
    if priority is not None and (type(priority)!=int or not 0<=priority<=5):
        raise ValueError('Приоритет должен быть от 0 до 5')
    until=action.get('priority_until')
    if until is not None: _date(until)
    kind=action.get('kind')
    if kind is not None and (not isinstance(kind,str) or len(kind)>60):
        raise ValueError('Некорректный тип события')
    notes=action.get('notes')
    if notes is not None and (not isinstance(notes,str) or len(notes)>1200):
        raise ValueError('Слишком длинное примечание')
    if action.get('status') not in (None,'active','paused','cancelled'):
        raise ValueError('Неподдерживаемый статус')
    return action


def _check_order(order):
    if not isinstance(order,dict): raise ValueError('Пустой ответ модели')
    actions=order.get('actions')
    if not isinstance(actions,list) or not (1<=len(actions)<=6):
        raise ValueError('Извлечено 0 или слишком много действий: нужно уточнить распоряжение')
    if not isinstance(order.get('summary'),str): raise ValueError('Нет описания приказа')
    for a in actions: _check_action(a)
    return order


async def interpret(text:str, *, manager):
    if not os.getenv('OPENAI_API_KEY','').strip():
        raise ValueError('Для свободной речи и текста нужен OPENAI_API_KEY; настройки не изменены')
    today=date.today().isoformat()
    from openai import AsyncOpenAI
    client=AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'),timeout=45)
    M = manager
    M.llm_request(os.getenv('DIRECTOR_MODEL',os.getenv('OPENAI_MODEL','gpt-5-mini')))
    try:
        response=await client.responses.create(
            model=os.getenv('DIRECTOR_MODEL',os.getenv('OPENAI_MODEL','gpt-5-mini')),
            instructions=(
                'Ты извлекаешь распоряжения руководства для Soft Skills. Верни только JSON по схеме. '
                'События: лагеря, поездки, акции, курсы и мероприятия. '
                f'Сегодня {today}. Вход на русском. Указывай только дословно сообщённые даты, цены и ограничения. '
                'Преобразуй однозначные русские календарные даты в ISO YYYY-MM-DD, не добавляй выходные или пропущенные даты. '
                'Если год отсутствует и не следует однозначно из текста, верни вопросы и не выдумывай дату. '
                'При команде "главный приоритет" ставь priority=5, для "второй по важности" ставь priority=4, при "после даты" учитывай указанную дату как priority_until. '
                'Если в приказе несколько событий, верни отдельные действия. '
                'При неполных данных оставляй nullable поля null: это означает НЕ МЕНЯТЬ существующее значение. '
                'Никогда не используй примерные цены и не превращай их в подтверждённые. '
                'Если сказано, что мероприятие отменено, status=cancelled. '
                'Если нужно изменить отдельное поле, используй точно указанное название существующего предложения. '
                'Если инструкция не содержит ни одного конкретного изменения, actions=[] (такая команда будет отклонена). '
                'Для уточнения недостающей цены лагеря укажи вопрос в questions; НЕ придумывай цену.'
            ),
            input=text[:4000],
            max_output_tokens=1300,
            text={'format':{'type':'json_schema','name':'director_orders','strict':True,'schema':ORDER_SCHEMA}},
        )
    finally:
        await client.close()
    if getattr(response,'usage',None):
        M.usage(os.getenv('DIRECTOR_MODEL',os.getenv('OPENAI_MODEL','gpt-5-mini')),response.usage.input_tokens,response.usage.output_tokens)
    if not response.output_text: raise ValueError('GPT не вернул распоряжение')
    return _check_order(json.loads(response.output_text))


def add_pending(order:dict,admin_id:int,source_text:str)->int:
    _check_order(order)
    state=_read()
    identifier=state['next_id']
    state['next_id']+=1
    state['pending'][str(identifier)]={'id':identifier,'admin_id':admin_id,'revision':state['revision'],
          'created_at':_when(),'source':source_text[:500],'order':order}
    _write(state)
    return identifier


def preview(order:dict,identifier:int)->str:
    lines=[f'📝 РАСПОРЯЖЕНИЕ №{identifier} (ЧЕРНОВИК — НЕ АКТИВИРОВАНО)',order.get('summary','')[:250]]
    for a in order['actions']:
        label=f'\n• {a["name"]}'
        if a.get('city'): label+=f' | {a["city"]}'
        if a.get('dates'): label+=' | дни: '+', '.join(a['dates'])
        if a.get('price_rub') is not None: label+=f' | {a["price_rub"]:,} ₽'.replace(',',' ')
        if a.get('priority') is not None: label+=f' | приоритет {a["priority"]}/5'
        if a.get('priority_until'): label+=' до '+a['priority_until']
        if a.get('status'):label+=' | '+a['status']
        if a.get('notes'):label+='\n  '+a['notes'][:220]
        lines.append(label)
    if order.get('questions'):
        lines.append('\n❓ Уточнить: '+ '; '.join(order['questions'])[:400])
    lines.append(f'\n/утвердить_приказ {identifier} — подтвердить любое из двух администраторов'
                 f'\n/отклонить_приказ {identifier} — отменить черновик')
    return '\n'.join(lines)


def _offer_context(offer):
    o=dict(offer)
    until=o.get('priority_until')
    if until and date.fromisoformat(until)<date.today():
        o['priority']=0
    return o


def approve(identifier:int,admin_id:int):
    state=_read()
    pending=state['pending'].get(str(identifier))
    if pending is None: raise ValueError('Черновик отсутствует или уже обработан')
    if pending['revision']!=state['revision']:
        raise ValueError('Данные изменились после создания черновика. Отправьте распоряжение повторно')
    for a in pending['order']['actions']:
        name=a['name'].strip()
        city=a.get('city')
        existing_keys=[k for k,v in state['offers'].items() if v['name'].casefold()==name.casefold()]
        key=_normalize_key(name,city)
        if city is None and len(existing_keys)==1:
            key=existing_keys[0]
        previous=state['offers'].get(key, {'name':name,'city':city,'dates':None,'price_rub':None,'priority':0,
                                              'priority_until':None,'kind':None,'notes':None,'status':'active'})
        merged={**previous}
        for field in ('kind','city','dates','price_rub','priority','priority_until','notes','status'):
            if a.get(field) is not None:
                merged[field]=a[field]
        merged['name']=name
        merged['updated_at']=_when()
        merged['updated_by']=admin_id
        state['offers'][key]=merged
    state['revision']+=1
    state['audit'].append({'action':'approved','id':identifier,'by':admin_id,'at':_when(),
                            'summary':pending['order']['summary'][:200]})
    state['audit']=state['audit'][-500:]
    del state['pending'][str(identifier)]
    _write(state)
    return pending['order']['summary']


def reject(identifier:int,admin_id:int):
    state=_read()
    if str(identifier) not in state['pending']:
        raise ValueError('Черновик отсутствует')
    state['audit'].append({'action':'rejected','id':identifier,'by':admin_id,'at':_when()})
    del state['pending'][str(identifier)]
    _write(state)


def summary()->str:
    state=_read()
    lines=[f'📌 Утверждённые предложения ({len(state["offers"])})']
    for val in sorted(state['offers'].values(),key=lambda x:x.get('priority') or 0,reverse=True):
        o=_offer_context(val)
        if o['status']=='cancelled': continue
        price=f'{o["price_rub"]} ₽' if o.get('price_rub') is not None else 'цена не утверждена'
        dates=', '.join(o.get('dates') or []) or 'даты не утверждены'
        lines.append(f'• {o["name"]} ({o.get("city") or "город не указан"}): {dates}; {price}; приоритет {o.get("priority",0)}/5; {o["status"]}')
    if state['pending']:
        lines.append('Черновики: '+', '.join('#'+x for x in state['pending']))
    return '\n'.join(lines)[:3700]


def public_context()->str:
    """Public AI context: ONLY approved offers, no raw instructions or customer data."""
    state=_read()
    offers=[_offer_context(o) for o in state['offers'].values() if o['status']=='active']
    offers=sorted(offers,key=lambda x:x.get('priority') or 0,reverse=True)
    today=date.today()
    for offer in offers:
        if offer.get('priority_until') and date.fromisoformat(offer['priority_until'])<today:
            offer['priority']=0
    small=[{'name':o['name'],'city':o.get('city'),'kind':o.get('kind'),'dates':o.get('dates'),
            'price_rub':o.get('price_rub'),'price_unknown':o.get('price_rub') is None,
            'priority':o.get('priority'),'notes':o.get('notes')} for o in offers[:30]]
    return json.dumps(small,ensure_ascii=False)


def admin_ids(service_id:int):
    """Only two specifically pinned administrator accounts, never usernames or display names."""
    raw_owner=os.getenv('ADMIN_OWNER_TG_ID','').strip()
    raw_wife=os.getenv('ADMIN_WIFE_TG_ID','').strip()
    if not (raw_owner and raw_wife):
        return set()  # Fail closed until both principals are configured.
    if not (raw_owner.isdigit() and raw_wife.isdigit()):
        raise ValueError('ADMIN_OWNER_TG_ID и ADMIN_WIFE_TG_ID должны быть числовыми Telegram ID')
    result={int(raw_owner),int(raw_wife)}
    if len(result)!=2 or min(result)<=0:
        raise ValueError('Telegram ID двух администраторов должны быть разными положительными числами')
    return result


def is_admin(sender_id:int,service_id:int):
    return int(sender_id) in admin_ids(service_id)
