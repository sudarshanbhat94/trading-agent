"""Receiving old history must not report successful current-session coverage."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch

from app.config import Settings
from app.models import Candle
from scripts import candle_ingest as ingest


def bar(symbol,day):
    return Candle(symbol,day+'T00:00:00+05:30',100,105,95,101,1000,'upstox-live:day')


class CandleJobCompletionTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'market.db'
        with closing(sqlite3.connect(self.path)) as con:
            con.execute('CREATE TABLE candles(symbol,ts,source,PRIMARY KEY(symbol,ts,source))');con.commit()
        self.db=Mock();self.db.path=self.path
        self.rows=[dict(symbol='ACTIVE',enabled=1)]
        self.disabled=dict(symbol='HELD',enabled=0)
        self.db.get_universe.side_effect=lambda enabled_only,market_region:self.rows if enabled_only else self.rows+[self.disabled]
        def store(rows):
            with closing(sqlite3.connect(self.path)) as con:
                con.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?)',
                    [(c.symbol,c.ts,c.source) for values in rows.values() for c in values]);con.commit()
        self.db.upsert_candles.side_effect=store

    def run_ingest(self,responses,held=set(),passes=2):
        provider=Mock();provider.get_candles=AsyncMock(side_effect=responses)
        with patch.object(ingest,'build_market_data_provider',return_value=provider), \
             patch.object(ingest,'expected_session',return_value='2026-10-06'), \
             patch.object(ingest,'_session_records',side_effect=ValueError('test source unavailable')), \
             patch.object(ingest,'_held',return_value=held),patch.object(ingest.time,'sleep'):
            result=ingest.ingest('IN','upstox',self.db,Settings(),pause=0,max_passes=passes)
        return result,provider

    def test_old_history_is_retried_and_only_actual_target_bar_finishes_job(self):
        result,provider=self.run_ingest([{'ACTIVE':[bar('ACTIVE','2026-10-05')]},
                                        {'ACTIVE':[bar('ACTIVE','2026-10-06')]}])
        self.assertEqual(provider.get_candles.await_count,2)
        self.assertEqual((result['status'],result['requested'],result['fresh'],result['missing']),('complete',1,1,0))

    def test_partial_history_and_not_yet_completed_day_cannot_be_green(self):
        result,provider=self.run_ingest([{'ACTIVE':[bar('ACTIVE','2026-10-05'),bar('ACTIVE','2026-10-07')]},{}])
        self.assertEqual((result['status'],result['fresh'],result['missing']),('partial',0,1))
        self.assertEqual(provider.get_candles.await_count,2)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute('SELECT substr(ts,1,10) FROM candles').fetchall(),[('2026-10-05',)])

    def test_disabled_personal_holding_is_done_first_without_reenabling_it(self):
        result,provider=self.run_ingest([{'HELD':[bar('HELD','2026-10-06')]},
                                        {'ACTIVE':[bar('ACTIVE','2026-10-06')]}],{'HELD'})
        self.assertEqual(provider.get_candles.await_args_list[0].args[0],[self.disabled])
        self.assertEqual((result['status'],result['requested'],result['fresh']),('complete',2,2))
        self.assertEqual(self.disabled['enabled'],0)

    def test_missing_held_identity_prevents_complete_status_even_when_universe_current(self):
        result,_=self.run_ingest([{'ACTIVE':[bar('ACTIVE','2026-10-06')]}],{'UNKNOWN'})
        self.assertEqual((result['status'],result['fresh'],result['missing_held']),('partial',1,1))

    def test_main_returns_nonzero_for_failure_partial_or_unsupported_market(self):
        for outcome in ({'status':'partial'},RuntimeError('synthetic error')):
            options=Mock(market='IN',limit=0)
            with patch.object(ingest.argparse.ArgumentParser,'parse_args',return_value=options), \
                 patch('app.market_identity.refresh',return_value={'status':'complete','issues':[]}), \
                 patch.object(ingest,'_held',return_value=set()), \
                 patch.object(ingest,'Database',return_value=self.db),patch.object(self.db,'runtime_settings',return_value={}), \
                 patch.object(ingest,'ingest',**({'side_effect':outcome} if isinstance(outcome,Exception) else {'return_value':outcome})):
                self.assertEqual(ingest.main(),2)
        with patch.object(ingest.argparse.ArgumentParser,'parse_args',return_value=Mock(market='US',limit=0)), \
             patch.object(ingest,'Database',return_value=self.db),patch.object(self.db,'runtime_settings',return_value={}), \
             patch.object(ingest,'MARKETS',{'IN':'upstox'}),patch.object(ingest,'ingest') as fetch:
            self.assertEqual(ingest.main(),2);fetch.assert_not_called()


    def test_daily_job_requests_only_full_daily_history(self):
        settings=ingest._market_settings(Settings(),'IN','upstox')
        self.assertEqual(settings.upstox_candle_interval,'day')
        self.assertEqual(settings.upstox_candle_lookback_days,420)
        self.assertFalse(settings.enable_upstox_multi_timeframe_candles)

    def test_incomplete_exposure_inventory_refuses_identity_mutation(self):
        from scripts.v2_quote_feed import _held
        with self.assertRaisesRegex(ValueError,'Complete exposure inventory'):
            _held(self.path,strict=True)

    def test_dated_no_trade_report_never_excuses_unknown_volume_or_held_prices(self):
        records={(str(i),'EQ'):dict(session='2026-10-06',volume=10) for i in range(1000)}
        for symbol,volume in [('ZERO',0),('UNKNOWN',None),('BAD',-1),('TRADED',20)]:
            records[symbol,'EQ']=dict(session='2026-10-06',volume=volume)
        missing={'ABSENT','ZERO','UNKNOWN','BAD','TRADED','HELD'}
        self.assertEqual(ingest._no_reported_trade(records,missing,{'HELD'},'2026-10-06'),{'ABSENT','ZERO'})
        records['ZERO','BE']=dict(session='2026-10-06',volume=1)
        self.assertNotIn('ZERO',ingest._no_reported_trade(records,missing,set(),'2026-10-06'))
        wrong_day={k:dict(v,session='2026-10-05') for k,v in records.items()}
        for broken in ({},wrong_day):
            with self.assertRaises(ValueError):ingest._no_reported_trade(broken,missing,set(),'2026-10-06')


if __name__=='__main__':unittest.main()
