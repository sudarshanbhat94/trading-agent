import sqlite3
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from app import v2_live
from app.sleeves.readiness import book_readiness
from scripts.audit_market_prices import bars_from_response, range_check


class ReleaseIntegrityTest(unittest.TestCase):
    def test_direct_production_pass_cannot_open_outside_market_hours(self):
        with patch.object(v2_live,'market_open',return_value=False), patch.object(v2_live,'_rw') as writer:
            v2_live.sleeve_pass('IN')
            writer.assert_not_called()
            self.assertIn('market closed',v2_live.status()['IN'])

    def test_readiness_reports_real_drawdown_without_resetting_state(self):
        c=sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        v2_live.ensure_schema(c)
        c.execute("UPDATE v2_book SET started_at='2026-08-01T00:00:00+00:00'")
        c.execute("INSERT INTO v2_trades(market,pnl,entry_date,exit_date,closed_at) "
                  "VALUES('IN',-1050,'2026-08-10','2026-08-12','2026-08-12T10:00:00+00:00')")
        c.execute("INSERT INTO v2_equity(market,date,equity) VALUES('IN','LIVE_2026-07-31T10:00:00Z',100000)")
        got=book_readiness(c,'IN',{},datetime(2026,9,17,tzinfo=timezone.utc))
        self.assertTrue(got['halted'])
        self.assertIn('drawdown',got['reason'])
        self.assertEqual((got['capital'],got['equity'],got['cash'],got['daily_pnl']),(10000,8950,8950,0))
        self.assertEqual(got['peak'],10000)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM v2_trades').fetchone()[0],1)

    def test_missing_mark_is_reported_not_called_ready(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        v2_live.ensure_schema(c)
        c.execute("INSERT INTO v2_positions(market,symbol,strategy,entry_date,shares,entry_price,stop) "
                  "VALUES('IN','X','mean_reversion','2026-09-17',1,100,95)")
        got=book_readiness(c,'IN',{},datetime(2026,9,17,tzinfo=timezone.utc))
        self.assertTrue(got['halted'])
        self.assertFalse(got['valuation_complete'])
        self.assertIn('stale',got['reason'])

    def test_external_daily_range_audit_discloses_missing_data_and_splits(self):
        body={'chart':{'result':[{'timestamp':[1789530300], 'indicators':{'quote':[
            {'open':[100],'high':[110],'low':[90],'close':[105],'volume':[1000]}]}}]}}
        bars,splits=bars_from_response(body)
        day=next(iter(bars))
        self.assertEqual(range_check(100,day,bars,splits),'within_daily_range')
        self.assertEqual(range_check(120,day,bars,splits),'outside_daily_range')
        self.assertEqual(range_check(100,'2020-01-01',bars,splits),'unavailable')
        self.assertEqual(range_check(100,day,bars,['2099-01-01']),'corporate_action_review')


if __name__=='__main__': unittest.main()
