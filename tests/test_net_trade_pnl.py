"""Recorded P&L must be net of costs, like the cash ledger already was.

Exits credited `cash` net of both sides' costs but wrote the GROSS move into
v2_trades. Equity was therefore honest while everything derived from the trade
table — realised P&L, win rate, profit factor, per-lane attribution — was
flattered by exactly the cost of trading. The dashboard headline overstated the
edge; equity quietly disagreed with it.

Three exit paths each carried their own copy of the arithmetic (the engine's
exit_monitor and two manual-sell endpoints), which is how they drifted apart.
"""

from __future__ import annotations

import inspect
import unittest

from app import v2_live


class NetTradePnlTest(unittest.TestCase):
    def test_costs_are_charged_on_both_sides(self) -> None:
        """Round-trip: cost applies to the buy AND the sell notional."""
        cside = v2_live.COST_SIDE["IN"]
        net, _pct = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        expected = 10 * (110.0 - 100.0) - cside * 10 * (100.0 + 110.0)
        self.assertAlmostEqual(net, expected)

    def test_net_is_always_below_gross(self) -> None:
        gross = 10 * (110.0 - 100.0)
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertLess(net, gross)

    def test_a_loss_is_made_worse_not_better(self) -> None:
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 90.0)
        self.assertLess(net, 10 * (90.0 - 100.0))

    def test_a_marginal_winner_can_become_a_net_loser(self) -> None:
        """The case that corrupts win rate: a move smaller than the round trip
        is a LOSS. Reported gross it counted as a win."""
        net, pct = v2_live.net_trade_pnl("IN", 100, 100.0, 100.1)
        self.assertLess(net, 0)
        self.assertLess(pct, 0)

    def test_a_breakeven_exit_is_a_loss_of_exactly_the_costs(self) -> None:
        cside = v2_live.COST_SIDE["IN"]
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 100.0)
        self.assertAlmostEqual(net, -cside * 10 * 200.0)

    def test_return_pct_is_net_and_on_the_entry_basis(self) -> None:
        net, pct = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertAlmostEqual(pct, net / (10 * 100.0) * 100)

    def test_zero_size_does_not_divide_by_zero(self) -> None:
        self.assertEqual(v2_live.net_trade_pnl("IN", 0, 100.0, 110.0)[1], 0.0)

    def test_zero_entry_price_does_not_divide_by_zero(self) -> None:
        self.assertEqual(v2_live.net_trade_pnl("IN", 10, 0.0, 110.0)[1], 0.0)

    def test_us_uses_its_own_lower_cost(self) -> None:
        self.assertGreater(v2_live.net_trade_pnl("US", 10, 100.0, 110.0)[0],
                           v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)[0])

    def test_an_unknown_market_charges_nothing_rather_than_crashing(self) -> None:
        net, _ = v2_live.net_trade_pnl("XX", 10, 100.0, 110.0)
        self.assertAlmostEqual(net, 100.0)


class SingleDefinitionTest(unittest.TestCase):
    """Three exit paths must share one cost calculation, or they drift again."""

    def test_engine_exit_uses_the_helper(self) -> None:
        self.assertIn("net_trade_pnl(", inspect.getsource(v2_live.exit_monitor))

    def test_no_exit_path_still_writes_a_gross_figure(self) -> None:
        """`shares * (px - entry)` written straight into v2_trades is the bug."""
        import pathlib
        root = pathlib.Path(inspect.getfile(v2_live)).parent
        for name in ("v2_live.py", "v2_web.py"):
            src = (root / name).read_text(encoding="utf-8")
            for bad in ("shares * (px - entry), (px / entry - 1) * 100",
                        "shares * (ex - p[\"entry\"]), (ex / p[\"entry\"] - 1) * 100"):
                with self.subTest(file=name, pattern=bad):
                    self.assertNotIn(bad, src)

    def test_both_manual_sell_paths_use_the_helper(self) -> None:
        import pathlib
        src = (pathlib.Path(inspect.getfile(v2_live)).parent / "v2_web.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("net_trade_pnl(market"), 2,
                         "both manual-sell endpoints must use the shared helper")
        self.assertEqual(src.count("from .v2_live import net_trade_pnl"), 2)


if __name__ == "__main__":
    unittest.main()
