import sqlite3
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch

from app import v2_live,order_journal,protection,broker
from app.instrument_catalog import Instrument,import_snapshot


class ProtectionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close);v2_live.ensure_schema(self.con)
        approval=patch.object(protection,'live_scope_authorized',return_value=(True,''));approval.start();self.addCleanup(approval.stop)
        self.now=datetime.now(timezone.utc)
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,product,status,broker_order_id,intent_key,filled_qty) "
                         "VALUES(?,2,'IN','TEST','NSE_EQ|TEST','BUY',20,100,2000,'D','submitted','entry','intent',0)",(self.now.isoformat(),))
        self.entry=self.con.execute('SELECT id FROM v2_live_orders').fetchone()[0]
        order_journal.protect(self.con,2,'TEST',95,110)
        self.port=Mock();self.port.place_stop.return_value={'ok':True,'data':{'gtt_order_ids':['GTT-stop']}}
        self.evidence=dict(type='SINGLE',instrument_token='NSE_EQ|TEST',product='D',quantity=20,gtt_order_id='GTT-stop',
            expires_at=(time.time()+86400)*1_000_000,rules=[dict(strategy='ENTRY',transaction_type='SELL',trigger_type='BELOW',trigger_price=95,status='SCHEDULED',order_id=None)])
        self.port.protection_status.return_value=[self.evidence]
        spec=Instrument('NSE','NSE_EQ','EQUITY','TEST','INR','TEST','EQ',tick_size='0.05',freeze_quantity=10000,settlement='T+1')
        import_snapshot(self.con,[(spec,'NSE_EQ|TEST')],provider='upstox',source='fixture',source_day=self.now.date().isoformat(),observed_at=self.now.isoformat())
        from app import execution_contracts
        from datetime import timedelta
        execution_contracts.record(self.con,'rules',spec.id,
            dict(lot_size=1,tick_size='0.05',freeze_quantity=10000,settlement='T+1',lower_circuit=80,upper_circuit=120,
                 calendar='NSE:fixture',banned=False,corporate_action_pending=False,actions_reviewed_at=self.now.isoformat()),
            source='synthetic test only',observed_at=self.now.isoformat(),effective_from=self.now.isoformat(),
            effective_until=(self.now+timedelta(days=1)).isoformat())
        self.con.commit()
        from app import entry_contracts
        context=entry_contracts.using(self.con,self.now);context.__enter__();self.addCleanup(context.__exit__,None,None,None)

    def fill(self,qty=20,side='BUY',oid='entry'):
        order_journal.reconcile(self.con,2,[dict(order_id=oid,tag='intent',instrument_token='NSE_EQ|TEST',transaction_type=side,
                                                product='D',filled_quantity=qty,average_price=100,status='complete')])

    def native(self):
        self.fill();protection.request_native(self.con,2,self.entry,authorization_reference='explicit isolated test authorization')
        protection.submit_stop(self.con,2,self.entry,self.port);protection.refresh(self.con,2,self.port)

    def state(self):return protection.report(self.con,2)['rows'][0]['state']

    def test_acknowledgement_creates_no_protected_inventory(self):
        self.assertEqual(protection.report(self.con,2)['rows'],[])
        self.fill(5);self.assertEqual(self.state(),'app_only')
        self.assertFalse(protection.report(self.con,2)['activation_automatic'])
        self.assertFalse(protection.report(self.con,2)['live_certified'])
        self.port.place_stop.assert_not_called()

    def test_reviewed_automatic_coverage_requires_new_canonical_terminal_fill(self):
        from app import entry_contracts,live_release
        from app.account_safety import atomic
        self.fill(5)
        policy={'reference':'separately reviewed fixture only','source_commit':'a'*40}
        with patch.object(live_release,'native_policy',return_value=policy):
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),0)  # Legacy/partial inventory is not adopted.
            with atomic(self.con):entry_contracts.record(self.con,'broker',2,self.entry,{'synthetic':True})
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),0)
            self.fill(20)
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),1)
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),0)
            self.assertEqual(self.state(),'required');self.port.place_stop.assert_not_called()

    def test_timeout_and_restart_never_repeat_native_submission(self):
        self.fill();protection.request_native(self.con,2,self.entry,authorization_reference='isolated explicit approval')
        self.port.place_stop.side_effect=TimeoutError('ambiguous')
        self.assertEqual(protection.submit_stop(self.con,2,self.entry,self.port),'unknown')
        self.assertEqual(protection.submit_stop(self.con,2,self.entry,self.port),'skipped')
        protection.refresh(self.con,2,self.port);self.assertEqual(self.state(),'unknown')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM execution_incidents WHERE user_id=2 AND code='NATIVE_PROTECTION' AND resolved_at IS NULL").fetchone()[0],1)
        self.assertTrue(protection.blocks_entry(self.con,2));self.assertFalse(protection.blocks_entry(self.con,3))
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port));self.assertEqual(self.port.place_stop.call_count,1)

    def test_accepted_stop_needs_exact_status_and_cancellation_needs_confirmation(self):
        self.native();self.assertEqual(self.state(),'armed')
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port));self.assertEqual(self.port.cancel_protection.call_count,1)
        protection.refresh(self.con,2,self.port);self.assertEqual(self.state(),'cancelling')
        self.evidence['rules'][0]['status']='CANCELLED';protection.refresh(self.con,2,self.port)
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port));self.assertEqual(self.state(),'cancelled')

    def test_trigger_cancel_race_attaches_owned_exit_and_never_sells_twice(self):
        self.native();protection.prepare_exit(self.con,2,'TEST',self.port)
        self.evidence['rules'][0].update(status='TRIGGERED',order_id='native-exit')
        protection.refresh(self.con,2,self.port);protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'triggered')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0],1)
        self.fill(10,'SELL','native-exit');self.assertEqual(self.state(),'triggered')
        with patch.object(broker,'state',side_effect=AssertionError('must not reach broker')):
            result=order_journal.submit(self.con,2,'IN','TEST','NSE_EQ|TEST','SELL',10,90,'D','manual')
        self.assertIn('native protection',result)
        self.fill(20,'SELL','native-exit');self.assertEqual(self.state(),'closed')
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))

    def test_reviewed_partial_entry_cancels_once_and_waits_for_terminal_fills(self):
        from app import entry_contracts
        from app.account_safety import atomic
        self.fill(7)
        with atomic(self.con):entry_contracts.record(self.con,'broker',2,self.entry,{'fixture':True})
        snapshot=dict(order_id='entry',instrument_token='NSE_EQ|TEST',transaction_type='BUY',product='D',filled_quantity=7,average_price=100,status='open')
        with patch('app.live_release.native_policy',return_value={'reference':'isolated review','source_commit':'a'*40}), \
                patch.object(broker,'cancel_order',side_effect=TimeoutError('fixture ambiguous cancel')) as cancel, \
                patch.object(broker,'orders',return_value=[snapshot]):
            self.assertEqual(protection.settle_partial_entries(self.con,2),1)
            protection.settle_partial_entries(self.con,2)
            self.assertEqual(cancel.call_count,1)
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),0)
            self.assertEqual(self.state(),'app_only');self.assertTrue(protection.blocks_entry(self.con,2))
            order_journal.reconcile(self.con,2,[dict(snapshot,status='cancelled')])
            self.assertEqual(protection.activate_reviewed_fills(self.con,2),1)
            self.assertEqual(self.state(),'required')
        self.port.place_stop.assert_not_called()

    def test_missing_expired_changed_and_duplicate_native_evidence_block(self):
        self.native()
        for data in ([],[dict(self.evidence,quantity=19)],[self.evidence,self.evidence],
                     [dict(self.evidence,expires_at=None)]):
            self.port.protection_status.return_value=data;protection.refresh(self.con,2,self.port)
            self.assertEqual(self.state(),'unknown');self.assertTrue(protection.blocks_entry(self.con,2))
        self.port.protection_status.return_value=[dict(self.evidence,expires_at=1)]
        protection.refresh(self.con,2,self.port);self.assertEqual(self.state(),'unknown')
        self.evidence['rules'][0]['status']='EXPIRED';self.port.protection_status.return_value=[self.evidence]
        protection.refresh(self.con,2,self.port);self.assertEqual(self.state(),'failed')

    def test_no_activation_without_ownership_rules_or_authorization(self):
        self.fill()
        for uid,ref in ((3,'approval'),(2,'')):
            with self.assertRaises(ValueError):protection.request_native(self.con,uid,self.entry,authorization_reference=ref)
        self.con.execute('DELETE FROM instrument_contracts');self.con.commit()
        with self.assertRaises(ValueError):protection.request_native(self.con,2,self.entry,authorization_reference='approval')
        self.assertEqual(self.state(),'app_only');self.port.place_stop.assert_not_called()
        self.assertEqual(protection.report(self.con,3)['rows'],[])

    def test_submitting_crash_and_append_only_event_log(self):
        self.fill();self.con.execute("UPDATE protection_obligations SET state='submitting'");self.con.commit()
        protection.refresh(self.con,2,self.port);self.assertEqual(self.state(),'unknown')
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM protection_events')

    def test_authorization_revocation_and_pending_entry_refuse_native_io(self):
        self.fill(5)
        with self.assertRaises(ValueError):
            protection.request_native(self.con,2,self.entry,authorization_reference='fixture')
        self.fill()
        with patch.object(protection,'live_scope_authorized',return_value=(False,'revoked')):
            with self.assertRaisesRegex(ValueError,'revoked'):
                protection.request_native(self.con,2,self.entry,authorization_reference='fixture')
        protection.request_native(self.con,2,self.entry,authorization_reference='fixture')
        with patch.object(protection,'live_scope_authorized',return_value=(False,'revoked')):
            self.assertEqual(protection.submit_stop(self.con,2,self.entry,self.port),'failed')
        self.port.place_stop.assert_not_called()
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))

    def test_untransmitted_stop_is_withdrawn_before_app_exit(self):
        self.fill();protection.request_native(self.con,2,self.entry,authorization_reference='fixture')
        self.assertTrue(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertEqual(self.state(),'cancelled')
        self.assertEqual(protection.submit_stop(self.con,2,self.entry,self.port),'skipped')
        self.port.place_stop.assert_not_called();self.port.cancel_protection.assert_not_called()

    def test_later_same_ticker_entry_does_not_revive_old_stop(self):
        self.fill()
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,product,status,filled_qty) "
                         "VALUES(?,2,'IN','TEST','NSE_EQ|TEST','SELL',20,100,2000,'D','filled',20)",(self.now.isoformat(),))
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,product,status,filled_qty) "
                         "VALUES(?,2,'IN','TEST','NSE_EQ|TEST','BUY',10,100,1000,'D','filled',10)",(self.now.isoformat(),))
        self.con.commit();protection.observe_fills(self.con,2);self.con.commit()
        rows=protection.report(self.con,2)['rows']
        self.assertEqual(rows[1]['state'],'closed');self.assertEqual(rows[1]['remaining_quantity'],0)
        self.assertEqual(rows[0]['state'],'app_only');self.assertEqual(rows[0]['remaining_quantity'],10)
        with self.assertRaises(ValueError):
            protection.request_native(self.con,2,self.entry,authorization_reference='fixture')
        self.port.place_stop.assert_not_called()


class UpstoxProtectionTransportTest(unittest.TestCase):
    def test_single_sell_stop_payload_and_no_retry_on_timeout(self):
        with patch.object(broker,'_execution_request',return_value={'ok':True}) as send:
            broker.place_stop(2,'NSE_EQ|TEST',20,product='D',stop=95)
            payload=send.call_args.kwargs['payload']
            self.assertEqual(payload['transaction_type'],'SELL');self.assertEqual(payload['type'],'SINGLE')
            self.assertEqual(payload['rules'],[dict(strategy='ENTRY',trigger_type='BELOW',trigger_price=95,market_protection=5)])
            for qty in (True,1.5,0):
                with self.assertRaises(ValueError):broker.place_stop(2,'NSE_EQ|TEST',qty,product='D',stop=95)
            self.assertEqual(send.call_count,1)
        with patch.object(broker,'_execution_request',side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError):broker.place_stop(2,'NSE_EQ|TEST',20,product='D',stop=95)
            self.assertEqual(send.call_count,1)
