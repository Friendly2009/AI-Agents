"""Application submission: consent, durable claim, CRM result and notifications."""
from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from app_settings import safe_test_mode
from conversation_service import ConversationState

LOG = logging.getLogger('softskills')


async def maybe_crm(app, client, user_id):
    if safe_test_mode() or user_id in app.TESTERS:
        return
    contact = app.STORE.get(user_id)
    if not contact or not contact.get('submission_pending') or contact.get('stopped') or not contact.get('wants_trial'):
        return
    if not (contact.get('crm_consent') and contact.get('name') and contact.get('phone') and contact.get('city')):
        return
    status = 'отключена; заявка сохранена локально'
    if contact.get('crm_id'):
        status = 'карточка #' + str(contact['crm_id'])
    elif os.getenv('CRM_ENABLED','false').lower() == 'true':
        from crm import _config
        import hashlib
        crm_key = "crm:" + hashlib.sha256((contact["city"] + ":" + __import__("crm").normalized_phone(contact["phone"])).encode()).hexdigest()
        legacy_key = "crm:" + hashlib.sha256((contact["city"] + ":" + contact["phone"]).encode()).hexdigest()
        if legacy_key != crm_key and app.STORE.db.execute('SELECT 1 FROM effects WHERE effect_key=?',(legacy_key,)).fetchone():
            return  # Preserve pre-migration suppression; recover through read-only reconciliation.
        try:
            configured = _config(contact['city'])
        except ValueError:
            configured = None
        if not configured:
            status = 'не настроена для города; заявка сохранена локально'
        elif not app.STORE.claim_effect(crm_key):
            return  # Another attempt owns delivery; ambiguous attempts need manual reconciliation.
        else:
            try:
                crm_id = await app.create_lead(contact)
                if crm_id is None:
                    raise RuntimeError('CRM configuration changed during submission')
                ConversationState(app.STORE).submitted(user_id, crm_id, crm_key)
                status = 'карточка #' + str(crm_id)
            except (Exception, asyncio.CancelledError) as exc:
                ConversationState(app.STORE).submission_uncertain(user_id, crm_key)
                from funnel import Funnel
                Funnel(app.STORE).metric('crm_error')
                LOG.error('AlfaCRM: %s; результат требует сверки', type(exc).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                status = 'результат неизвестен; нужна ручная сверка, автоповтор отключён'
            else:
                try:
                    app.LEARNING.mark_user(user_id, 'lead')
                except Exception:
                    LOG.exception('Карточка CRM подтверждена, но статистика не обновилась')
    await app.deliver_lead(client, app.STORE, contact, app.director_admin_ids(0), status)
    return {'crm_id': (app.STORE.get(user_id) or {}).get('crm_id'), 'status': status}


