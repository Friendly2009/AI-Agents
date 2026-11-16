"""Local SQLite; purge history regularly; user deletion is supported."""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | None = None):
        path = path if path is not None else str(Path(__file__).resolve().parent / "data" / "softskills.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._inbound = {}
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS dialogue_state (
          user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbox (
          user_id INTEGER, message_id INTEGER, content TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'received', created_at TEXT NOT NULL,
          PRIMARY KEY(user_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS effects (
          effect_key TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS safety_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts (
          user_id INTEGER PRIMARY KEY, username TEXT, city TEXT, source TEXT,
          ai_consent INTEGER NOT NULL DEFAULT 0,
          crm_consent INTEGER NOT NULL DEFAULT 0,
          stopped INTEGER NOT NULL DEFAULT 0,
          initial_sent INTEGER NOT NULL DEFAULT 0,
          name TEXT, phone TEXT, course TEXT, child_age INTEGER,
          wanted_time TEXT, wants_trial INTEGER NOT NULL DEFAULT 0,
          crm_id INTEGER, pending_notified INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id, id);
        CREATE TABLE IF NOT EXISTS send_audit (user_id INTEGER, sent_at TEXT, source TEXT);
        CREATE TABLE IF NOT EXISTS sends (
          user_id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL, source TEXT NOT NULL
        );
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(contacts)")}
        if 'speech_preferences' not in columns:
            self.db.execute("ALTER TABLE contacts ADD COLUMN speech_preferences TEXT")
        if 'consultation_notes' not in columns:
            self.db.execute("ALTER TABLE contacts ADD COLUMN consultation_notes TEXT")
        if 'submission_pending' not in columns:
            self.db.execute("ALTER TABLE contacts ADD COLUMN submission_pending INTEGER NOT NULL DEFAULT 0")
        if 'ai_notice_shown' not in columns:
            self.db.execute("ALTER TABLE contacts ADD COLUMN ai_notice_shown INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    def get(self, user_id: int):
        r = self.db.execute("SELECT * FROM contacts WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    def ensure(self, user_id: int, username: str = "", city: str = "", source: str = ""):
        self.db.execute("INSERT OR IGNORE INTO contacts(user_id,username,city,source,updated_at) VALUES(?,?,?,?,?)",
                        (user_id, username, city, source, utc_now()))
        self.db.commit()

    def update(self, user_id: int, **fields):
        if not fields:
            return
        valid = {"username", "city", "source", "ai_consent", "crm_consent", "stopped",
                 "submission_pending", "ai_notice_shown", "initial_sent", "name", "phone", "course", "child_age", "wanted_time",
                 "wants_trial", "crm_id", "pending_notified", "consultation_notes", "speech_preferences"}
        if not set(fields) <= valid:
            raise ValueError("Forbidden contact fields")
        fields["updated_at"] = utc_now()
        sets = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE contacts SET {sets} WHERE user_id=?", (*fields.values(), user_id))
        self.db.commit()

    def add_turn(self, user_id: int, role: str, content: str):
        from dialogue_state import after_reply
        with self.db:
            self.db.execute("INSERT INTO turns(user_id,role,content,created_at) VALUES(?,?,?,?)",
                            (user_id, role, content, utc_now()))
            if role == 'assistant':
                self._save_dialogue(user_id, after_reply(self.dialogue(user_id), content))
                if user_id in self._inbound:
                    self.db.execute("UPDATE inbox SET status='answered' WHERE user_id=? AND message_id=?",
                                    (user_id, self._inbound[user_id]))

    def dialogue(self, user_id):
        from dialogue_state import initial_state
        row = self.db.execute('SELECT payload FROM dialogue_state WHERE user_id=?', (user_id,)).fetchone()
        return dict(initial_state(), **json.loads(row[0])) if row else initial_state()

    def _save_dialogue(self, user_id, state):
        self.db.execute('INSERT OR REPLACE INTO dialogue_state VALUES(?,?)',
                        (user_id, json.dumps(state, ensure_ascii=False)))

    def set_dialogue(self, user_id, **fields):
        from dialogue_state import STAGES
        state = self.dialogue(user_id)
        if not set(fields) <= set(state) or ('stage' in fields and fields['stage'] not in STAGES):
            raise ValueError('Invalid dialogue state')
        state.update(fields)
        with self.db:
            self._save_dialogue(user_id, state)

    def claim_inbound(self, user_id, message_id, text):
        # Retain interrupted events for operator recovery; never blindly replay external effects.
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO inbox(user_id,message_id,content,created_at) VALUES(?,?,?,?)',
                                       (user_id, message_id, text, utc_now())).rowcount
        if inserted:
            self._inbound[user_id] = message_id
        return bool(inserted)

    def observe(self, user_id, text):
        from dialogue_state import reduce_user
        # Facts, evidence and state commit together, before any model/network call.
        with self.db:
            state, updates = reduce_user(self.dialogue(user_id), text, self.get(user_id) or {})
            if updates:
                fields = dict(updates, updated_at=utc_now())
                self.db.execute('UPDATE contacts SET ' + ','.join(k+'=?' for k in fields) + ' WHERE user_id=?',
                                (*fields.values(), user_id))
            self._save_dialogue(user_id, state)
        return state

    def pending_inbound(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM inbox WHERE status='received' ORDER BY created_at")]

    def complete_inbound(self, user_id, message_id):
        if self._inbound.get(user_id) == message_id:
            with self.db:
                self.db.execute("UPDATE inbox SET status='handled' WHERE user_id=? AND message_id=? AND status='received'",
                                (user_id, message_id))

    def recording_consent(self, user_id, decision):
        with self.db:
            self.db.execute('UPDATE contacts SET crm_consent=?,submission_pending=?,updated_at=? WHERE user_id=?',
                            (int(decision), int(decision), utc_now(), user_id))
            state = self.dialogue(user_id)
            state.update(application_status='pending' if decision else 'consent_refused',
                         expected_action='submit' if decision else None)
            self._save_dialogue(user_id, state)

    def submitted(self, user_id, crm_id, effect_key):
        with self.db:
            self.db.execute('UPDATE contacts SET crm_id=?,submission_pending=0,updated_at=? WHERE user_id=?',
                            (crm_id, utc_now(), user_id))
            self.db.execute("UPDATE effects SET state='sent',updated_at=? WHERE effect_key=?", (utc_now(), effect_key))
            state = self.dialogue(user_id)
            state.update(stage='lead_submitted', application_status='submitted')
            self._save_dialogue(user_id, state)

    def history(self, user_id: int, limit: int = 12) -> list[dict]:
        rows = self.db.execute("SELECT role,content FROM (SELECT id,role,content FROM turns WHERE user_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                               (user_id, limit)).fetchall()
        return [dict(x) for x in rows]

    def mark_sent(self, user_id: int, source: str):
        self.db.execute("INSERT OR IGNORE INTO sends(user_id,sent_at,source) VALUES(?,?,?)", (user_id, utc_now(), source))
        self.update(user_id, initial_sent=1)
        self.db.commit()

    def was_sent(self, user_id: int) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sends WHERE user_id=?", (user_id,)).fetchone())

    def sent_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM (SELECT user_id,sent_at FROM sends UNION SELECT user_id,sent_at FROM send_audit) WHERE substr(sent_at,1,10)=?", (utc_now()[:10],)).fetchone()[0]

    def erase(self, user_id: int):
        self.ensure(user_id)
        self.db.execute("DELETE FROM turns WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM dialogue_state WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM inbox WHERE user_id=?", (user_id,))
        # Retain minimal suppression record to prevent unwanted re-contact.
        self.db.execute("UPDATE contacts SET speech_preferences=NULL,consultation_notes=NULL,username=NULL,city=NULL,source=NULL,ai_consent=0,crm_consent=0,stopped=1,submission_pending=0,name=NULL,phone=NULL,course=NULL,child_age=NULL,wanted_time=NULL,wants_trial=0,pending_notified=0,updated_at=? WHERE user_id=?",
                        (utc_now(), user_id))
        self.db.commit()

    def purge_old_turns(self, days: int = 30):
        before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.db.execute("DELETE FROM turns WHERE created_at<?", (before,))
        # Keep deduplication keys, but expire raw text after the conversation retention period.
        self.db.execute("UPDATE inbox SET content='' WHERE created_at<? AND status IN ('answered','handled')", (before,))
        self.db.commit()

    def list_ready(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM contacts WHERE wants_trial=1 AND crm_consent=1 AND phone IS NOT NULL AND name IS NOT NULL AND city IN ('Александров','Ярославль') AND crm_id IS NULL")]

    def claim_effect(self, key):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO effects VALUES(?, 'sending', ?)",
                                   (key, utc_now())).rowcount == 1

    def finish_effect(self, key, state):
        if state not in {'sent', 'uncertain'}:
            raise ValueError('Invalid effect state')
        with self.db:
            self.db.execute('UPDATE effects SET state=?,updated_at=? WHERE effect_key=?',
                            (state, utc_now(), key))

    def block_outbound(self, reason):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO safety_state VALUES('outbound_block', ?)", (reason,))

    def outbound_blocked(self):
        return bool(self.db.execute("SELECT 1 FROM safety_state WHERE name='outbound_block'").fetchone())

    def ever_contacted(self, uid):
        return self.was_sent(uid) or bool(self.db.execute(
            'SELECT 1 FROM send_audit WHERE user_id=? LIMIT 1', (uid,)).fetchone())
