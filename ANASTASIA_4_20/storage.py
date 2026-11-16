"""Local SQLite; purge history regularly; user deletion is supported."""
from __future__ import annotations
import sqlite3
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
        self.db.executescript("""
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
                 "wants_trial", "crm_id", "pending_notified", "consultation_notes"}
        if not set(fields) <= valid:
            raise ValueError("Forbidden contact fields")
        fields["updated_at"] = utc_now()
        sets = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE contacts SET {sets} WHERE user_id=?", (*fields.values(), user_id))
        self.db.commit()

    def add_turn(self, user_id: int, role: str, content: str):
        self.db.execute("INSERT INTO turns(user_id,role,content,created_at) VALUES(?,?,?,?)",
                        (user_id, role, content, utc_now()))
        self.db.commit()

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
        # Retain minimal suppression record to prevent unwanted re-contact.
        self.db.execute("UPDATE contacts SET consultation_notes=NULL,username=NULL,city=NULL,source=NULL,ai_consent=0,crm_consent=0,stopped=1,submission_pending=0,name=NULL,phone=NULL,course=NULL,child_age=NULL,wanted_time=NULL,wants_trial=0,pending_notified=0,updated_at=? WHERE user_id=?",
                        (utc_now(), user_id))
        self.db.commit()

    def purge_old_turns(self, days: int = 30):
        before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.db.execute("DELETE FROM turns WHERE created_at<?", (before,))
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
