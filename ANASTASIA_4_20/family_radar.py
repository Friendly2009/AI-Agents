"""ANASTASIA FAMILY RADAR v3.

Records only self-reported family details from adults who initiate a Telegram
conversation. Does not read strangers' stories, enumerate group members or infer
parenthood from appearance. Separate affirmative marketing consent is required.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ages(text: str) -> list[int]:
    """Accept 7-17, 9,12, 9 и 12; do not mine general prose for ages."""
    normalized = text.strip().lower().replace('лет', '').replace('год', '').replace('года', '')
    normalized = re.sub(r'\s+и\s+', ',', normalized)
    if not re.fullmatch(r'\s*\d{1,2}(\s*[,;]\s*\d{1,2}){0,3}\s*', normalized):
        return []
    ages = sorted(set(map(int, re.findall(r'\d{1,2}', normalized))))
    return ages if ages and all(7 <= x <= 17 for x in ages) else []


def answer_kind(text: str) -> str:
    val = text.strip().lower().rstrip('.!')
    if val in {'да', 'согласен', 'согласна', 'подтверждаю', 'yes'}:
        return 'yes'
    if val in {'нет', 'не согласен', 'не согласна', 'no'}:
        return 'no'
    return 'unknown'


def campaign_key(campaign: dict) -> str:
    payload = json.dumps({k: campaign.get(k) for k in ('title','message_body','question','updated_utc')}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


class FamilyRadar:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else BASE / 'data' / 'family_radar.db'
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS families (
            user_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT,
            source TEXT NOT NULL DEFAULT 'telegram_inbound',
            stage TEXT NOT NULL DEFAULT 'adult',
            adult_confirmed INTEGER NOT NULL DEFAULT 0,
            city TEXT,
            child_ages TEXT,
            data_consent_at TEXT,
            marketing_consent_at TEXT,
            marketing_revoked_at TEXT,
            stopped INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS consent_references (
            user_id INTEGER PRIMARY KEY, evidence TEXT NOT NULL,
            recorded_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS radar_sends (
            user_id INTEGER NOT NULL, campaign_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(user_id, campaign_key)
          );
        ''')
        self.db.commit()

    def import_optin(self, entry: dict) -> None:
        # Only validated, documented opt-ins from our own sources; never scrape.
        uid=entry['user_id']
        existing=self.get(uid)
        if existing and (existing['stopped'] or existing['marketing_revoked_at']):
            raise ValueError('Consent was revoked')
        t=now()
        with self.db:
            self.db.execute("""INSERT INTO families
              (user_id,first_name,source,stage,adult_confirmed,city,child_ages,
               data_consent_at,marketing_consent_at,stopped,created_at,updated_at)
              VALUES(?,?,?,'complete',1,'Александров',?,?,?,0,?,?)
              ON CONFLICT(user_id) DO UPDATE SET
                 first_name=excluded.first_name,
                 source=excluded.source,
                 stage=excluded.stage,
                 adult_confirmed=excluded.adult_confirmed,
                 city=excluded.city,
                 child_ages=excluded.child_ages,
                 data_consent_at=excluded.data_consent_at,
                 marketing_consent_at=excluded.marketing_consent_at,
                 updated_at=excluded.updated_at""",
              (uid,entry['first_name'],entry['source'],entry['child_ages'],
               entry['data_consent_at'],entry['marketing_consent_at'],t,t))
            self.db.execute("""INSERT INTO consent_references(user_id,evidence,recorded_at)
              VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              evidence=excluded.evidence,recorded_at=excluded.recorded_at""",
              (uid,entry['consent_evidence'],t))

    def get(self, user_id: int) -> dict | None:
        row = self.db.execute('SELECT * FROM families WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def begin(self, user_id: int, username: str = '', first_name: str = '', source: str = 'telegram_inbound') -> str:
        existing = self.get(user_id)
        if existing and existing['stopped']:
            return 'Вы ранее остановили общение. Для новой регистрации напишите /возобновить_семью.'
        timestamp = now()
        self.db.execute('''INSERT INTO families(user_id,username,first_name,source,stage,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            first_name=excluded.first_name, stage='adult', adult_confirmed=0,city=NULL,child_ages=NULL,
            data_consent_at=NULL, marketing_consent_at=NULL,marketing_revoked_at=NULL,updated_at=excluded.updated_at''',
            (user_id, username[:50], first_name[:80], source[:70], 'adult', timestamp, timestamp))
        self.db.commit()
        return 'Здравствуйте! Я Анастасия Владимировна, менеджер образовательных проектов Soft Skills. Вы совершеннолетний родитель или законный представитель? Ответьте «да» или «нет».'

    def update(self, user_id: int, **fields):
        allowed = {'stage','adult_confirmed','city','child_ages','data_consent_at',
                   'marketing_consent_at','marketing_revoked_at','stopped'}
        if not set(fields).issubset(allowed):
            raise ValueError('Forbidden fields')
        fields['updated_at'] = now()
        self.db.execute('UPDATE families SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE user_id=?', (*fields.values(),user_id))
        self.db.commit()

    def step(self, user_id: int, text: str, privacy_url: str) -> str:
        rec = self.get(user_id)
        if not rec:
            return 'Для регистрации напишите /семья.'
        stage = rec['stage']
        yesno = answer_kind(text)
        if stage == 'adult':
            if yesno == 'no':
                self.remove(user_id)
                return 'Спасибо! Для регистрации нужен совершеннолетний родитель или законный представитель.'
            if yesno != 'yes':
                return 'Подскажите, вы совершеннолетний родитель или законный представитель? Ответьте «да» или «нет».'
            self.update(user_id, adult_confirmed=1,stage='city')
            return 'Вы проживаете в Александрове или Александровском районе Владимирской области? Ответьте «да» или «нет».'
        if stage == 'city':
            if yesno == 'no':
                self.update(user_id, stage='out_of_area')
                return 'Поняла, сейчас наши приглашения ориентированы на Александров. Спасибо за интерес!'
            if yesno != 'yes':
                return 'Мы уточняем город, чтобы не приглашать на неподходящие мероприятия. Ответьте «да» или «нет».'
            self.update(user_id, city='Александров', stage='age')
            return 'Сколько лет вашему ребёнку или детям? Можно написать, например: 9 или 9,12. Принимаем возраст от 7 до 17 лет. Имена детей не нужны.'
        if stage == 'age':
            ages = parse_ages(text)
            if not ages:
                return 'Укажите только возраст от 7 до 17 лет (например: 10 или 8,13). Если детей подходящего возраста нет — напишите /стоп.'
            self.update(user_id, child_ages=','.join(map(str,ages)), stage='data_consent')
            return ('Для подбора мероприятия мы сохраним ваш Telegram ID, имя из профиля, город и возраст детей, '
                    'чтобы обработать обращение. Организатор — Soft Skills. Данные можно удалить командой /удалить. '
                    f'Политика конфиденциальности: {privacy_url}\n'
                    'Согласны на эту обработку данных? Ответьте «да» или «нет».')
        if stage == 'data_consent':
            if yesno == 'no':
                self.remove(user_id)
                return 'Поняла. Данные регистрации не сохранены. Спасибо!'
            if yesno != 'yes':
                return 'Нужно явное «да» или «нет» на обработку указанных данных.'
            self.update(user_id, data_consent_at=now(),stage='marketing')
            return ('Отдельный вопрос: согласны получать в Telegram персональные приглашения на бесплатные мероприятия '
                    'и занятия Soft Skills? Это добровольно, можно отказаться командой /стоп. Ответьте «да» или «нет».')
        if stage == 'marketing':
            if yesno not in {'yes','no'}:
                return 'Ответьте «да», если хотите получать приглашения, или «нет» — если нет.'
            self.update(user_id, stage='complete', marketing_consent_at=now() if yesno=='yes' else None)
            if yesno == 'yes':
                return 'Спасибо! 🌷 Вы зарегистрированы для получения подходящих приглашений. Обязательно напишу, когда появятся мероприятия для вашего возраста. Отписаться: /стоп. Удалить данные: /удалить.'
            return 'Спасибо! Ваши сведения сохранены только для обработки текущего обращения. Рекламные приглашения отправлять не буду. Удалить данные: /удалить.'
        return 'Регистрация уже завершена. Чтобы обновить возраст или город, напишите /семья. Для отказа от приглашений: /стоп.'

    def stop(self, user_id: int) -> str:
        if self.get(user_id):
            self.update(user_id, stopped=1, marketing_revoked_at=now(), marketing_consent_at=None,stage='stopped')
        return 'Готово. Приглашения остановлены. Удалить данные можно командой /удалить.'

    def remove(self, user_id: int):
        self.db.execute('DELETE FROM families WHERE user_id=?',(user_id,))
        self.db.execute('DELETE FROM consent_references WHERE user_id=?',(user_id,))
        self.db.execute('DELETE FROM radar_sends WHERE user_id=?',(user_id,))
        self.db.commit()

    def can_send(self, rec: dict, campaign: dict, date_now: datetime | None = None) -> tuple[bool,str]:
        date_now = date_now or datetime.now(timezone.utc)
        if rec['stopped'] or rec.get('marketing_revoked_at') or rec['stage'] != 'complete':
            return False,'Неактивная регистрация или стоп-лист'
        if rec['city'] != 'Александров' or not rec['adult_confirmed']:
            return False,'Не подтверждён взрослый родитель из Александрова'
        ages = [int(a) for a in (rec['child_ages'] or '').split(',') if a.isdigit()]
        if not ages or not all(7 <= a <= 17 for a in ages):
            return False,'Нет подходящего подтверждённого возраста'
        if not rec['data_consent_at'] or not rec['marketing_consent_at']:
            return False,'Нет согласия на получение приглашений'
        try:
            ts = datetime.fromisoformat(rec['marketing_consent_at'])
            if ts.tzinfo is None:
                return False,'Дата согласия без часового пояса'
        except (ValueError, TypeError):
            return False,'Некорректная дата согласия'
        if not (timedelta(0) <= date_now - ts <= timedelta(days=90)):
            return False,'Согласие старше 90 дней / некорректная дата'
        if campaign.get('paused', True):
            return False,'Кампания на паузе'
        if self.db.execute('SELECT 1 FROM radar_sends WHERE user_id=? AND campaign_key=?',
                           (rec['user_id'],campaign_key(campaign))).fetchone():
            return False,'Уже приглашали на эту кампанию'
        last = self.db.execute('SELECT MAX(sent_at) FROM radar_sends WHERE user_id=?', (rec['user_id'],)).fetchone()[0]
        if last and date_now - datetime.fromisoformat(last) < timedelta(days=7):
            return False,'Последнее приглашение менее 7 дней назад'
        return True,'ok'

    def candidates(self) -> list[dict]:
        return [dict(r) for r in self.db.execute('''SELECT * FROM families
                WHERE stage='complete' AND stopped=0 AND marketing_consent_at IS NOT NULL
                ORDER BY marketing_consent_at ASC''')]

    def mark_sent(self, user_id: int, campaign: dict):
        self.db.execute('INSERT OR IGNORE INTO radar_sends(user_id,campaign_key,sent_at) VALUES(?,?,?)',
                        (user_id,campaign_key(campaign),now()))
        self.db.commit()

    def sent_today(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM radar_sends WHERE substr(sent_at,1,10)=?', (now()[:10],)).fetchone()[0]

    def summary(self) -> dict:
        row = self.db.execute('''SELECT COUNT(*) AS total,
         SUM(CASE WHEN city='Александров' AND adult_confirmed=1 AND child_ages IS NOT NULL AND data_consent_at IS NOT NULL THEN 1 ELSE 0 END) AS families,
         SUM(CASE WHEN stage='complete' AND stopped=0 AND marketing_consent_at IS NOT NULL THEN 1 ELSE 0 END) AS opted_in,
         SUM(CASE WHEN stopped=1 THEN 1 ELSE 0 END) AS stopped
         FROM families''').fetchone()
        return {'registered':row['total'] or 0,'verified_families':row['families'] or 0,
                'allowed_outreach':row['opted_in'] or 0,'stopped':row['stopped'] or 0,
                'sent_today':self.sent_today()}
