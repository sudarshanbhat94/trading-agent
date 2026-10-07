import json
import os
from pathlib import Path
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from app import entry_readiness as readiness, v2_web, books, execution_contracts
from tests.test_approved_execution import ApprovedPaperPipelineTest


class AccountPreflightTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ApprovedPaperPipelineTest(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.quote = {'TEST': dict(price=100, ts=self.fixture.now.isoformat(),execution=self.fixture.snapshot(self.fixture.now))}

    def report(self, **kwargs):
        params = dict(symbols=['TEST'], regime='ON', regime_current=True,
                      engine_observed_at=self.fixture.now.isoformat(), now=self.fixture.now)
        params.update(kwargs)
        return readiness.report(self.fixture.con, self.fixture.catalogue, 2, self.quote, **params)

    def test_actual_gates_match_paper_fill_without_read_mutation_or_alpha_claim(self):
        before = self.fixture.con.total_changes
        result = self.report()
        self.assertEqual(result['book']['cash'], 10000); self.assertEqual(result['book']['equity'], 10000)
        self.assertEqual(result['paper']['status'], 'ready_for_order_checks')
        self.assertEqual(self.fixture.con.total_changes, before)
        self.assertFalse(result['profitability_verified']); self.assertFalse(result['commercial_release_certified'])
        self.assertEqual(result['live']['status'], 'not_certified')
        self.assertTrue(self.fixture.submit()['ok'])

    def test_off_stale_quotes_worker_and_missing_rules_block_distinct_checks(self):
        self.assertIn('regime', self.report(regime='OFF')['blockers'])
        self.assertIn('regime', self.report(regime_current=False)['blockers'])
        self.quote['TEST']['ts'] = (self.fixture.now-timedelta(minutes=10)).isoformat()
        result = self.report(engine_observed_at=(self.fixture.now-timedelta(minutes=10)).isoformat())
        self.assertIn('quotes', result['blockers']); self.assertIn('paper_worker', result['blockers'])
        empty = sqlite3.connect(':memory:'); self.addCleanup(empty.close)
        result = readiness.report(self.fixture.con, empty, 2, self.quote, symbols=['TEST'], regime='ON', regime_current=True, now=self.fixture.now)
        self.assertIn('contracts', result['blockers']); self.assertIn('exchange_session', result['blockers'])
        self.assertEqual(result['paper']['status'], 'blocked')

    def test_closed_session_waits_without_claiming_after_hours_feed_failure(self):
        at = self.fixture.now+timedelta(seconds=1)
        start = self.fixture.now.replace(hour=0, minute=0, second=0, microsecond=0)
        execution_contracts.record(self.fixture.catalogue, 'session', 'NSE:FIXTURE:ALL_DAY', {'open':False},
            source='SYNTHETIC CLOSED SESSION', observed_at=at.isoformat(), effective_from=start.isoformat(),
            effective_until=(start+timedelta(days=1)).isoformat(), now=at)
        result = self.report(now=at, engine_observed_at=None)
        self.assertEqual(result['paper']['status'], 'waiting'); self.assertNotIn('quotes', result['blockers'])
        self.assertEqual(result['instruments'][0]['quote_status'], 'after_hours')

    def test_owner_scope_does_not_initialize_missing_accounts_or_accept_write_transaction(self):
        before = self.fixture.con.total_changes
        result = readiness.report(self.fixture.con, self.fixture.catalogue, 3, self.quote,
                                  symbols=['TEST'], regime='ON', regime_current=True, now=self.fixture.now)
        self.assertIsNone(result['book']); self.assertIn('book', result['blockers'])
        self.assertEqual(self.fixture.con.total_changes, before)
        self.fixture.con.execute('BEGIN')
        with self.assertRaises(ValueError): self.report()
        self.fixture.con.rollback()

    def test_ledger_mismatch_blocks_even_when_book_risk_passes(self):
        self.assertTrue(self.fixture.submit()['ok'])
        self.assertNotIn('paper_ledger', self.report()['blockers'])
        self.fixture.con.execute('DELETE FROM user_positions')
        self.fixture.con.commit()
        result = self.report()
        self.assertIn('paper_ledger', result['blockers'])
        self.assertEqual(result['paper']['status'], 'blocked')

    def test_private_api_uses_same_catalogue_and_preserves_book(self):
        source = Path(self.fixture.tmp.name)/'readiness-catalogue.db'
        with sqlite3.connect(source) as con: self.fixture.catalogue.backup(con)
        self.fixture.con.execute("INSERT INTO v2_equity VALUES('IN',?,10000,10000,0,0)", ('LIVE_'+self.fixture.now.isoformat(),))
        self.fixture.con.commit()
        before = self.fixture.con.total_changes
        with patch.dict(os.environ, OPENSTOCKS_CATALOGUE_DB=str(source)), patch.object(v2_web, 'V2_DB', str(self.fixture.path)), \
                patch.object(v2_web, '_live_map', return_value=self.quote), \
                patch('app.v2_live.sleeve_view', return_value=dict(asof=self.fixture.now.date().isoformat(), regime='ON')):
            response = v2_web.api_trading_readiness(symbols='TEST', user={'id':2})
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.body); self.assertEqual(result['owner_user_id'], 2)
        self.assertEqual(result['paper']['status'], 'ready_for_order_checks')
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(self.fixture.con.total_changes, before)
