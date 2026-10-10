import unittest
import json
import sqlite3
from fastapi.testclient import TestClient
from scripts.rehearse_approved_ui import fixture_app


class ApprovedOrderAPITest(unittest.TestCase):
    def setUp(self):
        # Isolated process-wide fixture config restored even when a test fails.
        from app import v2_web,broker
        self.original=(v2_web.V2_DB,v2_web.MAIN_DB,v2_web._regime_state,v2_web._live_map,broker.STATE_DIR,broker.LEGACY_PATH,broker.verify)
        self.addCleanup(self.restore)
        self.app=fixture_app();self.fixture=self.app.state.fixture
        self.addCleanup(self.fixture.doCleanups);self.addCleanup(self.fixture.con.close);self.addCleanup(self.fixture.catalogue.close)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def restore(self):
        from app import v2_web,broker
        v2_web.V2_DB,v2_web.MAIN_DB,v2_web._regime_state,v2_web._live_map,broker.STATE_DIR,broker.LEGACY_PATH,broker.verify=self.original

    def test_real_handlers_report_owned_fills_and_reconcile_accounting(self):
        plan=self.client.get('/v2/api/approved-plans').json()['plans'][0]
        self.assertEqual(plan['status'],'approved')
        payload=dict(plan_id=plan['id'],request_key='api-request-one')
        fill=self.client.post('/v2/api/approved-orders',json=payload)
        self.assertEqual(fill.status_code,202,fill.text)
        self.assertEqual(fill.json()['status'],'pending')
        self.assertFalse(fill.json()['paper_recorded'])
        retry=self.client.post('/v2/api/approved-orders',json=payload)
        self.assertEqual(retry.json(),fill.json())
        self.assertEqual(self.client.get('/v2/api/paper-performance').json()['book']['positions'],0)
        self.assertEqual(self.client.get('/v2/api/paper-orders').json()['orders'][0]['status'],'pending')
        self.assertEqual(self.client.post('/fixture/fill').json()['filled'],1)
        receipt=self.client.post('/v2/api/approved-orders',json=payload)
        self.assertEqual(receipt.status_code,200,receipt.text)
        self.assertEqual(receipt.json()['status'],'filled')
        self.assertEqual(self.client.get('/v2/api/paper-ledger').json()['cash_difference_minor'],0)
        self.assertEqual(self.client.get('/v2/api/paper-performance').json()['book']['positions'],1)
        self.assertEqual(self.client.post('/fixture/close').json()['closed'],1)
        report=self.client.get('/v2/api/paper-performance').json()
        self.assertEqual(report['book']['positions'],0);self.assertEqual(report['current_epoch']['trades'],1)
        self.assertEqual(report['by_regime_epoch']['ON']['trades'],1)

    def test_override_cross_owner_no_session_and_regime_off_refuse(self):
        from app import v2_web
        plan=self.client.get('/v2/api/approved-plans').json()['plans'][0]
        payload=dict(plan_id=plan['id'],request_key='api-request-one')
        self.assertEqual(self.client.post('/v2/api/approved-orders',json=dict(payload,qty=1)).status_code,400)
        v2_web._regime_state=lambda market:'OFF'
        self.assertEqual(self.client.post('/v2/api/approved-orders',json=payload).status_code,409)
        self.app.dependency_overrides[v2_web.require_session]=lambda:dict(id=3,username='other')
        self.assertEqual(self.client.get('/v2/api/approved-plans').json()['plans'],[])
        self.assertEqual(self.client.post('/v2/api/approved-orders',json=payload).status_code,404)
        self.assertEqual(self.client.get('/v2/api/paper-performance').json()['book']['equity'],10000)
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get('/v2/api/approved-plans').status_code,401)
        self.assertEqual(self.client.post('/v2/api/approved-orders',json=payload).status_code,401)

    def test_malformed_order_and_health_evidence_fail_explicitly(self):
        for payload in ({},{'plan_id':[],'request_key':'request-test'},
                        {'plan_id':'plan_fixture','request_key':True}):
            self.assertEqual(self.client.post('/v2/api/approved-orders',json=payload).status_code,400)
        with sqlite3.connect(self.fixture.path) as con:
            con.execute('INSERT INTO broker_reconciliation VALUES(2,1,\'unknown\',NULL,?)',
                        (json.dumps({'reasons':['fixture']}),))
        health=self.client.get('/v2/api/execution-health')
        self.assertEqual(health.status_code,200);self.assertEqual(health.json()['owner_user_id'],2)
        self.assertEqual(health.json()['reconciliation']['evidence']['reasons'],['fixture'])
        with sqlite3.connect(self.fixture.path) as con:
            con.execute("UPDATE broker_reconciliation SET payload='corrupt'")
        self.assertEqual(self.client.get('/v2/api/execution-health').status_code,503)
