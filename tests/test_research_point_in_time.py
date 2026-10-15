from copy import deepcopy
from datetime import datetime,timedelta,timezone
import sqlite3
import unittest
from app import research_data as data


class PointInTimeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close)
        data.ensure_schema(self.con)
        self.now=datetime.now(timezone.utc);self.before=(self.now-timedelta(days=1)).isoformat()
        self.row=dict(instrument_id='ins_fixture',kind='membership',fact_key='NIFTY100:membership',
            effective_at=self.before,published_at=self.before,observed_at=self.before,source='SYNTHETIC',rights_reference='fixture',
            payload=dict(index='NIFTY100',member=True))

    def test_later_correction_cannot_rewrite_decision_time_cohort(self):
        data.record(self.con,[self.row],now=self.now)
        update=deepcopy(self.row);update.update(published_at=self.now.isoformat(),observed_at=self.now.isoformat())
        update['payload']['member']=False;data.record(self.con,[update],now=self.now)
        self.assertTrue(data.known(self.con,'ins_fixture','membership',self.before)[0]['payload']['member'])
        self.assertFalse(data.known(self.con,'ins_fixture','membership',self.now.isoformat())[0]['payload']['member'])
        self.assertFalse(data.coverage(self.con,['ins_fixture'],self.now.isoformat())['instruments'][0]['research_complete'])
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM research_facts')

    def test_future_observation_and_unadjusted_bar_cannot_enter_validation(self):
        bad=dict(self.row,observed_at=(self.now+timedelta(days=1)).isoformat())
        with self.assertRaises(ValueError):data.record(self.con,[bad],now=self.now)
        bad=dict(self.row,kind='bar',payload=dict(open=100,high=110,low=90,close=101,volume=100,currency='INR'))
        with self.assertRaises(ValueError):data.record(self.con,[bad],now=self.now)
        self.assertEqual(data.known(self.con,'ins_fixture','membership',self.before),[])
