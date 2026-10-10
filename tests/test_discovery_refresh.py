"""A cold evidence cache must not masquerade as a fully screened empty market."""
import copy
import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx

from app.screening import store, plans
from app.screening.health import discovery_health
from scripts.evidence_screen import statement_queue, capture_statements
from tests.test_evidence_screen import NOW, financial_payload
from tests.test_stock_plans import book, screen
from app.screening.providers import statements


class DiscoveryRefreshTest(unittest.TestCase):
    def test_cold_setup_is_fetched_before_alphabetic_nonsetup_and_recent_failures_wait(self):
        symbols=['AAA','SETUP','FAILED','READY','SHORT','NEVER']
        f=statements(financial_payload(),NOW)
        short=copy.deepcopy(f);short['earnings_periods']=short['earnings_periods'][-1:]
        short['earnings_years']=short['positive_earnings_years']=1
        cached={'READY':f,'SHORT':short}
        attempts={'FAILED':dict(status='failed',known_at=(NOW-timedelta(minutes=5)).isoformat()),
                  'SETUP':dict(status='captured',known_at=(NOW-timedelta(days=1)).isoformat())}
        features={'SETUP':dict(setup='pullback')}
        queue=statement_queue(symbols,cached,features,attempts,NOW)
        self.assertEqual(queue,['AAA','NEVER','SETUP'])
        attempts['FAILED']['known_at']=(NOW-timedelta(hours=2)).isoformat()
        self.assertIn('FAILED',statement_queue(symbols,cached,features,attempts,NOW))
        # Priority within the same attempted/unattempted group is structural,
        # but new source coverage is never starved by a failing favourite.
        self.assertEqual(statement_queue(['AAA','SETUP'],{},features,{},NOW),['SETUP','AAA'])

    def evidence(self):
        con=sqlite3.connect(':memory:');store.initialise(con)
        self.addCleanup(con.close)
        return con

    def test_capture_appends_real_history_and_records_failure_without_relabelling_old_data(self):
        con=self.evidence();old=dict(financial_contract_version='legacy')
        store.save(con,'FAILED','fundamentals','old',old,NOW.isoformat(),NOW)
        def fetch(http,symbol,now):
            if symbol=='FAILED':raise ValueError('malformed source')
            return statements(financial_payload(),NOW)
        errors=[]
        with patch('scripts.evidence_screen.providers.fetch_statements',side_effect=fetch):
            result=capture_statements(None,con,['GOOD','FAILED','DEFERRED'],errors,2,min_interval=0)
        self.assertEqual((result['requested'],result['captured'],result['deferred']),(2,1,1))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM evidence WHERE symbol='DEFERRED'").fetchone()[0],0)
        self.assertEqual(json.loads(con.execute("SELECT payload FROM evidence WHERE kind='fundamentals' AND symbol='FAILED'").fetchone()[0]),old)
        attempts={s:json.loads(p)['status'] for s,p in con.execute("SELECT symbol,payload FROM evidence WHERE kind='fundamental_attempt'")}
        self.assertEqual(attempts,dict(GOOD='captured',FAILED='failed'))
        self.assertEqual(errors,['FAILED fundamentals unavailable: ValueError'])
        saved=json.loads(con.execute("SELECT payload FROM evidence WHERE kind='fundamentals' AND symbol='GOOD'").fetchone()[0])
        self.assertEqual(len(saved['earnings_periods']),3)
        self.assertEqual({r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")},{'evidence','screens'})

    def test_time_budget_stops_submission_and_throttling_leaves_remainder_for_next_cycle(self):
        con=self.evidence()
        with patch('scripts.evidence_screen.providers.fetch_statements') as fetch:
            result=capture_statements(None,con,['A','B'],[],400,budget_seconds=0)
        fetch.assert_not_called();self.assertEqual(result['deferred'],2)
        def throttled(http,symbol,now):
            response=httpx.Response(429,request=httpx.Request('GET','https://example.test/statements'))
            response.raise_for_status()
        errors=[]
        with patch('scripts.evidence_screen.providers.fetch_statements',side_effect=throttled):
            result=capture_statements(None,con,list('ABCDEFGH'),errors,400,min_interval=0)
        self.assertTrue(result['source_paused'])
        self.assertLessEqual(result['requested'],3)
        self.assertGreaterEqual(result['deferred'],5)

    def test_current_partial_coverage_and_regime_are_separate_from_research_admission(self):
        data=screen(2);data['equities'][1]['fundamentals'].pop('earnings_periods')
        data['regime']='OFF'
        result=plans.shortlist(data,book(),now=NOW)
        self.assertEqual([p['symbol'] for p in result['ideas']],['STOCK0'])
        health=result['discovery_health']
        self.assertEqual((health['status'],health['screened'],health['earnings_history_ready'],health['earnings_history_missing']),('incomplete',2,1,1))
        self.assertFalse(result['ideas'][0]['actionable'])
        self.assertTrue(any('counts alone' in r['reason'] for r in health['rejection_counts']))

    def test_complete_cache_does_not_admit_failed_participation_or_expired_history(self):
        data=screen(1);data['equities'][0]['metrics']['relative_volume']=1
        result=plans.shortlist(data,book(),now=NOW)
        self.assertEqual((result['count'],result['discovery_health']['status']),(0,'complete'))
        self.assertIn('Participation gate failed',result['discovery_health']['rejection_counts'][0]['reason'])
        self.assertEqual(discovery_health(data,[],NOW+timedelta(days=600))['earnings_history_ready'],0)
        data['stale']=True
        self.assertEqual(discovery_health(data,[],NOW)['status'],'stale')

    def test_counts_keep_independent_predicates_overlapping(self):
        health=discovery_health(screen(2),[dict(symbol='STOCK0',reason='news; delivery; news'),dict(symbol='STOCK1',reason='news'),dict(symbol='STOCK0',reason='news')],NOW)
        self.assertEqual(health['rejection_counts'],[dict(reason='news',count=2),dict(reason='delivery',count=1)])


if __name__=='__main__':unittest.main()
