"""Local conversation decisions. Every method commits before returning to async code."""

class ConversationState:
    def __init__(self, store):
        self.store = store

    def update(self, user_id, **fields):
        from funnel import Funnel
        if not fields:
            return
        with self.store.transaction():
            before = self.store.get(user_id) or {}
            for field, event in [('ai_consent','contact_authorized'), ('stopped','client_declined')]:
                if fields.get(field) and not before.get(field):
                    Funnel(self.store).emit(user_id, event, confirmation='explicit_client_action')
            self.store.write_contact(user_id, **fields)

    def add_turn(self, user_id: int, role: str, content: str):
        from dialogue_state import after_reply
        with self.store.transaction():
            self.store.append_turn(user_id, role, content)
            if role == 'assistant':
                self.store._save_dialogue(user_id, after_reply(self.store.dialogue(user_id), content))
                if user_id in self.store._inbound:
                    self.store.mark_answered(user_id, self.store._inbound[user_id])


    def claim_inbound(self, user_id, message_id, text):
        from funnel import Funnel
        with self.store.transaction():
            inserted = self.store.insert_inbound(user_id,message_id,text)
            if inserted:
                f = Funnel(self.store)
                f.emit(user_id,'conversation_started',confirmation='telegram_inbound')
                if self.store.has_assistant_turn(user_id):
                    f.emit(user_id,'client_replied',confirmation='telegram_inbound')
                self.store.journal_inbound(user_id)
        if inserted:
            self.store._inbound[user_id] = message_id
        return bool(inserted)

    def observe(self, user_id, text):
        from dialogue_state import reduce_user
        # Facts, evidence and state commit together, before any model/network call.
        with self.store.transaction():
            state, updates = reduce_user(self.store.dialogue(user_id), text, self.store.get(user_id) or {})
            if updates:
                self.store.write_contact(user_id, **updates)
            self.store._save_dialogue(user_id, state)
            from funnel import Funnel
            from objections import classify
            f = Funnel(self.store)
            contact = self.store.get(user_id) or {}
            if contact.get('name') and contact.get('city') and contact.get('child_age'):
                f.emit(user_id,'qualified',reliability='intent',confirmation='user_text_rules')
            if contact.get('wants_trial'):
                f.emit(user_id,'trial_interested',reliability='intent',confirmation='user_text_rules')
            if state.get('declined'):
                f.emit(user_id,'client_declined',reliability='intent',confirmation='user_text_rules',
                       details={'reason': classify(text) or 'unspecified'})
        return state


    def recording_consent(self, user_id, decision):
        with self.store.transaction():
            if decision:
                from funnel import Funnel
                Funnel(self.store).emit(user_id,'trial_requested',reliability='intent',confirmation='explicit_recording_consent')
            self.store.write_contact(user_id, crm_consent=int(decision), submission_pending=int(decision))
            state = self.store.dialogue(user_id)
            state.update(application_status='pending' if decision else 'consent_refused',
                         expected_action='submit' if decision else None)
            self.store._save_dialogue(user_id, state)


    def submitted(self, user_id, crm_id, effect_key):
        with self.store.transaction():
            from funnel import Funnel
            Funnel(self.store).emit(user_id,'lead_created',reliability='crm',confirmation='alfacrm_customer',external_id=crm_id)
            self.store.write_contact(user_id, crm_id=crm_id, submission_pending=0)
            self.store.write_effect(effect_key, 'sent')
            state = self.store.dialogue(user_id)
            state.update(stage='lead_submitted', application_status='submitted')
            self.store._save_dialogue(user_id, state)



    def submission_uncertain(self, user_id, effect_key):
        with self.store.transaction():
            self.store.write_effect(effect_key, 'uncertain')
            state = self.store.dialogue(user_id)
            state['application_status'] = 'uncertain'
            self.store._save_dialogue(user_id,state)
