import asyncio
import hashlib
import json

from crm import _config, _post

CONTACT = 'Установлен контакт'
TRIAL = 'Назначено пробное'


def prepare(config, stage):
    base, email, key, branch, source = config
    token = _post(f'{base}/v2api/auth/login', {'email': email, 'api_key': key})['token']
    headers = {'X-ALFACRM-TOKEN': token}
    root = f'{base}/v2api/{int(branch)}'
    for page in range(100):
        items = _post(root + '/lead-status/index', {'page': page}, headers)['items']
        for item in items:
            if item['name'].strip().casefold() == stage.casefold():
                return root, headers, int(item['id'])
        if not items:
            break
    raise ValueError('CRM stage missing: ' + stage)


async def sync(store, uid, contact, display_name, trial):
    name = contact.get('name') or display_name
    if not name:
        return False
    db = store.db
    db.execute('CREATE TABLE IF NOT EXISTS crm_pipeline(uid INTEGER PRIMARY KEY, city TEXT, fingerprint TEXT, stage TEXT)')
    row = db.execute('SELECT * FROM crm_pipeline WHERE uid=?', (uid,)).fetchone()
    city = row['city'] if row else contact.get('city')
    if not city:
        cities = [c for c in ('Александров', 'Ярославль') if _config(c)]
        if len(cities) != 1:
            return False
        city = cities[0]
    config = _config(city)
    if not config:
        return False
    stage = TRIAL if trial or (row and row['stage'] == TRIAL) else CONTACT
    fields = {'name': name[:50], 'note': 'Telegram ID ' + str(uid) + chr(10) + chr(10).join(
        f'{label}: {contact[k]}' for k, label in [('city','Город'), ('course','Курс'), ('child_age','Возраст'), ('wanted_time','Пожелания по времени')] if contact.get(k))}
    if contact.get('phone'):
        fields['phone'] = [contact['phone']]
    fingerprint = hashlib.sha256(json.dumps([fields, stage], sort_keys=True).encode()).hexdigest()
    if row and row['fingerprint'] == fingerprint and contact.get('crm_id'):
        return True
    root, headers, status_id = await asyncio.to_thread(prepare, config, stage)
    fields['lead_status_ids'] = [status_id]
    crm_id = contact.get('crm_id')
    reservation = f'pipeline:create:{uid}'
    if crm_id:
        url = root + f'/customer/update?id={int(crm_id)}'
    else:
        fields.update(is_study=0, legal_type=1, branch_ids=[int(config[3])])
        if config[4]:
            fields['lead_source_id'] = int(config[4])
        url = root + '/customer/create'
        if not store.claim_effect(reservation):
            raise RuntimeError('Previous CRM create needs reconciliation')
    result = await asyncio.to_thread(_post, url, fields, headers)
    if not result.get('success'):
        if not crm_id:
            with db:
                db.execute('DELETE FROM effects WHERE effect_key=?', (reservation,))
        raise RuntimeError('CRM rejected lead')
    confirmed = int(crm_id or result['model']['id'])
    with db:
        store.write_contact(uid, crm_id=confirmed)
        db.execute('INSERT OR REPLACE INTO crm_pipeline VALUES(?,?,?,?)', (uid, city, fingerprint, stage))
        if not crm_id:
            store.write_effect(reservation, 'sent')
    return True
