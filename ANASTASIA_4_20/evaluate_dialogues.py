"""Offline checks for captured structured model responses; never invent scores.

Usage: python evaluate_dialogues.py captured.json --output evaluation.json
captured.json is an object mapping D01..D62 to complete structured outputs.
Missing responses are reported as not_run. No credentials or network needed.
"""
import argparse
import json
import re
from pathlib import Path
from conversation_style import BehaviorSettings, turn_state

CRITERIA = ['naturalness','relevance','factual_accuracy','coherence',
            'humor_fit','non_pushiness','context_retention']


def check_response(case, data):
    # Import lazily: corpus and review templates do not need SDK dependencies.
    from brain import validate_response
    failures=[]
    try: validate_response(data)
    except (TypeError, ValueError, KeyError) as exc: return ['schema: '+str(exc)]
    reply=data['reply'];state=turn_state(case['history'],BehaviorSettings())
    if state['suppress_sales'] and (data['wants_trial'] or data['trial_decision']=='yes' or data['next_action']=='recording'):
        failures.append('blocked_sales_action')
    if not state['allow_emoji'] and re.search(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]',reply): failures.append('emoji_policy')
    if re.search(r'прекрасный вопрос|раскроем потенциал|передач\w* лида',reply,re.I): failures.append('canned_or_internal_language')
    if re.search(r'я (?:живой человек|не бот)|у меня (?:сын|дочь|дети)',reply,re.I): failures.append('identity_claim_review')
    if re.search(r'место (?:забронировано|закреплено)|записала вас|бронь готова',reply,re.I): failures.append('unconfirmed_booking_claim')
    if state['mood']=='ending' and '?' in reply: failures.append('question_after_closing')
    return failures


def evaluate(captured, cases):
    output=[]
    for case in cases:
        data=captured.get(case['id'])
        output.append(dict(id=case['id'],status='checked' if data is not None else 'not_run',
                           deterministic_failures=check_response(case,data) if data is not None else [],
                           human_scores={k:None for k in CRITERIA},llm_judge_scores={k:None for k in CRITERIA},
                           review=case['review']))
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captured',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    cases=json.loads((Path(__file__).parent/'tests/dialogue_cases.json').read_text(encoding='utf-8'))
    args.output.write_text(json.dumps(evaluate(json.loads(args.captured.read_text(encoding='utf-8')),cases),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
