"""volume_surge sized every position at 1.60x its slot, on every trade, forever.

The lane had "volatility-normalised" sizing that normalised nothing:

    atr_pct  = 0.0175                          # a hardcoded constant
    vol_mult = clamp(VOL_TARGET_ATR / atr_pct, VOL_SIZE_MIN, VOL_SIZE_MAX)
             = clamp(0.030 / 0.0175)
             = clamp(1.714) = 1.60             # VOL_SIZE_MAX, unconditionally

A constant through a min/max is a constant. Two consequences, both visible in
the live ledger:

  * every ticket was Rs 26,667 against a Rs 16,667 slot — 27% of the book in one
    intraday position (SKYGOLD 26,639 · BLS 26,462 · MOTHERSON 26,580 ·
    SCI 26,514 · STLTECH 26,440 · IMFA 26,046);

  * six slots at that size need Rs 160,000 of a Rs 100,000 book, so the lane
    could hold at most THREE names. It ran out of cash instead of diversifying,
    which is the opposite of what `slots=6` says it does.

And the constant was stale. 0.0175 was the stop before 2026-07-29; widening the
stop to 2.5% should have shrunk the size to hold rupee risk flat, exactly as
poll_market's BASE_ATR_STOP/atr_stop scaling does for the daily lanes. Nobody
touched it, so widening the stop raised risk per trade by 43% instead.
"""
from __future__ import annotations

import inspect
import unittest

from app import v2_live


class TheOldFormulaWasAConstantTest(unittest.TestCase):
    def test_it_pinned_to_the_cap_regardless_of_input(self) -> None:
        """Reproduce the defect so the arithmetic is on the record."""
        atr_pct = 0.0175
        vol_mult = max(v2_live.VOL_SIZE_MIN,
                       min(v2_live.VOL_SIZE_MAX,
                           v2_live.VOL_TARGET_ATR / max(atr_pct, 0.005)))
        self.assertEqual(vol_mult, v2_live.VOL_SIZE_MAX)
        self.assertAlmostEqual(v2_live.VOL_TARGET_ATR / atr_pct, 1.714, places=3)

    def test_the_hardcoded_atr_is_gone(self) -> None:
        src = inspect.getsource(v2_live.volume_surge_pass)
        self.assertNotIn("atr_pct = 0.0175", src)
        self.assertNotIn("alloc * vol_mult", src)


class RiskBasedSizingTest(unittest.TestCase):
    BUDGET, MAX_POS = 100_000.0, 6

    def _notional(self, sl=None, budget=None):
        budget = budget or self.BUDGET
        sl = sl if sl is not None else v2_live.VOLSURGE["sl"]
        alloc = budget / self.MAX_POS
        risk_notional = (budget * v2_live.VOLSURGE_RISK_PCT) / max(sl, 0.001)
        return min(alloc, risk_notional)

    def test_a_position_is_at_most_one_slot(self) -> None:
        self.assertLessEqual(self._notional(), self.BUDGET / self.MAX_POS + 1e-9)

    def test_the_lane_can_now_fill_all_six_slots(self) -> None:
        """The whole point of slots=6."""
        self.assertLessEqual(self._notional() * 6, self.BUDGET + 1e-6)

    def test_it_is_smaller_than_what_shipped(self) -> None:
        old = (self.BUDGET / self.MAX_POS) * v2_live.VOL_SIZE_MAX
        self.assertLess(self._notional(), old)
        self.assertAlmostEqual(old, 26_666.67, places=1)

    def test_widening_the_stop_shrinks_the_position(self) -> None:
        """The property the stale constant destroyed: risk per trade must not
        rise just because an exit rule was loosened."""
        tight, wide = self._notional(sl=0.02), self._notional(sl=0.05)
        self.assertLess(wide, tight)

    def test_rupee_risk_is_capped_whatever_the_stop(self) -> None:
        cap = self.BUDGET * v2_live.VOLSURGE_RISK_PCT
        for sl in (0.02, 0.025, 0.035, 0.05, 0.08):
            with self.subTest(sl=sl):
                self.assertLessEqual(self._notional(sl=sl) * sl, cap + 1e-6)

    def test_risk_at_the_live_config_is_sane(self) -> None:
        risk = self._notional() * v2_live.VOLSURGE["sl"]
        self.assertLess(risk / self.BUDGET, 0.005, "one trade must not risk >0.5% of book")

    def test_it_scales_with_the_book(self) -> None:
        self.assertAlmostEqual(self._notional(budget=50_000.0),
                               self._notional() / 2, places=6)


class SizingIsWiredInTest(unittest.TestCase):
    def test_the_pass_uses_the_configured_stop(self) -> None:
        src = inspect.getsource(v2_live.volume_surge_pass)
        self.assertIn('VOLSURGE["sl"]', src,
                      "size must follow the stop that is actually configured")
        self.assertIn("VOLSURGE_RISK_PCT", src)

    def test_cash_still_bounds_it(self) -> None:
        src = inspect.getsource(v2_live.volume_surge_pass)
        self.assertIn("cash / (1 + cside)", src, "must never overdraw the book")


class VolumeSurgeIsParkedTest(unittest.TestCase):
    """Parked 2026-08-10 on the standard that quarantined gap_momentum.

    43 trades, -Rs 7,184, the only lane losing money while index_options is
    +Rs 28,936 over 39 real trades. Needs a 38% win rate, does 28%.

    Parked, NOT deleted: signals still compute for the radar, the lane just
    stops buying. The sizing fix above stays in place so that if it is ever
    unparked it trades at the right size from the first entry.
    """

    def test_it_is_quarantined(self) -> None:
        self.assertIn("volume_surge", v2_live.DISABLED_LANES)

    def test_the_pass_honours_the_quarantine(self) -> None:
        """The flag is worthless if the lane's own entry path ignores it —
        volume_surge enters via volume_surge_pass, not the daily candidate
        loop where DISABLED_LANES is checked."""
        src = inspect.getsource(v2_live.volume_surge_pass)
        self.assertIn('if "volume_surge" in DISABLED_LANES:', src)
        head = src[:src.index('if "volume_surge" in DISABLED_LANES:') + 200]
        self.assertIn("return", head, "the check must bail out, not just log")

    def test_every_legacy_lane_is_now_retired(self) -> None:
        """Superseded 2026-08-15: this once asserted the other lanes were still
        live. They are all retired now and the sleeve system replaces them."""
        for lane in ("volume_surge", "intraday_news", "mom_breakout",
                     "swing_meanrev", "gap_momentum", "btst"):
            with self.subTest(lane=lane):
                self.assertIn(lane, v2_live.DISABLED_LANES)

    def test_the_sizing_fix_survives_the_parking(self) -> None:
        """So an unpark does not silently restore 1.60x tickets."""
        src = inspect.getsource(v2_live.volume_surge_pass)
        self.assertIn("VOLSURGE_RISK_PCT", src)
        self.assertNotIn("atr_pct = 0.0175", src)

    def test_an_open_position_can_still_be_exited(self) -> None:
        """Parking blocks BUYING. exit_monitor must keep managing what is
        already held — there is an open OIL position at the time of parking,
        and a quarantine that stranded it would be far worse than the lane."""
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertNotIn("DISABLED_LANES", src,
                         "exits must never consult the quarantine list")


if __name__ == "__main__":
    unittest.main()
