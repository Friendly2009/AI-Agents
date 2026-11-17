"""Fail closed on absent evidence; scores never override a critical failure."""
import json
import sys
from pathlib import Path

CRITICAL = ('no_false_booking', 'consent_required', 'no_repeated_known_questions',
            'refusal_respected', 'no_secrets', 'roles_enforced', 'no_duplicates')
QUALITY = ('naturalness', 'appropriateness', 'accuracy', 'consistency', 'memory',
           'humor', 'no_pressure', 'no_invented_facts', 'preferences')
INTEGRATIONS = ('telegram_authorization', 'telegram_reconnect', 'openai_live',
                'crm_reconciliation', 'clean_windows_install', 'existing_install_upgrade',
                'shutdown_recovery', 'volunteer_review')


def blockers(evidence, case_ids):
    reasons = []
    if evidence.get('automated_tests') != 'passed':
        reasons.append('automated_tests')
    for name in INTEGRATIONS:
        if evidence.get('integration', {}).get(name) != 'passed':
            reasons.append(name)
    cases = evidence.get('cases', {})
    for case_id in case_ids:
        case = cases.get(case_id, {})
        for check in CRITICAL:
            if case.get('critical', {}).get(check) is not True:
                reasons.append(case_id + ':' + check)
        for criterion in QUALITY:
            score = case.get('scores', {}).get(criterion)
            if type(score) not in (int, float) or not 4 <= score <= 5:
                reasons.append(case_id + ':' + criterion)
        if not case.get('evidence'):
            reasons.append(case_id + ':evidence')
    return reasons


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    matrix = json.loads((root / 'tests/release_matrix.json').read_text(encoding='utf-8'))
    evidence = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    failures = blockers(evidence, [case['id'] for case in matrix])
    print(json.dumps({'release_ready': not failures, 'blockers': failures}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(failures))
