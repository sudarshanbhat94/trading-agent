from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from app import catalogue_ingestion as ingestion,execution_contracts,entry_contracts
from app.instrument_catalog import Instrument


class EvidenceBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close)
        self.now=datetime.now(timezone.utc);self.at=self.now.isoformat();start=self.now.replace(hour=0,minute=0,second=0,microsecond=0)
        self.start=start.isoformat();self.end=(start+timedelta(days=1)).isoformat()
        content=b'SYNTHETIC exchange document; not commercial permission';(self.root/'source.fixture').write_bytes(content)
        self.spec=Instrument('NSE','NSE_EQ','EQUITY','TEST','INR','TEST','EQ')
        common=dict(observed_at=self.at,effective_from=self.start,effective_until=self.end,source_artifacts=['fixture'])
        rules=dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',calendar='NSE:fixture',
                   lower_circuit=80,upper_circuit=120,banned=False,corporate_action_pending=False,actions_reviewed_at=self.at)
        self.bundle=dict(schema='openstocks-contract-evidence-v1',normalizer_version='synthetic-v1',review_reference='fixture-only',
            observed_at=self.at,source_day=self.at[:10],artifacts=[dict(id='fixture',path='source.fixture',
            source='https://www.nseindia.com/all-reports',sha256=hashlib.sha256(content).hexdigest(),
            observed_at=self.at,rights_reference='SYNTHETIC; not licensed market data')],
            instruments=[dict(common,contract=asdict(self.spec),aliases={'upstox':'NSE_EQ|TEST','angelone':'NSE_EQ|123'},rules=rules)],
            sessions=[dict(common,calendar='NSE:fixture',payload=dict(open=True,opens_at=self.start,closes_at=self.end))])

    def test_reviewed_import_is_atomic_dated_and_idempotent_without_enabling_execution(self):
        result=ingestion.import_bundle(self.con,self.bundle,self.root,now=self.now)
        self.assertEqual(result['rules'],1);self.assertFalse(result['execution_enabled'])
        self.assertEqual(ingestion.import_bundle(self.con,self.bundle,self.root,now=self.now),result)
        spec,key,evidence=execution_contracts.order_contract(self.con,instrument_id=self.spec.id,quantity=20,price=100,now=self.now)
        self.assertEqual(key,'NSE_EQ|TEST');self.assertEqual(spec.tick_size,'0.05')
        with entry_contracts.using(self.con,self.now):
            self.assertEqual(entry_contracts.check('IN','TEST',20,100,99,110,regime='ON')['instrument_id'],self.spec.id)

    def test_corruption_future_source_unknown_rules_and_ambiguous_aliases(self):
        bad=deepcopy(self.bundle);bad['artifacts'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):ingestion.import_bundle(self.con,bad,self.root,now=self.now)
        bad=deepcopy(self.bundle);bad['artifacts'][0]['observed_at']=(self.now+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):ingestion.import_bundle(self.con,bad,self.root,now=self.now)
        bad=deepcopy(self.bundle);bad['instruments'][0]['rules']['lot_size']=True
        with self.assertRaises(ValueError):ingestion.import_bundle(self.con,bad,self.root,now=self.now)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM instrument_snapshots').fetchone()[0],0)
        bad=deepcopy(self.bundle);bad['instruments'][0]['rules']=None
        result=ingestion.import_bundle(self.con,bad,self.root,now=self.now);self.assertEqual(result['quarantined'],1)
        with self.assertRaises(ValueError):execution_contracts.order_contract(self.con,instrument_id=self.spec.id,quantity=20,price=100,now=self.now)

    def test_traversal_and_unreviewed_sources_refuse(self):
        for updates in ({'path':'../outside'},{'source':'https://attacker.invalid/feed'},{'rights_reference':''}):
            bad=deepcopy(self.bundle);bad['artifacts'][0].update(updates)
            with self.assertRaises(ValueError):ingestion.import_bundle(self.con,bad,self.root,now=self.now)
