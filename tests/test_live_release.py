import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from app import live_release,release_gate,broker,order_journal,v2_live


class LiveReleaseAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'approval.json';self.commit='a'*40
        self.now=datetime(2026,10,6,12,tzinfo=timezone.utc)
        dates=[];day=self.now.date()-timedelta(days=80)
        while len(dates)<30:
            if day.weekday()<5 and day.isoformat() not in release_gate.INDIA_TRADING_HOLIDAYS:dates.append(day.isoformat())
            day+=timedelta(days=1)
        self.evidence=dict(source_commit=self.commit,known_blockers=0,known_critical=0,unexplained_reconciliation_mismatches=0,
            checks={k:dict(passed=True,source_commit=self.commit,evidence_reference='SYNTHETIC TEST ONLY',reviewed_by='fixture') for k in release_gate.REQUIRED},
            paper_sessions=dates,lifecycle_counts=dict(confirmed_entries=1,confirmed_exits=1,protection_observations=1),
            certified_scopes=['upstox:NSE:NSE_EQ:D:manual:2'],execution_authorization=dict(source_commit=self.commit,
                approved_by='fixture',reference='ISOLATED TEST ONLY',issued_at=(self.now-timedelta(hours=1)).isoformat(),
                expires_at=(self.now+timedelta(hours=1)).isoformat(),scopes=[dict(account_id=2,broker='upstox',venue='NSE',segment='NSE_EQ',product='D',model='manual')]))
        self.env=patch.dict(os.environ,OPENSTOCKS_LIVE_RELEASE_EVIDENCE=str(self.path),OPENSTOCKS_BUILD_COMMIT=self.commit)
        self.env.start();self.addCleanup(self.env.stop);self.write()

    def write(self):self.path.write_text(json.dumps(self.evidence));self.path.chmod(0o600)

    def test_exact_reviewed_account_route_model_only(self):
        self.assertTrue(live_release.authorized(2,now=self.now)[0])
        for args in (dict(user_id=3),dict(user_id=True),dict(user_id=2,product='I'),dict(user_id=2,model='quality_momentum'),dict(user_id=2,broker='angelone')):
            self.assertFalse(live_release.authorized(**args,now=self.now)[0])

    def test_malformed_scope_or_nontext_approval_never_grants_access(self):
        for scopes in ({'account_id':2},[dict(self.evidence['execution_authorization']['scopes'][0],account_id=True)],
                       [dict(self.evidence['execution_authorization']['scopes'][0],extra='unreviewed')]):
            self.evidence['execution_authorization']['scopes']=scopes;self.write()
            self.assertFalse(live_release.authorized(2,now=self.now)[0])

    def test_expired_naive_wrong_build_missing_and_public_evidence_refuse(self):
        self.assertFalse(live_release.authorized(2,now=self.now+timedelta(hours=2))[0])
        self.path.chmod(0o644);self.assertFalse(live_release.authorized(2,now=self.now)[0]);self.path.chmod(0o600)
        with patch.dict(os.environ,OPENSTOCKS_BUILD_COMMIT='b'*40):self.assertFalse(live_release.authorized(2,now=self.now)[0])
        self.evidence['execution_authorization']['issued_at']='2026-10-06T10:00:00';self.write()
        self.assertFalse(live_release.authorized(2,now=self.now)[0]);self.path.unlink()
        self.assertFalse(live_release.authorized(2,now=self.now)[0])

    def test_missing_gate_or_idle_sessions_cannot_authorize(self):
        self.evidence['checks']['native_protection']['passed']=False;self.write()
        self.assertFalse(live_release.authorized(2,now=self.now)[0])
        self.evidence['checks']['native_protection']['passed']=True
        self.evidence['lifecycle_counts']['confirmed_exits']=0;self.write()
        self.assertFalse(live_release.authorized(2,now=self.now)[0])

    def test_armed_account_cannot_send_new_order_without_authorization(self):
        con=sqlite3.connect(':memory:');self.addCleanup(con.close);v2_live.ensure_schema(con)
        self.path.unlink()
        with patch.object(broker,'state',return_value=dict(live_ready=True)),patch.object(broker,'place_order',side_effect=AssertionError('real order')):
            result=order_journal.submit(con,2,'IN','TEST','NSE_EQ|TEST','BUY',20,100,'D','manual',stop=99,available_cash=10000)
        self.assertIn('unavailable',result)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM v2_live_orders').fetchone()[0],0)
