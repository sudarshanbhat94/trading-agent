"""Rejected predicates survive replacement; research observations never fill."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.screening import confirmation as c,tracking as t,validation as v
from tests.test_idea_tracking import NOW,plan


class AssessmentAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ideas.db'
        self.plan=plan(qty=20)
        self.plan.update(model_version='conditional-pullback-v2',confirmation_policy=c.POLICY,cost_profile=t.cost_profile())
        t.publish(self.path,2,[self.plan],issued_at=NOW.isoformat(),now=NOW)
        self.con=t.connect(self.path);self.addCleanup(self.con.close)

    def assessment(self,seconds=0):
        return c.assess(self.plan,{'status':'WAITING'},[],{},
                        {'regime':'OFF','regime_fresh':True,'risk_ok':False},NOW+timedelta(seconds=seconds))

    def test_negative_and_after_hours_predicates_are_not_lost(self):
        first=self.assessment();second=self.assessment(30)
        first['context']={'regime':'OFF','evidence_ok':False,'news_checked_at':NOW.isoformat()}
        with self.con:
            c.record_assessment(self.con,1,first,{})
            c.record_assessment(self.con,1,second,first)
        history=t.assessment_history(self.path,2,1)
        self.assertEqual(history['total'],2)
        self.assertEqual(history['events'][1]['assessment']['context']['news_checked_at'],NOW.isoformat())
        self.assertFalse(history['execution_approved'])
        self.assertTrue(any(not x['passed'] for x in history['events'][0]['assessment']['checks']))
        self.assertEqual(json.loads(self.con.execute('SELECT payload FROM assessments').fetchone()[0])['checked_at'],second['checked_at'])

    def test_observations_do_not_change_registered_replay_or_protocol(self):
        protocol=v.protocol(NOW-timedelta(seconds=1));digest=json.dumps(protocol,sort_keys=True)
        before=v.report(self.path,2,protocol,end=(NOW+timedelta(seconds=30)).isoformat())
        with self.con:c.record_assessment(self.con,1,self.assessment(),{})
        after=v.report(self.path,2,protocol,end=(NOW+timedelta(seconds=30)).isoformat())
        self.assertEqual(before,after);self.assertEqual(digest,json.dumps(protocol,sort_keys=True))
        self.assertEqual(after['confirmed']['fills'],[])

    def test_history_is_owned_paginated_read_only_and_immutable(self):
        with self.con:
            for n in range(3):c.record_assessment(self.con,1,self.assessment(n),{})
        before=hashlib.sha256(self.path.read_bytes()).digest();changes=self.con.total_changes
        self.assertIsNone(t.assessment_history(self.path,3,1))
        page=t.assessment_history(self.path,2,1,limit=1,offset=1)
        self.assertEqual(page['total'],3);self.assertEqual(len(page['events']),1)
        self.assertEqual(self.con.total_changes,changes)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),before)
        for table in ('assessment_events','publications'):
            with self.assertRaises(sqlite3.IntegrityError):self.con.execute(f'DELETE FROM {table}')
            self.con.rollback()

    def test_repeated_same_observation_is_idempotent_and_positive_events_preserved(self):
        result=self.assessment();result.update(eligible=True,baseline_eligible=True,confirmation_at=NOW.isoformat())
        with self.con:
            c.record_assessment(self.con,1,result,{})
            c.record_assessment(self.con,1,result,{})
        kinds=dict(self.con.execute('SELECT kind,COUNT(*) FROM assessment_events GROUP BY kind'))
        self.assertEqual(kinds,{'ASSESSMENT_OBSERVED':1,'CONFIRMATION_RECORDED':1,'ENTRY_ELIGIBLE_SHADOW':1,'ZONE_ELIGIBLE_BASELINE':1})
