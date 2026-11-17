"""Authoritative dialogue ownership and durable handoff transitions.

External attempts are at most once. Interrupted attempts require operator review.
Legacy fields are read only during migration, never used as delivery authority.
"""
import json
import sqlite3
from pathlib import Path

ACK = 'Передала вопрос менеджеру вместе с нашей перепиской. Он сможет продолжить здесь. Время ответа пока не подтверждено.'
FAILED = 'Не удалось подтвердить доставку уведомления менеджеру. Успешная передача пока не подтверждена.'


class DialogueControl:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def migrate(self, legacy_path=None):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS dialogue_control (
          uid INTEGER PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
          request TEXT, receipt INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS handoff_requests (
          id TEXT PRIMARY KEY, uid INTEGER NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dialogue_migrations (name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS dialogue_transition_events (id TEXT PRIMARY KEY);
        ''')
        if self.db.execute("SELECT 1 FROM dialogue_migrations WHERE name='ownership_v1'").fetchone():
            return
        legacy = {}
        if legacy_path and Path(legacy_path).exists():
            db = sqlite3.connect(Path(legacy_path).resolve().as_uri() + '?mode=ro', uri=True)
            try:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='handoffs'").fetchone():
                    legacy = dict(db.execute('SELECT uid,active FROM handoffs'))
            finally:
                db.close()
        states = {r[0]: json.loads(r[1]) for r in self.db.execute('SELECT user_id,payload FROM dialogue_state')}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute("SELECT 1 FROM dialogue_migrations WHERE name='ownership_v1'").fetchone():
                return
            for uid in states.keys() | legacy.keys():
                old = states.get(uid, {}).get('handoff_status', 'none')
                # Any positive ownership evidence wins. Legacy delivered is not a
                # verifiable receipt: never create an acknowledgement from it.
                state = 'human' if legacy.get(uid) or old == 'delivered' else ('review' if old == 'pending' else 'ai')
                self.db.execute('INSERT OR IGNORE INTO dialogue_control(uid,state) VALUES(?,?)', (uid,state))
            self.db.execute("INSERT INTO dialogue_migrations VALUES('ownership_v1')")

    def get(self, uid):
        row = self.db.execute('SELECT * FROM dialogue_control WHERE uid=?', (uid,)).fetchone()
        return dict(row) if row else dict(uid=uid, state='ai', revision=0, request=None, receipt=0)

    def blocked(self, uid):
        return self.get(uid)['state'] != 'ai'

    def takeover(self, uid):
        with self.db:
            self.db.execute("INSERT INTO dialogue_control(uid,state,revision) VALUES(?,'human',1) ON CONFLICT(uid) DO UPDATE SET state='human',revision=revision+1,request=NULL,receipt=0", (uid,))

    def resume(self, uid, event=None):
        with self.db:
            if event is not None and not self.db.execute(
                    'INSERT OR IGNORE INTO dialogue_transition_events VALUES(?)',
                    (f'resume:{uid}:{event}',)).rowcount:
                return False
            self.db.execute("INSERT INTO dialogue_control(uid,state,revision) VALUES(?,'ai',1) ON CONFLICT(uid) DO UPDATE SET state='ai',revision=revision+1,request=NULL,receipt=0", (uid,))
            self.db.execute('UPDATE contacts SET stopped=0 WHERE user_id=?', (uid,))
            state = self.store.dialogue(uid)
            state.update(stage='engaged', paused=False, declined=False)
            self.store._save_dialogue(uid, state)
        return True

    def begin(self, uid, event):
        key = f'handoff:{uid}:{event}'
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.get(uid)
            if self.db.execute('SELECT 1 FROM handoff_requests WHERE id=?', (key,)).fetchone() or current['state'] != 'ai':
                return None
            self.db.execute("INSERT INTO handoff_requests VALUES(?,?,'notifying')", (key,uid))
            self.db.execute("INSERT INTO dialogue_control(uid,state,revision,request) VALUES(?,'notifying',1,?) ON CONFLICT(uid) DO UPDATE SET state='notifying',revision=revision+1,request=excluded.request,receipt=0", (uid,key))
        return key

    def finish(self, uid, key, delivered, uncertain=False):
        state = 'human' if delivered else ('review' if uncertain else 'ai')
        with self.db:
            self.db.execute('UPDATE handoff_requests SET state=? WHERE id=?', ('delivered' if delivered else ('uncertain' if uncertain else 'failed'),key))
            self.db.execute("UPDATE dialogue_control SET state=?,receipt=? WHERE uid=? AND request=? AND state='notifying'", (state,int(bool(delivered)),uid,key))

    def permits_ack(self, operation):
        if not operation:
            return False
        current = self.get(operation['uid'])
        return (current['state'] == 'human' and current['receipt'] == 1
                and operation['id'] == str(current['request']) + ':ack'
                and operation['kind'] == 'handoff_ack'
                and operation['source'] == 'dialogue_control' and operation['body'] == ACK)

