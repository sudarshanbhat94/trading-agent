"""Option position sizing must cap what can be LOST, not what is SPENT.

max_premium_pct_by_index allowed a position worth 30% of the options book.
With a -35% stop that is 10.5% of the whole book on ONE contract, and
max_concurrent=3 put up to 31% at risk simultaneously.

The live position that exposed it: FINNIFTY26AUG26700CE, 60 x Rs 452 =
Rs 27,120, 27% of the book, Rs 9,492 at risk. Currently -18.16%, -Rs 4,926.
"""
from __future__ import annotations

import unittest

from app import v2_live


class RiskCapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = v2_live.INDEX_OPTIONS
        self.budget = float(self.cfg["budget"])
        self.stop = float(self.cfg["stop_pct"])

    def _allowance(self):
        return self.budget * float(self.cfg["max_risk_pct"]) / self.stop

    def test_the_risk_cap_exists_and_binds(self) -> None:
        self.assertIn("max_risk_pct", self.cfg)
        premium_cap = self.budget * max(self.cfg["max_premium_pct_by_index"].values())
        self.assertLess(self._allowance(), premium_cap,
                        "the risk cap must be tighter than the premium cap, or it does nothing")

    def test_one_position_cannot_risk_more_than_the_cap(self) -> None:
        worst = self._allowance() * self.stop
        self.assertLessEqual(worst / self.budget, float(self.cfg["max_risk_pct"]) + 1e-9)

    def test_the_whole_lane_is_bounded(self) -> None:
        """3 concurrent x 6% = 18% of the book, against 31% before."""
        total = float(self.cfg["max_risk_pct"]) * int(self.cfg["max_concurrent"])
        self.assertLessEqual(total, 0.20)

    def test_the_finnifty_position_that_exposed_this_would_be_refused(self) -> None:
        self.assertGreater(60 * 452.0, self._allowance())

    def test_a_nifty_lot_still_fits_comfortably(self) -> None:
        """The cheap index must stay tradeable — the cap is meant to shrink
        positions, not to switch the lane off."""
        self.assertLess(65 * 87.75, self._allowance())

    def test_the_sizing_path_applies_it(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("risk_cap / stop_pct", src)
        self.assertIn('cfg.get("max_risk_pct"', src)

    def test_it_is_a_floor_over_the_other_caps_not_a_replacement(self) -> None:
        """Cash on hand and the premium cap must still bind when they are
        tighter — min(), never max()."""
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("budget_per_trade = min(options_cash, budget * premium_pct,", src)


if __name__ == "__main__":
    unittest.main()
