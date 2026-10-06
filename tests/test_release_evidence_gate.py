import unittest
from datetime import datetime,timedelta,timezone
from app.release_gate import evaluate,REQUIRED,INDIA_TRADING_HOLIDAYS
from app.jobs_health import assess


class ReleaseEvidenceTest(unittest.TestCase):
    def evidence(self):
        commit='a'*40
        days=[(datetime(2026,6,1)+timedelta(days=i)).date() for i in range(90)]
        dates=[d.isoformat() for d in days if d.weekday()<5 and d.isoformat() not in INDIA_TRADING_HOLIDAYS][:30]
        return dict(source_commit=commit,known_blockers=0,known_critical=0,unexplained_reconciliation_mismatches=0,
                    checks={k:dict(passed=True,source_commit=commit,evidence_reference='isolated-test-fixture',reviewed_by='fixture-reviewer') for k in REQUIRED},
                    paper_sessions=dates,paper_session_evidence={d:dict(source_commit=commit,evidence_reference='synthetic session fixture',reviewed_by='fixture',events={'owned_position_observations':1}) for d in dates},
                    lifecycle_counts=dict(confirmed_entries=1,confirmed_exits=1,protection_observations=1),
                    certified_scopes=['upstox:NSE:NSE_EQ:D:manual:2'])

    def test_missing_or_wrong_build_evidence_fails_closed(self):
        self.assertFalse(evaluate({},'a'*40)['go'])
        self.assertFalse(evaluate(self.evidence(),'b'*40)['go'])
        with self.assertRaises(ValueError):evaluate(self.evidence(),'short')

    def test_each_reviewed_gate_is_required(self):
        for key in REQUIRED:
            with self.subTest(key=key):
                evidence=self.evidence();del evidence['checks'][key]
                self.assertFalse(evaluate(evidence,'a'*40)['go'])
        self.assertTrue(evaluate(self.evidence(),'a'*40)['go'])

    def test_idle_duplicate_or_incomplete_sessions_do_not_establish_readiness(self):
        for update in ({'paper_sessions':['2026-01-01']*30},{'paper_sessions':['2026-99-99']*30},{'paper_sessions':['2099-01-01']*30},{'paper_session_evidence':{}},{'lifecycle_counts':{'confirmed_entries':0}},{'known_critical':1},{'certified_scopes':[]}):
            self.assertFalse(evaluate(dict(self.evidence(),**update),'a'*40)['go'])

    def test_true_objects_and_idle_record_cannot_act_as_review_evidence(self):
        evidence=self.evidence();evidence['checks']['security']['reviewed_by']=True
        self.assertFalse(evaluate(evidence,'a'*40)['go'])
        evidence=self.evidence();day=evidence['paper_sessions'][0];evidence['paper_session_evidence'][day]['events']={'reconciliation_observations':1}
        self.assertFalse(evaluate(evidence,'a'*40)['go'])

    def test_quote_sla_is_not_rounded_to_hide_121_second_staleness(self):
        now=datetime(2026,10,6,4,tzinfo=timezone.utc)
        for delta,expected in [(120,'ok'),(121,'stale'),(-600,'unknown')]:
            checks=assess({'quotes':{'latest':(now-timedelta(seconds=delta)).isoformat(),'rows':1}},now)
            self.assertEqual(next(c['status'] for c in checks if c['pipeline']=='quotes'),expected)
