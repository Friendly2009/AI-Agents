"""Explicit administrator-controlled persistent greeting batches."""
import asyncio
import re
import uuid
from delivery import Ledger


def parse_names(text):
    result=[]
    for item in re.split(r'[\s,;]+',text.strip()):
        name=item.removeprefix('https://t.me/').lstrip('@').rstrip('/')
        if not (name.isdigit() and int(name)>0) and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',name):
            raise ValueError('Нужны Telegram ID или @username, по одному в строке.')
        if name.lower() not in [x.lower() for x in result]: result.append(name)
    if not 1<=len(result)<=10: raise ValueError('В списке от 1 до 10 контактов.')
    return result


def greeting(name):
    name=' '.join((name or '').split())
    if len(name)>40 or '@' in name or 'http' in name.lower() or sum(c.isalpha() for c in name)<2: name=''
    for c in '*_`[]': name=name.replace(c,'')
    return ('Добрый день'+(', '+name if name else '')+'! 🙂 Я Анастасия, ассистент Soft Skills в Александрове. Можно коротко рассказать о бесплатном занятии для детей?')

class Invitations:
    def __init__(self,client,store,safe,excluded_ids=(),consent=None):
        self.client,self.store,self.safe=client,store,safe
        self.excluded_ids=set(excluded_ids)
        self.consent=consent
        self.ledger=Ledger(store)
        self.collecting=set();self.drafts={};self.task=None;self.stop=asyncio.Event()
        self.campaign_admins=set()

    def rows(self,uid):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM outbound_operations WHERE batch=? ORDER BY created,id',(self.drafts.get(uid,''),))]

    async def handle(self,event):
        uid=event.sender_id;text=(event.raw_text or '').strip()
        if text == '/очереди':
            rows=self.store.db.execute('SELECT DISTINCT batch FROM outbound_operations WHERE source=? AND batch IS NOT NULL ORDER BY created DESC LIMIT 10',(f'admin:{uid}',)).fetchall()
            await event.reply('\n'.join(r[0] for r in rows) or 'Очередей нет. После перезапуска: /очередь UUID, затем /результаты или /отправить.')
            return True
        if text.startswith('/очередь '):
            batch=text.split(maxsplit=1)[1]
            if self.store.db.execute('SELECT 1 FROM outbound_operations WHERE batch=? AND source=?',(batch,f'admin:{uid}')).fetchone():
                self.drafts[uid]=batch
                await event.reply('Очередь выбрана. /результаты; /отправить — продолжить только ещё не начатые операции.')
            else: await event.reply('Ваша очередь не найдена.')
            return True
        if text.startswith('/разрешение '):
            try:
                _,target,day,source=text.split(maxsplit=3)
                self.ledger.grant(int(target),source,day)
                await event.reply('Разрешение записано. Источник должен ссылаться на реальное согласие получателя. Отправка ещё не разрешена.')
            except ValueError as exc: await event.reply(str(exc))
            return True
        if text in {'/приглашения','/приглашение','подготовить приглашения','/кампания_список'}:
            self.collecting.add(uid)
            if text=='/кампания_список': self.campaign_admins.add(uid)
            else: self.campaign_admins.discard(uid)
            await event.reply('Пришлите до 10 Telegram ID или ников. Сначала проверка и предпросмотр. /исключить ID, /отправить, /остановить, /результаты. Разрешение: /разрешение ID YYYY-MM-DD источник_доказательства.')
            return True
        if text in {'/отменить_список','/остановить'}:
            self.collecting.discard(uid);self.stop.set()
            self.ledger.cancel(self.drafts.get(uid,''))
            await event.reply('Неотправленные операции отменены. Уже начатый запрос может завершиться.');return True
        if text.startswith('/исключить '):
            try:
                target=int(text.split()[1])
                with self.store.db:
                    self.store.db.execute("UPDATE outbound_operations SET state='cancelled' WHERE batch=? AND uid=? AND state IN ('draft','validated','approved')",(self.drafts.get(uid,''),target))
                await event.reply('Адресат исключён из текущего списка.')
            except ValueError: await event.reply('Формат: /исключить Telegram_ID')
            return True
        if text=='/результаты':
            rows=self.rows(uid)
            if not rows:
                rows=[dict(r) for r in self.store.db.execute('SELECT * FROM outbound_operations WHERE source=? ORDER BY created DESC LIMIT 20',(f'admin:{uid}',))]
            await event.reply('\n'.join(f"{r['uid']}: {r['state']} {r['error'] or ''}" for r in rows) or 'Операций пока нет.')
            return True
        if uid in self.collecting and not text.startswith('/'):
            try:
                from telethon import types
                names=parse_names(text);seen=set();preview=[]
                self.ledger.cancel(self.drafts.get(uid,''))
                batch=str(uuid.uuid4());self.drafts[uid]=batch
                for name in names:
                    entity=await self.client.get_entity(int(name) if name.isdigit() else name)
                    if not isinstance(entity,types.User) or entity.bot or entity.deleted or entity.is_self or entity.id in self.excluded_ids:
                        preview.append(name+': исключён');continue
                    if entity.id in seen: continue
                    seen.add(entity.id)
                    msg=greeting(entity.first_name)
                    kind='initial'
                    if uid in self.campaign_admins:
                        from campaigns import compose_first_message
                        msg=compose_first_message(entity.first_name or '')
                        if self.store.ever_contacted(entity.id) or self.store.history(entity.id): kind='campaign'
                    key=batch+':'+str(entity.id)
                    self.ledger.prepare(key,entity.id,kind,f'admin:{uid}',msg,batch)
                    self.ledger.validate(key)
                    row=self.ledger.row(key)
                    preview.append(f"ID {entity.id}: {row['state']} {row['error'] or ''}\n{msg}")
                self.collecting.discard(uid)
                preview_text='\n\n'.join(preview)+'\n\n/отправить — подтвердить текущий список. SAFE TEST блокирует внешнюю доставку.'
                for offset in range(0,len(preview_text),3500):
                    await event.reply(preview_text[offset:offset+3500])
            except Exception as exc:
                self.ledger.cancel(self.drafts.get(uid,''))
                await event.reply('Список отменён: '+type(exc).__name__+'. Проверьте ID и доступность контактов.')
            return True
        if text=='/отправить':
            if self.safe():
                await event.reply('SAFE TEST: внешняя доставка отключена.');return True
            if self.task and not self.task.done():
                await event.reply('Очередь уже выполняется.');return True
            batch=self.rows(uid)
            if not batch:
                await event.reply('Нет подготовленного списка. /приглашения');return True
            for r in batch: self.ledger.approve(r['id'])
            self.stop.clear();self.task=asyncio.create_task(self.run(event,batch))
            await event.reply('Список подтверждён. Результаты: /результаты.');return True
        return False

    async def run(self,event,batch):
        sent=0;skipped=0;error=''
        for row in batch:
            # Legacy callers cannot bypass preparation and explicit approval.
            if not isinstance(row,dict): skipped+=1;continue
            if self.stop.is_set():
                self.ledger.cancel(row['batch']);break
            try:
                uid=row['uid']
                if uid in self.excluded_ids: skipped+=1;continue
                ok=await self.ledger.deliver(row['id'],lambda: self.client.send_message(uid,row['body'],parse_mode=None),safe=self.safe())
                if ok:
                    self.store.ensure(uid,source='Подтверждённый список')
                    ConversationState(self.store).add_turn(uid,'assistant',row['body'])
                    if row['kind']=='initial': self.store.mark_sent(uid,'Подтверждено Telegram')
                    sent+=1
                else: skipped+=1
            except Exception as exc:
                error=type(exc).__name__;break
        try: await event.reply(f'Очередь завершена. Подтверждено: {sent}; пропущено: {skipped}. {error}. /результаты')
        except Exception:
            import logging
            logging.getLogger(__name__).warning('queue_result_notification_failed')

from conversation_service import ConversationState
