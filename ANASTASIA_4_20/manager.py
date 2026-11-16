"""Operational state: reservations, handoff, deduplication, reminders and usage."""
import json, os, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from outgoing import safe_mode, send_once

class Manager:
    def __init__(self, path=None):
        path = path if path is not None else str(Path(__file__).resolve().parent / "data" / "manager.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS messages(uid INTEGER, mid INTEGER, PRIMARY KEY(uid,mid));
        CREATE TABLE IF NOT EXISTS handoffs(uid INTEGER PRIMARY KEY, active INTEGER);
        CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY, city TEXT, course TEXT, starts TEXT, capacity INTEGER, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, uid INTEGER, slot INTEGER, status TEXT, reminder INTEGER DEFAULT 0, sent INTEGER DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_booking ON bookings(uid,slot) WHERE status != 'cancelled';
        CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost REAL, at TEXT);
        ''')
        self.db.commit()
    def claim(self,uid,mid):
        with self.db:
            return self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?)',(uid,mid)).rowcount==1
    def handoff(self,uid,active=True):
        with self.db:self.db.execute('INSERT OR REPLACE INTO handoffs VALUES(?,?)',(uid,int(active)))
    def paused(self,uid):
        row=self.db.execute('SELECT active FROM handoffs WHERE uid=?',(uid,)).fetchone()
        return bool(row and row[0])
    def add_slot(self,city,course,starts,capacity):
        dt=datetime.fromisoformat(starts)
        if city not in ('Александров','Ярославль') or not course.strip() or dt.tzinfo is None or dt<=datetime.now(timezone.utc) or not 1<=capacity<=1000:
            raise ValueError('Нужны город, курс, будущая дата с +03:00 и 1–1000 мест')
        with self.db:
            return self.db.execute('INSERT INTO slots(city,course,starts,capacity) VALUES(?,?,?,?)',(city,course,dt.astimezone(timezone.utc).isoformat(),capacity)).lastrowid
    def slots(self):
        rows=self.db.execute("SELECT s.*, s.capacity-(SELECT count(*) FROM bookings b WHERE b.slot=s.id AND b.status!='cancelled') AS free FROM slots s WHERE active=1 AND starts>? ORDER BY starts",(datetime.now(timezone.utc).isoformat(),)).fetchall()
        return [dict(r) for r in rows]
    def book(self,uid,sid):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old=self.db.execute("SELECT id FROM bookings WHERE uid=? AND slot=? AND status!='cancelled'",(uid,sid)).fetchone()
            if old:return old[0]
            slot=next((s for s in self.slots() if s['id']==sid),None)
            if not slot or slot['free']<=0:raise ValueError('Свободных мест нет или событие недоступно')
            return self.db.execute("INSERT INTO bookings(uid,slot,status) VALUES(?,?,'pending')",(uid,sid)).lastrowid
    def booking(self,bid):
        r=self.db.execute('SELECT b.*,s.starts,s.course,s.city FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.id=?',(bid,)).fetchone()
        if not r:raise ValueError('Бронь не найдена')
        return dict(r)
    def set_booking(self,bid,status,uid=None):
        b=self.booking(bid)
        if uid is not None and b['uid']!=uid:raise ValueError('Это не ваша бронь')
        if b['status']=='cancelled' and status!='cancelled':raise ValueError('Создайте новую бронь')
        with self.db:self.db.execute('UPDATE bookings SET status=? WHERE id=?',(status,bid))
    def remind(self,bid,uid):
        b=self.booking(bid)
        if b['uid']!=uid or b['status']=='cancelled':raise ValueError('Бронь недоступна')
        with self.db:self.db.execute('UPDATE bookings SET reminder=1 WHERE id=?',(bid,))
    def due(self):
        now=datetime.now(timezone.utc)
        return [dict(r) for r in self.db.execute("SELECT b.*,s.starts,s.course FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.status='confirmed' AND b.reminder=1 AND b.sent=0 AND s.active=1 AND s.starts>? AND s.starts<=?",(now.isoformat(),(now+timedelta(hours=24)).isoformat()))]
    def erase(self,uid):
        with self.db:
            self.db.execute('DELETE FROM bookings WHERE uid=?',(uid,))
            self.db.execute('DELETE FROM handoffs WHERE uid=?',(uid,))
    def usage(self,model,inp,out):
        a=os.getenv('API_INPUT_USD_PER_MILLION','');b=os.getenv('API_OUTPUT_USD_PER_MILLION','')
        cost=(inp*float(a)+out*float(b))/1e6 if a and b else None
        with self.db:self.db.execute('INSERT INTO usage(model,input_tokens,output_tokens,cost,at) VALUES(?,?,?,?,?)',(model,inp,out,cost,datetime.now(timezone.utc).isoformat()))
    def report(self,store):
        n=store.db.execute('SELECT count(*) FROM contacts').fetchone()[0]
        leads=store.db.execute('SELECT count(*) FROM contacts WHERE wants_trial=1').fetchone()[0]
        crm=store.db.execute('SELECT count(*) FROM contacts WHERE crm_id IS NOT NULL').fetchone()[0]
        bookings=self.db.execute("SELECT count(*) FROM bookings WHERE status='confirmed'").fetchone()[0]
        u=self.db.execute('SELECT count(*),coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),sum(cost),sum(cost IS NULL) FROM usage').fetchone()
        cost=f'${u[3]:.4f}' if u[0] and not u[4] else 'не рассчитано: заполните тарифы API'
        return f'За всё время (включая тесты):\nКонтактов: {n}; заявок: {leads}; лидов CRM: {crm}; подтверждённых броней: {bookings}\nКонтакт → заявка: {leads/n*100 if n else 0:.1f}%\nGPT: {u[0]} вызовов; токены {u[1]} / {u[2]}; оценка стоимости: {cost}. Голосовой API в сумму не включён.'

M=Manager()

def knowledge():
    p=Path(__file__).parent/'knowledge.json'
    return json.loads(p.read_text(encoding='utf-8'))

async def commands(client,event,store,admin=False):
    text=(event.raw_text or '').strip();uid=event.sender_id
    if not text.startswith('/'):return False
    command,*rest=text.split(maxsplit=1); arg=rest[0] if rest else ''
    admin_cmds={'/отчёт','/слот','/изменить_слот','/закрыть_слот','/подтвердить_бронь','/отменить_бронь','/вернуть_ии','/брони','/менеджер_помощь'}
    if command in admin_cmds and not admin:return False
    try:
        if command=='/менеджер_помощь':
            answer='/слот Город | Курс | 2026-12-01T15:00:00+03:00 | 8\n/расписание\n/закрыть_слот ID\n/брони\n/подтвердить_бронь ID\n/отменить_бронь ID\n/вернуть_ии TelegramID\n/отчёт\nКлиенту: /бронь ID_слота, /отмена ID_брони, /напомнить ID_брони'
        elif command=='/отчёт':answer=M.report(store)
        elif command=='/слот':
            city,course,starts,cap=[s.strip() for s in arg.split('|')]
            answer=f'Слот #{M.add_slot(city,course,starts,int(cap))} создан в локальном расписании.'
        elif command=='/изменить_слот':
            sid,city,course,starts,cap=[v.strip() for v in arg.split('|')]
            current=M.db.execute('SELECT id FROM slots WHERE id=?',(int(sid),)).fetchone()
            if not current:raise ValueError('Слот не найден')
            occupied=M.db.execute("SELECT count(*) FROM bookings WHERE slot=? AND status!='cancelled'",(int(sid),)).fetchone()[0]
            if int(cap)<occupied:raise ValueError('Вместимость меньше числа броней')
            # Validate through the same constructor and atomically replace the slot values.
            new=M.add_slot(city,course,starts,int(cap))
            with M.db:
                row=M.db.execute('SELECT * FROM slots WHERE id=?',(new,)).fetchone()
                M.db.execute('UPDATE slots SET city=?,course=?,starts=?,capacity=? WHERE id=?',(city,course,row['starts'],int(cap),int(sid)))
                M.db.execute('DELETE FROM slots WHERE id=?',(new,))
            answer='Слот изменён. Уведомите записанных людей о переносе лично; /брони.'
        elif command=='/закрыть_слот':
            sid=int(arg)
            with M.db:
                M.db.execute('UPDATE slots SET active=0 WHERE id=?',(sid,))
                M.db.execute("UPDATE bookings SET status='cancelled' WHERE slot=?",(sid,))
            answer='Слот закрыт, брони отменены. Уведомите записанных людей лично; контакты доступны через /брони.'
        elif command=='/расписание':
            answer='\n'.join(f"#{s['id']} {s['city']} · {s['course']} · {datetime.fromisoformat(s['starts']).astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')} МСК · мест {s['free']}" for s in M.slots()) or 'Подтверждённого расписания пока нет. Время уточнит менеджер.'
        elif command=='/бронь':
            contact=store.get(uid) or {}
            if not contact.get('crm_consent'):answer='Для сохранения брони напишите /согласие_на_запись, затем повторите /бронь. Это согласие на обработку данных для записи и передачу заявки в CRM Soft Skills.'
            else:
                bid=M.book(uid,int(arg)); b=M.booking(bid); store.update(uid,wants_trial=1,city=b['city'],course=b['course'],wanted_time=b['starts'])
                answer=f'Предварительная бронь #{bid}. Место в локальном расписании зарезервировано, менеджер подтвердит запись. Напоминание по желанию: /напомнить {bid}. Отмена: /отмена {bid}.'
                for aid in __import__('director_orders').admin_ids(0):
                    try:await client.send_message(aid,f'Новая бронь #{bid}, клиент ID {uid}. /брони')
                    except Exception:pass
        elif command=='/согласие_на_запись':
            store.ensure(uid);store.update(uid,crm_consent=1);answer='Согласие на обработку данных для записи и передачу заявки в CRM Soft Skills сохранено.'
        elif command=='/отмена':M.set_booking(int(arg),'cancelled',uid);answer='Бронь отменена, место освобождено.'
        elif command=='/напомнить':M.remind(int(arg),uid);answer='Отправим одно напоминание в течение суток перед подтверждённым занятием. Отмена уведомлений: /стоп.'
        elif command in {'/подтвердить_бронь','/отменить_бронь'}:
            status='confirmed' if command=='/подтвердить_бронь' else 'cancelled'
            if safe_mode():
                await event.reply('SAFE TEST: изменение реальной брони и уведомление отключены.')
                return True
            M.set_booking(int(arg),status);b=M.booking(int(arg))
            await send_once(store, f"booking:{b['id']}:{status}", b['uid'],
                lambda: client.send_message(b['uid'],f"Бронь #{arg}: {'подтверждена менеджером' if status=='confirmed' else 'отменена менеджером'}. {b['course']}",parse_mode=None))
            answer='Статус обновлён локально. Это не запись занятия в AlfaCRM.'
        elif command=='/брони':answer='\n'.join(str(dict(r)) for r in M.db.execute('SELECT * FROM bookings ORDER BY id DESC LIMIT 25')) or 'Броней нет'
        elif command=='/вернуть_ии':M.handoff(int(arg),False);answer='Автоответы для этого клиента возобновлены.'
        else:return False
        await event.reply(answer[:3900]);return True
    except (ValueError,TypeError):
        await event.reply('Проверьте параметры команды. Для руководителя: /менеджер_помощь');return True
