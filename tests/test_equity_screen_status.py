"""Coverage is research-only; no regime substitution, publication or order."""
import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.screening.status import equity_screen_status, paper_execution_scope
from tests.test_stock_plans import screen, NOW


class EquityScreenStatusTest(unittest.TestCase):
    def test_all_individual_stock_rows_are_counted_without_an_index_candidate(self):
        data = screen(5); data['universe_count'] = 500
        before = copy.deepcopy(data)
        got = equity_screen_status(data, NOW)
        self.assertEqual((got['status'],got['universe_count'],got['screened_count'],got['evidence_passes']),
                         ('current',500,5,5))
        self.assertNotIn('NIFTYBEES', [r['symbol'] for r in data['equities']])
        self.assertEqual(data, before)

    def test_score_cannot_make_failed_evidence_a_pass(self):
        data = screen(2); data['equities'][0]['score'] = 100
        data['equities'][0]['fundamentals']['roe_pct'] = 1
        got = equity_screen_status(data, NOW)
        self.assertEqual(got['evidence_passes'], 1)
        self.assertEqual(got['rejections'][0]['count'], 1)
        self.assertIn('Quality gate', got['rejections'][0]['reason'])

    def test_expired_evidence_never_presents_current_passes(self):
        for flag in ('stale','price_stale'):
            data = screen(2); data[flag] = True
            with self.subTest(flag=flag):
                got = equity_screen_status(data, NOW)
                self.assertEqual((got['status'],got['screened_count'],got['evidence_passes']),('stale',2,None))

    def test_unavailable_is_unknown_not_zero_stocks_passed(self):
        for data in (None, {},dict(status='unavailable'),dict(status='ok',equities=[None]),
                     dict(status='ok',equities=[dict(symbol=['bad'])])):
            with self.subTest(data=data):
                got = equity_screen_status(data, NOW)
                self.assertEqual(got['status'],'unavailable')
                self.assertIsNone(got['screened_count'])
                self.assertIsNone(got['evidence_passes'])

    def test_malformed_metric_object_is_a_rejection_not_a_dashboard_error(self):
        data=screen(1);data['equities'][0]['metrics']=True
        got=equity_screen_status(data,NOW)
        self.assertEqual(got['evidence_passes'],0)
        self.assertIn('Incomplete',got['rejections'][0]['reason'])

    def test_duplicate_stock_is_not_two_independent_passes(self):
        data = screen(1); data['equities'].append(copy.deepcopy(data['equities'][0]))
        got = equity_screen_status(data, NOW)
        self.assertEqual((got['screened_count'],got['evidence_passes']),(1,0))
        self.assertEqual(got['rejections'][0]['count'],1)

    def test_permission_is_described_separately_and_never_enables_a_sleeve(self):
        from app.sleeves.config import PRODUCTION_SLEEVES, SLEEVES
        before = copy.deepcopy(SLEEVES)
        got = paper_execution_scope()
        self.assertEqual(got['production_sleeves'],list(PRODUCTION_SLEEVES))
        self.assertTrue(got['stock_entries_enabled'])
        self.assertEqual(got['stock_model_version'], 'selective-paper-v1')
        self.assertEqual(got['stock_validation'], 'unvalidated paper trial')
        self.assertEqual(got['automated_index_instruments'],['NIFTYBEES'])
        self.assertEqual(SLEEVES,before)

    def test_overview_sends_actual_stock_coverage_alongside_execution_scope(self):
        from app import v2_live, v2_web
        data=screen(5);data['universe_count']=500
        with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
            c=sqlite3.connect(tmp.name);v2_live.ensure_schema(c);c.close()
            with patch.object(v2_web,'V2_DB',tmp.name), \
                 patch.object(v2_web,'_live_map',return_value={}), \
                 patch.object(v2_live,'_option_live',return_value={}), \
                 patch.object(v2_web,'_markets',return_value=[]), \
                 patch.object(v2_web,'_options_book',return_value={}), \
                 patch.object(v2_web,'_evidence_screen',return_value=data) as read, \
                 patch.object(v2_web,'_regime_state',return_value='OFF'), \
                 patch.object(v2_web,'_regime',return_value='OFF'), \
                 patch('app.broker.state',return_value=dict(connected=False)), \
                 patch('app.broker.account_snapshot') as broker:
                got=json.loads(v2_web.api_overview(dict(id=2,account_plan='free')).body)
                broker.assert_not_called();read.assert_called_once_with('IN')
        self.assertEqual(got['stock_screen']['screened_count'],5)
        self.assertTrue(got['execution_scope']['stock_entries_enabled'])
        self.assertEqual(got['regime_state']['IN'],'OFF')


if __name__ == '__main__':
    unittest.main()
