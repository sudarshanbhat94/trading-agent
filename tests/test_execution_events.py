"""Crash/fencing and append-only intent/status evidence without broker orders."""
import sqlite3
import unittest
from unittest.mock import patch
from app import execution_events,order_journal,worker_fencing,broker,broker_reconciliation,v2_live


class ExecutionObservationTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close);v2_live.ensure_schema(self.con)
        broker_reconciliation.reconcile(self.con,2,positions=[],holdings=[],trades=[],funds={'data':{'equity':{'available_margin':10000}}})
        for context in (patch.object(order_journal,'live_scope_authorized',return_value=(True,'isolated fixture')),
                        patch.object(broker,'state',return_value={'live_ready':True,'exit_ready':True,'budget':10000})):
            context.start();self.addCleanup(context.stop)

    def submit(self):
        return order_journal.submit(self.con,2,'IN','TEST','NSE_EQ|TEST','BUY',10,200,'D','fixture',stop=198,target=220,available_cash=10000)

    def test_ack_partial_duplicate_and_immutability_are_owned(self):
        with patch.object(broker,'place_order',return_value={'ok':True,'order_id':'owned'}):self.assertEqual(self.submit(),'submitted')
        evidence=dict(order_id='owned',instrument_token='NSE_EQ|TEST',transaction_type='BUY',product='D',filled_quantity=4,average_price=200,status='open')
        order_journal.reconcile(self.con,2,[evidence]);order_journal.reconcile(self.con,2,[evidence])
        rows=execution_events.report(self.con,2)['rows'];self.assertEqual(len(rows),3)
        self.assertEqual(execution_events.report(self.con,3)['rows'],[])
        self.assertEqual([r['order']['cumulative_filled_qty'] for r in rows if r['kind']=='broker-status-observation'],[4])
        self.assertFalse(execution_events.report(self.con,2)['fee_ledger_certified'])
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM live_execution_events')
        self.con.rollback()

    def test_fill_and_observation_are_atomic(self):
        with patch.object(broker,'place_order',return_value={'ok':True,'order_id':'owned'}):self.submit()
        evidence=dict(order_id='owned',instrument_token='NSE_EQ|TEST',transaction_type='BUY',product='D',filled_quantity=10,average_price=200,status='complete')
        with patch.object(execution_events,'record',side_effect=RuntimeError('event write failed')):
            with self.assertRaises(RuntimeError):order_journal.reconcile(self.con,2,[evidence])
        self.assertEqual(self.con.execute('SELECT filled_qty FROM v2_live_orders').fetchone()[0],0)
        self.assertEqual(len(execution_events.report(self.con,2)['rows']),2)

    def test_worker_takeover_after_transmission_preserves_unresolved_intent(self):
        worker_fencing.acquire(self.con,'old',ttl=120)
        token=worker_fencing.ACTIVE.set(('old',1));self.addCleanup(worker_fencing.ACTIVE.reset,token)
        def send(*args,**kwargs):
            self.con.execute("UPDATE worker_leases SET owner='new',generation=2");self.con.commit()
            return {'ok':True,'order_id':'late-ack'}
        with patch.object(broker,'place_order',side_effect=send):
            with self.assertRaisesRegex(RuntimeError,'stale'):self.submit()
        self.assertEqual(self.con.execute('SELECT status,broker_order_id FROM v2_live_orders').fetchone(),('pending',None))
        self.assertEqual(len(execution_events.report(self.con,2)['rows']),1)
        # A new worker uses the persisted tag, rather than resubmitting.
        tag=self.con.execute('SELECT intent_key FROM v2_live_orders').fetchone()[0]
        worker_fencing.ACTIVE.set(('new',2))
        order_journal.reconcile(self.con,2,[dict(order_id='late-ack',tag=tag,instrument_token='NSE_EQ|TEST',transaction_type='BUY',product='D',filled_quantity=10,average_price=200,status='complete')])
        self.assertEqual(self.con.execute('SELECT status FROM v2_live_orders').fetchone()[0],'filled')
