"""Operational state: reservations, handoff, deduplication, reminders and usage."""
import json, os, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from runtime_policy import safe_mode
from outgoing import send_once

class Manager:
    def __init__(self, path=None):
        path = path if path is not None else str(Path(__file__).resolve().parent / "data" / "manager.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.store = None
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
        cols={r[1] for r in self.db.execute('PRAGMA table_info(usage)')}
        if 'mode' not in cols:
            self.db.execute("ALTER TABLE usage ADD COLUMN mode TEXT NOT NULL DEFAULT 'legacy'")
        self.db.execute("CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY,model TEXT,mode TEXT,at TEXT)")
        self.db.commit()
    def llm_request(self,model):
        with self.db:
            self.db.execute('INSERT INTO llm_calls(model,mode,at) VALUES(?,?,?)',
                            (model,'test' if safe_mode() else 'live',datetime.now(timezone.utc).isoformat()))
    def claim(self,uid,mid):
        with self.db:
            return self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?)',(uid,mid)).rowcount==1
    def bind(self, store):
        self.store = store
        return self
    def control(self):
        if self.store is None:
            raise RuntimeError('Manager must be bound to an explicit Store')
        return self.store.control
    def handoff(self,uid,active=True):
        control = self.control()
        control.takeover(uid) if active else control.resume(uid)
    def paused(self,uid):
        return self.control().blocked(uid)
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
        if status not in {'pending','confirmed','cancelled','attended','missed'}:
            raise ValueError('Недопустимый статус')
        b=self.booking(bid)
        if status in {'attended','missed'} and b['status'] != 'confirmed':
            raise ValueError('Сначала подтвердите занятие')
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
        if model != os.getenv('OPENAI_MODEL','gpt-5-mini'):
            a=b=''
        try:
            prices=json.loads(os.getenv('API_PRICES_JSON','{}')).get(model)
            if prices: a,b=prices['input'],prices['output']
        except (ValueError,TypeError,KeyError,AttributeError):
            a=b='' 
        try:
            import math
            rates=(float(a),float(b))
            cost=(inp*rates[0]+out*rates[1])/1e6 if all(math.isfinite(v) and v>=0 for v in rates) else None
        except (TypeError,ValueError):
            cost=None
        with self.db:self.db.execute('INSERT INTO usage(model,input_tokens,output_tokens,cost,at,mode) VALUES(?,?,?,?,?,?)',(model,inp,out,cost,datetime.now(timezone.utc).isoformat(),'test' if safe_mode() else 'live'))
    def report(self,store):
        from funnel import Funnel
        u=self.db.execute("SELECT count(*),coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),sum(cost),sum(cost IS NULL) FROM usage WHERE mode='live'").fetchone()
        calls=self.db.execute("SELECT COUNT(*) FROM llm_calls WHERE mode='live'").fetchone()[0]
        complete=bool(calls and calls==u[0] and not u[4])
        cost=f'${u[3]:.4f}' if complete else 'не рассчитано: тарифы или полные usage недоступны'
        n=Funnel(store).counts()['trial_attended']
        per=f'${u[3]/n:.4f}' if complete and n else 'нет достоверных данных'
        return (Funnel(store).report()+f'\nLLM: {calls} запросов, {u[0]} ответов с usage; токены вход/выход: {u[1]}/{u[2]}; стоимость: {cost}. '
                f'API на подтверждённое посещение: {per}. Учтены текстовые запросы консультаций, приглашений и распоряжений; голосовой API и другие расходы исключены.')



def knowledge():
    p=Path(__file__).parent/'knowledge.json'
    return json.loads(p.read_text(encoding='utf-8'))

async def commands(client,event,store,admin=False, *, manager=None, learning=None):
    M = manager
    LEARNING = learning
    text=(event.raw_text or '').strip();uid=event.sender_id
    if not text.startswith('/'):return False
    command,*rest=text.split(maxsplit=1); arg=rest[0] if rest else ''
    admin_cmds={'/воронка','/результат','/оценка','/crm_сверка','/пропуск','/отчёт','/слот','/изменить_слот','/закрыть_слот','/подтвердить_бронь','/посещение','/отменить_бронь','/вернуть_ии','/брони','/менеджер_помощь'}
    if command in admin_cmds and not admin:return False
    try:
        if command=='/менеджер_помощь':
            answer='/воронка · /результат TelegramID trial_attended ID_занятия · /crm_сверка TelegramID · /оценка ID_ответа 1–5 · /пропуск ID_брони\n/слот Город | Курс | 2026-12-01T15:00:00+03:00 | 8\n/расписание\n/закрыть_слот ID\n/брони\n/подтвердить_бронь ID\n/отменить_бронь ID\n/вернуть_ии TelegramID\n/отчёт\nКлиенту: /бронь ID_слота, /отмена ID_брони, /напомнить ID_брони'
        elif command in {'/отчёт','/воронка'}:
            from agent_config import require_admin
            require_admin(uid)
            answer=M.report(store)
        elif command=='/результат':
            from funnel import Funnel
            from agent_config import require_admin
            require_admin(uid)
            if safe_mode():
                answer='SAFE TEST: рабочие результаты не изменены.'
            else:
                target,outcome,reference=arg.split(maxsplit=2)
                Funnel(store).confirm(int(target),outcome,uid,reference)
                if outcome in {'trial_attended','trial_missed'}:
                    LEARNING.mark_user(int(target),'attended' if outcome=='trial_attended' else 'missed',uid)
                answer='Результат подтверждён сотрудником; источник и ID подтверждения сохранены.'
        elif command=='/оценка':
            from agent_config import require_admin
            from funnel import Funnel
            require_admin(uid)
            interaction,score=map(int,arg.split())
            if not 1<=score<=5 or not LEARNING.db.execute('SELECT 1 FROM interactions WHERE id=?',(interaction,)).fetchone():
                raise ValueError('Нужны ID ответа и оценка 1–5')
            with store.db:
                store.db.execute('CREATE TABLE IF NOT EXISTS response_reviews(interaction INTEGER,actor INTEGER,score INTEGER,PRIMARY KEY(interaction,actor))')
                if not safe_mode():
                    store.db.execute('INSERT INTO response_reviews VALUES(?,?,?) ON CONFLICT(interaction,actor) DO UPDATE SET score=excluded.score',(interaction,uid,score))
            answer='Оценка сохранена.' if not safe_mode() else 'SAFE TEST: оценка не сохранена.'
        elif command=='/crm_сверка':
            from agent_config import require_admin
            require_admin(uid)
            from crm import reconcile_lead
            target=int(arg); contact=store.get(target)
            if not contact: raise ValueError('Клиент не найден')
            result=await reconcile_lead(contact)
            if result:
                from funnel import Funnel
                with store.db:
                    store.db.execute('UPDATE contacts SET crm_id=?,submission_pending=0 WHERE user_id=?',(result,target))
                    Funnel(store).emit(target,'lead_created',reliability='crm',confirmation='alfacrm_reconciliation',external_id=result)
                store.set_dialogue(target,application_status='submitted')
                answer='Карточка найдена и связана локально. Повторное создание и уведомления не выполнялись.'
            else:
                answer='Карточка не найдена или SAFE TEST. Новая карточка не создавалась; требуется ручная проверка.'
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
                from funnel import Funnel
                with store.db:
                    Funnel(store).emit(uid,'trial_requested',reliability='intent',confirmation='booking_command')
                bid=M.book(uid,int(arg)); b=M.booking(bid); ConversationState(store).update(uid,wants_trial=1,city=b['city'],course=b['course'],wanted_time=b['starts'])
                answer=f'Предварительная бронь #{bid}. Место в локальном расписании зарезервировано, менеджер подтвердит запись. Напоминание по желанию: /напомнить {bid}. Отмена: /отмена {bid}.'
                for aid in __import__('director_orders').admin_ids(0):
                    if safe_mode(): continue
                    try:await client.send_message(aid,f'Новая бронь #{bid}, клиент ID {uid}. /брони')
                    except Exception:pass
        elif command=='/согласие_на_запись':
            store.ensure(uid);ConversationState(store).update(uid,crm_consent=1);answer='Согласие на обработку данных для записи и передачу заявки в CRM Soft Skills сохранено.'
        elif command=='/отмена':M.set_booking(int(arg),'cancelled',uid);answer='Бронь отменена, место освобождено.'
        elif command=='/напомнить':M.remind(int(arg),uid);answer='Отправим одно напоминание в течение суток перед подтверждённым занятием. Отмена уведомлений: /стоп.'
        elif command in {'/подтвердить_бронь','/отменить_бронь','/посещение','/пропуск'}:
            status={'/подтвердить_бронь':'confirmed','/отменить_бронь':'cancelled','/посещение':'attended','/пропуск':'missed'}[command]
            if safe_mode():
                await event.reply('SAFE TEST: изменение реальной брони и уведомление отключены.')
                return True
            from agent_config import require_admin
            require_admin(uid)
            M.set_booking(int(arg),status);b=M.booking(int(arg))
            from funnel import Funnel
            store.ensure(b['uid'])
            if status in {'confirmed','attended','missed'}:
                Funnel(store).confirm(b['uid'],{'confirmed':'trial_confirmed','attended':'trial_attended','missed':'trial_missed'}[status],uid,'booking:'+str(b['id']))
            store.ensure(b['uid'])
            store.set_dialogue(b['uid'], stage={'confirmed':'visit_confirmed','cancelled':'declined','attended':'attended','missed':'declined'}[status], application_status=status)
            if status in {'attended','missed'}:
                LEARNING.mark_user(b['uid'], status, uid)
            await send_once(store, f"booking:{b['id']}:{status}", b['uid'],
                lambda: client.send_message(b['uid'],f"Бронь #{arg}: { {'confirmed':'подтверждена менеджером','cancelled':'отменена менеджером','attended':'посещение подтверждено сотрудником','missed':'пропуск подтверждён сотрудником'}[status] }. {b['course']}",parse_mode=None))
            answer='Статус обновлён локально. Это не запись занятия в AlfaCRM.'
        elif command=='/брони':answer='\n'.join(str(dict(r)) for r in M.db.execute('SELECT * FROM bookings ORDER BY id DESC LIMIT 25')) or 'Броней нет'
        elif command=='/вернуть_ии':
            store.control.resume(int(arg), f'telegram:{uid}:{event.id}')
            answer='Автоответы для этого клиента возобновлены.'
        else:return False
        await event.reply(answer[:3900]);return True
    except (ValueError,TypeError,PermissionError):
        await event.reply('Проверьте параметры команды. Для руководителя: /менеджер_помощь');return True

from conversation_service import ConversationState
