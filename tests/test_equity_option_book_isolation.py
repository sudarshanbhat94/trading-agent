"""The options lane starved the equity lanes of cash and slots.

index_options funds itself. Its pass computes `budget - spent + realised` over
`strategy='index_options'` alone, and the comment there says so plainly: "the
equity book is never touched and never funds an option."

That isolation was ONE-WAY. Every equity pass computed its own cash as

    budget - SUM(ALL v2_positions) + SUM(ALL v2_trades)

so option positions were charged against the equity book while option P&L was
credited to it, and the same all-strategies query fed `len(positions) >= max_pos`.

The live book on 2026-08-10, with three options open:

    equity positions   LAURUSLABS, RELIANCE, RBLBANK, OIL          4
    option positions   MIDCPNIFTY PE, BANKNIFTY CE, FINNIFTY CE    3
                                                          total    7  >= max_pos 6

    invested (all)   Rs 117,627
    realised (all)   Rs  13,685
    cash             Rs  -3,942     on a Rs 100,000 budget

Both gates shut at once: the entry loop bails at `cash < 0.25 * alloc`, and
seven rows tripped a six-slot cap only four of them belonged to. The better the
options lane did, the less the equity lanes could buy — and index_options is the
lane that works (+Rs 21,136), so it was quietly switching off the rest of the
book.

This — not the meta floor — is why no equity lane has bought since 2026-07-29.
The meta gate is very selective but does still pass signals: replayed over 12
sessions, three cleared it on 2026-07-31.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import v2_live


class EquityCashExcludesOptionsTest(unittest.TestCase):
    BUDGET = 100_000.0

    def _book(self, positions=(), trades=()):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_positions(market,strategy,symbol,shares,entry_price)")
        con.execute("CREATE TABLE v2_trades(market,strategy,pnl)")
        for strat, sym, sh, px in positions:
            con.execute("INSERT INTO v2_positions VALUES('IN',?,?,?,?)", (strat, sym, sh, px))
        for strat, pnl in trades:
            con.execute("INSERT INTO v2_trades VALUES('IN',?,?)", (strat, pnl))
        return con

    def test_the_live_book_that_exposed_it(self) -> None:
        """Reproduce 2026-08-10 exactly: -Rs 3,942 before, positive after."""
        con = self._book(
            positions=[("mom_breakout", "LAURUSLABS", 10, 1791.90),
                       ("manual", "RELIANCE", 12, 1305.00),
                       ("manual", "RBLBANK", 43, 382.90),
                       ("volume_surge", "OIL", 45, 458.15),
                       ("index_options", "MIDCPNIFTY26AUG14875PE", 120, 140.00),
                       ("index_options", "BANKNIFTY26AUG58200CE", 30, 518.45),
                       ("index_options", "FINNIFTY26AUG26750CE", 60, 243.55)],
            trades=[("volume_surge", -7184.0), ("manual", -198.0),
                    ("mom_breakout", -70.0), ("index_options", 21136.0)])
        inv_all = con.execute("SELECT SUM(shares*entry_price) FROM v2_positions").fetchone()[0]
        real_all = con.execute("SELECT SUM(pnl) FROM v2_trades").fetchone()[0]
        old_cash = self.BUDGET - inv_all + real_all
        self.assertLess(old_cash, 0, "the old formula really did go negative")
        self.assertAlmostEqual(old_cash, -3_942.0, delta=1.0)

        new_cash = v2_live._equity_cash(con, "IN", self.BUDGET)
        self.assertGreater(new_cash, 0, "equity lanes must have capital again")
        # 100,000 - 70,661 (equity only) - 7,452 (equity realised)
        self.assertAlmostEqual(new_cash, 21_887.0, delta=1.0)

    def test_option_profit_is_not_credited_to_the_equity_book(self) -> None:
        """Excluding option POSITIONS while keeping option P&L would hand the
        equity lanes money they did not earn — the mirror of the same bug."""
        con = self._book(trades=[("index_options", 50_000.0)])
        self.assertEqual(v2_live._equity_cash(con, "IN", self.BUDGET), self.BUDGET)

    def test_an_options_only_book_leaves_equity_cash_whole(self) -> None:
        con = self._book(positions=[("index_options", "NIFTY26AUG24000CE", 75, 200.0)])
        self.assertEqual(v2_live._equity_cash(con, "IN", self.BUDGET), self.BUDGET)

    def test_equity_positions_still_consume_equity_cash(self) -> None:
        con = self._book(positions=[("swing_meanrev", "ACME", 10, 1000.0)])
        self.assertAlmostEqual(v2_live._equity_cash(con, "IN", self.BUDGET), 90_000.0)

    def test_equity_losses_still_reduce_it(self) -> None:
        con = self._book(trades=[("volume_surge", -5_000.0)])
        self.assertAlmostEqual(v2_live._equity_cash(con, "IN", self.BUDGET), 95_000.0)


class SlotCapCountsEquityOnlyTest(unittest.TestCase):
    def test_options_do_not_occupy_equity_slots(self) -> None:
        positions = {
            "LAURUSLABS": dict(strategy="mom_breakout"),
            "RELIANCE": dict(strategy="manual"),
            "RBLBANK": dict(strategy="manual"),
            "OIL": dict(strategy="volume_surge"),
            "MIDCPNIFTY26AUG14875PE": dict(strategy="index_options"),
            "BANKNIFTY26AUG58200CE": dict(strategy="index_options"),
            "FINNIFTY26AUG26750CE": dict(strategy="index_options"),
        }
        self.assertEqual(len(positions), 7, "seven rows tripped a six-slot cap")
        eq = v2_live._equity_positions(positions)
        self.assertEqual(len(eq), 4)
        self.assertLess(len(eq), 6, "four equity holdings must leave slots free")
        for sym in eq:
            self.assertNotIn("NIFTY", sym)

    def test_the_passes_no_longer_count_every_row(self) -> None:
        import inspect
        for fn in (v2_live.poll_market, v2_live.volume_surge_pass):
            with self.subTest(fn=fn.__name__):
                src = inspect.getsource(fn)
                self.assertNotIn("len(positions) >= max_pos", src)

    def test_poll_market_grows_its_filtered_copy(self) -> None:
        """eq_positions is a COPY; if fills do not extend it the slot cap stops
        binding after the first buy of a pass."""
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("eq_positions[sym] = positions[sym]", src)


class CircuitBreakerStillSeesTheWholeBookTest(unittest.TestCase):
    """Deliberate asymmetry: entries are per-lane, risk is account-wide."""

    def test_the_breaker_uses_book_cash(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("equity_now = book_cash + pv_now", src)
        self.assertIn("book_cash = budget - sum(", src)

    def test_option_lanes_are_named_in_one_place(self) -> None:
        self.assertIn("index_options", v2_live.OPTION_LANES)


if __name__ == "__main__":
    unittest.main()
