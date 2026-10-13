import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


class Store:
    def __init__(self, path):
        self.path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, slot TEXT, created_at TEXT NOT NULL,
                    lease_until TEXT NOT NULL, status TEXT NOT NULL,
                    report TEXT, error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running ON runs(status) WHERE status='running';
                CREATE INDEX IF NOT EXISTS by_slot ON runs(slot);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, slot=None, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE runs SET status='failed',error='Запуск прерван; срок выполнения истёк' "
                "WHERE status='running' AND lease_until < ?",
                (now.isoformat(),),
            )
            if db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                return None
            if slot:
                runs = db.execute(
                    "SELECT * FROM runs WHERE slot=? ORDER BY created_at DESC", (slot,)
                ).fetchall()
                if any(r["status"] in {"completed", "degraded"} for r in runs) or len(runs) >= 3:
                    return None
                if runs and now - datetime.fromisoformat(runs[0]["created_at"]) < timedelta(minutes=15):
                    return None
            run_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO runs(id,slot,created_at,lease_until,status) VALUES(?,?,?,?, 'running')",
                (run_id, slot, now.isoformat(), (now + timedelta(minutes=11)).isoformat()),
            )
            return run_id

    def finish(self, run_id, report=None, error=None):
        status = "failed" if error else ("degraded" if report["warnings"] else "completed")
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, report=?, error=? WHERE id=? AND status='running'",
                (status, json.dumps(report, ensure_ascii=False) if report else None, error, run_id),
            )

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result.pop("lease_until")
        result["report"] = json.loads(result["report"]) if result["report"] else None
        return result

    def list(self, limit=30):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,slot,created_at,status,error FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            ]
