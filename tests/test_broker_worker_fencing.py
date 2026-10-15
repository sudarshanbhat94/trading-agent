import unittest
from unittest.mock import patch

from app import broker,broker_ledger,broker_reconciliation,order_journal,protection,worker_fencing
from tests import test_protection_races as fixtures


class BrokerWriteFenceTest(unittest.TestCase):
    setUp=fixtures.ProtectionRaceTest.setUp
    fill=fixtures.ProtectionRaceTest.fill
    native=fixtures.ProtectionRaceTest.native
    state=fixtures.ProtectionRaceTest.state
    sell=fixtures.ProtectionRaceTest.sell

    def stale(self):
        worker_fencing.ensure_schema(self.con)
        token=worker_fencing.acquire(self.con,'old')
        self.con.execute('UPDATE worker_leases SET expires=0');self.con.commit()
        worker_fencing.acquire(self.con,'new')
        context=worker_fencing.ACTIVE.set(token);self.addCleanup(worker_fencing.ACTIVE.reset,context)

    def test_stale_worker_cannot_post_fees_settle_change_stops_or_request_exit(self):
        self.fill();self.stale()
        calls=(lambda:broker_ledger.record_fee(self.con,2,'entry','fee',20,source='fixture',occurred_at=self.now.isoformat()),
               lambda:order_journal.protect(self.con,2,'TEST',80,120),
               lambda:order_journal.request_exit(self.con,2,'TEST','late exit'),
               lambda:broker_reconciliation.reconcile(self.con,2,positions=[],holdings=[],funds={},trades=[]),
               lambda:protection.observe_fills(self.con,2))
        for call in calls:
            with self.assertRaisesRegex(RuntimeError,'stale paper-engine fence'):call()
        self.assertEqual(self.con.execute('SELECT stop,exit_reason FROM v2_live_protection').fetchone(),(95,None))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM broker_ledger_events').fetchone()[0],0)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM broker_reconciliation').fetchone()[0],0)

    def test_sell_cannot_use_another_product_or_contract_inventory(self):
        self.fill(10)
        self.con.execute("UPDATE v2_live_orders SET status='cancelled' WHERE side='BUY'")
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,product,status,filled_qty) "
                         "VALUES(?,2,'IN','TEST','NSE_EQ|TEST','BUY',10,100,1000,'I','filled',10)",(self.now.isoformat(),))
        self.con.commit()
        with patch.object(broker,'state',return_value={'exit_ready':True}),patch.object(protection,'prepare_exit',return_value=True), \
                patch.object(broker,'place_order',side_effect=AssertionError('must not transmit')):
            result=order_journal.submit(self.con,2,'IN','TEST','NSE_EQ|TEST','SELL',20,100,'D','fixture')
        self.assertIn('position changed',result)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0],0)

    def test_stop_or_exit_write_never_commits_parent_transaction(self):
        self.con.execute('BEGIN IMMEDIATE')
        order_journal.protect(self.con,2,'TEST',90,120)
        order_journal.request_exit(self.con,2,'TEST','fixture')
        self.assertTrue(self.con.in_transaction);self.con.rollback()
        self.assertEqual(self.con.execute('SELECT stop,exit_reason FROM v2_live_protection').fetchone(),(95,None))
