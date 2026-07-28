"""Point-in-time universe screening.

The universe used to be picked once, from the whole sample, which answers
"which stocks turned out to be worth trading?" rather than "which looked
tradeable at the time?". That single bug inflated an intraday-momentum result
from +1.6% to +18.7% and turned a -0.165%/trade catalyst strategy into an
apparent +0.573%.

Every case below is built from synthetic bars where the correct answer is known
by construction, so these test the screen rather than restate its output. The
two that matter most are `test_a_late_bloomer_is_absent_before_it_was_liquid`
(look-ahead) and `test_a_delisted_name_stays_while_it_traded` (survivorship) —
they are the two halves of the bias.
"""

from __future__ import annotations

import sys
import types
import unittest

import pandas as pd

# backtest_v2 lives in scripts/ and pulls in the whole engine at import time;
# load just the two functions under test.
import importlib.util
import pathlib

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "backtest_v2.py"
_spec = importlib.util.spec_from_file_location("backtest_v2", _path)
backtest_v2 = importlib.util.module_from_spec(_spec)
sys.modules["backtest_v2"] = backtest_v2
_spec.loader.exec_module(backtest_v2)

point_in_time_universe = backtest_v2.point_in_time_universe
universe_lookup = backtest_v2.universe_lookup


def bars(spec, start="2024-01-01", days=400):
    """spec: {symbol: (turnover, first_day_index, last_day_index)} -> long df."""
    dates = pd.bdate_range(start, periods=days)
    rows = []
    for sym, (turn, lo, hi) in spec.items():
        for i, d in enumerate(dates):
            if not (lo <= i <= hi):
                continue
            rows.append({"symbol": sym, "date": d, "close": 100.0,
                         "volume": turn / 100.0})
    return pd.DataFrame(rows)


class PointInTimeUniverseTest(unittest.TestCase):
    def test_a_late_bloomer_is_absent_before_it_was_liquid(self) -> None:
        """LATE only starts trading on day 200. It must not appear in any screen
        before then, no matter how liquid it later becomes. Under the old
        full-sample median it would have ranked top from day one."""
        df = bars({
            "OLD":  (1e7, 0, 399),
            "LATE": (9e9, 200, 399),      # hugely liquid, but only later
        })
        uni = point_in_time_universe(df, topn=5, min_bars=20)
        early = [rd for rd in sorted(uni) if rd < pd.Timestamp("2024-08-01")]
        self.assertTrue(early)
        for rd in early:
            with self.subTest(rebalance=str(rd.date())):
                self.assertNotIn("LATE", uni[rd])

    def test_the_late_bloomer_does_appear_once_it_qualifies(self) -> None:
        """The screen must not be merely restrictive — once the name genuinely
        has the history and liquidity, it has to show up."""
        df = bars({"OLD": (1e7, 0, 399), "LATE": (9e9, 200, 399)})
        uni = point_in_time_universe(df, topn=5, min_bars=20)
        late = [rd for rd in sorted(uni) if rd > pd.Timestamp("2024-12-01")]
        self.assertTrue(any("LATE" in uni[rd] for rd in late),
                        "late bloomer never entered the universe")

    def test_a_delisted_name_stays_while_it_traded(self) -> None:
        """Survivorship. DEAD trades for the first half then stops. The old
        `cnt >= min_bars` over the whole sample erased such names entirely, so
        every stock that died was invisible. It must be present in the early
        screens."""
        df = bars({"ALIVE": (1e7, 0, 399), "DEAD": (5e7, 0, 199)})
        uni = point_in_time_universe(df, topn=5, min_bars=20)
        early = [rd for rd in sorted(uni)
                 if pd.Timestamp("2024-04-01") <= rd <= pd.Timestamp("2024-07-01")]
        self.assertTrue(early)
        for rd in early:
            with self.subTest(rebalance=str(rd.date())):
                self.assertIn("DEAD", uni[rd])

    def test_only_data_strictly_before_the_rebalance_is_used(self) -> None:
        """A name whose entire history begins ON the rebalance date cannot be in
        that date's screen — that would be same-day information."""
        dates = pd.bdate_range("2024-01-01", periods=200)
        boundary = pd.Timestamp("2024-06-01")
        df = bars({"BASE": (1e7, 0, 199)})
        newrows = [{"symbol": "SAMEDAY", "date": d, "close": 100.0, "volume": 1e9}
                   for d in dates if d >= boundary]
        df = pd.concat([df, pd.DataFrame(newrows)], ignore_index=True)
        uni = point_in_time_universe(df, topn=5, min_bars=1)
        self.assertNotIn("SAMEDAY", uni.get(boundary, frozenset()))

    def test_topn_is_respected(self) -> None:
        df = bars({f"S{i}": (1e6 * (i + 1), 0, 399) for i in range(20)})
        uni = point_in_time_universe(df, topn=5, min_bars=20)
        for rd, names in uni.items():
            with self.subTest(rebalance=str(rd.date())):
                self.assertLessEqual(len(names), 5)

    def test_ranking_prefers_the_more_liquid_name(self) -> None:
        df = bars({"BIG": (9e8, 0, 399), "SMALL": (1e5, 0, 399)})
        uni = point_in_time_universe(df, topn=1, min_bars=20)
        last = uni[max(uni)]
        self.assertEqual(last, frozenset({"BIG"}))

    def test_min_bars_excludes_names_without_enough_history(self) -> None:
        df = bars({"OLD": (1e7, 0, 399), "YOUNG": (9e9, 380, 399)})
        uni = point_in_time_universe(df, topn=5, min_bars=100)
        for rd, names in uni.items():
            with self.subTest(rebalance=str(rd.date())):
                self.assertNotIn("YOUNG", names)


class UniverseLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uni = {
            pd.Timestamp("2024-01-01"): frozenset({"A"}),
            pd.Timestamp("2024-02-01"): frozenset({"A", "B"}),
            pd.Timestamp("2024-03-01"): frozenset({"C"}),
        }
        self.at = universe_lookup(self.uni)

    def test_uses_the_most_recent_screen_at_or_before_the_date(self) -> None:
        self.assertEqual(self.at(pd.Timestamp("2024-02-15")), frozenset({"A", "B"}))

    def test_exact_rebalance_date_uses_that_screen(self) -> None:
        self.assertEqual(self.at(pd.Timestamp("2024-03-01")), frozenset({"C"}))

    def test_before_the_first_screen_nothing_is_investable(self) -> None:
        """No screen has run yet, so no name may be bought — not 'all of them'."""
        self.assertEqual(self.at(pd.Timestamp("2023-12-01")), frozenset())

    def test_after_the_last_screen_the_last_one_persists(self) -> None:
        self.assertEqual(self.at(pd.Timestamp("2025-06-01")), frozenset({"C"}))


class WiringTest(unittest.TestCase):
    """The previous attempt at this fix added a flag that never reached the
    selection loop, so biased and clean runs printed identical numbers. These
    pin the wiring itself."""

    def test_load_market_returns_three_values(self) -> None:
        import inspect
        src = inspect.getsource(backtest_v2.load_market)
        self.assertIn("return {s: g.set_index(\"date\") for s, g in df.groupby(\"symbol\")}, "
                      "market_df, eligible_at", src)

    def test_the_selection_loop_consults_eligibility(self) -> None:
        import inspect
        src = inspect.getsource(backtest_v2.portfolio)
        self.assertIn("eligible_at(d)", src)
        self.assertIn("sym not in investable", src)

    def test_hindsight_path_returns_no_eligibility_function(self) -> None:
        """asof=False must reproduce the ORIGINAL behaviour exactly, so the two
        can be compared; if it also filtered, the comparison would be worthless."""
        import inspect
        src = inspect.getsource(backtest_v2.load_market)
        self.assertIn("eligible_at = None", src)


if __name__ == "__main__":
    unittest.main()
