"""Per-user paper books: one subscriber's actions never touch another's.

Pro is sold as "your own Rs 1,00,000 paper book" and until now there was ONE
book — the engine's — that every subscriber saw and any of them could wipe.

The engine's tables are deliberately untouched. It reads and writes
v2_positions in fifty places, all assuming a single book, and the same tables
are the evidence base for every measured claim in this codebase. Reshaping them
to ship a product feature would put that at risk; users get their own tables
instead.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import books, v2_live


def _db():
    con = sqlite3.connect(":memory:")
    v2_live.ensure_schema(con)
    books.ensure_schema(con)
    return con


class IsolationTest(unittest.TestCase):
    """THE point of the exercise."""

    def setUp(self) -> None:
        self.con = _db()

    def test_a_reset_clears_only_the_caller(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.buy(self.con, 2, "IN", "manual", "ITC", 300.0)
        books.reset(self.con, 1)
        self.assertEqual(len(books.positions(self.con, 1)), 0)
        self.assertEqual(len(books.positions(self.con, 2)), 1, "user 2 must be untouched")

    def test_a_reset_does_not_touch_the_engines_book(self) -> None:
        """The failure that started this: unqualified DELETEs took the house
        book and its whole history with them."""
        v2_live.record_entry(self.con, "IN", "swing_meanrev", "RELIANCE",
                             "2026-08-04", 1300.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
        books.reset(self.con, 1)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0], 1)

    def test_one_users_buy_is_invisible_to_another(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(books.open_symbols(self.con, 2), set())

    def test_cash_is_per_user(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertLess(books.cash(self.con, 1), books.cash(self.con, 2))

    def test_a_sell_credits_only_the_seller(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.buy(self.con, 2, "IN", "manual", "ITC", 300.0)
        books.sell(self.con, 1, "IN", "ITC", 360.0)
        self.assertEqual(len(books.positions(self.con, 2)), 1)
        self.assertEqual(len(books.positions(self.con, 1)), 0)


class SizingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = _db()

    def test_a_position_is_a_sixth_of_the_book(self) -> None:
        qty = books.size_for(self.con, 1, "IN", 1000.0)
        self.assertEqual(qty, 16)              # 100000/6 = 16666 -> 16 shares

    def test_it_shrinks_once_cash_is_the_binding_cap(self) -> None:
        """Two caps: budget/6 per position, and the cash actually free. The
        first binds on a full book (a sixth of Rs 1,00,000 is Rs 16,666), so
        the size only falls once free cash drops below that."""
        first = books.size_for(self.con, 1, "IN", 1000.0)
        self.assertEqual(first, 16)
        books.buy(self.con, 1, "IN", "manual", "BIG", 19000.0, shares=5)  # Rs 95,000
        self.assertLess(books.cash(self.con, 1), 16666)
        self.assertLess(books.size_for(self.con, 1, "IN", 1000.0), first)

    def test_a_book_never_goes_negative(self) -> None:
        """The house book's manual-buy path computed a NEGATIVE quantity when
        cash ran out. A smaller book must skip, not borrow."""
        for sym in ("A", "B", "C", "D", "E", "F"):
            books.buy(self.con, 1, "IN", "manual", sym, 16000.0)
        self.assertGreaterEqual(books.cash(self.con, 1), 0)

    def test_an_unaffordable_stock_is_skipped_not_an_error(self) -> None:
        self.assertEqual(books.buy(self.con, 1, "IN", "manual", "MRF", 200000.0), 0)

    def test_the_book_is_capped_at_six_positions(self) -> None:
        for sym in "ABCDEFGH":
            books.buy(self.con, 1, "IN", "manual", sym, 100.0)
        self.assertEqual(len(books.positions(self.con, 1)), books.MAX_POSITIONS)

    def test_the_same_symbol_is_not_doubled(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(books.buy(self.con, 1, "IN", "manual", "ITC", 300.0), 0)


class CostsMatchTheHouseTest(unittest.TestCase):
    def test_pnl_is_net_of_the_same_costs(self) -> None:
        """A user book reporting gross while the engine reports net would make
        the two incomparable, which defeats running them side by side."""
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        net, pct = books.sell(con, 1, "IN", "ITC", 110.0)
        expected, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertAlmostEqual(net, expected)

    def test_a_breakeven_round_trip_loses_the_costs(self) -> None:
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        net, _ = books.sell(con, 1, "IN", "ITC", 100.0)
        self.assertLess(net, 0)


class MirrorTest(unittest.TestCase):
    """The engine's decision, applied to each subscriber's own cash."""

    class _DB:
        def __init__(self, users):
            self._u = users

        def list_users(self):
            return self._u

    def setUp(self) -> None:
        self.con = _db()
        from app import plans
        self.plans = plans
        self.db = self._DB([
            dict(id=1, active=True, account_plan="paper"),
            dict(id=2, active=True, account_plan="auto"),
            dict(id=3, active=True, account_plan="watch"),   # no paper book
            dict(id=4, active=False, account_plan="auto"),   # inactive
        ])

    def test_only_entitled_active_users_get_the_trade(self) -> None:
        n = books.mirror_entry(self.con, self.db, self.plans, "IN",
                               "swing_meanrev", "ITC", 300.0)
        self.assertEqual(n, 2)
        self.assertEqual(books.open_symbols(self.con, 1), {"ITC"})
        self.assertEqual(books.open_symbols(self.con, 3), set())
        self.assertEqual(books.open_symbols(self.con, 4), set())

    def test_each_book_sizes_to_its_own_cash(self) -> None:
        """Not the house quantity, and not each other's — that is the whole
        point of a personal book. User 1 is spent down below the per-position
        cap so cash becomes the binding constraint for them and not for user 2."""
        books.buy(self.con, 1, "IN", "manual", "X", 19000.0, shares=5)  # Rs 95,000
        books.mirror_entry(self.con, self.db, self.plans, "IN", "swing_meanrev",
                           "ITC", 300.0)
        p1 = books.positions(self.con, 1)[-1]["shares"]
        p2 = books.positions(self.con, 2)[-1]["shares"]
        self.assertLess(p1, p2)

    def test_the_exit_closes_every_book_holding_it(self) -> None:
        books.mirror_entry(self.con, self.db, self.plans, "IN", "swing_meanrev",
                           "ITC", 300.0)
        n = books.mirror_exit(self.con, None, self.plans, "IN", "ITC", 330.0, "target")
        self.assertEqual(n, 2)
        self.assertEqual(books.open_symbols(self.con, 1), set())
        self.assertEqual(books.open_symbols(self.con, 2), set())

    def test_an_exit_skips_books_that_never_took_it(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(
            books.mirror_exit(self.con, None, self.plans, "IN", "ITC", 330.0, "target"), 1)


class StatsTest(unittest.TestCase):
    def test_an_untouched_book_reports_its_full_budget(self) -> None:
        con = _db()
        s = books.stats(con, 9, "IN", {})
        self.assertEqual(s["equity"], 100000.0)
        self.assertEqual(s["positions"], 0)
        self.assertEqual(s["cash"], 100000.0)

    def test_equity_follows_the_live_price(self) -> None:
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        s = books.stats(con, 1, "IN", {"ITC": {"price": 120.0}})
        self.assertAlmostEqual(s["unrealised"], 200.0)
        self.assertAlmostEqual(s["equity"], 100000.0 + 200.0)


if __name__ == "__main__":
    unittest.main()
