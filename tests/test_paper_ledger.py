"""Currency/ownership/crash invariants, isolated from real broker APIs."""
import sqlite3
import unittest
from tests.contract_storage_fixtures import ContractStorageCase
from unittest.mock import patch

from app import books, paper_ledger as ledger, v2_live
from tests.test_release_safety import quotes


class PaperLedgerTest(ContractStorageCase):
    def setUp(self):
        self.con=sqlite3.connect(":memory:")
        v2_live.ensure_schema(self.con)
        self.addCleanup(self.con.close)

    def buy(self,uid=1):
        return books.buy(self.con,uid,"IN","manual","TEST",100,20,99,110,
                         quotes=quotes(TEST=100),request_key="request")

    def report(self,uid=1):
        return ledger.report(self.con,uid,"IN",books.current_epoch(self.con,uid),books.cash(self.con,uid))

    def test_cash_and_inventory_reconcile_after_entry_exit_and_retry(self):
        self.assertEqual(self.buy(),20)
        self.assertEqual(self.buy(),20)
        opened=self.report();self.assertEqual(opened['status'],'ok');self.assertEqual(opened['events'],2)
        self.assertEqual(opened['balances_minor']['inventory'],200000)
        self.assertTrue(opened['balanced'])
        pnl,_=books.sell(self.con,1,"IN","TEST",110)
        closed=self.report();self.assertEqual(closed['status'],'ok');self.assertEqual(closed['events'],3)
        self.assertEqual(closed['balances_minor']['inventory'],0)
        self.assertEqual(closed['balances_minor']['cash'],ledger.minor(10000+pnl))
        self.assertIsNone(books.sell(self.con,1,"IN","TEST",110))
        self.assertEqual(self.buy(),20);self.assertEqual(self.report()['events'],3)

    def test_posting_failure_rolls_back_position_and_receipt(self):
        with patch.object(ledger,'entry',side_effect=RuntimeError('disk fault')):
            with self.assertRaises(RuntimeError):self.buy()
        self.assertEqual(books.positions(self.con,1),[])
        self.assertIsNone(books.entry_receipt(self.con,1,'IN','request'))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM paper_ledger_events').fetchone()[0],0)

    def test_exit_failure_keeps_position_and_does_not_realise_pnl(self):
        self.buy()
        with patch.object(ledger,'exit',side_effect=RuntimeError('disk fault')):
            with self.assertRaises(RuntimeError):books.sell(self.con,1,'IN','TEST',110)
        self.assertEqual(len(books.positions(self.con,1)),1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM user_trades').fetchone()[0],0)
        self.assertEqual(self.report()['status'],'ok')

    def test_reset_keeps_old_postings_and_other_account(self):
        self.buy();self.buy(2);old=books.current_epoch(self.con,1)
        books.reset_book(self.con,1,'IN')
        self.assertEqual(self.report()['status'],'uninitialised')
        self.assertEqual(self.report(2)['status'],'ok')
        self.assertEqual(ledger.report(self.con,1,'IN',old)['events'],2)
        self.buy();self.assertEqual(self.report()['status'],'ok')

    def test_opening_snapshot_does_not_fabricate_legacy_trade_history(self):
        books.ensure_book(self.con,1)
        self.con.execute("INSERT INTO user_trades(user_id,market,symbol,pnl,return_pct,book_epoch) VALUES(1,'IN','OLD',100,1,?)",(books.current_epoch(self.con,1),))
        self.con.commit();self.buy()
        first=self.con.execute('SELECT kind,provenance FROM paper_ledger_events ORDER BY id LIMIT 1').fetchone()
        self.assertEqual(first,('OPENING_SNAPSHOT','observed-opening-snapshot'))
        self.assertEqual(self.report()['status'],'ok')

    def test_immutable_rows_and_unbalanced_or_rebound_events_refused(self):
        self.buy()
        for table in ('paper_ledger_events','paper_ledger_postings'):
            with self.assertRaises(sqlite3.IntegrityError):self.con.execute(f'DELETE FROM {table}')
            self.con.rollback()
        with self.assertRaises(ValueError),self.con:
            self.con.execute('BEGIN IMMEDIATE')
            ledger.post(self.con,1,'IN',books.current_epoch(self.con,1),'bad','ENTRY',1,{'cash':-1,'inventory':2})
        with self.assertRaises(ValueError),self.con:
            self.con.execute('BEGIN IMMEDIATE')
            ledger.entry(self.con,1,'IN',books.current_epoch(self.con,1),books.positions(self.con,1)[0]['id'],2001,1)

    def test_read_report_does_not_write_and_unknown_epoch_is_empty(self):
        self.buy();before=self.con.total_changes
        self.assertEqual(self.report()['status'],'ok')
        self.assertEqual(ledger.report(self.con,99,'IN','missing')['events'],0)
        self.assertEqual(self.con.total_changes,before)

    def test_independent_balance_drift_is_visible(self):
        self.buy()
        self.assertEqual(ledger.report(self.con,1,'IN',books.current_epoch(self.con,1),1)['status'],'mismatch')
