"""Controlled learning loop for sales conversations.

The model does not rewrite its own rules. We measure strategy outcomes by
conversation bucket, select a better tested framing, and keep an audit trail
that a manager can review or label manually.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


STRATEGIES = {
    "result_first": "Сначала покажи конкретный результат, который ребёнок сможет попробовать, затем задай один естественный вопрос.",
    "interest_bridge": "Свяжи уже названный интерес ребёнка с одним подходящим направлением; не перечисляй каталог.",
    "objection_first": "Сначала ответь на сомнение родителя и снизь неопределённость; предложение записи пока не делай.",
    "choice_light": "Дай родителю небольшой понятный выбор из двух подходящих вариантов, без анкеты и давления.",
}

_BUCKETS = ("gaming", "creative", "exam", "price", "objection", "open")
_POSITIVE = {"trial_interest", "lead", "attended"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bucket(text: str, contact: dict | None = None) -> str:
    value = (text or "").lower()
    if re.search(r"игр|игра|гейм|minecraft|майнкрафт|роблокс|scratch", value):
        return "gaming"
    if re.search(r"рису|творч|комикс|мульт|открытк|дизайн|истори", value):
        return "creative"
    if re.search(r"огэ|егэ|экзамен|информатик", value):
        return "exam"
    if re.search(r"цен|стоим|дорог|руб", value):
        return "price"
    if re.search(r"сомне|боюсь|не знаю|не понрав|давит|навяз|подума|не сейчас|нет времени", value):
        return "objection"
    if contact:
        note = str(contact.get("consultation_notes") or "").lower()
        if re.search(r"игр|гейм", note):
            return "gaming"
        if re.search(r"рису|творч|комикс|мульт", note):
            return "creative"
    return "open"


def _safe_rate(successes: int, attempts: int) -> float:
    return (successes + 1.0) / (attempts + 2.0)


class SalesLearning:
    def __init__(self, path: str | Path = "data/sales_learning.db"):
        self.path = str(path)
        if self.path == ":memory:":
            self.db = sqlite3.connect(self.path)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS interactions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              bucket TEXT NOT NULL,
              strategy TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'pending',
              next_action TEXT,
              trial_decision TEXT,
              response_chars INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              labeled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_learning_user ON interactions(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_learning_bucket ON interactions(bucket, strategy, outcome);
            CREATE TABLE IF NOT EXISTS labels (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              interaction_id INTEGER NOT NULL,
              outcome TEXT NOT NULL,
              actor_id INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
              name TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self.db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES('enabled','1')")
        self.db.commit()

    @property
    def enabled(self) -> bool:
        row = self.db.execute("SELECT value FROM settings WHERE name='enabled'").fetchone()
        return bool(row and row[0] == "1")

    def set_enabled(self, value: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(name,value) VALUES('enabled',?)", ("1" if value else "0",))
        self.db.commit()

    def bucket(self, text: str, contact: dict | None = None) -> str:
        return _bucket(text, contact)

    def choose(self, bucket: str) -> str:
        if bucket not in _BUCKETS:
            bucket = "open"
        rows = self.db.execute(
            "SELECT strategy, COUNT(*) attempts, SUM(outcome IN ('trial_interest','lead','attended')) positive "
            "FROM interactions WHERE bucket=? GROUP BY strategy",
            (bucket,),
        ).fetchall()
        stats = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}
        # Give each framing a fair initial sample. Afterwards use a smoothed
        # success rate; a strategy must earn its way into the default path.
        least = min(STRATEGIES, key=lambda name: stats.get(name, (0, 0))[0])
        if stats.get(least, (0, 0))[0] < 5:
            return least
        return max(STRATEGIES, key=lambda name: _safe_rate(*stats.get(name, (0, 0))[::-1]))

    def prompt_hint(self, text: str, contact: dict | None, history: list[dict], selection=None) -> str:
        if not self.enabled:
            return "Обучаемый режим выключен: следуй утверждённой консультационной политике."
        bucket, strategy = selection or (self.bucket(text, contact), self.choose(self.bucket(text, contact)))
        recent = [r for r in self.db.execute(
            "SELECT strategy,outcome FROM interactions WHERE bucket=? ORDER BY id DESC LIMIT 40", (bucket,)
        ).fetchall()]
        results = {}
        for name in STRATEGIES:
            sample = [r for r in recent if r[0] == name and r[1] != "pending"]
            if sample:
                results[name] = f"{sum(r[1] in _POSITIVE for r in sample)}/{len(sample)}"
        score_text = ", ".join(f"{k}: {v}" for k, v in results.items()) or "пока нет размеченных исходов"
        return (
            f"Обучаемый менеджер: тип диалога={bucket}; рабочая стратегия={strategy}. "
            f"Правило стратегии: {STRATEGIES[strategy]} История по этому типу: {score_text}. "
            "Это рекомендация по форме ответа, а не разрешение нарушать факты, согласия и запрет давления."
        )

    def record(self, user_id: int, text: str, parsed: dict, reply: str, contact: dict | None = None, selection=None) -> int | None:
        if not self.enabled:
            return None
        bucket, strategy = selection or (self.bucket(text, contact), self.choose(self.bucket(text, contact)))
        decision = parsed.get("trial_decision")
        outcome = "pending"
        cur = self.db.execute(
            "INSERT INTO interactions(user_id,bucket,strategy,outcome,next_action,trial_decision,response_chars,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, bucket, strategy, outcome, parsed.get("next_action"), decision, len(reply), _now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def mark_engaged(self, user_id: int, text: str = "") -> None:
        row = self.db.execute(
            "SELECT id FROM interactions WHERE user_id=? AND outcome='pending' ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if row:
            outcome = 'paused' if re.search(r'давите|странно|не понимаю|не понрав|подумаю|не сейчас', text, re.I) else 'continued'
            self.db.execute('UPDATE interactions SET outcome=?,labeled_at=? WHERE id=?', (outcome, _now(), row[0]))
            self.db.commit()

    def mark(self, interaction_id: int, outcome: str, actor_id: int | None = None) -> bool:
        allowed = {"continued", "trial_interest", "lead", "attended", "paused", "lost", "handoff"}
        if outcome not in allowed:
            raise ValueError("Неизвестный исход")
        row = self.db.execute("SELECT id FROM interactions WHERE id=?", (interaction_id,)).fetchone()
        if not row:
            return False
        self.db.execute("UPDATE interactions SET outcome=?,labeled_at=? WHERE id=?", (_safe_outcome(outcome), _now(), interaction_id))
        self.db.execute("INSERT INTO labels(interaction_id,outcome,actor_id,created_at) VALUES(?,?,?,?)", (interaction_id, outcome, actor_id, _now()))
        self.db.commit()
        return True

    def mark_user(self, user_id: int, outcome: str) -> None:
        row = self.db.execute("SELECT id FROM interactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if row:
            self.mark(int(row[0]), outcome)

    def report(self) -> str:
        total = self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        labeled = self.db.execute("SELECT COUNT(*) FROM interactions WHERE outcome!='pending'").fetchone()[0]
        lines = [f"🧠 Обучение менеджера: {'включено' if self.enabled else 'пауза'}", f"Ответов в статистике: {total}; размечено: {labeled}"]
        rows = self.db.execute(
            "SELECT bucket,strategy,COUNT(*) attempts,SUM(outcome IN ('trial_interest','lead','attended')) positive "
            "FROM interactions WHERE outcome!='pending' GROUP BY bucket,strategy ORDER BY bucket,positive DESC,attempts DESC"
        ).fetchall()
        if not rows:
            lines.append("Пока нет размеченных исходов. После диалогов появится сравнение подходов.")
        else:
            lines.append("Сценарии по типам диалогов:")
            for r in rows:
                lines.append(f"• {r[0]} · {r[1]} — {r[3]}/{r[2]} положительных")
        recent = self.db.execute("SELECT id,bucket,strategy,outcome FROM interactions ORDER BY id DESC LIMIT 8").fetchall()
        if recent:
            lines.append("Последние ID для ручной разметки: " + ", ".join(f"#{r[0]} {r[1]}/{r[2]}={r[3]}" for r in recent))
        lines.append("Команды: /обучение успех ID · /обучение посещение ID · /обучение провал ID · /обучение пауза · /обучение вкл")
        return "\n".join(lines)[:3900]


def _safe_outcome(value: str) -> str:
    return value


def anonymized_user_key(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


LEARNING = SalesLearning()
