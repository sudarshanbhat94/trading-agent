import sqlite3
import unittest
from unittest.mock import patch

from app import protection, protection_amendments, broker, worker_fencing
from tests import test_protection_races as fixtures


class NativeAmendmentTest(unittest.TestCase):
    setUp=fixtures.ProtectionRaceTest.setUp
    fill=fixtures.ProtectionRaceTest.fill
    native=fixtures.ProtectionRaceTest.native
    state=fixtures.ProtectionRaceTest.state
    sell=fixtures.ProtectionRaceTest.sell

    def ready(self):
        self.native();self.sell(7)
        protection.observe_fills(self.con,2);self.con.commit()
        for name in ('app.live_release.native_policy','app.broker_reconciliation.ready'):
            mock=patch(name,return_value=True);mock.start();self.addCleanup(mock.stop)

    def test_reduction_needs_later_exact_evidence_and_survives_restart(self):
        self.ready()
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'unknown')
        self.port.modify_protection.assert_called_once_with(2,'GTT-stop',quantity=13,stop=95)
        protection_amendments.reduce(self.con,2,self.entry,self.port)
        protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'unknown');self.assertEqual(self.port.modify_protection.call_count,1)
        self.evidence['quantity']=13
        protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'armed')
        self.assertEqual(protection.report(self.con,2)['rows'][0]['coverage_quantity'],13)
        self.assertFalse(protection.blocks_entry(self.con,2))
        self.assertEqual(protection_amendments.latest(self.con,2,self.entry)['state'],'confirmed')
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'skipped')
        self.assertEqual(protection_amendments.reduce(self.con,3,self.entry,self.port),'skipped')
        # A new connection/restart sees the immutable claim and never repeats I/O.
        restarted=sqlite3.connect(':memory:');self.addCleanup(restarted.close);self.con.backup(restarted)
        self.assertEqual(protection_amendments.reduce(restarted,2,self.entry,self.port),'skipped')
        self.assertEqual(self.port.modify_protection.call_count,1)
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM native_amendment_intents')

    def test_timeout_does_not_retry_and_trigger_race_keeps_original_sell_reserved(self):
        self.ready();self.port.modify_protection.side_effect=TimeoutError('ambiguous')
        protection_amendments.reduce(self.con,2,self.entry,self.port)
        self.evidence['rules'][0].update(status='TRIGGERED',order_id='old-size-exit')
        protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'triggered')
        self.assertEqual(self.con.execute("SELECT qty FROM v2_live_orders WHERE broker_order_id='old-size-exit'").fetchone()[0],20)
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'unknown')
        self.assertEqual(self.port.modify_protection.call_count,1)
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))

    def test_open_trigger_revoked_scope_unreconciled_or_pending_sell_never_modify(self):
        self.ready()
        self.evidence['rules'][0].update(status='OPEN',order_id='native-child')
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'skipped')
        self.evidence['rules'][0].update(status='SCHEDULED',order_id=None)
        with patch.object(protection,'live_scope_authorized',return_value=(False,'revoked')):
            self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'skipped')
        with patch('app.broker_reconciliation.ready',return_value=False):
            self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'skipped')
        self.con.execute("UPDATE v2_live_orders SET status='partial' WHERE side='SELL'");self.con.commit()
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'skipped')
        self.port.modify_protection.assert_not_called()

    def test_late_ack_cannot_revert_exact_confirmed_reduction(self):
        self.ready()
        def observed_before_ack(*_,**__):
            self.evidence['quantity']=13
            protection.refresh(self.con,2,self.port)
            return {'ok':True}
        self.port.modify_protection.side_effect=observed_before_ack
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'confirmed')
        self.assertEqual(protection_amendments.latest(self.con,2,self.entry)['state'],'confirmed')
        self.assertEqual(self.state(),'armed')

    def test_takeover_during_amendment_claim_preserves_unknown_without_retry(self):
        self.ready();worker_fencing.ensure_schema(self.con)
        token=worker_fencing.acquire(self.con,'old-worker');context=worker_fencing.ACTIVE.set(token)
        self.addCleanup(worker_fencing.ACTIVE.reset,context)
        def takeover(*_,**__):
            self.con.execute('UPDATE worker_leases SET expires=0');self.con.commit()
            worker_fencing.acquire(self.con,'replacement-worker')
            return {'ok':True}
        self.port.modify_protection.side_effect=takeover
        with self.assertRaisesRegex(RuntimeError,'stale paper-engine fence'):
            protection_amendments.reduce(self.con,2,self.entry,self.port)
        self.assertEqual(self.state(),'amending')
        worker_fencing.ACTIVE.set(None)
        self.assertEqual(protection_amendments.reduce(self.con,2,self.entry,self.port),'unknown')
        self.assertEqual(self.port.modify_protection.call_count,1)

    def test_cancelled_coverage_with_remaining_inventory_blocks_new_risk(self):
        self.native();protection.prepare_exit(self.con,2,'TEST',self.port)
        self.evidence['rules'][0]['status']='CANCELLED';protection.refresh(self.con,2,self.port)
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertTrue(protection.blocks_entry(self.con,2))
        self.sell(20);protection.observe_fills(self.con,2);self.con.commit()
        self.assertEqual(self.state(),'closed');self.assertFalse(protection.blocks_entry(self.con,2))

    def test_trigger_winning_reduction_is_cancelled_once_before_another_exit(self):
        self.ready();protection_amendments.reduce(self.con,2,self.entry,self.port)
        self.evidence['rules'][0].update(status='TRIGGERED',order_id='raced-child')
        protection.refresh(self.con,2,self.port)
        protection.cancel_overreserved_children(self.con,2,self.port)
        protection.cancel_overreserved_children(self.con,2,self.port)
        self.port.cancel.assert_called_once_with(2,'raced-child')
        self.assertEqual(self.state(),'triggered')
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))
        from app import order_journal
        order_journal.reconcile(self.con,2,[dict(order_id='raced-child',instrument_token='NSE_EQ|TEST',transaction_type='SELL',
            product='D',filled_quantity=3,average_price=94,status='cancelled')])
        self.assertEqual(self.state(),'cancelled')
        self.assertEqual(protection.report(self.con,2)['rows'][0]['remaining_quantity'],10)
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertTrue(protection.blocks_entry(self.con,2))

    def test_unknown_child_cancellation_never_retries_or_frees_inventory(self):
        self.ready();self.evidence['rules'][0].update(status='TRIGGERED',order_id='raced-child')
        protection.refresh(self.con,2,self.port)
        self.port.cancel.side_effect=TimeoutError('unknown cancellation')
        protection.cancel_overreserved_children(self.con,2,self.port)
        protection.cancel_overreserved_children(self.con,2,self.port)
        self.assertEqual(self.port.cancel.call_count,1)
        self.assertEqual(self.state(),'triggered')
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))

    def test_confirmed_cancel_after_quantity_reduction_does_not_stick_unknown(self):
        self.ready();protection_amendments.reduce(self.con,2,self.entry,self.port)
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.evidence['quantity']=13;self.evidence['rules'][0].update(status='CANCELLED',order_id=None)
        protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'cancelled')
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertTrue(protection.blocks_entry(self.con,2))


class NativeAmendmentTransportTest(unittest.TestCase):
    def test_exact_scheduled_single_payload_and_no_transport_retry(self):
        with patch.object(broker,'_execution_request',return_value={'ok':True}) as send:
            broker.modify_protection(2,'GTT-stop',quantity=13,stop=95)
            self.assertEqual(send.call_args.args,(2,'PUT','/order/gtt/modify'))
            self.assertEqual(send.call_args.kwargs['payload'],dict(type='SINGLE',gtt_order_id='GTT-stop',quantity=13,
                rules=[dict(strategy='ENTRY',trigger_type='BELOW',trigger_price=95,market_protection=5)]))
            for quantity in (True,0,1.5):
                with self.assertRaises(ValueError):broker.modify_protection(2,'GTT-stop',quantity=quantity,stop=95)
            self.assertEqual(send.call_count,1)
