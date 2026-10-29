"""Risk permission must describe the same owned state as its broker evidence."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile

from unittest.mock import patch

from app import broker, broker_reconciliation as recon
from tests import test_execution_hardening as fixtures
from tests import test_execution_events as execution_fixtures
from tests.contract_storage_fixtures import ContractStorageCase


class ReconciliationSnapshotTest(ContractStorageCase):
    setUp = fixtures.ReconciliationTest.setUp
    review = fixtures.ReconciliationTest.review

    def ready(self):
        return recon.ready(self.con, 1, self.now.timestamp() + 1)

    def test_confirmed_fill_change_invalidates_recent_permission(self):
        self.assertEqual(self.review()['status'], 'ok')
        self.con.execute('UPDATE v2_live_orders SET filled_qty=19')
        self.con.commit()
        self.assertFalse(self.ready())

    def test_new_pending_commitment_invalidates_recent_permission(self):
        self.review()
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,"
                         "product,status,filled_qty) VALUES(?,1,'IN','OTHER','NSE_EQ|OTHER','BUY',1,100,'D','unknown',0)",
                         (self.now.isoformat(),))
        self.con.commit()
        self.assertFalse(self.ready())

    def test_other_owner_and_observation_time_do_not_invalidate_permission(self):
        self.review()
        self.con.execute('UPDATE v2_live_orders SET reconciled_at=?', (self.now.isoformat(),))
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,"
                         "product,status,filled_qty) VALUES(?,2,'IN','OTHER','NSE_EQ|OTHER','BUY',1,100,'D','unknown',0)",
                         (self.now.isoformat(),))
        self.con.commit()
        self.assertTrue(self.ready())

    def test_unbound_or_corrupt_old_evidence_never_grants_permission(self):
        for payload in ('{}', '{bad', json.dumps({'owned_state_fingerprint': 'wrong'})):
            with self.subTest(payload=payload):
                self.review()
                self.con.execute('UPDATE broker_reconciliation SET payload=?', (payload,))
                self.con.commit()
                self.assertFalse(self.ready())

    def test_owned_reads_and_evidence_write_are_one_serialized_snapshot(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        db = str(Path(directory.name) / 'book.db')
        target = sqlite3.connect(db); self.con.backup(target); self.con.close()
        self.con = target; self.addCleanup(target.close)
        writer = sqlite3.connect(db); self.addCleanup(writer.close)
        original_atomic = recon.atomic

        @contextmanager
        def mutate_before_lock(con):
            # The competing commit happens just before the reviewer acquires
            # its lock. It must not retain comparisons made before this commit.
            writer.execute('UPDATE v2_live_orders SET filled_qty=19'); writer.commit()
            with original_atomic(con):
                yield

        with patch.object(recon, 'atomic', mutate_before_lock):
            result = self.review()
        self.assertEqual(result['status'], 'mismatch')
        self.assertFalse(self.ready())


class ReconciliationEntryGateTest(ContractStorageCase):
    setUp = execution_fixtures.ExecutionObservationTest.setUp
    submit = execution_fixtures.ExecutionObservationTest.submit

    def test_closed_round_trip_cannot_reuse_pre_trade_funds_permission(self):
        # Inventory returns to zero, but those fills changed cash and risk.
        # A 120-second-old empty-inventory reconciliation is insufficient.
        for side, price in (('BUY', 200), ('SELL', 199)):
            self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,"
                             "product,status,broker_order_id,filled_qty,average_price) "
                             "VALUES(datetime('now'),2,'IN','TEST','NSE_EQ|TEST',?,5,?,'D','filled',?,5,?)",
                             (side, price, 'prior-' + side, price))
        self.con.commit()
        with patch.object(broker, 'place_order', side_effect=AssertionError('must not transmit')) as transport:
            self.assertIn('reconciliation', self.submit())
            transport.assert_not_called()
