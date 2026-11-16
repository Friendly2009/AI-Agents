import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from storage import Store
from reset_outreach import reset
from sales_learning import SalesLearning
class ResetTests(unittest.TestCase):
    def test_reset_preserves_optout_and_daily_limit(self):
        with TemporaryDirectory() as d:
            s=Store(str(Path(d)/'data'/'softskills.db'))
            for uid in (1,2):
                s.ensure(uid);s.mark_sent(uid,'test');s.add_turn(uid,'user','hello')
            s.update(1,child_age=10,crm_id=7);s.update(2,stopped=1)
            n,b=reset(d)
            self.assertEqual(n,1);self.assertTrue((b/'softskills.db').exists())
            self.assertFalse(s.was_sent(1));self.assertTrue(s.was_sent(2))
            self.assertEqual(s.sent_today(),2);self.assertEqual(s.history(1),[])
            self.assertEqual(s.get(1)['crm_id'],7);self.assertIsNone(s.get(1)['child_age'])
            self.assertEqual(s.get(2)['stopped'],1)
            self.assertEqual(reset(d)[0],0)
    def test_learning_records_selected_strategy(self):
        l=SalesLearning(':memory:')
        n=l.record(1,'Игры',{'trial_decision':'yes'},'ok',selection=('open','choice_light'))
        r=l.db.execute('SELECT bucket,strategy,outcome FROM interactions WHERE id=?',(n,)).fetchone()
        self.assertEqual(tuple(r),('open','choice_light','pending'))
        l.mark_engaged(1,'Вы давите')
        self.assertEqual(l.db.execute('SELECT outcome FROM interactions').fetchone()[0],'paused')
