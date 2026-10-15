import copy
import json
import sqlite3
import unittest
from unittest.mock import Mock,patch

from app import broker,order_journal,portfolio_stream,protection
from tests import test_protection_races as fixtures


class OwnedPortfolioStreamTest(unittest.TestCase):
    setUp=fixtures.ProtectionRaceTest.setUp
    fill=fixtures.ProtectionRaceTest.fill
    native=fixtures.ProtectionRaceTest.native
    state=fixtures.ProtectionRaceTest.state

    def connect(self,uid=2,account='isolated-profile'):
        return portfolio_stream.open_connection(self.con,uid,account)

    def event(self,oid='completed-exit'):
        event=copy.deepcopy(self.evidence)
        event['update_type']='gtt_order'
        event['rules'][0].update(status='COMPLETED',order_id=oid)
        return event

    def test_completed_trigger_survives_gap_and_restart_without_inventing_fill(self):
        self.native();connection=self.connect();event=self.event()
        portfolio_stream.ingest(self.con,connection,event)
        portfolio_stream.ingest(self.con,connection,event)
        portfolio_stream.close_connection(self.con,connection)
        self.port.protection_status.return_value=[]
        protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'triggered')
        self.assertEqual(self.con.execute("SELECT COUNT(*),SUM(filled_qty) FROM v2_live_orders WHERE side='SELL'").fetchone(),(1,0))
        self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))
        restarted=sqlite3.connect(':memory:');self.addCleanup(restarted.close);self.con.backup(restarted)
        protection.refresh(restarted,2,self.port)
        self.assertEqual(restarted.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0],1)
        self.assertFalse(portfolio_stream.report(restarted,2)['connected'])
        self.fill(20,'SELL','completed-exit');self.assertEqual(self.state(),'closed')

    def test_authenticated_connection_and_payload_identity_are_owner_bound(self):
        self.native();connection=self.connect(3,'another-profile')
        portfolio_stream.ingest(self.con,connection,self.event())
        self.port.protection_status.return_value=[];protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'unknown')
        with self.assertRaisesRegex(ValueError,'already bound'):self.connect(2,'another-profile')
        connection=self.connect()
        with self.assertRaisesRegex(ValueError,'identity conflicts'):
            portfolio_stream.ingest(self.con,connection,dict(self.event(),user_id='wrong-owner'))
        portfolio_stream.close_connection(self.con,connection)
        with self.assertRaises(ValueError):self.connect(2,'different-new-profile')
        self.assertEqual(portfolio_stream.report(self.con,2)['event_counts'],{'connection':1,'gap':1})

    def test_expired_replaced_connections_and_ambiguous_children_refuse(self):
        self.native();old=self.connect()
        self.con.execute('UPDATE broker_stream_connections SET expires_at=0');self.con.commit()
        current=self.connect()
        with self.assertRaises(ValueError):portfolio_stream.ingest(self.con,old,self.event())
        portfolio_stream.ingest(self.con,current,self.event('first-child'))
        portfolio_stream.ingest(self.con,current,self.event('different-child'))
        self.port.protection_status.return_value=[];protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'unknown')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0],0)

    def test_notifications_never_arm_or_free_inventory_and_originals_are_immutable(self):
        self.native();connection=self.connect()
        for status in ('SCHEDULED','CANCELLED','FAILED'):
            event=self.event();event['rules'][0].update(status=status,order_id=None)
            portfolio_stream.ingest(self.con,connection,event)
        self.port.protection_status.return_value=[];protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'unknown')
        self.assertTrue(protection.blocks_entry(self.con,2))
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM broker_stream_events')
        self.con.rollback()
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute("UPDATE broker_stream_accounts SET user_id=3")
        self.con.rollback()

    def test_order_notification_preceding_child_can_reconcile_exact_owned_fill(self):
        self.native();connection=self.connect()
        order=dict(update_type='order',user_id='isolated-profile',order_id='completed-exit',quantity=20,
                   instrument_token='NSE_EQ|TEST',transaction_type='SELL',product='D',filled_quantity=20,
                   average_price=94,status='complete',status_message='never retain arbitrary messages')
        portfolio_stream.ingest(self.con,connection,order)
        order_journal.reconcile(self.con,2,portfolio_stream.order_updates(self.con,2))
        self.assertEqual(self.state(),'armed')
        portfolio_stream.ingest(self.con,connection,self.event())
        self.port.protection_status.return_value=[];protection.refresh(self.con,2,self.port)
        order_journal.reconcile(self.con,2,portfolio_stream.order_updates(self.con,2))
        self.assertEqual(self.state(),'closed')
        self.assertNotIn('never retain',json.dumps(portfolio_stream.order_updates(self.con,2)))
        self.assertEqual(portfolio_stream.order_updates(self.con,3),[])

    def test_changed_contract_and_malformed_quantity_never_attach(self):
        self.native();connection=self.connect()
        event=self.event();event['instrument_token']='NSE_EQ|ANOTHER'
        portfolio_stream.ingest(self.con,connection,event)
        for quantity in (True,[],0,1.5):
            with self.assertRaises(ValueError):portfolio_stream.ingest(self.con,connection,dict(self.event(),quantity=quantity))
        self.port.protection_status.return_value=[];protection.refresh(self.con,2,self.port)
        self.assertEqual(self.state(),'unknown')
        self.assertIsNone(self.con.execute('SELECT exit_order_id FROM protection_obligations').fetchone()[0])

    def test_conflicting_connection_owner_or_backward_clock_cannot_ingest(self):
        connection=self.connect()
        with self.assertRaises(ValueError):portfolio_stream.ingest(self.con,connection,self.event(),now=0)
        self.con.execute("UPDATE broker_stream_connections SET account_hash='wrong-profile'");self.con.commit()
        with self.assertRaises(ValueError):portfolio_stream.ingest(self.con,connection,self.event())
        self.assertFalse(portfolio_stream.report(self.con,2)['connected'])
        self.assertEqual(portfolio_stream.report(self.con,2)['event_counts'],{'connection':1})


class PortfolioStreamTransportTest(unittest.TestCase):
    def test_only_authenticated_upstox_uri_and_profile_allowed(self):
        profile=Mock();profile.json.return_value=dict(status='success',data={'user_id':'fixture-profile'})
        response=Mock();response.json.return_value=dict(status='success',data={'authorized_redirect_uri':'wss://stream.upstox.com/feed?code=private-fixture'})
        with patch.object(broker,'_headers',return_value={'Authorization':'Bearer isolated-fixture'}),patch('httpx.get',side_effect=[profile,response]) as fetch:
            self.assertEqual(broker.portfolio_stream_access(2)[0],'fixture-profile')
            self.assertEqual(fetch.call_args.kwargs['params'],{'update_types':'order,gtt_order'})
        for uri in ('ws://stream.upstox.com/feed','wss://upstox.com.evil.invalid/feed','wss://user@stream.upstox.com/feed',
                    'wss://stream.upstox.com:80/feed','wss://evil.invalid/feed','https://stream.upstox.com/feed'):
            response.json.return_value=dict(status='success',data={'authorized_redirect_uri':uri})
            with patch.object(broker,'_headers',return_value={}),patch('httpx.get',side_effect=[profile,response]):
                with self.assertRaises(ValueError):broker.portfolio_stream_access(2)
