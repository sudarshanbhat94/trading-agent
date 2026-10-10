import sqlite3
import unittest
from app import v2_live,execution_outbox,incident_inbox


class IncidentInboxTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close);v2_live.ensure_schema(self.con)
        with self.con:execution_outbox.incident(self.con,2,'NATIVE_PROTECTION','fixture','Unknown broker stop')

    def test_ack_is_owned_immutable_idempotent_and_does_not_clear_risk(self):
        row=incident_inbox.report(self.con,2)[0]
        self.assertEqual(incident_inbox.report(self.con,3),[])
        with self.assertRaises(ValueError):incident_inbox.acknowledge(self.con,3,row['id'],row['fingerprint'])
        first=incident_inbox.acknowledge(self.con,2,row['id'],row['fingerprint'])
        self.assertEqual(incident_inbox.acknowledge(self.con,2,row['id'],row['fingerprint']),first)
        self.assertFalse(first['risk_cleared']);self.assertIsNotNone(incident_inbox.report(self.con,2)[0]['acknowledged_at'])
        self.assertIsNone(self.con.execute('SELECT resolved_at FROM execution_incidents').fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM execution_incident_acknowledgements')
        self.con.rollback()

    def test_changed_incident_requires_a_new_review(self):
        row=incident_inbox.report(self.con,2)[0];incident_inbox.acknowledge(self.con,2,row['id'],row['fingerprint'])
        with self.con:execution_outbox.incident(self.con,2,'NATIVE_PROTECTION','fixture','Changed stop evidence')
        latest=incident_inbox.report(self.con,2)[0]
        self.assertIsNone(latest['acknowledged_at'])
        with self.assertRaises(ValueError):incident_inbox.acknowledge(self.con,2,row['id'],row['fingerprint'])
