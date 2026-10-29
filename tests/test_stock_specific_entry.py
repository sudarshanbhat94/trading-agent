"""Market context cannot silently become universal entry permission."""
import copy
import unittest

import pandas as pd

from app import entry_contracts
from app.market_entry_policy import allows_paper_entry, allows_live_entry
from app.screening import plans
from app.sleeves.regime import RegimeGate
from tests.test_stock_plans import screen, book, NOW
from tests.test_approved_execution import ApprovedPaperPipelineTest


class StockSpecificPolicyTest(unittest.TestCase):
    def test_paper_stock_context_is_not_broker_or_index_permission(self):
        self.assertTrue(allows_paper_entry('OFF', 'quality_momentum'))
        self.assertFalse(allows_live_entry('OFF'))
        self.assertFalse(allows_paper_entry('OFF', 'index_directional'))
        for unknown in (None, '', 'UNKNOWN', 'UNAVAILABLE', 'invalid'):
            self.assertFalse(allows_paper_entry(unknown))

    def test_missing_market_data_is_unavailable_not_a_downtrend(self):
        view = RegimeGate().view({}, pd.DataFrame(), pd.Timestamp('2026-10-09'))
        self.assertEqual(view.state, 'UNAVAILABLE')
        self.assertFalse(view.allows_equity_longs)

    def test_quality_watch_preserves_missing_evidence_and_risk_refusals(self):
        data = screen(1); row = data['equities'][0]
        row['flags'] = ['no controlled entry setup']
        row['metrics'].update(setup='no controlled entry setup', relative_volume=.7)
        row['participation']['delivery_pct'] = 40
        self.assertEqual(plans.shortlist(data, book(), now=NOW)['ideas'], [])
        found = plans.research_candidates(data, book(), now=NOW)
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertFalse(candidate['actionable'])
        self.assertEqual(candidate['state'], 'RESEARCH WATCH')
        self.assertEqual(candidate['entry_blocker'], 'no controlled entry setup')
        self.assertLess(candidate['stop'], candidate['entry_low'])
        self.assertLess(candidate['entry_high'], candidate['t1'])
        self.assertLessEqual(candidate['estimated_stop_loss'], 150)
        for mode in ('news', 'earnings', 'delivery', 'quality', 'stale', 'cash'):
            changed = copy.deepcopy(data); account = book()
            if mode == 'news': changed['equities'][0]['flags'].append('adverse filing headline; manual review required')
            if mode == 'earnings': changed['equities'][0]['fundamentals']['earnings_periods'] = []
            if mode == 'delivery': changed['equities'][0]['participation']['delivery_pct'] = None
            if mode == 'quality': changed['equities'][0]['fundamentals']['roe_pct'] = 1
            if mode == 'stale': changed['stale'] = True
            if mode == 'cash': account.cash = 100
            self.assertEqual(plans.research_candidates(changed, account, now=NOW), [], mode)
        self.assertEqual(plans.research_candidates(screen(1), book(), now=NOW), [])

    def test_live_contract_boundary_still_refuses_defensive_context(self):
        fixture = ApprovedPaperPipelineTest(); fixture.setUp()
        try:
            with entry_contracts.using(fixture.catalogue, fixture.now):
                self.assertTrue(entry_contracts.check('IN', 'TEST', 20, 100, 99, 110, regime='OFF'))
                with self.assertRaisesRegex(ValueError, 'execution route'):
                    entry_contracts.check('IN', 'TEST', 20, 100, 99, 110, regime='OFF', broker='upstox')
        finally:
            fixture.doCleanups()
