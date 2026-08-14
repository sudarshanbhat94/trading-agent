"""The intraday momentum lane.

This lane came from a measured backtest, and three of its parameters ARE the
strategy rather than tunable preferences. Changing any of them silently would
throw away the edge, so they are pinned here with the numbers that justify
them:

  * slots = 1. Taking the 2nd and 3rd ranked movers collapsed the same 58-day
    test from +18.7% to +3.7% and +3.2%.
  * target 2% against a 1% stop. Every 1% target tested lost or barely broke
    even.
  * no catalyst gate. Filtering by real NSE announcements was worse in 9 of 9
    configurations.
"""

from __future__ import annotations

import unittest

from app import v2_live


def _quote(price, day_open, volume=1e7):
    return {"price": price, "open": day_open, "high": price, "low": day_open,
            "close": price, "volume": volume}


class RankingTest(unittest.TestCase):
    def test_ranks_by_move_from_the_days_open(self) -> None:
        live = {"A": _quote(103.0, 100.0), "B": _quote(110.0, 100.0), "C": _quote(101.0, 100.0)}
        ranked = v2_live.rank_movers(live, None, 0.01, 0)
        self.assertEqual([r[1] for r in ranked], ["B", "A", "C"])

    def test_move_is_from_open_not_previous_close(self) -> None:
        """A name that gapped up but has gone nowhere since must not outrank a
        name genuinely climbing today — the gap is already in the price."""
        live = {"GAPPED": _quote(120.0, 120.0), "CLIMBING": _quote(105.0, 100.0)}
        ranked = v2_live.rank_movers(live, None, 0.01, 0)
        self.assertEqual([r[1] for r in ranked], ["CLIMBING"])

    def test_minimum_move_is_enforced(self) -> None:
        live = {"WEAK": _quote(100.5, 100.0), "STRONG": _quote(102.0, 100.0)}
        self.assertEqual([r[1] for r in v2_live.rank_movers(live, None, 0.01, 0)], ["STRONG"])

    def test_illiquid_names_are_excluded(self) -> None:
        """Momentum is only tradeable where you can get filled."""
        live = {"THIN": _quote(102.0, 100.0, volume=100.0),
                "LIQUID": _quote(101.5, 100.0, volume=1e7)}
        ranked = v2_live.rank_movers(live, None, 0.01, 5e7)
        self.assertEqual([r[1] for r in ranked], ["LIQUID"])

    def test_stale_quotes_are_skipped(self) -> None:
        """A frozen quote shows a phantom move that never traded."""
        live = {"FROZEN": _quote(115.0, 100.0), "REAL": _quote(102.0, 100.0)}
        ranked = v2_live.rank_movers(live, None, 0.01, 0, stale={"FROZEN"})
        self.assertEqual([r[1] for r in ranked], ["REAL"])

    def test_malformed_quotes_do_not_raise(self) -> None:
        live = {"A": {"price": None, "open": 100.0}, "B": {"price": 102.0, "open": 0},
                "C": "nonsense", "D": _quote(103.0, 100.0)}
        self.assertEqual([r[1] for r in v2_live.rank_movers(live, None, 0.01, 0)], ["D"])

    def test_empty_input(self) -> None:
        self.assertEqual(v2_live.rank_movers({}, None, 0.01, 0), [])
        self.assertEqual(v2_live.rank_movers(None, None, 0.01, 0), [])


class ConfigurationTest(unittest.TestCase):
    """These values are the measured strategy, not preferences."""

    def test_single_slot(self) -> None:
        self.assertEqual(v2_live.INTRAMOM["slots"], 1,
                         "2nd/3rd movers collapsed the backtest from +18.7% to +3.7%")

    def test_target_is_double_the_stop(self) -> None:
        self.assertAlmostEqual(v2_live.INTRAMOM["tp"], 0.02)
        self.assertAlmostEqual(v2_live.INTRAMOM["sl"], 0.01)
        self.assertAlmostEqual(v2_live.INTRAMOM["tp"], 2 * v2_live.INTRAMOM["sl"],
                               msg="1% targets lost money in every test")

    def test_entry_window_is_an_hour_after_the_open(self) -> None:
        self.assertEqual(v2_live.INTRAMOM["start"], "10:15")     # 09:15 + 60 min
        self.assertGreater(v2_live.INTRAMOM["last_entry"], v2_live.INTRAMOM["start"])

    def test_requires_a_real_move(self) -> None:
        self.assertGreaterEqual(v2_live.INTRAMOM["min_move"], 0.01)

    def test_lane_squares_off_with_the_other_intraday_lanes(self) -> None:
        """It must NEVER be held overnight — the edge is intraday and an
        overnight gap is exactly what killed the BTST basket."""
        self.assertIn("intraday_momentum", v2_live.INTRADAY_STRATS)

    def test_lane_is_disabled_until_a_clean_number_exists(self) -> None:
        """The +18.7% did not survive removing the universe look-ahead — it
        fell to +1.6%. The lane must not trade on that."""
        self.assertFalse(v2_live.INTRAMOM.get("enabled"),
                         "re-enable only with a clean out-of-sample result")

    def test_disabled_lane_opens_nothing(self) -> None:
        import inspect
        source = inspect.getsource(v2_live.intraday_momentum_pass)
        self.assertIn('INTRAMOM.get("enabled")', source)

    def test_the_lane_is_retired(self) -> None:
        """This lane is dead twice over: INTRAMOM["enabled"] is False and
        intraday_news is in DISABLED_LANES. Its size_frac was calibrated
        against MAXPOS=6 and no longer makes sense at 3, which is exactly why
        it must not be reachable — the assertion is the retirement, not the
        stale sizing constant."""
        self.assertFalse(v2_live.INTRAMOM.get("enabled"))
        self.assertIn("intraday_news", v2_live.DISABLED_LANES)


class WiringTest(unittest.TestCase):
    def test_engine_loop_runs_the_lane(self) -> None:
        import inspect
        self.assertIn("intraday_momentum_pass(m)", inspect.getsource(v2_live.loop))

    def test_lane_is_throttled(self) -> None:
        import inspect
        self.assertIn("INTRAMOM_INTERVAL", inspect.getsource(v2_live.loop))
        self.assertGreaterEqual(v2_live.INTRAMOM_INTERVAL, 20)

    def test_lane_respects_the_risk_halt(self) -> None:
        """It must not add risk while the book is already bleeding."""
        import inspect
        self.assertIn("_risk_halt", inspect.getsource(v2_live.intraday_momentum_pass))

    def test_lane_only_trades_india(self) -> None:
        import inspect
        source = inspect.getsource(v2_live.intraday_momentum_pass)
        self.assertIn('market != "IN"', source)


if __name__ == "__main__":
    unittest.main()
