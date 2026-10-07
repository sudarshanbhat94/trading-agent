import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from app import approved_execution as pipeline,books,v2_live,paper_ledger,execution_contracts,paper_exchange,executable_quotes
from app.instrument_catalog import Instrument,import_snapshot


class ApprovedPaperPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.db';self.con=sqlite3.connect(self.path);self.addCleanup(lambda:self.con.close())
        v2_live.ensure_schema(self.con);self.catalogue=sqlite3.connect(':memory:');self.addCleanup(self.catalogue.close)
        books.ensure_book(self.con,2);self.con.commit()
        self.now=datetime.now(timezone.utc);start=self.now.replace(hour=0,minute=0,second=0,microsecond=0);end=start+timedelta(days=1)
        self.spec=Instrument('NSE','NSE_EQ','EQUITY','TEST','INR','TEST','EQ')
        import_snapshot(self.catalogue,[(self.spec,'NSE_EQ|TEST')],provider='upstox',source='SYNTHETIC FIXTURE',
                         source_day=self.now.date().isoformat(),observed_at=self.now.isoformat(),now=self.now)
        calendar='NSE:FIXTURE:ALL_DAY'
        attrs=dict(source='ISOLATED TEST, NOT AN EXCHANGE CALENDAR',observed_at=self.now.isoformat(),effective_from=start.isoformat(),effective_until=end.isoformat(),now=self.now)
        execution_contracts.record(self.catalogue,'session',calendar,dict(open=True,opens_at=start.isoformat(),closes_at=end.isoformat()),**attrs)
        execution_contracts.record(self.catalogue,'rules',self.spec.id,dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',
            lower_circuit=80,upper_circuit=120,calendar=calendar,banned=False,corporate_action_pending=False,actions_reviewed_at=self.now.isoformat()),**attrs)
        self.plan=dict(user_id=2,market='IN',mode='paper',side='BUY',product='D',epoch=books.current_epoch(self.con,2),
            instrument_id=self.spec.id,symbol='TEST',model_version='manual-fixture-v1',evidence_reference='synthetic fixture, not alpha validation',
            quantity=20,stop=99,entry_low=100,entry_high=100,target=110,evidence_at=self.now.isoformat(),expires_at=(self.now+timedelta(hours=1)).isoformat(),
            approval_kind='manual',sleeve='manual')
        self.plan_id=pipeline.approve(self.con,self.plan,approved_by='fixture account owner',approval_reference='explicit isolated manual fixture',now=self.now)

    def submit(self,uid=2,key='request-0001',regime='ON',price=100):
        result=pipeline.submit(self.con,self.catalogue,uid,self.plan_id,key,{'TEST':dict(price=price,ts=self.now.isoformat())},regime=regime,now=self.now)
        if result.get('status')=='pending':
            self.fill_pending(regime=regime)
            return paper_exchange.status(self.con,uid,result['order_id'])
        return result

    def snapshot(self,at=None,price=100,quantity=1000):
        at=at or self.now+timedelta(seconds=1)
        return executable_quotes.normalize_upstox(dict(instrument_token='NSE_EQ|TEST',symbol='TEST',timestamp=at.isoformat(),
            lower_circuit_limit=80,upper_circuit_limit=120,
            depth=dict(buy=[dict(price=price-.05,quantity=quantity)],sell=[dict(price=price,quantity=quantity)])),
            'NSE_EQ|TEST','TEST',observed_at=at.isoformat())

    def fill_pending(self,regime='ON',snapshot=None,now=None):
        now=now or self.now+timedelta(seconds=1);snapshot=snapshot or self.snapshot(now)
        return paper_exchange.service(self.con,self.catalogue,{'TEST':dict(price=100,ts=now.isoformat(),execution=snapshot)},regime=regime,now=now)

    def test_entry_restart_protection_exit_pnl_and_immutable_identity(self):
        before=books.cash(self.con,2);result=self.submit();self.assertTrue(result['ok']);self.assertEqual(result['qty'],20)
        self.assertEqual(result['instrument_id'],self.spec.id);self.assertEqual(result['protection'],'application-paper')
        self.assertEqual(self.con.execute('SELECT plan_id FROM user_positions WHERE user_id=2').fetchone()[0],self.plan_id)
        self.con.close();self.con=sqlite3.connect(self.path)
        self.assertEqual(self.submit(),result)
        self.assertEqual(len(books.positions(self.con,2)),1)
        self.assertEqual(books.monitor_positions(self.con,quotes={'TEST':dict(price=110,ts=self.now.isoformat())},market='IN'),1)
        self.assertEqual(len(books.positions(self.con,2)),1)  # Stop/target request is not a sale.
        at=self.now+timedelta(seconds=2)
        paper_exchange.service_exits(self.con,{'TEST':dict(price=110,ts=at.isoformat(),execution=self.snapshot(at,price=110.05))},now=at)
        self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(self.con.execute('SELECT plan_id,instrument_id,model_version FROM user_trades WHERE user_id=2').fetchone(),
                         (self.plan_id,self.spec.id,'manual-fixture-v1'))
        ledger=paper_ledger.report(self.con,2,'IN',books.current_epoch(self.con,2),books.cash(self.con,2))
        self.assertEqual(ledger['status'],'ok');self.assertEqual(ledger['cash_difference_minor'],0)
        self.assertGreater(books.cash(self.con,2),before)  # Synthetic favourable price, not strategy evidence.
        self.assertEqual(self.submit(),result);self.assertEqual(books.positions(self.con,2),[])

    def test_off_unknown_outside_zone_stale_and_account_epoch_refuse(self):
        for i,(regime,price) in enumerate((('OFF',100),('UNKNOWN',100),('ON',105))):
            self.assertFalse(self.submit(key='rejected-'+str(i),regime=regime,price=price)['ok'])
        with self.assertRaises(ValueError):self.submit(uid=3)
        books.reset_book(self.con,2,'IN');self.assertFalse(self.submit(key='new-epoch')['ok'])
        self.assertEqual(books.cash(self.con,2),10000);self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(pipeline.report(self.con,3)['plans'],[])

    def test_unapproved_research_and_key_rebinding_refuse(self):
        bad=dict(self.plan,approval_kind='research',publication_id=1)
        with self.assertRaises(ValueError):pipeline.approve(self.con,bad,approved_by='fixture',approval_reference='fixture',now=self.now)
        bad['approval_kind']='manual'
        with self.assertRaises(ValueError):pipeline.approve(self.con,bad,approved_by='fixture',approval_reference='fixture',now=self.now)
        self.submit();other=pipeline.approve(self.con,dict(self.plan,model_version='manual-fixture-v2'),approved_by='fixture',approval_reference='fixture',now=self.now)
        with self.assertRaises(ValueError):pipeline.submit(self.con,self.catalogue,2,other,'request-0001',{},regime='ON',now=self.now)
        for table in ('approved_execution_plans','approved_execution_events'):
            with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM '+table)
            self.con.rollback()

    def test_atomic_ledger_failure_and_insufficient_cash_cannot_create_a_fill(self):
        with patch.object(paper_ledger,'entry',side_effect=RuntimeError('disk fault')):
            with self.assertRaises(RuntimeError):self.submit()
        self.assertEqual(books.positions(self.con,2),[])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM approved_execution_events WHERE kind='FILLED'").fetchone()[0],0)
        self.assertEqual(len(paper_exchange.pending(self.con,2)),1)
        books.reset_book(self.con,2,'IN',budget=100)
        self.assertFalse(self.submit()['ok']);self.assertEqual(books.cash(self.con,2),100)
