"""Production orchestration, including automatic protection, in temporary books."""
import unittest
from scripts.verify_paper_automation import run


class AutomationProofTest(unittest.TestCase):
    def test_stop_and_target_lifecycles_reconcile_and_never_submit_broker_orders(self):
        result=run()
        self.assertFalse(result['profitability_evidence'])
        self.assertEqual(result['production_orders_submitted'],0)
        self.assertEqual({s['scenario'] for s in result['scenarios']},{'stop','target'})
        for case in result['scenarios']:
            with self.subTest(case=case['scenario']):
                self.assertEqual(case['status'],'passed')
                self.assertEqual(case['capital'],10000)
                self.assertEqual(case['open_positions'],0)
                self.assertEqual(case['ledger_cash_difference_minor'],0)
                self.assertEqual(case['inventory_difference'],0)
                self.assertEqual(case['broker_orders'],0)
                self.assertEqual(case['engine_errors'],0)
                self.assertEqual(case['cash'],case['equity'])
                self.assertTrue(case['restarted'])
                self.assertTrue(case['wrong_identity_quote_refused'])
                self.assertTrue(case['missing_depth_refused'])
                self.assertTrue(case['stale_depth_refused'])
                self.assertEqual(case['timeline'][0]['cash'],10000)
                self.assertEqual(case['timeline'][0]['owned_positions'],0)
                self.assertEqual(case['timeline'][-2]['owned_positions'],1)
                self.assertEqual(case['timeline'][-1]['owned_positions'],0)
