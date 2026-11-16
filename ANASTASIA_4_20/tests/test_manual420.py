import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from storage import Store
from manager import Manager
from manual_dialogue import remember_opening, recover_opening
class ManualTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s=Store(':memory:');self.m=Manager(':memory:')
    def test_no_username(self):
        self.assertTrue(remember_opening(self.s,self.m,42,'Добрый день! Можно рассказать о занятии?'))
        self.assertEqual(self.s.get(42)['username'],'')
        self.assertEqual(self.s.get(42)['ai_notice_shown'],1)
        self.assertEqual(len(self.s.history(42)),1)
        self.assertTrue(self.s.was_sent(42))
        self.assertFalse(remember_opening(self.s,self.m,42,'Добрый день!'))
    def test_guards(self):
        for kw in [{'safe':True},{'bot':True},{'excluded':{42}}]:
            self.assertFalse(remember_opening(self.s,self.m,42,'Привет',**kw))
        self.assertIsNone(self.s.get(42))
        self.s.ensure(42);self.s.update(42,stopped=1)
        self.assertFalse(remember_opening(self.s,self.m,42,'Привет'))
        self.s.update(42,stopped=0);self.m.handoff(42)
        self.assertFalse(remember_opening(self.s,self.m,42,'Привет'))
    async def test_recovery(self):
        e=SimpleNamespace(sender_id=42,chat_id=42,id=15,get_sender=AsyncMock(return_value=SimpleNamespace(bot=False,deleted=False,username=None)))
        c=SimpleNamespace(get_messages=AsyncMock(return_value=[SimpleNamespace(out=True,message='Вас интересует ИИ?')]))
        self.assertTrue(await recover_opening(c,e,self.s,self.m))
        c.get_messages.assert_awaited_once_with(42,limit=1,max_id=15)
        self.assertEqual(self.s.history(42)[0]['content'],'Вас интересует ИИ?')
    async def test_reset_not_reimported(self):
        self.s.ensure(42)
        c=SimpleNamespace(get_messages=AsyncMock())
        e=SimpleNamespace(sender_id=42)
        self.assertFalse(await recover_opening(c,e,self.s,self.m));c.get_messages.assert_not_called()
    async def test_incoming_is_not_manual_start(self):
        e=SimpleNamespace(sender_id=42,chat_id=42,id=15,get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        c=SimpleNamespace(get_messages=AsyncMock(return_value=[SimpleNamespace(out=False,message='Привет')]))
        self.assertFalse(await recover_opening(c,e,self.s,self.m))
        self.assertIsNone(self.s.get(42))
