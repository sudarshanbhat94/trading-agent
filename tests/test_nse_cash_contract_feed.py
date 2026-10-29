"""Synthetic source fixtures; no market outcome or profitability claims."""
import copy
import csv
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
import httpx

from app import nse_cash_contract_feed as feed, execution_contracts, instrument_catalog, entry_contracts


class NseCashContractsTest(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,12,5,tzinfo=timezone.utc)
        self.day=self.now.astimezone(feed.IST).date()
        self.master=feed.previous_session(self.day)
        self.members={f'TEST{i}' for i in range(400)}
        self.nse=[];self.broker=[];self.csv=[]
        for i,s in enumerate(sorted(self.members)):
            isin='INE'+f'{i:09d}';alias='NSE_EQ|'+isin
            self.nse.append(dict(TckrSymb=s,SctySrs='EQ',ISIN=isin,FinInstrmId=str(i),NewBrdLotQty='1',
                BidIntrvl='5',PricRg='80.00-120.00',SctyStsNrmlMkt='6',ElgbltyNrmlMkt='1',DelFlg='N',SttlmTp='1'))
            self.broker.append(dict(trading_symbol=s,segment='NSE_EQ',exchange='NSE',instrument_type='EQ',
                isin=isin,instrument_key=alias,exchange_token=str(i),tick_size=5,lot_size=1,freeze_quantity=100000))
            self.csv.append(dict(tradingsymbol=s,exchange='NSE_EQ',instrument_type='EQUITY',
                instrument_key=alias,exchange_token=str(i),tick_size='0.05',lot_size='1'))
        self.asm=dict(longterm=dict(data=[]),shortterm=dict(data=[]));self.gsm=[];self.actions=[]

    def assemble(self):
        return feed.assemble(self.nse,self.broker,self.csv,self.asm,self.gsm,self.actions,self.members,
            day=self.day,master_day=self.master)

    def test_units_identity_series_and_source_rules_cross_check(self):
        rows,rejected=self.assemble()
        self.assertEqual(len(rows),400);self.assertEqual(rejected,{})
        spec,alias,rules=rows[0]
        self.assertEqual(spec.tick_size,'0.05');self.assertEqual(spec.series,'EQ')
        self.assertEqual(rules['execution_scope'],'paper');self.assertEqual(rules['master_day'],'2026-10-09')
        self.broker[0]['tick_size']=.05
        self.nse[1]['SctyStsNrmlMkt']='3'
        self.csv[2]['instrument_key']='NSE_EQ|wrong'
        self.nse.append(copy.deepcopy(self.nse[3]))
        rows,rejected=self.assemble()
        self.assertEqual(len(rows),396)
        self.assertEqual(sum(rejected.values()),4)

    def test_surveillance_actions_unknown_and_future_sources(self):
        self.asm['longterm']['data']=[dict(isin=self.nse[0]['ISIN'],symbol=self.nse[0]['TckrSymb'],asmTime='09-Oct-2026')]
        self.actions=[dict(isin=self.nse[1]['ISIN'],symbol=self.nse[1]['TckrSymb'],exDate='13-Oct-2026')]
        rows,_=self.assemble()
        self.assertTrue(rows[0][2]['banned']);self.assertTrue(rows[1][2]['corporate_action_pending'])
        self.actions[0]['exDate']='20-Oct-2026'
        with self.assertRaises(ValueError):self.assemble()
        self.actions=[];self.asm['shortterm']={}
        with self.assertRaises(ValueError):self.assemble()
        with self.assertRaises(ValueError):
            feed.assemble(self.nse,self.broker,self.csv,{},[],[],self.members,day=self.day,master_day=self.day)

    def transport(self,stale=False):
        def gz_csv(rows):
            handle=io.StringIO();writer=csv.DictWriter(handle,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
            return gzip.compress(handle.getvalue().encode())
        def handle(request):
            url=str(request.url).split('?')[0]
            stamp=self.now-timedelta(days=1 if stale else 0,hours=1)
            headers={'Last-Modified':format_datetime(stamp)}
            if url.endswith('.csv.gz') and 'nsearchives' in url:raw=gz_csv(self.nse)
            elif url==feed.JSON_URL:raw=gzip.compress(json.dumps(self.broker).encode())
            elif url==feed.CSV_URL:raw=gz_csv(self.csv)
            elif url==feed.ASM_URL:raw=json.dumps(self.asm).encode()
            elif url==feed.GSM_URL:raw=b'[]'
            elif url==feed.ACTIONS_URL:
                self.assertEqual(request.url.params['from_date'],'09-10-2026')
                self.assertEqual(request.url.params['to_date'],'19-10-2026');raw=b'[]'
            else:raise AssertionError(url)
            return httpx.Response(200,content=raw,headers=headers)
        return httpx.Client(transport=httpx.MockTransport(handle))

    def test_real_refresh_transaction_archive_and_paper_entry_contract(self):
        with sqlite3.connect(':memory:') as con,tempfile.TemporaryDirectory() as root,self.transport() as client:
            result=feed.refresh(con,root,self.members,client=client,now=self.now)
            self.assertEqual(result['rules'],400);self.assertFalse(result['broker_execution'])
            spec,key=instrument_catalog.resolve(con,symbol='TEST0',venue='NSE',segment='NSE_EQ',now=self.now)
            session=dict(open=True,opens_at=self.now.replace(hour=3,minute=45).isoformat(),closes_at=self.now.replace(hour=10,minute=0).isoformat())
            execution_contracts.record(con,'session',feed.CALENDAR,session,source='synthetic NSE session',observed_at=self.now.isoformat(),
                effective_from=self.now.replace(hour=0).isoformat(),effective_until=(self.now+timedelta(days=1)).isoformat(),now=self.now)
            with entry_contracts.using(con,self.now):
                self.assertEqual(entry_contracts.check('IN','TEST0',1,100,95,110,regime='ON')['broker_key'],key)
                with self.assertRaisesRegex(ValueError,'paper-only'):
                    entry_contracts.check('IN','TEST0',1,100,95,110,regime='ON',broker='upstox')
            # A current Monday observation may refer to Friday's EOD master;
            # neither an old observation nor a previous-day rule can linger.
            with self.assertRaises(ValueError):instrument_catalog.resolve(con,symbol='TEST0',now=self.now+timedelta(hours=26))
            with self.assertRaises(ValueError):execution_contracts._latest(con,'rules',spec.id,self.now+timedelta(days=1))

    def test_stale_broker_master_refuses_without_partial_catalogue(self):
        with sqlite3.connect(':memory:') as con,tempfile.TemporaryDirectory() as root,self.transport(stale=True) as client:
            with self.assertRaisesRegex(ValueError,'Current official broker master'):
                feed.refresh(con,root,self.members,client=client,now=self.now)
            self.assertFalse(con.execute("SELECT name FROM sqlite_master WHERE name='instrument_snapshots'").fetchone())
