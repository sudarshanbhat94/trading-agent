"""Unknown prices are distinct from zero profit; empty books remain exact."""
from datetime import timedelta
import json
import unittest
from unittest.mock import patch
from app import books
from app.product_status import owned_book_snapshot
from tests import test_approved_execution as fixtures


class ProductValuationTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ApprovedPaperPipelineTest();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_missing_held_quote_preserves_cash_but_cannot_report_flat_equity(self):
        self.f.submit()
        before=self.f.con.total_changes
        result=owned_book_snapshot(self.f.con,2,{},self.f.now)
        self.assertFalse(result['valuation_complete'])
        self.assertEqual(result['missing_marks'],['TEST'])
        self.assertIsNone(result['equity'])
        self.assertIsNone(result['unrealised'])
        self.assertEqual(result['cash'],round(books.cash(self.f.con,2),2))
        self.assertEqual(self.f.con.total_changes,before)

    def test_stale_or_future_quotes_are_not_current_account_values(self):
        self.f.submit()
        for delta in (-121,1):
            quote=dict(price=110,ts=(self.f.now+timedelta(seconds=delta)).isoformat())
            self.assertIsNone(owned_book_snapshot(self.f.con,2,{'TEST':quote},self.f.now)['equity'])

    def test_fresh_mark_is_used_and_empty_book_needs_no_quotes(self):
        empty=owned_book_snapshot(self.f.con,2,{},self.f.now)
        self.assertEqual(empty['cash'],10000)
        self.assertEqual(empty['equity'],10000)
        self.assertTrue(empty['valuation_complete'])
        self.f.submit()
        result=owned_book_snapshot(self.f.con,2,{'TEST':dict(price=101,ts=self.f.now.isoformat())},self.f.now)
        self.assertTrue(result['valuation_complete'])
        self.assertEqual(result['equity'],round(result['cash']+2020,2))

    def test_positions_and_portfolio_http_payloads_do_not_invent_missing_marks(self):
        from app import v2_web
        self.f.submit()
        with patch.object(v2_web,'V2_DB',str(self.f.path)),patch.object(v2_web,'_live_map',return_value={}):
            positions=v2_web._my_positions(2)
            portfolio=json.loads(v2_web.api_portfolio('IN','mine',dict(id=2,account_plan='auto')).body)
        for key in ('live','value','pnl','pnl_amt'):self.assertIsNone(positions[0][key])
        self.assertEqual(positions[0]['quote_status'],'entry_only')
        self.assertEqual(positions[0]['entry'],100)
        self.assertFalse(portfolio['valuation_complete'])
        self.assertIsNone(portfolio['equity'])
        self.assertIsNone(portfolio['drawdown']['current_drawdown_pct'])
        self.assertIsNone(portfolio['allocations'][0]['value'])
        self.assertEqual(portfolio['cash'],round(books.cash(self.f.con,2),2))
        self.assertEqual(portfolio['concentration']['n_positions'],1)
