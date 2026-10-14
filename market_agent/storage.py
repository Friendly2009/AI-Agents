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
                CREATE TABLE IF NOT EXISTS news_cache (
                    id TEXT NOT NULL, feed_url TEXT NOT NULL, published_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(id,feed_url)
                );
                CREATE INDEX IF NOT EXISTS news_by_date ON news_cache(published_at);
                CREATE TABLE IF NOT EXISTS news_polls (
                    id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, feed_key TEXT NOT NULL,
                    item_count INTEGER NOT NULL, warnings TEXT NOT NULL
                );
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

    def cache_news(self, items, feeds, checked_at, warnings):
        checked_at = checked_at.astimezone(timezone.utc)
        with self.connect() as db:
            for item in items:
                db.execute(
                    "INSERT OR REPLACE INTO news_cache VALUES(?,?,?,?)",
                    (
                        item.id,
                        item.feed_url,
                        item.published_at.astimezone(timezone.utc).isoformat(),
                        item.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT INTO news_polls(checked_at,feed_key,item_count,warnings) VALUES(?,?,?,?)",
                (
                    checked_at.isoformat(),
                    json.dumps(sorted(feeds)),
                    len(items),
                    json.dumps(warnings, ensure_ascii=False),
                ),
            )
            cutoff = (checked_at - timedelta(days=35)).isoformat()
            db.execute("DELETE FROM news_cache WHERE published_at < ?", (cutoff,))
            db.execute("DELETE FROM news_polls WHERE checked_at < ?", (cutoff,))

    def cached_news(self, feeds, since, until):
        from .models import News

        if not feeds:
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM news_cache WHERE feed_url IN ("
                + ",".join("?" for _ in feeds)
                + ") AND published_at BETWEEN ? AND ? ORDER BY published_at DESC LIMIT 5000",
                (
                    *feeds,
                    since.astimezone(timezone.utc).isoformat(),
                    until.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchall()
        result = {}
        for row in rows:
            item = News.model_validate_json(row["payload"])
            result.setdefault(item.id, item)
        return list(result.values())

    def news_status(self, feeds):
        key = json.dumps(sorted(feeds))
        with self.connect() as db:
            last = db.execute(
                "SELECT * FROM news_polls WHERE feed_key=? ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            first = db.execute(
                "SELECT MIN(checked_at) AS first FROM news_polls WHERE feed_key=?", (key,)
            ).fetchone()["first"]
            successful = db.execute(
                "SELECT MAX(checked_at) AS last FROM news_polls WHERE feed_key=? AND warnings='[]'", (key,)
            ).fetchone()["last"]
        if not last:
            return {
                "last_checked_at": None,
                "collection_started_at": None,
                "last_success_at": None,
                "item_count": 0,
                "warnings": [],
            }
        return {
            "last_checked_at": last["checked_at"],
            "collection_started_at": first,
            "last_success_at": successful,
            "item_count": last["item_count"],
            "warnings": json.loads(last["warnings"]),
        }
