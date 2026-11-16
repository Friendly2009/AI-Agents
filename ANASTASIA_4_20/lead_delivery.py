"""Send confirmed lead summaries independently to both directors."""
import re
import logging


def lead_summary(contact, status):
    username=(contact.get('username') or '').lstrip('@')
    telegram=('https://t.me/'+username if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username) else 'Username не указан; Telegram ID: '+str(contact['user_id']))
    return ('📝 Заявка на пробное\n'+telegram+'\n'+
            '\n'.join(f'{label}: {contact.get(key) or "не указано"}' for key,label in (
                ('name','Родитель'),('phone','Телефон'),('child_age','Возраст ребёнка'),
                ('city','Город'),('course','Направление'),('wanted_time','Желаемое время')))+
            '\nCRM: '+status+'\nВремя занятия требует подтверждения менеджером.')


async def deliver_lead(client, store, contact, recipients, status):
    store.db.execute('CREATE TABLE IF NOT EXISTS lead_notices (uid INTEGER, recipient INTEGER, state TEXT, PRIMARY KEY(uid,recipient))')
    store.db.commit()
    for recipient in recipients:
        # Reserve before network: do not duplicate an ambiguous delivery on restart.
        with store.db:
            inserted=store.db.execute('INSERT OR IGNORE INTO lead_notices VALUES(?,?,?)',
                                     (contact['user_id'],recipient,'sending')).rowcount
        if not inserted:
            continue
        try:
            await client.send_message(recipient,lead_summary(contact,status),parse_mode=None,link_preview=False)
            state='sent'
        except Exception as exc:
            state='uncertain'
            logging.getLogger('softskills').error('Заявка %s: уведомление директору %s не подтверждено (%s)',contact['user_id'],recipient,type(exc).__name__)
        with store.db:
            store.db.execute('UPDATE lead_notices SET state=? WHERE uid=? AND recipient=?',(state,contact['user_id'],recipient))
