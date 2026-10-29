"""Bank collection coverage remains separate from financial-sector admission."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

import httpx
import pandas as pd

from app.screening import store
from app.screening.providers import statements
from app.screening.official_financials import BANK_COLLECTOR_VERSION
from app.screening.screen import build
from scripts.evidence_screen import capture_bank_metrics
from tests.test_evidence_screen import NOW, financial_payload, prices


class BankEvidenceCollectionTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');store.initialise(self.con)
        self.addCleanup(self.con.close)
        self.now=datetime.now(timezone.utc)

    def test_bounded_failures_rotate_behind_untried_lenders(self):
        store.save(self.con,'AAA','bank_metric_attempt','NSE',dict(status='failed'),
                   (self.now-timedelta(hours=2)).isoformat(),self.now)
        errors=[]
        with patch('app.screening.official_financials.fetch_bank_metrics',side_effect=ValueError('missing ratios')) as fetch:
            count=capture_bank_metrics(None,self.con,['AAA','BBB','INDUSTRIAL','BBB'],
                {'AAA':'Banks','BBB':'Financial Services','INDUSTRIAL':'Manufacturing'},self.now,errors,limit=1)
        self.assertEqual(count,1)
        self.assertEqual(fetch.call_args.args[1],'BBB')
        self.assertEqual(errors,['BBB bank metrics unavailable: missing ratios'])
        result=json.loads(self.con.execute("SELECT payload FROM evidence WHERE symbol='BBB'").fetchone()[0])
        self.assertEqual(result,dict(status='failed',reason='missing ratios',collector_version=BANK_COLLECTOR_VERSION))

    def test_oldest_failure_is_retried_before_more_recent_failure(self):
        for symbol,hours in [('AAA',2),('ZZZ',3)]:
            store.save(self.con,symbol,'bank_metric_attempt','NSE',dict(status='failed'),
                (self.now-timedelta(hours=hours)).isoformat(),self.now)
        with patch('app.screening.official_financials.fetch_bank_metrics',side_effect=ValueError('missing')) as fetch:
            capture_bank_metrics(None,self.con,['AAA','ZZZ'],dict(AAA='Banks',ZZZ='Banks'),self.now,[],limit=1)
        self.assertEqual(fetch.call_args.args[1],'ZZZ')

    def test_cached_missing_facts_and_recent_attempt_keep_original_timestamps(self):
        old=self.now-timedelta(minutes=30)
        store.save(self.con,'CACHED','bank_metrics','NSE',
                   dict(complete=False,collector_version=BANK_COLLECTOR_VERSION),old.isoformat(),self.now)
        store.save(self.con,'RECENT','bank_metric_attempt','NSE',dict(status='failed'),old.isoformat(),self.now)
        before=self.con.execute('SELECT * FROM evidence ORDER BY symbol').fetchall()
        with patch('app.screening.official_financials.fetch_bank_metrics') as fetch:
            self.assertEqual(capture_bank_metrics(None,self.con,['CACHED','RECENT'],
                dict(CACHED='Banks',RECENT='Banks'),self.now,[]),0)
        fetch.assert_not_called()
        self.assertEqual(before,self.con.execute('SELECT * FROM evidence ORDER BY symbol').fetchall())

    def test_old_version_complete_and_partial_snapshots_are_recaptured_once_with_bounded_rotation(self):
        old=self.now-timedelta(minutes=30)
        symbols=['A_PARTIAL','B_COMPLETE','C_PARTIAL','D_COMPLETE']
        for symbol,complete,version in zip(symbols,[False,True,False,True],[None,None,'older','older']):
            payload=dict(complete=complete,contract_version='nse-bank-ratios-v1')
            if version:payload['collector_version']=version
            store.save(self.con,symbol,'bank_metrics','old source',payload,old.isoformat(),self.now)
        originals=self.con.execute('SELECT * FROM evidence ORDER BY symbol').fetchall()
        repaired=dict(complete=False,collector_version=BANK_COLLECTOR_VERSION,contract_version='nse-bank-ratios-v1')
        sectors={s:'Banks' for s in symbols}
        with patch('app.screening.official_financials.fetch_bank_metrics',return_value=repaired) as fetch:
            for symbol in symbols:
                now=datetime.now(timezone.utc)
                self.assertEqual(capture_bank_metrics(None,self.con,symbols,sectors,now,[],limit=1),1)
                self.assertEqual(fetch.call_args.args[1],symbol)
            self.assertEqual(capture_bank_metrics(None,self.con,symbols,sectors,datetime.now(timezone.utc),[],limit=1),0)
        self.assertEqual(fetch.call_count,len(symbols))
        self.assertEqual(originals,self.con.execute("SELECT * FROM evidence WHERE source='old source' ORDER BY symbol").fetchall())
        for symbol in symbols:
            latest=store.latest(self.con,symbol,'bank_metrics',datetime.now(timezone.utc),7)
            self.assertEqual(latest['collector_version'],BANK_COLLECTOR_VERSION)
            self.assertFalse(latest['complete'])
            self.assertEqual(latest['contract_version'],'nse-bank-ratios-v1')
            self.assertGreater(store.timestamp(latest['known_at']),old)

    def test_failed_old_cache_recollection_keeps_original_and_honors_backoff(self):
        old=self.now-timedelta(minutes=30)
        store.save(self.con,'BANK','bank_metrics','old source',dict(complete=False),old.isoformat(),self.now)
        original=self.con.execute('SELECT * FROM evidence').fetchone()
        with patch('app.screening.official_financials.fetch_bank_metrics',side_effect=ValueError('source unavailable')) as fetch:
            self.assertEqual(capture_bank_metrics(None,self.con,['BANK'],dict(BANK='Banks'),self.now,[]),1)
            self.assertEqual(capture_bank_metrics(None,self.con,['BANK'],dict(BANK='Banks'),datetime.now(timezone.utc),[]),0)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(original,self.con.execute("SELECT * FROM evidence WHERE kind='bank_metrics'").fetchone())
        attempt=store.latest(self.con,'BANK','bank_metric_attempt',datetime.now(timezone.utc),1)
        self.assertEqual(attempt['collector_version'],BANK_COLLECTOR_VERSION)
        self.assertEqual(attempt['status'],'failed')

    def test_throttle_stops_new_requests_and_deadline_submits_nothing(self):
        response=httpx.Response(429,request=httpx.Request('GET','https://example.test/financials'))
        error=httpx.HTTPStatusError('throttled',request=response.request,response=response)
        sectors=dict(AAA='Banks',BBB='Banks')
        with patch('app.screening.official_financials.fetch_bank_metrics',side_effect=error) as fetch:
            self.assertEqual(capture_bank_metrics(None,self.con,['AAA','BBB'],sectors,self.now,[]),1)
            self.assertEqual(fetch.call_count,1)
        with patch('app.screening.official_financials.fetch_bank_metrics') as fetch:
            self.assertEqual(capture_bank_metrics(None,self.con,['BBB'],sectors,self.now,[],budget_seconds=0),0)
        fetch.assert_not_called()

    def test_new_capture_uses_capture_time_and_retains_publication_provenance(self):
        data=dict(complete=False,period_end='2026-09-30',source_evidence=[
            dict(source='official',sha256='a'*64,published_at='2026-10-17T17:46:30+05:30')])
        with patch('app.screening.official_financials.fetch_bank_metrics',return_value=data):
            capture_bank_metrics(None,self.con,['BANK'],dict(BANK='Banks'),self.now,[])
        result=store.latest(self.con,'BANK','bank_metrics',datetime.now(timezone.utc),7)
        self.assertEqual(result['source_evidence'],data['source_evidence'])
        self.assertGreaterEqual(store.timestamp(result['known_at']),self.now)

    def test_complete_bank_evidence_is_visible_but_policy_remains_blocked(self):
        f=statements(financial_payload(),NOW)
        store.save(self.con,'BANK','fundamentals','secondary',f,NOW.isoformat(),NOW)
        metrics=dict(complete=True,gross_npa_pct=1.1,net_npa_pct=.3,capital_adequacy_pct=17,
                     statement_basis='Standalone',period_end='2026-06-30')
        store.save(self.con,'BANK','bank_metrics','NSE',metrics,NOW.isoformat(),NOW)
        row=build({'BANK':prices()},{'BANK'},{'BANK':'Private Banks'},self.con,NOW,
                  pd.Timestamp('2026-09-30'))['equities'][0]
        self.assertEqual(row['bank_metrics']['capital_adequacy_pct'],17)
        self.assertIn('financial-sector entry policy not validated',row['flags'])
        self.assertNotIn('financial-sector asset quality / capital adequacy not verified',row['flags'])
        self.assertFalse(row['actionable'])
        self.assertLessEqual(row['fundamentals']['points'],30)

    def test_partial_and_expired_bank_evidence_keep_unverified_flag(self):
        for symbol,complete,at in [('PARTIAL',False,NOW),('EXPIRED',True,NOW-timedelta(days=8))]:
            store.save(self.con,symbol,'fundamentals','secondary',statements(financial_payload(),NOW),NOW.isoformat(),NOW)
            store.save(self.con,symbol,'bank_metrics','NSE',dict(complete=complete),at.isoformat(),NOW)
            row=build({symbol:prices()},{symbol},{symbol:'Banks'},self.con,NOW,
                      pd.Timestamp('2026-09-30'))['equities'][0]
            self.assertIn('financial-sector asset quality / capital adequacy not verified',row['flags'])
            self.assertFalse(row['actionable'])
            self.assertEqual(row['bank_metrics'] is None,symbol=='EXPIRED')
