"""Owned resources and explicit orchestration bindings for one application."""
from functools import partial
from contextlib import ExitStack
from pathlib import Path
from storage import Store
from manager import Manager, commands
from family_radar import FamilyRadar
from sales_learning import SalesLearning
from brain import respond
from crm import create_lead
from lead_delivery import deliver_lead
from director_orders import admin_ids
from campaigns import load_active
import consultation_service
import submission_service
import telegram_adapter
import application_commands
import cli
from app_settings import PERMISSION, is_greeting, safe_test_mode

class Application:
    PERMISSION = PERMISSION
    is_greeting = staticmethod(is_greeting)
    safe_test_mode = staticmethod(safe_test_mode)

    def __init__(self, store, manager, radar, learning):
        self.STORE, self.M, self.RADAR, self.LEARNING = store, manager.bind(store), radar, learning
        self.CHAT_LOCKS, self.PENDING_QUESTIONS = {}, {}
        self.GROUP_REPLIES, self.TESTERS, self.TEST_SESSIONS = set(), set(), {}
        self.respond, self.create_lead, self.deliver_lead = respond, create_lead, deliver_lead
        self.director_admin_ids, self.load_active = admin_ids, load_active
        self.manager_commands = commands
        self._closed = False
        self._handle_dm = partial(consultation_service._handle_dm, self)
        self.maybe_crm = partial(submission_service.maybe_crm, self)
        self.director_reply = partial(telegram_adapter.director_reply, self)
        self.director_message = partial(telegram_adapter.director_message, self)
        self.notice_owner = partial(telegram_adapter.notice_owner, self)
        self.handle_dm = partial(telegram_adapter.handle_dm, self)
        self.handle_groups = partial(telegram_adapter.handle_groups, self)
        self.listen = partial(telegram_adapter.listen, self)
        self.send_optins = partial(application_commands.send_optins, self)
        self.send_radar = partial(application_commands.send_radar, self)
        self.discover = partial(application_commands.discover, self)
        self.retry_crm = partial(application_commands.retry_crm, self)
        self.reset_test = partial(application_commands.reset_test, self)
        self.main = partial(cli.main, self)

    def end_test(self, user_id):
        self.TESTERS.discard(user_id)
        previous = self.TEST_SESSIONS.pop(user_id, None)
        if previous:
            previous[0].db.close()
            previous[1].db.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        with ExitStack() as closing:
            for resource in (self.STORE, self.M, self.RADAR, self.LEARNING):
                closing.callback(resource.db.close)
            for store, manager in self.TEST_SESSIONS.values():
                closing.callback(store.db.close)
                closing.callback(manager.db.close)
        self.TEST_SESSIONS.clear()
        self.CHAT_LOCKS.clear()
        self.PENDING_QUESTIONS.clear()

def create_application(data_dir=None):
    """Only startup calls this factory. Partial construction is also closed."""
    root = Path(data_dir) if data_dir is not None else Path(__file__).parent / 'data'
    with ExitStack() as failed:
        resources = []
        for cls, filename in ((Store, 'softskills.db'), (Manager, 'manager.db'),
                              (FamilyRadar, 'family_radar.db'), (SalesLearning, 'sales_learning.db')):
            resource = cls(str(root / filename))
            failed.callback(resource.db.close)
            resources.append(resource)
        app = Application(*resources)
        failed.pop_all()
        return app
