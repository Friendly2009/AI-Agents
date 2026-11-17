"""Offline reset of previously contacted recipients. Never sends Telegram messages."""
from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime
from storage import Store

def reset(base):
    base=Path(base); data=base/'data'
    if not (data/'softskills.db').exists():
        raise RuntimeError('Не найдена data/softskills.db. Поместите файл в рабочую папку Анастасии.')
    lock=sqlite3.connect(data/'running.lock.db',timeout=0)
    try:
        lock.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError:
        lock.close()
        raise RuntimeError('Сначала остановите Анастасию (Ctrl+C). Сброс не выполнен.')
    try:
        backup=base/'backups'/datetime.now().strftime('before_reset_%Y%m%d_%H%M%S_%f')
        backup.mkdir(parents=True)
        for src in data.glob('*.db'):
            if src.name=='running.lock.db':continue
            with closing(sqlite3.connect(src)) as origin, closing(sqlite3.connect(backup/src.name)) as dest:
                origin.backup(dest)
        store=Store(str(data/'softskills.db'));db=store.db
        db.execute('CREATE TEMP TABLE reset_ids AS SELECT user_id FROM contacts WHERE stopped=0 AND (initial_sent=1 OR user_id IN (SELECT user_id FROM sends))')
        count=db.execute('SELECT COUNT(*) FROM reset_ids').fetchone()[0]
        for alias,name in [('ops','manager.db'),('learning','sales_learning.db')]:
            if (data/name).exists():db.execute(f'ATTACH DATABASE ? AS {alias}',(str(data/name),))
        with db:
            db.execute('INSERT INTO send_audit SELECT * FROM sends WHERE user_id IN (SELECT user_id FROM reset_ids)')
            db.execute('DELETE FROM sends WHERE user_id IN (SELECT user_id FROM reset_ids)')
            db.execute('DELETE FROM turns WHERE user_id IN (SELECT user_id FROM reset_ids)')
            db.execute('DELETE FROM dialogue_state WHERE user_id IN (SELECT user_id FROM reset_ids)')
            db.execute('DELETE FROM inbox WHERE user_id IN (SELECT user_id FROM reset_ids)')
            db.execute('''UPDATE contacts SET city='', ai_consent=0,crm_consent=0,initial_sent=0,name=NULL,phone=NULL,course=NULL,child_age=NULL,wanted_time=NULL,wants_trial=0,pending_notified=0,consultation_notes=NULL,submission_pending=0,ai_notice_shown=0 WHERE user_id IN (SELECT user_id FROM reset_ids)''')
            # Ownership is intentionally retained. Only an explicit dialogue
            # resume may release the manager lock; clearing history must not.
            if (data/'sales_learning.db').exists():
                db.execute('DELETE FROM learning.labels WHERE interaction_id IN (SELECT id FROM learning.interactions WHERE user_id IN (SELECT user_id FROM reset_ids))')
                db.execute('DELETE FROM learning.interactions WHERE user_id IN (SELECT user_id FROM reset_ids)')
        db.close()
        return count,backup
    finally:lock.close()

if __name__=='__main__':
    try:
        count,backup=reset(Path(__file__).resolve().parent)
        print(f'Сброшено контактов: {count}. Резервная копия: {backup}')
        print('Отказы, CRM и брони сохранены. Чаты Telegram не удалены. Отправка не запускалась.')
    except Exception as exc:
        print('Ошибка сброса:',exc)
        raise SystemExit(1)
