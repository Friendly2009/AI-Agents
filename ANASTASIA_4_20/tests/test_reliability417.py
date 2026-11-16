import unittest
from unittest.mock import AsyncMock
from policy import is_stop
from handoff_flow import requests_handoff, transfer_to_managers
from manager import Manager
class Reliability(unittest.IsolatedAsyncioTestCase):
    def test_questions(self):
        for t in ['Мне интересно узнать сколько они стоят?', 'Мне интересно', 'Ребёнку не интересно программирование, а рисовать любит', 'А если ему не понравится?', 'Сколько стоит?']:
            self.assertFalse(is_stop(t), t)
    def test_stop(self):
        for t in ['Стоп, пожалуйста', 'Не пишите мне', 'Нет, спасибо, не интересно', 'Не актуально.', 'Отписаться']:
            self.assertTrue(is_stop(t),t)
    def test_context(self):
        self.assertTrue(requests_handoff('Да',[{'role':'assistant','content':'Хотите, чтобы я связала вас с менеджером прямо сейчас?'}]))
        self.assertFalse(requests_handoff('Да',[{'role':'assistant','content':'Хотите узнать стоимость?'}]))
    async def test_delivery(self):
        m=Manager(':memory:');n=AsyncMock(return_value=2)
        r=await transfer_to_managers(None,n,m,1,{'username':'parent_test'},'Да',[{'role':'user','content':'Можно родителю?'}])
        self.assertTrue(m.paused(1));self.assertIn('Передала',r)
        self.assertIn('https://t.me/parent_test',n.call_args.args[1]);self.assertIn('Можно родителю?',n.call_args.args[1])
    async def test_failure(self):
        for n in [AsyncMock(return_value=0),AsyncMock(side_effect=RuntimeError('offline'))]:
            m=Manager(':memory:');r=await transfer_to_managers(None,n,m,1,{},'Да',[])
            self.assertFalse(m.paused(1));self.assertIn('не передан',r)
    async def test_sandbox(self):
        m=Manager(':memory:');n=AsyncMock()
        r=await transfer_to_managers(None,n,m,1,{},'Да',[],True)
        self.assertFalse(m.paused(1));n.assert_not_called();self.assertIn('Тест:',r)
