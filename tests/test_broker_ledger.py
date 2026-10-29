import sqlite3
import unittest
from datetime import datetime,timedelta,timezone
from app import v2_live,broker_ledger as ledger


class ActualBrokerLedgerTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close);v2_live.ensure_schema(self.con)
        self.now=datetime.now(timezone.utc);self.ts=self.now.isoformat()
        self.order('buy','BUY',20);self.order('sell','SELL',20);self.con.commit()

    def order(self,oid,side,qty):
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,product,status,broker_order_id) "
                         "VALUES(?,2,'IN','TEST','NSE_EQ|TEST',?,?,100,2000,'D','submitted',?)",(self.ts,side,qty,oid))

    def trade(self,tid='t1',oid='buy',side='BUY',qty=10,px=100):
        return dict(trade_id=tid,order_id=oid,instrument_token='NSE_EQ|TEST',transaction_type=side,product='D',
                    quantity=qty,average_price=px,executed_at=(self.now-timedelta(seconds=2 if side=='BUY' else 1)).isoformat())

    def test_partial_duplicates_restart_and_actual_fifo_outcome(self):
        first=self.trade();second=dict(self.trade('t2',qty=10,px=102),executed_at=(self.now-timedelta(seconds=2)+timedelta(microseconds=1)).isoformat())
        self.assertEqual(ledger.ingest_trades(self.con,2,[first,first,second]),2)
        self.assertEqual(ledger.ingest_trades(self.con,2,[second,first]),0)
        self.assertEqual(ledger.ingest_trades(self.con,2,[self.trade('s1','sell','SELL',15,110)]),1)
        result=ledger.report(self.con,2)
        self.assertTrue(result['balanced']);self.assertEqual(result['realised_gross_minor'],14000)
        self.assertIsNone(result['realised_net_minor']);self.assertIsNone(result['available_margin'])
        self.assertEqual(ledger.report(self.con,3)['events_by_kind'],{})

    def test_conflicting_id_external_activity_and_batch_fault_are_atomic(self):
        ledger.ingest_trades(self.con,2,[self.trade()])
        for rows in ([self.trade(px=101)],[self.trade('extra',qty=11)],
                     [self.trade('valid'),self.trade('external',oid='somebody-else')],
                     [self.trade('bad',qty=True)],[dict(self.trade('bad'),executed_at='2026-10-06')]):
            with self.assertRaises(ValueError):ledger.ingest_trades(self.con,2,rows)
        self.assertEqual(ledger.report(self.con,2)['events_by_kind'],{'FILL':1})
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM broker_ledger_events')
        self.con.rollback()

    def test_sourced_fees_settlement_and_reversals_preserve_history(self):
        ledger.ingest_trades(self.con,2,[self.trade(qty=20)])
        fee=ledger.record_fee(self.con,2,'buy','statement-fee',20,source='fixture contract note',occurred_at=self.ts)
        self.assertEqual(ledger.record_fee(self.con,2,'buy','statement-fee',20,source='fixture contract note',occurred_at=self.ts),fee)
        paid=ledger.settle(self.con,2,'statement-cash',-2020,source='fixture statement',occurred_at=self.ts)
        self.assertEqual(ledger.settle(self.con,2,'statement-cash',-2020,source='fixture statement',occurred_at=self.ts),paid)
        self.assertEqual(ledger.report(self.con,2)['balances_minor']['trade_payable'],0)
        with self.assertRaises(ValueError):ledger.settle(self.con,2,'overpay',-1,source='fixture',occurred_at=self.ts)
        ledger.reverse(self.con,2,fee,'correct-fee',source='fixture correction',occurred_at=self.ts)
        self.assertEqual(ledger.report(self.con,2)['balances_minor']['fees'],0)
        with self.assertRaises(ValueError):ledger.reverse(self.con,2,fee,'second',source='fixture',occurred_at=self.ts)
        with self.assertRaises(ValueError):ledger.record_fee(self.con,3,'buy','cross-owner',1,source='fixture',occurred_at=self.ts)

    def test_microsecond_execution_order_beats_arrival_and_julian_rounding(self):
        base=self.now-timedelta(seconds=2)
        sell=dict(self.trade('later','sell','SELL',10,110),executed_at=(base+timedelta(microseconds=2)).isoformat())
        buy=dict(self.trade('earlier','buy','BUY',10,100),executed_at=(base+timedelta(microseconds=1)).isoformat())
        ledger.ingest_trades(self.con,2,[sell,buy])
        self.assertEqual(ledger.report(self.con,2)['realised_gross_minor'],10000)
        self.assertTrue(ledger.report(self.con,2)['inventory_consistent'])
        self.assertEqual(self.con.execute('SELECT julianday(?)-julianday(?)',(buy['executed_at'],sell['executed_at'])).fetchone()[0],0)

    def test_net_requires_terminal_exact_final_fee_coverage_and_allocates_only_closed_units(self):
        ledger.ingest_trades(self.con,2,[self.trade(qty=20),self.trade('exit','sell','SELL',10,110)])
        self.con.execute("UPDATE v2_live_orders SET status='filled',filled_qty=20 WHERE broker_order_id='buy'")
        self.con.execute("UPDATE v2_live_orders SET status='cancelled',filled_qty=10 WHERE broker_order_id='sell'");self.con.commit()
        ledger.record_fee(self.con,2,'buy','entry-final',20,source='sourced total fixture',occurred_at=self.ts,covered_quantity=20)
        self.assertIsNone(ledger.report(self.con,2)['realised_net_minor'])
        fee=ledger.record_fee(self.con,2,'sell','exit-final',10,source='sourced total fixture',occurred_at=self.ts,covered_quantity=10)
        result=ledger.report(self.con,2)
        self.assertEqual(result['realised_gross_minor'],10000);self.assertEqual(result['realised_net_minor'],8000)
        self.assertEqual(result['realised_fee_allocation_minor'],2000)
        self.assertIsNone(result['available_margin']);self.assertFalse(result['certified'])
        correction=ledger.reverse(self.con,2,fee,'wrong-final-fee',source='fixture correction',occurred_at=self.ts)
        self.assertIsNone(ledger.report(self.con,2)['realised_net_minor'])
        with self.assertRaisesRegex(ValueError,'cannot themselves'):
            ledger.reverse(self.con,2,correction,'reverse-reversal',source='fixture',occurred_at=self.ts)
        ledger.record_fee(self.con,2,'sell','replacement',12,source='fixture corrected total',occurred_at=self.ts,covered_quantity=10)
        self.assertEqual(ledger.report(self.con,2)['realised_net_minor'],7800)

    def test_tied_opposite_fills_or_ambiguous_fee_totals_do_not_publish_profits(self):
        ledger.ingest_trades(self.con,2,[self.trade(qty=20),dict(self.trade('exit','sell','SELL',20,110),executed_at=self.trade()['executed_at'])])
        self.assertTrue(ledger.report(self.con,2)['execution_ordering_ambiguous'])
        self.assertIsNone(ledger.report(self.con,2)['realised_gross_minor'])
        self.assertIsNone(ledger.report(self.con,2)['realised_net_minor'])

    def test_reversed_reversal_cannot_restore_cash_with_missing_active_trade(self):
        ledger.ingest_trades(self.con,2,[self.trade()])
        event=self.con.execute("SELECT id FROM broker_ledger_events WHERE kind='FILL'").fetchone()[0]
        reversed_event=ledger.reverse(self.con,2,event,'correct-fill',source='fixture correction',occurred_at=self.ts)
        with self.assertRaisesRegex(ValueError,'cannot themselves'):
            ledger.reverse(self.con,2,reversed_event,'undo-correction',source='fixture',occurred_at=self.ts)
        self.assertEqual(ledger.report(self.con,2)['balances_minor']['inventory_cost'],0)

    def test_indistinguishable_priced_buy_lots_do_not_invent_partial_fifo_profit(self):
        ledger.ingest_trades(self.con,2,[self.trade('first',qty=10,px=100),self.trade('second',qty=10,px=102),
                                      self.trade('exit','sell','SELL',15,110)])
        result=ledger.report(self.con,2)
        self.assertTrue(result['execution_ordering_ambiguous'])
        self.assertIsNone(result['realised_gross_minor']);self.assertIsNone(result['realised_net_minor'])
