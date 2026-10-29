"""Provider timestamps and real latest-depth writes, without broker requests."""
import sqlite3
import asyncio
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app import executable_quotes as quotes
from app.market_data import UpstoxMarketDataProvider
from tests import test_approved_execution as fixtures


class ExecutableTimestampTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 5, 0, 0, 123000, tzinfo=timezone.utc)
        self.item = dict(instrument_token='NSE_EQ|TEST',symbol='TEST',timestamp=self.now.isoformat(),
                         lower_circuit_limit=80,upper_circuit_limit=120,
                         depth=dict(buy=[dict(price=99.95,quantity=100)],sell=[dict(price=100,quantity=100)]))

    def snapshot(self, at, *, quantity=100):
        return quotes.normalize_upstox(dict(self.item,timestamp=at,
                    depth=dict(buy=[dict(price=99.95,quantity=100)],sell=[dict(price=100,quantity=quantity)])),
                    'NSE_EQ|TEST','TEST',observed_at=(self.now+timedelta(seconds=1)).isoformat())

    def test_provider_epoch_milliseconds_equal_the_same_iso_instant(self):
        milliseconds = int(self.now.timestamp()) * 1000 + self.now.microsecond // 1000
        expected = self.snapshot(self.now.isoformat())
        for value in (milliseconds,str(milliseconds)):
            with self.subTest(value=value):
                actual=self.snapshot(value)
                self.assertEqual(actual['snapshot_id'],expected['snapshot_id'])
                self.assertEqual(quotes.top(actual,'BUY',key='NSE_EQ|TEST',symbol='TEST',now=self.now+timedelta(seconds=1)),(100,100))
        offset=self.now.astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()
        self.assertEqual(self.snapshot(offset)['snapshot_id'],expected['snapshot_id'])

    def test_submillisecond_update_replaces_earlier_depth_exactly(self):
        # These timestamps collapse to one SQLite Julian-day value.
        first=self.snapshot(self.now.isoformat(),quantity=100)
        newer=self.snapshot((self.now+timedelta(microseconds=10)).isoformat(),quantity=1)
        with sqlite3.connect(':memory:') as con:
            quotes.ensure_schema(con);quotes.write(con,first);quotes.write(con,newer)
            current=quotes.read(con,['TEST'])['TEST']
            self.assertEqual(current['snapshot_id'],newer['snapshot_id'])
            self.assertEqual(quotes.top(current,'BUY',key='NSE_EQ|TEST',symbol='TEST',now=self.now+timedelta(seconds=1)),(100,1))
            quotes.write(con,first)
            self.assertEqual(quotes.read(con,['TEST'])['TEST']['snapshot_id'],newer['snapshot_id'])

    def test_equal_source_time_conflict_removes_executable_permission(self):
        first=self.snapshot(self.now.isoformat(),quantity=100)
        conflict=self.snapshot(self.now.isoformat(),quantity=1)
        with sqlite3.connect(':memory:') as con:
            quotes.ensure_schema(con);quotes.write(con,first);quotes.write(con,conflict)
            self.assertEqual(quotes.read(con,['TEST']),{})
            quotes.write(con,first)
            self.assertEqual(quotes.read(con,['TEST']),{})
            later=self.snapshot((self.now+timedelta(microseconds=10)).isoformat(),quantity=2)
            quotes.write(con,later)
            self.assertEqual(quotes.read(con,['TEST'])['TEST']['snapshot_id'],later['snapshot_id'])
            event=con.execute('SELECT payload FROM market_execution_quote_conflicts').fetchone()[0]
            self.assertEqual(json.loads(event)['conflicting']['asks'][0]['quantity'],1)
            for operation in ('UPDATE market_execution_quote_conflicts SET payload=payload',
                              'DELETE FROM market_execution_quote_conflicts'):
                with self.assertRaises(sqlite3.IntegrityError):con.execute(operation)

    def test_ambiguous_units_future_floats_and_naive_values_refuse(self):
        ms=int(self.now.timestamp())*1000+self.now.microsecond//1000
        for value in (True,False,1.5,float(ms),str(ms)+'.0',int(self.now.timestamp()),str(int(self.now.timestamp())),
                      -ms,ms+10000,'2026-10-07T05:00:00',10**100):
            with self.subTest(value=value),self.assertRaises(ValueError):self.snapshot(value)

    def test_quote_write_never_commits_its_parent_transaction(self):
        with sqlite3.connect(':memory:') as con:
            quotes.ensure_schema(con);con.commit();con.execute('BEGIN IMMEDIATE')
            quotes.write(con,self.snapshot(self.now.isoformat()))
            self.assertTrue(con.in_transaction);con.rollback()
            self.assertEqual(quotes.read(con,['TEST']),{})

    def test_two_workers_cannot_restore_older_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'market.db'
            with sqlite3.connect(path) as con:quotes.ensure_schema(con)
            def worker(value):
                con=sqlite3.connect(path,timeout=5)
                try:quotes.write(con,value);con.commit()
                finally:con.close()
            first=self.snapshot(self.now.isoformat(),quantity=100)
            later=self.snapshot((self.now+timedelta(microseconds=10)).isoformat(),quantity=1)
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(worker,[later,first]))
            with sqlite3.connect(path) as con:
                self.assertEqual(quotes.read(con,['TEST'])['TEST']['snapshot_id'],later['snapshot_id'])

    def test_equal_time_conflict_respects_outer_rollback(self):
        with sqlite3.connect(':memory:') as con:
            quotes.ensure_schema(con);quotes.write(con,self.snapshot(self.now.isoformat()));con.commit()
            con.execute('BEGIN IMMEDIATE')
            quotes.write(con,self.snapshot(self.now.isoformat(),quantity=1))
            self.assertEqual(quotes.read(con,['TEST']),{})
            con.rollback()
            self.assertIn('TEST',quotes.read(con,['TEST']))
            self.assertEqual(con.execute('SELECT COUNT(*) FROM market_execution_quote_conflicts').fetchone()[0],0)


class ProviderToPaperDepthTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ApprovedPaperPipelineTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        # A deterministic source instant, avoiding fractional-ms conversions.
        self.now=self.f.now.replace(microsecond=0)+timedelta(seconds=1)
        self.item=dict(instrument_token='NSE_EQ|TEST',symbol='TEST',timestamp=int(self.now.timestamp())*1000,
                       last_price=100,lower_circuit_limit=80,upper_circuit_limit=120,
                       depth=dict(buy=[dict(price=99.95,quantity=1000)],sell=[dict(price=100,quantity=1000)]))

    def fetch(self,item):
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,url,**kwargs):
                class Response:
                    def raise_for_status(self):pass
                    def json(self):return {'data':{'NSE_EQ:TEST':item}}
                return Response()
        provider=object.__new__(UpstoxMarketDataProvider)
        provider.access_token='synthetic-fixture';provider.base_url='https://api.upstox.com/v2'
        with patch('app.market_data.httpx.AsyncClient',return_value=Client()), \
                patch('app.market_data.datetime',wraps=datetime) as clock:
            clock.now.return_value=self.now+timedelta(seconds=1)
            result=asyncio.run(provider.get_quotes([dict(symbol='TEST',upstox_instrument_key='NSE_EQ|TEST')]))
        return provider,result

    def test_millisecond_depth_reaches_actual_pending_order_fill_and_ledger(self):
        from app import approved_execution,paper_exchange,paper_ledger,books
        order=approved_execution.submit(self.f.con,self.f.catalogue,2,self.f.plan_id,'timestamp-flow',
            {'TEST':dict(price=100,ts=self.f.now.isoformat())},regime='ON',now=self.f.now)
        self.assertEqual(order['status'],'pending')
        provider,marks=self.fetch(self.item)
        self.assertEqual(provider.last_quote_diagnostics['executable_depth_returned'],1)
        self.assertEqual(provider.last_quote_diagnostics['executable_depth_missing_symbols'],[])
        with sqlite3.connect(':memory:') as market:
            quotes.ensure_schema(market);quotes.write(market,provider.execution_quotes['TEST'])
            snapshot=quotes.read(market,['TEST'])['TEST']
        evidence={'TEST':dict(price=marks['TEST'].price,ts=marks['TEST'].asof,execution=snapshot)}
        result=paper_exchange.service(self.f.con,self.f.catalogue,evidence,regime='ON',now=self.now+timedelta(seconds=2))
        self.assertEqual(result[0]['status'],'filled');self.assertEqual(result[0]['qty'],20)
        self.assertEqual(paper_exchange.service(self.f.con,self.f.catalogue,evidence,regime='ON',now=self.now+timedelta(seconds=3)),[])
        balance=paper_ledger.report(self.f.con,2,'IN',books.current_epoch(self.f.con,2),books.cash(self.f.con,2))
        self.assertEqual(balance['cash_difference_minor'],0)

    def test_missing_executable_timestamp_keeps_mark_but_refuses_fill(self):
        provider,marks=self.fetch(dict(self.item,timestamp=None))
        self.assertEqual(marks['TEST'].price,100)
        self.assertEqual(provider.execution_quotes,{})
        self.assertEqual(provider.last_quote_diagnostics['executable_depth_missing_symbols'],['TEST'])
