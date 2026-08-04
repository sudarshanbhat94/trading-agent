"""Expiry-day churn, and targets that could never be reached.

Both found in the live option book on 2026-08-04:

  * NINETEEN round trips of NIFTY2680424450PE in one session, net -Rs 2,610.
    The expiry exit fired for the whole expiry day on the 8-second cadence
    while index_options_pass kept opening new positions until
    EXPIRY_LAST_ENTRY. Buy, close seconds later on "expiry", re-buy the same
    strike, repeat. Nineteen of the book's twenty-six option trades were this
    one contract going nowhere.

  * 21 NIFTY option trades, average -0.1%, net -Rs 865, and NOT ONE reached its
    target. Best NIFTY trade ever: +24.1% against a 40-45% target. The 4
    BANKNIFTY trades cleared theirs (+61.7%, +43.7%).
"""
from __future__ import annotations

import unittest
from datetime import date

from app import v2_live


TODAY = date(2026, 8, 4)


class ExpiryDayIsNotAllDayTest(unittest.TestCase):
    def test_a_contract_expiring_today_is_held_before_squareoff(self) -> None:
        """THE churn fix. Before this it returned True from the opening bell,
        so a 0-DTE position was closed on the tick after it opened."""
        self.assertFalse(v2_live._expired_or_expiring("2026-08-04", TODAY, "09:30"))
        self.assertFalse(v2_live._expired_or_expiring("2026-08-04", TODAY, "14:00"))

    def test_it_flattens_at_the_squareoff(self) -> None:
        self.assertTrue(v2_live._expired_or_expiring(
            "2026-08-04", TODAY, v2_live.INDEX_OPT_SQUAREOFF))
        self.assertTrue(v2_live._expired_or_expiring("2026-08-04", TODAY, "15:20"))

    def test_an_already_expired_contract_always_closes(self) -> None:
        """Whatever the clock says — it is gone from the feed and would
        otherwise mark at a stale price forever."""
        for when in ("09:16", "12:00", "15:29"):
            with self.subTest(time=when):
                self.assertTrue(v2_live._expired_or_expiring("2026-08-03", TODAY, when))

    def test_a_future_contract_is_never_closed_for_expiry(self) -> None:
        for when in ("09:16", "15:29"):
            with self.subTest(time=when):
                self.assertFalse(v2_live._expired_or_expiring("2026-08-25", TODAY, when))

    def test_no_time_given_still_flattens(self) -> None:
        """Fail SAFE: an unknown clock must not silently hold an expiring
        contract past the close."""
        self.assertTrue(v2_live._expired_or_expiring("2026-08-04", TODAY))

    def test_junk_expiry_does_not_crash(self) -> None:
        for bad in (None, "", "not-a-date"):
            with self.subTest(expiry=bad):
                self.assertFalse(v2_live._expired_or_expiring(bad, TODAY, "10:00"))

    def test_the_frozen_quote_guard_stays_all_day(self) -> None:
        """It passes "23:59" deliberately: a frozen quote on a contract expiring
        today means the contract has already left the feed, so that guard must
        keep its old all-day meaning or the position is stranded."""
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn('today, "23:59"', src)


class IndexScaledTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = v2_live.INDEX_OPTIONS

    def _t(self, symbol, expiry):
        return v2_live._target_pct(self.cfg, expiry, TODAY, symbol)

    def test_nifty_target_is_below_the_best_it_has_ever_managed(self) -> None:
        """+24.1% is the best NIFTY option trade in the book. A target above
        that is not a target, it is a guarantee of riding to expiry."""
        self.assertLess(self._t("NIFTY2680424450PE", "2026-08-04"), 0.241)

    def test_banknifty_keeps_the_full_target(self) -> None:
        """It has twice cleared 43%+, so nothing here is broken for it."""
        self.assertAlmostEqual(self._t("BANKNIFTY26AUG57400PE", "2026-08-25"), 0.20)

    def test_nifty_is_scaled_below_banknifty_at_the_same_dte(self) -> None:
        self.assertLess(self._t("NIFTY26AUG24000CE", "2026-08-25"),
                        self._t("BANKNIFTY26AUG57400CE", "2026-08-25"))

    def test_midcpnifty_is_not_read_as_nifty(self) -> None:
        """Longest-prefix match: MIDCPNIFTY and BANKNIFTY both END in NIFTY."""
        self.assertEqual(v2_live._index_of("MIDCPNIFTY26AUG14825CE"), "MIDCPNIFTY")
        self.assertEqual(v2_live._index_of("BANKNIFTY26AUG57400PE"), "BANKNIFTY")
        self.assertEqual(v2_live._index_of("NIFTY2680424450PE"), "NIFTY")

    def test_the_dte_ladder_still_applies(self) -> None:
        """Scaling multiplies the ladder, it does not replace it."""
        near = self._t("BANKNIFTY26AUG57400CE", "2026-08-05")
        far = self._t("BANKNIFTY26AUG57400CE", "2026-08-25")
        self.assertGreater(near, far)

    def test_an_unknown_symbol_is_unscaled(self) -> None:
        self.assertAlmostEqual(self._t("SOMETHINGELSE", "2026-08-25"), 0.20)
        self.assertAlmostEqual(self._t(None, "2026-08-25"), 0.20)


if __name__ == "__main__":
    unittest.main()
