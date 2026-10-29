import json
import hashlib
import sqlite3
import tempfile
import unittest
from tests.contract_storage_fixtures import ContractStorageCase
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import v2_live,books,v2_web
from app.screening import tracking as t,confirmation as c
from tests.test_idea_tracking import NOW,plan
from tests.test_release_safety import quotes


class AccountEvidenceAPITest(ContractStorageCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.paper=self.root/'paper.db';self.tracker=self.root/'ideas.db'
        con=sqlite3.connect(self.paper);v2_live.ensure_schema(con)
        books.buy(con,2,'IN','manual','TEST',100,20,99,110,quotes=quotes(TEST=100))
        con.close();t.publish(self.tracker,2,[plan()],issued_at=NOW.isoformat(),now=NOW)
        con=t.connect(self.tracker)
        result=c.assess(plan(),{'status':'WAITING'},[],{}, {},NOW)
        with con:c.record_assessment(con,1,result,{})
        con.close()
        app=FastAPI();app.include_router(v2_web.router)
        self.user={'id':2}
        app.dependency_overrides[v2_web.require_session]=lambda:self.user
        self.client=TestClient(app);self.addCleanup(self.client.close)
        self.addCleanup(patch.stopall)
        patch.object(v2_web,'V2_DB',str(self.paper)).start()
        patch.object(v2_web,'_live_map',return_value=quotes(TEST=100)).start()
        patch.object(t,'default_path',return_value=str(self.tracker)).start()

    def test_balances_and_performance_are_account_owned_and_do_not_write(self):
        before=hashlib.sha256(self.paper.read_bytes()).digest()
        r=self.client.get('/v2/api/paper-ledger');self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['status'],'ok')
        p=self.client.get('/v2/api/paper-performance');self.assertEqual(p.status_code,200)
        self.assertEqual(p.json()['book']['positions'],1)
        self.assertTrue(p.json()['valuation_complete'])
        self.user={'id':3}
        self.assertEqual(self.client.get('/v2/api/paper-ledger').json()['events'],0)
        self.assertEqual(self.client.get('/v2/api/paper-performance').json()['book']['positions'],0)
        self.assertEqual(hashlib.sha256(self.paper.read_bytes()).digest(),before)

    def test_assessment_owner_and_invalid_paging_or_market(self):
        url='/v2/api/idea-publications/1/assessments'
        self.assertEqual(self.client.get(url).json()['total'],1)
        self.assertEqual(self.client.get(url+'?limit=0').status_code,400)
        self.assertEqual(self.client.get('/v2/api/paper-ledger?market=MCX').status_code,400)
        self.assertEqual(self.client.get('/v2/api/paper-performance?day=no').status_code,400)
        self.user={'id':3};self.assertEqual(self.client.get(url).status_code,404)
