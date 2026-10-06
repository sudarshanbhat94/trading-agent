import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from app import execution_contracts as contracts
from app.instrument_catalog import Instrument,InstrumentError,import_snapshot,upstox_contract


class ExecutionContractTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close)
        self.now=datetime(2026,10,6,5,tzinfo=timezone.utc);self.start=self.now.replace(hour=0);self.end=self.start+timedelta(days=1)
        self.spec=Instrument('NSE','NSE_EQ','EQUITY','TEST','INR','TEST','EQ')
        import_snapshot(self.con,[(self.spec,'NSE_EQ|TEST')],provider='upstox',source='fixture',source_day='2026-10-06',observed_at=self.start.isoformat(),now=self.now)
        self.rules=dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',lower_circuit=80,upper_circuit=120,
                        calendar='NSE:NSE_EQ:2026-10-06',banned=False,corporate_action_pending=False,actions_reviewed_at=self.start.isoformat())
        self.session=dict(open=True,opens_at=self.start.replace(hour=3,minute=45).isoformat(),closes_at=self.start.replace(hour=10).isoformat())
        self.record('session',self.rules['calendar'],self.session)

    def record(self,kind,identity,payload,observed=None):
        return contracts.record(self.con,kind,identity,payload,source='dated independent fixture',observed_at=(observed or self.start).isoformat(),
                                 effective_from=self.start.isoformat(),effective_until=self.end.isoformat(),now=self.now)

    def order(self,**kwargs):
        return contracts.order_contract(self.con,instrument_id=self.spec.id,quantity=20,price=100,now=kwargs.get('now',self.now))

    def test_sourced_contract_and_exact_session_enable_only_the_fixture(self):
        with self.assertRaises(InstrumentError):self.order()
        self.record('rules',self.spec.id,self.rules)
        spec,key,evidence=self.order();self.assertEqual(key,'NSE_EQ|TEST');self.assertEqual(spec.tick_size,'0.05')
        self.assertTrue(evidence['rules_id']);self.assertEqual(self.spec.id,spec.id)
        with self.assertRaises(InstrumentError):self.order(now=self.start.replace(hour=11))

    def test_bans_corporate_actions_holiday_and_circuits_refuse(self):
        self.record('rules',self.spec.id,self.rules)
        for field in ('banned','corporate_action_pending'):
            self.record('rules',self.spec.id,dict(self.rules,**{field:True}),self.now-timedelta(minutes=2))
            with self.assertRaises(InstrumentError):self.order()
        self.record('rules',self.spec.id,dict(self.rules,lower_circuit=101),self.now-timedelta(minutes=1))
        with self.assertRaises(InstrumentError):self.order()
        self.record('rules',self.spec.id,self.rules,self.now)
        self.record('session',self.rules['calendar'],dict(open=False),self.now)
        with self.assertRaises(InstrumentError):self.order()

    def test_future_conflicting_or_unreviewed_evidence_never_leaks_into_decisions(self):
        with self.assertRaises(InstrumentError):self.record('rules',self.spec.id,self.rules,self.now+timedelta(seconds=1))
        self.record('rules',self.spec.id,self.rules,self.now)
        self.record('rules',self.spec.id,dict(self.rules,freeze_quantity=50),self.now)
        with self.assertRaises(InstrumentError):self.order()
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM execution_contract_evidence')
        self.con.rollback()

    def test_malformed_units_are_refused_without_truncation(self):
        for kw in (dict(tick_size=True),dict(tick_size='invalid'),dict(freeze_quantity=True)):
            with self.assertRaises(InstrumentError):replace(self.spec,**kw)
        raw=dict(exchange='NSE',segment='NSE_EQ',instrument_type='EQ',isin='TEST',instrument_key='NSE_EQ|TEST',trading_symbol='TEST',lot_size=1)
        for value in (True,1.5,float('inf')):
            with self.assertRaises(InstrumentError):upstox_contract(dict(raw,freeze_quantity=value))
        with self.assertRaises(InstrumentError):self.record('rules',self.spec.id,dict(self.rules,lot_size=True))
