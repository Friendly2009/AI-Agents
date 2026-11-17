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
            'handoffs_require_review': [dict(r) for r in store.db.execute(
                "SELECT * FROM dialogue_control WHERE state IN ('notifying','review')")],
            'handoff_confirmations': [dict(r) for r in store.db.execute(
                "SELECT c.uid,c.request,o.state,o.error FROM dialogue_control c LEFT JOIN outbound_operations o ON o.id=c.request || ':ack' WHERE c.receipt=1 AND c.state='human' AND (o.state IS NULL OR o.state!='sent')")]}


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
