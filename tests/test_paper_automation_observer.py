"""Read-only monitoring distinguishes idle, unknown, and actual divergence."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app import books, approved_execution
from app.screening import tracking
from scripts.observe_paper_automation import observe
from tests import test_approved_execution as fixtures


class PaperAutomationObserverTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ApprovedPaperPipelineTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.journal=Path(self.f.tmp.name)/'automation.db'
        with closing(tracking.connect(self.journal)):pass
        self.market=Path(self.f.tmp.name)/'market.db'
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute('CREATE TABLE latest_quotes(symbol,price,ts,source)')
            con.execute('INSERT INTO latest_quotes VALUES(?,?,?,?)',('TEST',100,self.f.now.isoformat(),'upstox-live'))

    def read(self,uid=2):
        with patch('app.market_regions.market_session_for_region',return_value={'is_open':False}):
            return observe(self.f.path,self.journal,self.market,uid,now=self.f.now+timedelta(seconds=3))

    def test_idle_fresh_book_is_not_an_accounting_or_feed_failure(self):
        result=self.read()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['alerts'],[])
        self.assertEqual(result['book']['cash'],10000)
        self.assertEqual(result['reconciliation']['status'],'uninitialised')
        self.assertEqual(result['journal']['active_stocks'],0)
        self.assertEqual(result['fills_in_window'],{})

    def test_owned_fill_and_inventory_reconcile_without_mutation_or_other_accounts(self):
        self.f.submit();books.ensure_book(self.f.con,3);self.f.con.commit()
        before=self.f.con.total_changes
        result=self.read()
        self.assertEqual(self.f.con.total_changes,before)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['fills_in_window']['BUY']['orders'],1)
        self.assertEqual(result['book']['positions'],1)
        self.assertEqual(result['reconciliation']['cash_difference_minor'],0)
        self.assertEqual(result['reconciliation']['inventory_difference_minor'],0)
        self.assertEqual(result['unprotected_owned_positions'],0)
        self.assertEqual(self.read(uid=3)['fills_in_window'],{})

    def test_inventory_divergence_is_attention_not_success(self):
        self.f.submit()
        self.f.con.execute('UPDATE user_positions SET shares=shares+1 WHERE user_id=2');self.f.con.commit()
        result=self.read()
        self.assertEqual(result['status'],'attention')
        self.assertIn('Paper cash/inventory ledger divergence',result['alerts'])

    def test_missing_after_hours_quote_makes_valuation_unknown_without_a_feed_alarm(self):
        self.f.submit()
        with closing(sqlite3.connect(self.market)) as con, con:con.execute('DELETE FROM latest_quotes')
        result=self.read()
        self.assertIsNone(result['book']['equity'])
        self.assertFalse(result['book']['valuation_complete'])
        self.assertFalse(any('quote coverage' in s for s in result['alerts']))

    def test_missing_journal_is_unknown_and_never_created_by_reader(self):
        self.journal.unlink()
        result=self.read()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['journal']['status'],'unavailable')
        self.assertFalse(self.journal.exists())

    def test_unknown_account_and_missing_book_cannot_be_invented(self):
        with self.assertRaises(ValueError):self.read(uid=9876)
        self.f.con.close();self.f.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):self.read()
        self.assertFalse(self.f.path.exists())

    def test_future_observation_window_is_rejected(self):
        with self.assertRaises(ValueError):
            observe(self.f.path,self.journal,self.market,since=(self.f.now+timedelta(days=1)).isoformat(),now=self.f.now)

    def test_new_expired_order_retains_its_dated_terminal_reason(self):
        order=approved_execution.submit(self.f.con,self.f.catalogue,2,self.f.plan_id,'observer-expiry',
            {'TEST':dict(price=100,ts=self.f.now.isoformat())},regime='ON',now=self.f.now)
        self.assertEqual(order['status'],'pending')
        later=self.f.now+timedelta(hours=2)
        self.f.fill_pending(now=later)
        with patch('app.market_regions.market_session_for_region',return_value={'is_open':False}):
            result=observe(self.f.path,self.journal,self.market,2,since=self.f.now.isoformat(),now=later+timedelta(seconds=1))
        self.assertEqual(result['terminal_order_blockers'][0]['status'],'expired')
        self.assertIn('expired',result['terminal_order_blockers'][0]['reason'].lower())
        self.assertEqual(result['fills_in_window'],{})
