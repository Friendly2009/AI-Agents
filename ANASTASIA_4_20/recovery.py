"""Offline inspection of interrupted inbound messages and uncertain effects.

Never replays a message or an external action. Stop the listener before reconciliation.
"""
import argparse
import json
from pathlib import Path
from storage import Store


def report(store):
    return {'inbound_requires_review': store.pending_inbound(),
            'effects_require_review': [dict(r) for r in store.db.execute(
                "SELECT * FROM effects WHERE state IN ('sending','uncertain')")],
            'handoffs_require_review': [dict(user_id=r['user_id'], state=json.loads(r['payload']))
                for r in store.db.execute('SELECT * FROM dialogue_state')
                if json.loads(r['payload']).get('handoff_status') in {'pending','failed'}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=str(Path(__file__).parent/'data'/'softskills.db'))
    args = parser.parse_args()
    store = Store(args.database)
    try:
        print(json.dumps(report(store), ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
