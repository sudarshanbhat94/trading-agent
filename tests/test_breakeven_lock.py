"""The breakeven lock must lock in breakeven, not a loss.

"Green never goes red" was implemented as entry * 1.001 — a GROSS +0.1% — while
a round trip costs ~0.4%. Every time the lock fired it booked a ~0.3% loss and
called it a stop.

Measured on the live book before the fix: 9 of volume_surge's 25 closed trades
exited within 0.15% of entry, all 9 losers, together -Rs 693 of pure
transaction cost. Several exited ABOVE their entry price and were still
recorded as losses.
"""
from __future__ import annotations

import unittest

from app import v2_live


class BreakevenPriceTest(unittest.TestCase):
    def test_it_nets_exactly_zero(self) -> None:
        """Now takes SHARES, because brokerage and the DP fee are flat rupees on
        the position, not a percentage. Pricing them off the per-share price put
        the breakeven stop wildly too high on any real position."""
        be = v2_live.breakeven_price("IN", 1000.0, 10)
        net, pct = v2_live.net_trade_pnl("IN", 10, 1000.0, be)
        self.assertAlmostEqual(net, 0.0, places=4)
        self.assertAlmostEqual(pct, 0.0, places=4)

    def test_it_nets_zero_at_every_size_and_product(self) -> None:
        for shares, px, strat in ((10, 100.0, None), (1, 3000.0, None),
                                  (100, 50.0, None), (10, 900.0, "volume_surge")):
            with self.subTest(shares=shares, price=px, strategy=strat):
                be = v2_live.breakeven_price("IN", px, shares, strat)
                net, _ = v2_live.net_trade_pnl("IN", shares, px, be, strat)
                self.assertAlmostEqual(net, 0.0, places=3)

    def test_an_intraday_breakeven_is_nearer_than_a_delivery_one(self) -> None:
        """It costs a quarter as much, so it needs a quarter of the move."""
        self.assertLess(v2_live.breakeven_price("IN", 900.0, 10, "volume_surge"),
                        v2_live.breakeven_price("IN", 900.0, 10, "swing_meanrev"))

    def test_it_is_above_the_entry_price(self) -> None:
        self.assertGreater(v2_live.breakeven_price("IN", 1000.0, 10), 1000.0)

    def test_the_old_plus_one_tenth_percent_was_a_loss(self) -> None:
        """The exact defect: entry * 1.001 loses money on every fire."""
        net, _ = v2_live.net_trade_pnl("IN", 10, 1000.0, 1000.0 * 1.001)
        self.assertLess(net, 0)

    def test_it_scales_with_the_market_cost(self) -> None:
        """US keeps the flat model — COST_SIDE was fitted for it and there is no
        Indian fee schedule to apply."""
        self.assertLess(v2_live.breakeven_price("US", 1000.0, 10) - 1000.0,
                        v2_live.breakeven_price("IN", 1000.0, 10) - 1000.0)

    def test_an_unknown_market_falls_back_to_entry(self) -> None:
        self.assertEqual(v2_live.breakeven_price("XX", 1000.0, 10), 1000.0)

    def test_the_real_trades_that_exposed_this(self) -> None:
        """Live rows. Each exited at or above entry and was booked a loser."""
        for sym, entry, exit_px in (("TATACAP", 370.95, 371.15),
                                    ("KEI", 5299.70, 5304.20),
                                    ("ITC", 287.00, 287.25),
                                    ("SYRMA", 1410.60, 1411.80)):
            with self.subTest(symbol=sym):
                old, _ = v2_live.net_trade_pnl("IN", 10, entry, exit_px)
                self.assertLess(old, 0, "these really did lose money")
                # the fixed lock would not have exited there at all
                self.assertGreater(v2_live.breakeven_price("IN", entry, 10), exit_px)


class LockUsesItTest(unittest.TestCase):
    def test_both_locks_use_the_net_breakeven(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.evaluate_exit)
        # now carries shares and the lane, because the charge is flat rupees
        # on the POSITION and an intraday round trip costs a quarter of a
        # delivery one
        self.assertIn('be = breakeven_price(market, p["entry"], '
                      'p.get("shares") or 1.0, p["strategy"])', src)
        self.assertNotIn('p["entry"] * 1.001', src)
        self.assertEqual(src.count("eff = max(eff, be)"), 2,
                         "the ATR lock and the intraday lock must both use it")


if __name__ == "__main__":
    unittest.main()
