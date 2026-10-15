"""Protection writes must remain owned and fenced across broker I/O."""
import unittest

from app import protection, worker_fencing
from tests import test_protection_lifecycle as fixtures


class ProtectionRaceTest(unittest.TestCase):
    def setUp(self):
        fixtures.ProtectionLifecycleTest.setUp(self)

    fill = fixtures.ProtectionLifecycleTest.fill
    native = fixtures.ProtectionLifecycleTest.native
    state = fixtures.ProtectionLifecycleTest.state

    def sell(self, quantity, *, status='filled', filled=None):
        self.con.execute("INSERT INTO v2_live_orders(ts,user_id,market,symbol,instrument_key,side,qty,price,notional,"
                         "product,status,broker_order_id,filled_qty) VALUES(?,2,'IN','TEST','NSE_EQ|TEST','SELL',?,100,?,"
                         "'D',?,'other-exit',?)", (self.now.isoformat(), quantity, quantity*100, status,
                                                  quantity if filled is None else filled))
        self.con.commit()

    def test_worker_takeover_during_status_read_cannot_attach_exit(self):
        self.native()
        worker_fencing.ensure_schema(self.con)
        token = worker_fencing.acquire(self.con, 'old-worker')
        context = worker_fencing.ACTIVE.set(token)
        self.addCleanup(worker_fencing.ACTIVE.reset, context)

        def late_status(*_):
            self.con.execute("UPDATE worker_leases SET expires=0")
            self.con.commit()
            worker_fencing.acquire(self.con, 'replacement-worker')
            self.evidence['rules'][0].update(status='TRIGGERED', order_id='late-exit')
            return [self.evidence]

        self.port.protection_status.side_effect = late_status
        with self.assertRaisesRegex(RuntimeError, 'stale paper-engine fence'):
            protection.refresh(self.con, 2, self.port)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0], 0)
        self.assertIsNone(self.con.execute("SELECT exit_order_id FROM protection_obligations").fetchone()[0])

    def test_partial_owned_exit_cannot_leave_oversized_stop_armed(self):
        self.native()
        self.sell(7)
        protection.observe_fills(self.con, 2)
        self.con.commit()
        self.assertEqual(self.state(), 'unknown')
        protection.refresh(self.con, 2, self.port)
        self.assertEqual(self.state(), 'unknown')
        self.assertTrue(protection.blocks_entry(self.con, 2))

    def test_pending_exit_prevents_native_activation(self):
        self.fill()
        self.sell(20, status='submitted', filled=0)
        with self.assertRaisesRegex(ValueError, 'exit'):
            protection.request_native(self.con, 2, self.entry, authorization_reference='fixture review')
        self.assertEqual(self.state(), 'app_only')
        self.port.place_stop.assert_not_called()

    def test_repeated_valid_status_refreshes_coverage_age(self):
        self.native()
        self.con.execute('UPDATE protection_obligations SET updated_at=0');self.con.commit()
        self.assertTrue(protection.blocks_entry(self.con,2))
        protection.refresh(self.con,2,self.port)
        self.assertFalse(protection.blocks_entry(self.con,2))

    def test_disappearing_or_changed_child_cannot_release_sell_reservation(self):
        self.native();self.evidence['rules'][0].update(status='TRIGGERED',order_id='native-child')
        protection.refresh(self.con,2,self.port)
        for status,oid in (('SCHEDULED',None),('CANCELLED',None),('TRIGGERED','different-child')):
            self.evidence['rules'][0].update(status=status,order_id=oid)
            protection.refresh(self.con,2,self.port)
            self.assertEqual(self.state(),'unknown')
            self.assertFalse(protection.prepare_exit(self.con,2,'TEST',self.port))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v2_live_orders WHERE side='SELL'").fetchone()[0],1)
