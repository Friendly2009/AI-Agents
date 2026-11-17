import unittest
from sales_context import current_need, guard_sales_action, coaching_context
from conversation_logic import should_ask_recording
from sales_learning import SalesLearning

class Sales414Tests(unittest.TestCase):
    def test_objections_never_start_recording(self):
        c=dict(wants_trial=1,name='Анна',phone='123',city='Александров',course='ИИ')
        for text in ['Сомневаюсь, что понравится','Боюсь, ему будет сложно','Дорого','Не знаю будет ли интересно','Вы давите','На какое пробное?']:
            with self.subTest(text=text):
                p=guard_sales_action(dict(next_action='recording',wants_trial=True,trial_decision='yes'),text)
                self.assertFalse(p['wants_trial'])
                self.assertFalse(should_ask_recording(c,c,p,[]))
    def test_pause_is_not_a_new_lead(self):
        for text in ['Подумаю','Не сейчас','Пока не готова','Не записывайте']:
            p=guard_sales_action(dict(next_action='recording',wants_trial=True),text)
            self.assertEqual(p['trial_decision'],'no')
            self.assertEqual(p['next_action'],'pause')
    def test_actual_request_is_preserved(self):
        p=dict(next_action='recording',wants_trial=True,trial_decision='yes')
        self.assertEqual(guard_sales_action(p,'Да, запишите на воскресенье'),p)
    def test_price_query_not_an_objection(self):
        self.assertEqual(current_need('Сколько стоит?'),'open')
        self.assertEqual(current_need('Недорого. Запишите нас'),'open')
    def test_handoff_is_preserved(self):
        p=dict(next_action='handoff',handoff=True)
        self.assertEqual(guard_sales_action(p,'Вы давите, позовите Настю'),p)
    def test_remembers_recent_invitation(self):
        history=[dict(role='assistant',content='Хотите прийти?'),dict(role='user',content='А что он сделает?')]
        self.assertIn('Недавно уже было приглашение',coaching_context(history))
    def test_no_mutation(self):
        p=dict(next_action='recording',wants_trial=True)
        guard_sales_action(p,'Подумаю')
        self.assertTrue(p['wants_trial'])

    def test_learning_separates_buckets_and_tracks_outcomes(self):
        learning = SalesLearning(':memory:')
        first = learning.record(10, 'Ребёнок любит игры', {'next_action':'answer','trial_decision':None,'handoff':False}, 'Можно создать мини-игру', {'child_age':10})
        self.assertIsNotNone(first)
        learning.mark(first, 'lead', 1)
        report = learning.report()
        self.assertIn('gaming', report)
        self.assertIn('lead', report)

    def test_learning_does_not_use_unlabeled_failure_as_success(self):
        learning = SalesLearning(':memory:')
        item = learning.record(11, 'Сколько стоит?', {'next_action':'answer','trial_decision':None,'handoff':False}, '4000 рублей', {})
        learning.mark_engaged(11)
        row = learning.db.execute('SELECT outcome FROM interactions WHERE id=?', (item,)).fetchone()
        self.assertEqual(row[0], 'continued')

    def test_learning_can_pause(self):
        learning = SalesLearning(':memory:')
        self.assertTrue(learning.enabled)
        learning.set_enabled(False)
        self.assertFalse(learning.enabled)
        self.assertIn('выключен', learning.prompt_hint('Привет', {}, []))
