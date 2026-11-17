"""Evidence ledger. No inferred attendance; no historical backfill from mutable flags."""
import json
from datetime import datetime, timezone

EVENTS = ('contact_authorized', 'conversation_started', 'client_replied', 'qualified',
          'trial_interested', 'trial_requested', 'lead_created', 'lead_delivered',
          'manager_contacted', 'trial_confirmed', 'trial_attended', 'trial_missed',
          'enrollment_completed', 'client_declined')
VERIFIED = {'manager_contacted', 'trial_confirmed', 'trial_attended', 'trial_missed', 'enrollment_completed'}


class Funnel:
    def __init__(self, store):
        self.store, self.db = store, store.db

    @staticmethod
    def migrate(db):
        db.executescript('''
        CREATE TABLE IF NOT EXISTS funnel_events (
          id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, event TEXT NOT NULL,
          occurred_at TEXT NOT NULL, source TEXT NOT NULL, reliability TEXT NOT NULL,
          confirmation_source TEXT NOT NULL, external_id TEXT, actor_id INTEGER,
          mode TEXT NOT NULL, event_key TEXT NOT NULL, details TEXT NOT NULL,
          UNIQUE(mode,event_key));
        CREATE INDEX IF NOT EXISTS funnel_client ON funnel_events(client_id,event);
        CREATE TABLE IF NOT EXISTS sales_telemetry (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value REAL, mode TEXT NOT NULL,
          occurred_at TEXT NOT NULL);
        ''')

    def emit(self, uid, event, *, reliability='program', confirmation='application',
             external_id=None, actor_id=None, key=None, details=None, source=None):
        if event not in EVENTS or reliability not in {'intent', 'program', 'crm', 'human'}:
            raise ValueError('Unknown event or evidence type')
        if event in VERIFIED and reliability not in {'crm', 'human'}:
            raise ValueError('A business outcome requires verified evidence')
        if reliability == 'human':
            from agent_config import require_admin
            require_admin(actor_id)
        if reliability == 'crm' and not external_id:
            raise ValueError('CRM evidence requires an external record ID')
        if not confirmation:
            raise ValueError('Confirmation source is required')
        from runtime_policy import safe_mode
        mode = 'test' if getattr(self.store, 'analytics_test', False) or safe_mode() else 'live'
        contact = self.store.get(uid) or {}
        self.db.execute('''INSERT OR IGNORE INTO funnel_events
            (client_id,event,occurred_at,source,reliability,confirmation_source,external_id,
             actor_id,mode,event_key,details) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (uid,event,datetime.now(timezone.utc).isoformat(),source or contact.get('source') or 'unknown',
             reliability,confirmation,str(external_id) if external_id is not None else None,
             actor_id,mode,key or f'{event}:{uid}',json.dumps(details or {},ensure_ascii=False)))

    def metric(self, kind, value=None):
        from runtime_policy import safe_mode
        mode = 'test' if getattr(self.store, 'analytics_test', False) or safe_mode() else 'live'
        with self.db:
            self.db.execute('INSERT INTO sales_telemetry(kind,value,mode,occurred_at) VALUES(?,?,?,?)',
                            (kind,value,mode,datetime.now(timezone.utc).isoformat()))

    def confirm(self, uid, event, actor, reference):
        if event not in VERIFIED or not reference.strip():
            raise ValueError('Нужны подтверждаемый исход и ID подтверждения')
        if not self.store.get(uid):
            raise ValueError('Клиент не найден')
        if event in {'trial_attended','trial_missed'}:
            opposite='trial_missed' if event=='trial_attended' else 'trial_attended'
            if self.db.execute('SELECT 1 FROM funnel_events WHERE client_id=? AND external_id=? AND event=?',
                               (uid,reference,opposite)).fetchone():
                raise ValueError('Противоречащий исход: требуется разбор подтверждения')
        with self.db:
            self.emit(uid,event,reliability='human',confirmation='admin_command',actor_id=actor,
                      external_id=reference,key=f'{event}:{uid}:{reference}')

    def counts(self):
        return {e: self.db.execute('SELECT COUNT(DISTINCT client_id) FROM funnel_events WHERE event=? AND mode=?',
                                  (e,'live')).fetchone()[0] for e in EVENTS}

    def report(self):
        counts = self.counts()
        lines = ['Воронка: уникальные клиенты, только рабочий режим; с момента установки.']
        lines += [f'{e}: {counts[e]}' for e in EVENTS]
        stages = ('contact_authorized','conversation_started','client_replied','trial_requested',
                  'lead_created','trial_confirmed','trial_attended')
        for before, after in zip(stages, stages[1:]):
            # Intersection avoids mixing unrelated clients across stages.
            numerator = self.db.execute('''SELECT COUNT(DISTINCT a.client_id) FROM funnel_events a
              JOIN funnel_events b ON a.client_id=b.client_id AND b.occurred_at>=a.occurred_at
              WHERE a.event=? AND b.event=? AND a.mode='live' AND b.mode='live' ''',(before,after)).fetchone()[0]
            rate = f'{100*numerator/counts[before]:.1f}%' if counts[before] else 'нет данных'
            lines.append(f'{before} → {after}: {rate} ({numerator}/{counts[before]})')
        reasons = self.db.execute("SELECT details,COUNT(*) FROM funnel_events WHERE event='client_declined' AND mode='live' GROUP BY details").fetchall()
        lines.append('Причины отказов (эвристика): '+('; '.join(f'{r[0]}: {r[1]}' for r in reasons) or 'нет данных'))
        rows = self.db.execute("SELECT kind,COUNT(*),AVG(value) FROM sales_telemetry WHERE mode='live' GROUP BY kind").fetchall()
        lines += [f'{r[0]}: n={r[1]}' + (f', среднее={r[2]:.3f}' if r[2] is not None else '') for r in rows]
        uncertain = self.db.execute("SELECT COUNT(*) FROM outbound_operations WHERE simulated=0 AND state IN ('sending','uncertain')").fetchone()[0]
        failures = self.db.execute("SELECT COUNT(*) FROM outbound_operations WHERE simulated=0 AND state IN ('failed','uncertain') AND COALESCE(error,'')!=''").fetchone()[0]
        lines.append(f'Telegram: ошибки={failures}; неопределённые доставки={uncertain}')
        crm_uncertain=self.db.execute("SELECT COUNT(*) FROM effects WHERE effect_key LIKE 'crm:%' AND state IN ('sending','uncertain')").fetchone()[0]
        lines.append(f'CRM: операций, требующих сверки={crm_uncertain}')
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='response_reviews'").fetchone():
            r=self.db.execute('SELECT COUNT(*),AVG(score) FROM response_reviews').fetchone()
            lines.append(f'Ручное качество: n={r[0]}, среднее={r[1] if r[1] is not None else "нет данных"}/5')
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='lead_notices'").fetchone():
            for r in self.db.execute('SELECT recipient,state,COUNT(*) FROM lead_notices GROUP BY recipient,state'):
                lines.append(f'Уведомления руководителю {r[0]}: {r[1]}={r[2]}')
        lines.append('Качество: ручная оценка /оценка; причинная эффективность стратегий не доказана.')
        return '\n'.join(lines)
