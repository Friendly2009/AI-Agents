"""Read-only, offline diagnostics even when Telegram is restricted."""
import argparse
import json
from pathlib import Path
import sqlite3


def report(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'outbound_operations' not in tables:
            return {'schema':'legacy; run the updated application with SAFE_TEST_MODE=true first'}
        return {'states':[dict(r) for r in db.execute('SELECT state,COUNT(*) AS count FROM outbound_operations GROUP BY state')],
                'restrictions':[dict(r) for r in db.execute('SELECT * FROM safety_state')],
                'operations':[dict(r) for r in db.execute('SELECT id,uid,kind,source,state,created,attempted,error,message_id,batch,simulated FROM outbound_operations ORDER BY created DESC LIMIT 100')],
                'events':[dict(r) for r in db.execute('SELECT * FROM outbound_events ORDER BY seq DESC LIMIT 100')]}
    finally: db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'data'/'softskills.db'))
    args=parser.parse_args()
    print(json.dumps(report(args.db),ensure_ascii=False,indent=2))
