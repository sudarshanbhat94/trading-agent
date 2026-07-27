"""Per-lane performance breakdown for /v2/api/stats.

The stats view previously labelled every non-gap lane "swing", so btst,
volume_surge, intraday_news and mom_breakout were indistinguishable — and the
BTST trial could not be evaluated from the UI at all. Worse, the payload has no
`strategy` field, so the template's `s.strategy.indexOf('gap')` threw a
TypeError that aborted the whole render.
"""

from __future__ import annotations

import unittest

from app.v2_web import _hold_days, strategy_stats


def _row(strategy, ret, pnl=0.0, entry="2026-07-20", exit_="2026-07-24"):
    return (strategy, ret, pnl, entry, exit_)


class LaneIdentityTest(unittest.TestCase):
    def test_each_lane_is_reported_separately(self) -> None:
        rows = [
            _row("swing_meanrev", 1.0), _row("btst", 0.5),
            _row("volume_surge", 2.0), _row("mom_breakout", -1.0),
        ]
        lanes = {s["strategy"] for s in strategy_stats(rows)}
        self.assertEqual(lanes, {"swing_meanrev", "btst", "volume_surge", "mom_breakout"})

    def test_lanes_carry_readable_labels(self) -> None:
        stats = {s["strategy"]: s["label"] for s in strategy_stats(
            [_row("btst", 1.0), _row("swing_meanrev", 1.0)]
        )}
        self.assertEqual(stats["btst"], "BTST (overnight)")
        self.assertEqual(stats["swing_meanrev"], "swing mean-reversion")

    def test_unknown_lane_gets_a_readable_fallback(self) -> None:
        (stat,) = strategy_stats([_row("some_new_lane", 1.0)])
        self.assertEqual(stat["label"], "some new lane")

    def test_overnight_lanes_are_flagged(self) -> None:
        flags = {s["strategy"]: s["overnight"] for s in strategy_stats([
            _row("btst", 1.0), _row("swing_meanrev", 1.0),
            _row("volume_surge", 1.0), _row("intraday_news", 1.0),
        ])}
        self.assertTrue(flags["btst"])
        self.assertTrue(flags["swing_meanrev"])
        self.assertFalse(flags["volume_surge"])
        self.assertFalse(flags["intraday_news"])

    def test_sorted_by_evidence(self) -> None:
        rows = [_row("btst", 1.0)] + [_row("swing_meanrev", 1.0)] * 5
        self.assertEqual([s["strategy"] for s in strategy_stats(rows)],
                         ["swing_meanrev", "btst"])


class MetricsTest(unittest.TestCase):
    def test_win_rate_and_averages(self) -> None:
        rows = [_row("btst", 2.0), _row("btst", -1.0), _row("btst", 4.0), _row("btst", -1.0)]
        (stat,) = strategy_stats(rows)
        self.assertEqual(stat["trades"], 4)
        self.assertEqual(stat["win"], 50.0)
        self.assertEqual(stat["avg"], 1.0)          # (2-1+4-1)/4
        self.assertEqual(stat["avg_win"], 3.0)      # (2+4)/2
        self.assertEqual(stat["avg_loss"], -1.0)
        self.assertEqual(stat["best"], 4.0)
        self.assertEqual(stat["worst"], -1.0)

    def test_profit_factor(self) -> None:
        (stat,) = strategy_stats([_row("btst", 6.0), _row("btst", -2.0)])
        self.assertEqual(stat["pf"], 3.0)

    def test_break_even_trade_does_not_divide_by_zero(self) -> None:
        """The exact case that once 500'd the overview endpoint: a 0.0 return
        makes the loss bucket non-empty while summing to zero."""
        (stat,) = strategy_stats([_row("btst", 5.0), _row("btst", 0.0)])
        self.assertEqual(stat["pf"], 9.9)
        self.assertEqual(stat["win"], 50.0)

    def test_all_losses_gives_zero_profit_factor(self) -> None:
        (stat,) = strategy_stats([_row("btst", -1.0), _row("btst", -2.0)])
        self.assertEqual(stat["pf"], 0.0)
        self.assertEqual(stat["win"], 0.0)

    def test_pnl_is_summed(self) -> None:
        (stat,) = strategy_stats([_row("btst", 1.0, pnl=250.0), _row("btst", -1.0, pnl=-100.0)])
        self.assertEqual(stat["pnl"], 150.0)


class HoldingPeriodTest(unittest.TestCase):
    def test_hold_days(self) -> None:
        self.assertEqual(_hold_days("2026-07-20", "2026-07-24"), 4)
        self.assertEqual(_hold_days("2026-07-24", "2026-07-24"), 0)

    def test_hold_days_tolerates_timestamps(self) -> None:
        self.assertEqual(_hold_days("2026-07-20T09:15:00", "2026-07-24T15:30:00"), 4)

    def test_hold_days_on_bad_input(self) -> None:
        for entry, exit_ in (("", "2026-07-24"), ("nonsense", "2026-07-24"), (None, None)):
            with self.subTest(entry=entry):
                self.assertIsNone(_hold_days(entry, exit_))

    def test_btst_shows_a_one_day_hold(self) -> None:
        """BTST buys at the close and sells at the next open."""
        (stat,) = strategy_stats([
            _row("btst", 0.5, entry="2026-07-27", exit_="2026-07-28"),
            _row("btst", 0.3, entry="2026-07-28", exit_="2026-07-29"),
        ])
        self.assertEqual(stat["avg_hold_days"], 1.0)

    def test_intraday_shows_a_zero_day_hold(self) -> None:
        (stat,) = strategy_stats([_row("volume_surge", 1.0, entry="2026-07-27", exit_="2026-07-27")])
        self.assertEqual(stat["avg_hold_days"], 0.0)

    def test_missing_dates_leave_hold_none_without_dropping_the_lane(self) -> None:
        (stat,) = strategy_stats([_row("btst", 1.0, entry=None, exit_=None)])
        self.assertIsNone(stat["avg_hold_days"])
        self.assertEqual(stat["trades"], 1)


class RobustnessTest(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(strategy_stats([]), [])

    def test_unparseable_return_is_skipped_not_fatal(self) -> None:
        stats = strategy_stats([_row("btst", None), _row("btst", 1.0)])
        self.assertEqual(stats[0]["trades"], 1)

    def test_lane_with_only_bad_returns_is_dropped(self) -> None:
        self.assertEqual(strategy_stats([_row("btst", "abc")]), [])

    def test_null_strategy_becomes_unknown(self) -> None:
        (stat,) = strategy_stats([_row(None, 1.0)])
        self.assertEqual(stat["strategy"], "unknown")

    def test_bad_pnl_does_not_break_the_lane(self) -> None:
        (stat,) = strategy_stats([_row("btst", 1.0, pnl="oops")])
        self.assertEqual(stat["trades"], 1)
        self.assertEqual(stat["pnl"], 0.0)

    def test_payload_is_json_serialisable(self) -> None:
        import json
        json.dumps(strategy_stats([_row("btst", 1.0, pnl=5.0)]))


if __name__ == "__main__":
    unittest.main()
