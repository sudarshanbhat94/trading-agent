"""Actual index source contracts and explicit retirement, without account writes."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

import pandas as pd

from app import index_history, instrument_policy, broker, entry_contracts, v2_engine
from app.screening import store
from app.sleeves.regime import RegimeGate

NOW=datetime(2026,10,8,5,tzinfo=timezone.utc)
KEY=next(iter(instrument_policy.RETIRED_INSTRUMENT_KEYS))


def response():
    rows=[]
    for i in range(180):
        day=NOW.date()-timedelta(days=180-i)
        rows.append([day.isoformat()+'T00:00:00+05:30',20000+i,20002+i,19999+i,20001+i,0,0])
    return dict(status='success',data=dict(candles=rows[::-1]))


class ActualIndexHistoryTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close);store.initialise(self.con)

    def test_only_completed_actual_index_sessions_with_zero_index_volume(self):
        data=response();data['data']['candles'].append([NOW.isoformat(),99999,99999,99999,99999,0,0])
        got=index_history.normalize('NIFTY',data,NOW)
        self.assertEqual(got['price_asof'],'2026-10-07')
        self.assertEqual(got['instrument_key'],'NSE_INDEX|Nifty 50')
        self.assertEqual(got['kind'],'INDEX_LEVEL');self.assertEqual(len(got['bars']),180)
        self.assertEqual(got['bars'][-1]['volume'],0)

    def test_missing_settings_do_not_abort_the_stock_evidence_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.config.Settings',return_value=Mock(database_path=Path(tmp)/'missing.db')):
            errors=[]
            self.assertEqual(index_history.capture(self.con,'2026-10-07',errors),{})
            self.assertIn('settings could not be read',errors[0])
            self.assertFalse((Path(tmp)/'missing.db').exists())

    def test_conflicts_nan_boolean_unknown_short_and_invalid_ohlc_refuse(self):
        bad=[]
        for value in (float('nan'),float('inf'),True,-1):
            data=response();data['data']['candles'][0][4]=value;bad.append(data)
        data=response();dup=deepcopy(data['data']['candles'][0]);dup[4]-=1
        data['data']['candles'].append(dup);bad.append(data)
        data=response();data['data']['candles']=data['data']['candles'][:126];bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError):index_history.normalize('NIFTY',data,NOW)
        with self.assertRaises(ValueError):index_history.normalize('UNKNOWN',response(),NOW)

    def test_known_time_source_identity_and_staleness_are_preserved(self):
        data=index_history.normalize('NIFTY',response(),NOW)
        store.save(self.con,'NIFTY','index_history','SYNTHETIC actual index source',data,NOW.isoformat(),NOW)
        self.assertEqual(index_history.frames(self.con,NOW-timedelta(seconds=1)),{})
        frame=index_history.frames(self.con,NOW)['NIFTY']
        self.assertEqual(frame.attrs['known_at'],NOW.isoformat())
        self.assertEqual(frame.attrs['instrument_key'],index_history.INDEX_KEYS['NIFTY'])
        self.assertEqual(index_history.frames(self.con,NOW+timedelta(days=5)),{})
        damaged=dict(data,instrument_key='NSE_EQ|TEST')
        store.save(self.con,'NIFTY','index_history','SYNTHETIC wrong identity',damaged,
                   (NOW+timedelta(seconds=1)).isoformat(),NOW+timedelta(seconds=1))
        self.assertEqual(index_history.frames(self.con,NOW+timedelta(seconds=2)),{})

    def test_market_context_uses_index_not_a_retired_fund_and_honours_breadth(self):
        idx=pd.date_range('2026-01-01',periods=180,freq='B')
        stock=pd.DataFrame({'close':[100+i for i in range(180)]},index=idx)
        falling=pd.DataFrame({'close':[20000-i*40 for i in range(180)]},index=idx)
        rising=pd.DataFrame({'mkt_cum':[1+i/1000 for i in range(180)]},index=idx)
        gate=RegimeGate();tails={'NIFTY':falling,'BANKNIFTY':stock,'STOCK':stock,'TESTBEES':stock}
        got=gate.view(tails,rising,idx[-1],{'STOCK','TESTBEES'})
        self.assertEqual(got.state,'OFF');self.assertEqual(got.source,'actual Nifty 50 index')
        self.assertEqual(got.diagnostics['stocks'],1);self.assertEqual(got.breadth,1)
        tails['NIFTY']=stock
        got=gate.view(tails,rising,idx[-1],{'STOCK'})
        self.assertEqual(got.state,'ON');self.assertEqual(got.diagnostics['lookback_sessions'],50)
        rising.iloc[-1]=float('nan')
        self.assertEqual(gate.view({'STOCK':stock},rising,idx[-1]).state,'OFF')

    def test_old_benchmark_screen_stays_historical_and_cannot_become_a_new_intent(self):
        from app.screening import automation
        from app import v2_web
        old=dict(status='ok',data_contract_version='screening-data-v2',equities=[dict(symbol='OLD')],indices=[])
        with patch.object(store,'report',return_value=old):
            self.assertEqual(store.report('unused')['equities'],old['equities'])
            self.assertEqual(automation.current_screen(NOW)['status'],'unavailable')
            self.assertEqual(v2_web._evidence_screen('IN')['equities'],[])
        current=dict(old,data_contract_version='screening-data-v3')
        with patch.object(store,'report',return_value=current):
            self.assertEqual(automation.current_screen(NOW),current)


class RetiredInstrumentTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE universe(symbol,name,exchange,enabled,upstox_instrument_key)')
        self.con.executemany('INSERT INTO universe VALUES(?,?,?,?,?)',[
            ('OLD_FUND','Historic retired fund','NSE',1,KEY),
            ('STOCK','Individual stock','NSE',1,'NSE_EQ|TEST')])
        self.con.commit()

    def test_discovery_excludes_identity_without_deleting_the_master(self):
        from app.db import Database
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'main.db';disk=sqlite3.connect(path);self.con.backup(disk);disk.close()
            db=Database(path)
            self.assertEqual([r['symbol'] for r in db.get_universe()],['STOCK'])
            self.assertEqual(len(db.get_universe(enabled_only=False)),2)
        from app import v2_web
        import json
        master=sqlite3.connect(':memory:');self.con.backup(master)
        with patch.object(v2_web,'_ro',return_value=master):
            got=json.loads(v2_web.api_search('OLD').body)
        self.assertEqual(got,[])

    def test_retired_buy_refused_before_transport_owned_sell_not_abandoned(self):
        with patch('httpx.post') as post,patch.object(broker,'_headers',return_value={}):
            self.assertFalse(broker.place_order(2,KEY,1)['ok']);post.assert_not_called()
            post.return_value=Mock(status_code=200)
            post.return_value.json.return_value={'status':'success','data':{'order_id':'SYNTHETIC'}}
            self.assertTrue(broker.place_order(2,KEY,1,side='SELL')['ok'])
            self.assertEqual(post.call_args.kwargs['json']['transaction_type'],'SELL')

    def test_canonical_entry_gate_rejects_retired_alias(self):
        with entry_contracts.using(self.con,NOW),patch.object(entry_contracts,'resolve',return_value=(Mock(),KEY)),\
             patch('app.execution_contracts.order_contract') as contract:
            with self.assertRaisesRegex(ValueError,'Retired instrument'):
                entry_contracts.check('IN','OLD_FUND',1,100,99,103,regime='ON')
            contract.assert_not_called()

    def test_retired_prices_cannot_drive_synthetic_stock_market(self):
        self.con.execute('CREATE TABLE candles(symbol,ts,open,high,low,close,volume,source)')
        rows=[]
        for i,day in enumerate(pd.date_range('2025-01-01',periods=140)):
            for symbol,price in [('OLD_FUND',1000+i*100),('STOCK',100+i)]:
                rows.append((symbol,day.isoformat(),price,price+1,price-1,price,100000,'upstox-live:day'))
        self.con.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)',rows)
        tails,market=v2_engine.load_panel(self.con,'IN')
        self.assertEqual(set(tails),{'STOCK'});self.assertIsNotNone(market)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM candles').fetchone()[0],280)

    def test_runtime_sources_have_no_retired_named_route(self):
        root=Path(__file__).resolve().parents[1]
        forbidden=('NIFTY'+'BEES','BANK'+'BEES')
        for folder in ('app','scripts'):
            for path in (root/folder).rglob('*.py'):
                self.assertTrue(all(word not in path.read_text().upper() for word in forbidden),str(path))
