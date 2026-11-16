"""Explicit director-controlled greeting batches; no automatic follow-ups."""
import asyncio
import re


def parse_names(text):
    result=[]
    for item in re.split(r'[\s,;]+', text.strip()):
        name=item.removeprefix('https://t.me/').lstrip('@').rstrip('/')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',name):
            raise ValueError('Пришлите только @ники или ссылки https://t.me/username, по одному в строке.')
        if name.lower() not in [x.lower() for x in result]: result.append(name)
    if not 1 <= len(result) <= 10: raise ValueError('В одном списке от 1 до 10 контактов.')
    return result


def greeting(name):
    name=' '.join((name or '').split())
    if len(name)>40 or '@' in name or 'http' in name.lower() or sum(c.isalpha() for c in name)<2:
        name=''
    name=name.replace('*','').replace('_','').replace('`','').replace('[','').replace(']','')
    return ('Добрый день'+(', '+name if name else '')+'! 🙂 Я Анастасия, ассистент Soft Skills в Александрове. Можно коротко рассказать о бесплатном занятии для детей?')

class Invitations:
    def __init__(self, client, store, safe, excluded_ids=()):
        self.client,self.store,self.safe=client,store,safe
        self.excluded_ids=set(excluded_ids)
        self.collecting=set();self.drafts={};self.task=None;self.stop=asyncio.Event()
    async def handle(self,event):
        uid=event.sender_id;text=(event.raw_text or '').strip()
        if text in {'/приглашения','/приглашение','подготовить приглашения'}:
            self.collecting.add(uid)
            await event.reply('Пришлите до 10 ников, по одному в строке. Сначала покажу приветствия, отправка — только по /отправить. /отменить_список — отмена.')
            return True
        if text=='/отменить_список':
            self.collecting.discard(uid);self.drafts.pop(uid,None)
            await event.reply('Список отменён.');return True
        if text=='/остановить':
            self.stop.set();await event.reply('Очередь остановлена; уже выполняющийся запрос может завершиться.');return True
        if uid in self.collecting and not text.startswith('/'):
            try:
                from telethon import types
                names=parse_names(text)
                targets=[];seen=set();preview=[]
                for name in names:
                    entity=await self.client.get_entity(name)
                    if not isinstance(entity,types.User) or entity.bot or entity.deleted or entity.is_self or entity.id in self.excluded_ids:
                        preview.append('@'+name+': пропуск');continue
                    old=self.store.get(entity.id) or {}
                    if entity.id in seen or old.get('stopped') or self.store.was_sent(entity.id) or self.store.history(entity.id):
                        preview.append('@'+name+': уже общались / исключён');continue
                    seen.add(entity.id);msg=greeting(entity.first_name)
                    targets.append((entity,msg,name));preview.append('@'+name+': '+msg)
                self.drafts[uid]=targets;self.collecting.discard(uid)
                await event.reply('\n\n'.join(preview)+'\n\nК отправке: '+str(len(targets))+'. /отправить — запуск. В SAFE TEST отправки заблокированы.')
            except Exception as exc:
                await event.reply('Не удалось подготовить список: '+(str(exc) if isinstance(exc,ValueError) else type(exc).__name__)+'. Исправьте список и пришлите заново.')
            return True
        if text=='/отправить':
            if self.safe():
                await event.reply('SAFE TEST: отправка людям отключена. Предпросмотр доступен.');return True
            if self.task and not self.task.done():
                await event.reply('Очередь уже работает. /остановить — остановка.');return True
            batch=self.drafts.pop(uid,[])
            if not batch:
                await event.reply('Нет подготовленного списка. /приглашения');return True
            self.stop.clear();self.task=asyncio.create_task(self.run(event,batch))
            await event.reply('Очередь запущена. Повторных приветствий без ответа не будет.');return True
        return False
    async def run(self,event,batch):
        sent=0;skipped=0;error=''
        for entity,msg,name in batch:
            if self.stop.is_set(): break
            if self.store.sent_today()>=10:
                error='Достигнут внутренний дневной предел 10 отправок (это не гарантия отсутствия ограничений Telegram).';break
            old=self.store.get(entity.id) or {}
            if entity.id in self.excluded_ids or old.get('stopped') or self.store.was_sent(entity.id) or self.store.history(entity.id):
                skipped+=1;continue
            try:
                self.store.ensure(entity.id,username=name,source='Список директора: приветствие')
                # Reserve before network call: ambiguous delivery must not retry automatically.
                self.store.mark_sent(entity.id,'Список директора: попытка приветствия')
                await self.client.send_message(entity,msg)
                self.store.add_turn(entity.id,'assistant',msg);sent+=1
            except Exception as exc:
                error='Отправка остановлена: '+type(exc).__name__+'. Последняя доставка может быть неопределённой; автоповтора нет.';break
            if sent<len(batch):
                try: await asyncio.wait_for(self.stop.wait(),timeout=60)
                except asyncio.TimeoutError: pass
        try: await event.reply(f'Очередь завершена. Отправлено: {sent}; пропущено: {skipped}. '+error)
        except Exception: pass
