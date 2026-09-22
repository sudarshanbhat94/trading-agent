import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

from app import v2_live
from app.sleeves.readiness import book_readiness
from scripts.audit_market_prices import bars_from_response, range_check


class ReleaseIntegrityTest(unittest.TestCase):
    def test_actual_signal_delivers_volume_confirmation_and_missing_values_fail_closed(self):
        import numpy as np
        import pandas as pd
        from app import v2_engine
        from app.sleeves.mean_reversion import MeanReversionSleeve
        ix=pd.bdate_range('2025-01-01',periods=100)
        close=np.linspace(80,110,100);close[-1]=106
        frame=pd.DataFrame(dict(close=close,open=close,high=close*1.08,low=close*.97,volume=1000),index=ix)
        market=pd.DataFrame(dict(mkt_cum=np.ones(100)),index=ix)
        sig=v2_engine.signals_for_date({'TEST':frame},market,ix[-1],0,2,3.5)[0]
        self.assertEqual(sig['rvol'],1)
        self.assertTrue(MeanReversionSleeve._confirm(frame,sig)[0])
        for bad in (None,0,.8,float('nan'),float('inf')):
            with self.subTest(volume=bad):
                self.assertFalse(MeanReversionSleeve._confirm(frame,dict(sig,rvol=bad))[0])
        self.assertTrue(MeanReversionSleeve._confirm(frame,dict(sig,rs20=0))[0])

    def test_dashboard_regime_uses_completed_session_and_does_not_invent_missing_state(self):
        from app import v2_web
        import pandas as pd
        today=datetime.now(v2_web.IST).date()
        prior=pd.Timestamp(today-timedelta(days=1))
        with patch.object(v2_web,'_panel',return_value=({},None)), \
             patch.object(v2_web.eng,'complete_trading_dates',return_value=[prior,pd.Timestamp(today)]), \
             patch.object(v2_live,'trading_days_held',return_value=1), \
             patch('app.sleeves.regime.RegimeGate') as gate, \
             patch.dict(v2_web._regime_cache,{},clear=True):
            gate.return_value.view.return_value.state='OFF'
            v2_web._regime_bg('IN')
            self.assertEqual(gate.return_value.view.call_args.args[2],prior)
            self.assertEqual(v2_web._regime_cache['IN'][1],'OFF')
            gate.return_value.view.side_effect=ValueError('no data')
            v2_web._regime_bg('IN')
            self.assertIsNone(v2_web._regime_cache['IN'][1])

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

    def test_ideas_replaces_a_stale_cached_halt_after_book_reset(self):
        from app import v2_web
        with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
            c=sqlite3.connect(tmp.name)
            v2_live.ensure_schema(c)
            c.execute("UPDATE v2_book SET budget=10000, started_at='2026-09-22T12:00:00+00:00' WHERE market='IN'")
            c.execute("INSERT OR REPLACE INTO v2_equity(market,date,equity,cash,positions_value,n_positions) "
                      "VALUES('IN','LIVE_2026-09-22T12:00:00',10000,10000,0,0)")
            c.commit(); c.close()
            stale={'execution_halted':True, 'halt_reason':'drawdown halt: -10.5% off peak'}
            with patch.object(v2_web,'V2_DB',tmp.name), patch.object(v2_web,'_live_map',return_value={}):
                got=v2_web._decision_with_live_readiness('IN',stale)
        self.assertFalse(got['execution_halted'])
        self.assertEqual(got['halt_reason'],'')
        self.assertEqual((got['paper_book']['capital'],got['paper_book']['cash'],
                          got['paper_book']['equity'],got['paper_book']['positions']),
                         (10000,10000,10000,0))

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
