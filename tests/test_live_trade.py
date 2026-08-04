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

UID = 2


def _no_network_verify(b):
    """verify() asks Upstox whether the token works, and a test token really is
    invalid — so without this the engine correctly refuses to trade and the
    tests below would be asserting the verifier, not the mirror."""
    b.verify = lambda user_id, force=False: True
    return b


def _fresh_broker(**cfg):
    tmp = tempfile.mkdtemp()
    os.environ["BROKER_STATE_DIR"] = os.path.join(tmp, "brokers")
    os.environ["BROKER_STATE_PATH"] = os.path.join(tmp, "legacy.json")
    b = _no_network_verify(importlib.reload(_broker_mod))
    if cfg:
        b.configure(UID, **cfg)
    return b


def _dbs():
    """v2 in memory, main on DISK.

    The main DB must be a file: production opens a fresh read-only connection
    per call and closes it, so an in-memory handle shared across calls is closed
    after the first one and every later lookup fails. That is a test artifact
    that would otherwise look exactly like a broken exit.
    """
    v2 = sqlite3.connect(":memory:")
    v2_live.ensure_schema(v2)
    path = os.path.join(tempfile.mkdtemp(), "main.db")
    main = sqlite3.connect(path)
    main.execute("CREATE TABLE universe(symbol TEXT, upstox_instrument_key TEXT, enabled INT)")
    main.executemany("INSERT INTO universe VALUES(?,?,1)",
                     [("RELIANCE", "NSE_EQ|INE002A01018"), ("KEI", "NSE_EQ|INE878B01027"),
                      ("NOKEY", "")])
    main.commit()
    return v2, main, path


class InstrumentKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v2, self.main, self.main_path = _dbs()

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
        self.st = self.b.state(UID)

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
        self.v2, self.main, self.main_path = _dbs()
        self.b = _fresh_broker(budget=10000)

    def _arm(self):
        self.b.save_token(UID, "t0k")
        self.b.configure(UID, armed=True, kill_switch=False)

    def _entry(self, symbol="RELIANCE", price=1300.0, strategy="swing_meanrev"):
        return live_trade.mirror_entry(self.v2, self.main, UID, "IN", symbol, price, strategy)

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
            uid_, key, qty, side = po.call_args[0][:4]
            self.assertEqual(uid_, UID)
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
                self.v2, self.main, UID, "US", "AAPL", 200.0, "swing_meanrev"))
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
        self.v2, self.main, self.main_path = _dbs()
        self.b = _fresh_broker(budget=10000)
        self.b.save_token(UID, "t0k")
        self.b.configure(UID, armed=True, kill_switch=False)
        with mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="B1")):
            live_trade.mirror_entry(self.v2, self.main, UID, "IN", "RELIANCE",
                                    1300.0, "swing_meanrev")

    def test_it_sells_the_live_quantity_not_the_paper_one(self) -> None:
        """THE one that matters. Paper holds ~12 shares of this; the sleeve
        holds 2. Selling 12 shorts the account by 10."""
        self.assertEqual(live_trade.live_qty(self.v2, UID, "RELIANCE"), 2)
        with mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="S1")) as po:
            live_trade.mirror_exit(self.v2, self.main, UID, "IN", "RELIANCE", 1400.0, "target")
            self.assertEqual(po.call_args[0][2], 2)
            self.assertEqual(po.call_args[0][3], "SELL")

    def test_the_position_is_flat_afterwards(self) -> None:
        with mock.patch.object(_broker_mod, "place_order",
                               return_value=dict(ok=True, order_id="S1")):
            live_trade.mirror_exit(self.v2, self.main, UID, "IN", "RELIANCE", 1400.0, "target")
        self.assertEqual(live_trade.live_qty(self.v2, UID, "RELIANCE"), 0)
        self.assertNotIn("RELIANCE", live_trade.open_symbols(self.v2, UID))

    def test_selling_nothing_held_places_no_order(self) -> None:
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("nothing held", live_trade.mirror_exit(
                self.v2, self.main, UID, "IN", "KEI", 100.0, "target"))
            po.assert_not_called()

    def test_a_disarmed_exit_records_that_shares_are_still_held(self) -> None:
        """Silence here would leave a real position nobody is tracking."""
        self.b.configure(UID, armed=False, kill_switch=True)
        with mock.patch.object(_broker_mod, "place_order") as po:
            self.assertIn("still open", live_trade.mirror_exit(
                self.v2, self.main, UID, "IN", "RELIANCE", 1400.0, "stop"))
            po.assert_not_called()
        reason = self.v2.execute("SELECT reason FROM v2_live_orders ORDER BY id DESC"
                                 " LIMIT 1").fetchone()[0]
        self.assertIn("still held live", reason)


class RoundTripThroughTheEngineTest(unittest.TestCase):
    """Buy AND exit, driven through the real record_entry / record_exit.

    Not through live_trade directly: the question worth answering is whether the
    ENGINE's own writers fire the mirror, in the right order, with the right
    size — which is what actually happens in production.
    """

    def setUp(self) -> None:
        self.v2, self.main, self.main_path = _dbs()
        self.b = _fresh_broker(budget=9000)
        self.b.save_token(UID, "t0k")
        self.b.configure(UID, armed=True, kill_switch=False)
        self.sent = []

        def fake_place(uid, key, qty, side="BUY", **kw):
            self.sent.append((side, key, qty))
            return dict(ok=True, order_id=f"O{len(self.sent)}")
        self.fake_place = fake_place

    def _ro(self, _path):
        # a FRESH connection each call, like production — the caller closes it
        return sqlite3.connect(self.main_path)

    def test_a_full_buy_then_exit_places_both_real_orders(self) -> None:
        with mock.patch.object(_broker_mod, "place_order", side_effect=self.fake_place), \
             mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(v2_live, "_ro", self._ro):
            # paper opens 12 shares on a Rs 1,00,000 book
            v2_live.record_entry(self.v2, "IN", "swing_meanrev", "RELIANCE",
                                 "2026-08-04", 1305.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
            pid = self.v2.execute("SELECT id FROM v2_positions").fetchone()[0]
            # paper closes the same 12 shares
            v2_live.record_exit(self.v2, "IN", pid, "2026-08-05", 1400.0, 12, "target")

        self.assertEqual(len(self.sent), 2, self.sent)
        (b_side, b_key, b_qty), (s_side, s_key, s_qty) = self.sent
        self.assertEqual((b_side, b_key), ("BUY", "NSE_EQ|INE002A01018"))
        self.assertEqual((s_side, s_key), ("SELL", "NSE_EQ|INE002A01018"))
        # THE assertion: the sleeve bought 2 and sold 2, while paper did 12
        self.assertEqual(b_qty, 2)
        self.assertEqual(s_qty, 2)
        self.assertEqual(live_trade.live_qty(self.v2, UID, "RELIANCE"), 0)

    def test_the_exit_reads_the_symbol_before_the_row_is_deleted(self) -> None:
        """All three exit paths DELETE the position after record_exit. If that
        order ever inverts, the sell silently stops happening and real shares
        are stranded — so the ordering is pinned here."""
        import inspect
        import pathlib
        root = pathlib.Path(inspect.getfile(v2_live)).parent
        for name in ("v2_live.py", "v2_web.py"):
            src = (root / name).read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines()):
                if "DELETE FROM v2_positions WHERE id=?" in line:
                    # a generous window: there is a multi-line EXIT log between
                    # the two in the engine's exit_monitor
                    window = "\n".join(src.splitlines()[max(0, i - 30):i])
                    with self.subTest(file=name, line=i + 1):
                        self.assertIn("record_exit(", window,
                                      "the position row must be deleted AFTER record_exit")

    def test_an_exit_still_sells_when_the_live_buy_was_smaller(self) -> None:
        """Paper size and live size are independent; the sell follows the live
        ledger, not the paper position."""
        with mock.patch.object(_broker_mod, "place_order", side_effect=self.fake_place), \
             mock.patch.object(live_trade, "available_margin", return_value=3000.0), \
             mock.patch.object(v2_live, "_ro", self._ro):
            v2_live.record_entry(self.v2, "IN", "swing_meanrev", "RELIANCE",
                                 "2026-08-04", 1305.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
            pid = self.v2.execute("SELECT id FROM v2_positions").fetchone()[0]
            v2_live.record_exit(self.v2, "IN", pid, "2026-08-05", 1400.0, 12, "stop")
        self.assertEqual([q for _s, _k, q in self.sent], [2, 2])


class ManualBuyReachesTheBrokerTest(unittest.TestCase):
    """A manual Buy must place a real order too.

    The endpoint carried its own INSERT INTO v2_positions and never called
    record_entry, so the live mirror — which hangs off record_entry — never
    fired. Manual Sell already used record_exit, so the asymmetry was worse than
    "manual does not trade live": it would have tried to SELL shares the sleeve
    never bought.
    """

    def test_manual_buy_writes_the_callers_book_not_the_house(self) -> None:
        """Superseded: manual buy used to call record_entry, which writes the
        HOUSE book and fires the broker mirror — so any subscriber's click
        placed a real order in the OPERATOR's account. It now writes
        books.buy() and mirrors only when the caller IS the owner."""
        import inspect
        import pathlib
        from app import v2_web
        src = pathlib.Path(inspect.getfile(v2_web)).read_text(encoding="utf-8")
        start = src.index('@router.post("/api/buy")')
        body = src[start:src.index("@router.post", start + 10)]
        self.assertIn("books.buy(", body)
        self.assertNotIn("record_entry(", body)
        self.assertNotIn("INSERT INTO v2_positions", body)
        self.assertIn("_bk.state(uid)", body)

    def test_manual_is_a_mirrored_lane(self) -> None:
        """Otherwise api_buy's direct mirror_entry call is skipped and the Buy
        button silently places nothing."""
        self.assertIn("manual", live_trade.MIRRORED_LANES)

    def test_the_engine_never_fans_a_manual_entry_out(self) -> None:
        """"manual" in MIRRORED_LANES looks dangerous — record_entry fans out to
        EVERY linked broker — but no engine path records a manual entry. The
        only manual mirror is api_buy's direct call, which passes the clicking
        user's own id. If a lane ever starts recording "manual" through
        record_entry, this fails and the fan-out has to be reconsidered."""
        import inspect
        import pathlib as _pl
        src = _pl.Path(inspect.getfile(v2_live)).read_text(encoding="utf-8")
        calls = [ln for ln in src.splitlines()
                 if "record_entry(v2, market," in ln and "def " not in ln]
        self.assertTrue(calls)
        for ln in calls:
            self.assertNotIn('"manual"', ln)

    def test_a_manual_entry_places_a_real_buy(self) -> None:
        v2, main, path = _dbs()
        b = _fresh_broker(budget=9000)
        b.save_token(UID, "t0k")
        b.configure(UID, armed=True, kill_switch=False)
        sent = []

        def fake_place(uid, key, qty, side="BUY", **kw):
            sent.append((side, key, qty))
            return dict(ok=True, order_id="M1")

        with mock.patch.object(_broker_mod, "place_order", side_effect=fake_place), \
             mock.patch.object(live_trade, "available_margin", return_value=9115.0), \
             mock.patch.object(v2_live, "_ro", lambda _p: sqlite3.connect(path)):
            v2_live.record_entry(v2, "IN", "manual", "RELIANCE", "2026-08-04",
                                 1305.0, 7, 1226.7, 1383.3, 0.0, 1.0, None)
        self.assertEqual(sent, [("BUY", "NSE_EQ|INE002A01018", 2)])


class EngineIsolationTest(unittest.TestCase):
    """The paper book must survive the broker."""

    def test_a_broker_outage_does_not_stop_the_paper_book(self) -> None:
        v2, _main, _path = _dbs()
        _fresh_broker(budget=10000)
        with mock.patch.object(v2_live, "_live_mirror_entry",
                               side_effect=RuntimeError("broker down")):
            ok = v2_live.record_entry(v2, "IN", "swing_meanrev", "RELIANCE",
                                      "2026-08-04", 1300.0, 12, 1200.0, 1500.0,
                                      0.0, 0.5, None)
        self.assertTrue(ok, "paper entry must be recorded even if the mirror throws")
        self.assertEqual(v2.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0], 1)

    def test_the_mirror_is_a_noop_when_not_ready(self) -> None:
        v2, _main, _path = _dbs()
        _fresh_broker(budget=10000)          # disconnected, disarmed
        with mock.patch.object(_broker_mod, "place_order") as po:
            v2_live.record_entry(v2, "IN", "swing_meanrev", "RELIANCE", "2026-08-04",
                                 1300.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
            po.assert_not_called()


if __name__ == "__main__":
    unittest.main()
