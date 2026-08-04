"""The live mirror. Real orders, so every refusal path is asserted.

The two failures that would cost the most money are tested hardest:
  * sizing a Rs 10,000 sleeve with the paper book's Rs 1,00,000 quantities;
  * placing an order against a guessed instrument key, which is a real order
    for the wrong company.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import broker as _broker_mod, live_trade, v2_live


def _fresh_broker(**cfg):
    os.environ["BROKER_STATE_PATH"] = os.path.join(tempfile.mkdtemp(), "broker.json")
    b = importlib.reload(_broker_mod)
    if cfg:
        b.configure(**cfg)
    return b


def _dbs():
    v2 = sqlite3.connect(":memory:")
    v2_live.ensure_schema(v2)
    main = sqlite3.connect(":memory:")
    main.execute("CREATE TABLE universe(symbol TEXT, upstox_instrument_key TEXT, enabled INT)")
    main.executemany("INSERT INTO universe VALUES(?,?,1)",
                     [("RELIANCE", "NSE_EQ|INE002A01018"), ("KEI", "NSE_EQ|INE878B01027"),
                      ("NOKEY", "")])
    main.commit()
    return v2, main


class InstrumentKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v2, self.main = _dbs()

    def test_a_known_symbol_resolves(self) -> None:
        self.assertEqual(live_trade.instrument_key(self.main, "RELIANCE"),
                         "NSE_EQ|INE002A01018")

    def test_a_symbol_without_a_key_returns_none(self) -> None:
        """10,377 of 13,036 enabled symbols have no key. None must mean DO NOT
        TRADE — a guessed key is a real order for the wrong company."""
        self.assertIsNone(live_trade.instrument_key(self.main, "NOKEY"))

    def test_an_unknown_symbol_returns_none(self) -> None:
        self.assertIsNone(live_trade.instrument_key(self.main, "NOTLISTED"))


class SizingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh_broker(budget=10000)
        self.st = self.b.state()

    def test_it_sizes_to_the_sleeve_not_the_paper_book(self) -> None:
        """Paper runs Rs 1,00,000 over 6 slots (~Rs 16,600 a position). Copying
        that into a Rs 10,000 account is the expensive mistake."""
        qty = live_trade.size_for_sleeve(1300.0, self.st, margin=9115.0)
        self.assertEqual(qty, 2)                       # Rs 2,600, not Rs 16,600
        self.assertLessEqual(qty * 1300.0, float(self.st["max_order"]))

    def test_real_margin_caps_the_order(self) -> None:
        """The cap is the LOWER of the configured sleeve and what the broker
        says is actually there."""
        self.assertEqual(live_trade.size_for_sleeve(1000.0, self.st, margin=1500.0), 1)

    def test_a_stock_dearer_than_the_slice_is_unaffordable(self) -> None:
        self.assertEqual(live_trade.size_for_sleeve(5000.0, self.st, margin=9115.0), 0)

    def test_zero_and_negative_prices_do_not_divide(self) -> None:
        for px in (0, -5, None):
            with self.subTest(price=px):
                self.assertEqual(live_trade.size_for_sleeve(px, self.st, margin=9000.0), 0)


class MirrorEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v2, self.main = _dbs()
        self.b = _fresh_broker(budget=10000, owner_user_id=2)

    def _arm(self):
        self.b.save_token("t0k")
        self.b.configure(armed=True, kill_switch=False)

    def _entry(self, symbol="RELIANCE", price=1300.0, strategy="swing_meanrev"):
        return live_trade.mirror_entry(self.v2, self.main, "IN", symbol, price, strategy)

    def test_disarmed_places_nothing(self) -> None:
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("not armed", self._entry())
            po.assert_not_called()

    def test_armed_places_a_real_buy(self) -> None:
        self._arm()
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="OID1")) as po:
            self.assertEqual(self._entry(), "sent")
            po.assert_called_once()
            key, qty, side = po.call_args[0][0], po.call_args[0][1], po.call_args[0][2]
            self.assertEqual(key, "NSE_EQ|INE002A01018")
            self.assertEqual(side, "BUY")
            self.assertEqual(qty, 2)

    def test_a_symbol_without_a_key_is_skipped_and_logged(self) -> None:
        self._arm()
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("instrument key", self._entry("NOKEY"))
            po.assert_not_called()
        row = self.v2.execute("SELECT status,reason FROM v2_live_orders").fetchone()
        self.assertEqual(row[0], "skipped")
        self.assertIn("instrument key", row[1])

    def test_an_unreadable_margin_blocks_the_buy(self) -> None:
        """Unknown balance must not be read as plenty — the alternative is
        discovering it via a reject."""
        self._arm()
        with mock.patch.object(live_trade, "available_margin", return_value=None), \
             mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("margin unknown", self._entry())
            po.assert_not_called()

    def test_an_unmirrored_lane_is_skipped(self) -> None:
        """gap_momentum is a measured net loser, quarantined from the paper
        book. It must not reappear with real money."""
        self._arm()
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("not mirrored", self._entry(strategy="gap_momentum"))
            po.assert_not_called()

    def test_it_does_not_double_up_on_a_symbol_already_held(self) -> None:
        self._arm()
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="OID1")):
            self._entry()
            self.assertIn("already held", self._entry())

    def test_a_us_symbol_is_never_mirrored(self) -> None:
        self._arm()
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("non-IN", live_trade.mirror_entry(
                self.v2, self.main, "US", "AAPL", 200.0, "swing_meanrev"))
            po.assert_not_called()

    def test_a_broker_rejection_is_recorded_as_failed(self) -> None:
        self._arm()
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=False, status=400, response={"e": "x"})):
            self.assertIn("failed", self._entry())
        self.assertEqual(self.v2.execute("SELECT status FROM v2_live_orders").fetchone()[0],
                         "failed")


class MirrorExitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v2, self.main = _dbs()
        self.b = _fresh_broker(budget=10000, owner_user_id=2)
        self.b.save_token("t0k")
        self.b.configure(armed=True, kill_switch=False)
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="B1")):
            live_trade.mirror_entry(self.v2, self.main, "IN", "RELIANCE", 1300.0,
                                    "swing_meanrev")

    def test_it_sells_the_live_quantity_not_the_paper_one(self) -> None:
        """THE one that matters. Paper holds ~12 shares of this; the sleeve
        holds 2. Selling 12 shorts the account by 10."""
        self.assertEqual(live_trade.live_qty(self.v2, "RELIANCE"), 2)
        with mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="S1")) as po:
            live_trade.mirror_exit(self.v2, self.main, "IN", "RELIANCE", 1400.0, "target")
            self.assertEqual(po.call_args[0][1], 2)
            self.assertEqual(po.call_args[0][2], "SELL")

    def test_the_position_is_flat_afterwards(self) -> None:
        with mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="S1")):
            live_trade.mirror_exit(self.v2, self.main, "IN", "RELIANCE", 1400.0, "target")
        self.assertEqual(live_trade.live_qty(self.v2, "RELIANCE"), 0)
        self.assertNotIn("RELIANCE", live_trade.open_symbols(self.v2))

    def test_selling_nothing_held_places_no_order(self) -> None:
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("nothing held", live_trade.mirror_exit(
                self.v2, self.main, "IN", "KEI", 100.0, "target"))
            po.assert_not_called()

    def test_a_disarmed_exit_records_that_shares_are_still_held(self) -> None:
        """Silence here would leave a real position nobody is tracking."""
        self.b.configure(armed=False, kill_switch=True)
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("still open", live_trade.mirror_exit(
                self.v2, self.main, "IN", "RELIANCE", 1400.0, "stop"))
            po.assert_not_called()
        reason = self.v2.execute("SELECT reason FROM v2_live_orders ORDER BY id DESC"
                                 " LIMIT 1").fetchone()[0]
        self.assertIn("still held live", reason)


class EngineIsolationTest(unittest.TestCase):
    """The paper book must survive the broker."""

    def test_a_broker_outage_does_not_stop_the_paper_book(self) -> None:
        v2, _main = _dbs()
        _fresh_broker(budget=10000, owner_user_id=2)
        with mock.patch.object(v2_live, "_live_mirror_entry",
                               side_effect=RuntimeError("broker down")):
            ok = v2_live.record_entry(v2, "IN", "swing_meanrev", "RELIANCE",
                                      "2026-08-04", 1300.0, 12, 1200.0, 1500.0,
                                      0.0, 0.5, None)
        self.assertTrue(ok, "paper entry must be recorded even if the mirror throws")
        self.assertEqual(v2.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0], 1)

    def test_the_mirror_is_a_noop_when_not_ready(self) -> None:
        v2, _main = _dbs()
        _fresh_broker(budget=10000)          # disconnected, disarmed
        with mock.patch.object(_broker_mod, "place_order") as po:
            v2_live.record_entry(v2, "IN", "swing_meanrev", "RELIANCE", "2026-08-04",
                                 1300.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
            po.assert_not_called()


if __name__ == "__main__":
    unittest.main()
