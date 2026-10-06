import json
import os
from pathlib import Path
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from app import approved_execution,books,entry_contracts,paper_ledger,v2_web
from tests.test_approved_execution import ApprovedPaperPipelineTest


class ManualApprovedExecutionTest(unittest.TestCase):
    def setUp(self):
        self.fixture=ApprovedPaperPipelineTest();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source=Path(self.fixture.tmp.name)/'separate-catalogue.db'
        with sqlite3.connect(self.source) as con:self.fixture.catalogue.backup(con)
        self.quote={'TEST':{'price':100,'ts':self.fixture.now.isoformat()}}

    def submit(self,con,catalogue):
        return approved_execution.submit_manual_paper(con,catalogue,2,'TEST','concurrent-manual-request',self.quote,
            quantity=20,stop=99,target=110,regime='ON',now=self.fixture.now)

    def test_concurrent_manual_requests_create_one_owned_plan_and_fill(self):
        barrier=Barrier(2)
        def run():
            with sqlite3.connect(self.fixture.path,timeout=5) as con,sqlite3.connect(self.source) as catalogue:
                barrier.wait();return self.submit(con,catalogue)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(results[0],results[1]);self.assertTrue(results[0]['ok'])
        con=self.fixture.con
        self.assertEqual(con.execute('SELECT COUNT(*) FROM manual_plan_bindings').fetchone()[0],1)
        self.assertEqual(len(books.positions(con,2)),1)
        ledger=paper_ledger.report(con,2,'IN',books.current_epoch(con,2),books.cash(con,2))
        self.assertEqual(ledger['cash_difference_minor'],0)

    def test_fill_fault_rolls_back_manual_approval_binding_and_position(self):
        con=self.fixture.con;before=con.execute('SELECT COUNT(*) FROM approved_execution_plans').fetchone()[0]
        with patch.object(paper_ledger,'entry',side_effect=RuntimeError('synthetic disk failure')):
            with self.assertRaises(RuntimeError):self.submit(con,self.fixture.catalogue)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM approved_execution_plans').fetchone()[0],before)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM manual_plan_bindings').fetchone()[0],0)
        self.assertEqual(books.positions(con,2),[]);self.assertEqual(books.cash(con,2),10000)
        self.assertTrue(self.submit(con,self.fixture.catalogue)['ok'])

    def test_api_discovery_and_approved_fill_use_configured_separate_catalogue(self):
        with patch.dict(os.environ,OPENSTOCKS_CATALOGUE_DB=str(self.source)), \
                patch.object(v2_web,'MAIN_DB','/nonexistent-market-data.db'), \
                patch.object(v2_web,'V2_DB',str(self.fixture.path)), \
                patch.object(v2_web,'_live_map',return_value=self.quote),patch.object(v2_web,'_regime_state',return_value='ON'):
            data=json.loads(v2_web.api_instrument(symbol='TEST',venue='NSE',segment='NSE_EQ',user={'id':2}).body)
            self.assertEqual(data['instrument_id'],self.fixture.spec.id)
            result=v2_web.api_approved_order({'plan_id':self.fixture.plan_id,'request_key':'separate-catalogue-request'},{'id':2})
            self.assertEqual(result.status_code,200,result.body)

    def test_manual_approval_never_rounds_or_promotes_invalid_levels(self):
        for stop in (True,99.03):
            try:
                result=approved_execution.submit_manual_paper(self.fixture.con,self.fixture.catalogue,2,'TEST','manual-invalid-'+str(stop),
                    self.quote,quantity=20,stop=stop,target=110,regime='ON',now=self.fixture.now)
            except ValueError:continue
            self.assertFalse(result['ok'])
        self.assertEqual(books.positions(self.fixture.con,2),[])
