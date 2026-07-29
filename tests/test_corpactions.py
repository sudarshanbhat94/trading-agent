"""Unadjusted splits and bonuses in the daily history.

The feed stores raw traded prices, so a 1:5 split appears as a -80% return that
never happened. Only 32 such bars exist in the IN history, but they are exactly
what a mean-reversion scorer hunts for: a stock that looks 83% down is
maximally oversold on every dip metric at once and outranks every genuine
setup.

The risk in "fixing" this is worse than the bug. A real crash and a split look
identical in price alone, so a careless detector rewrites genuine losses out of
the record. Every test below that asserts something is NOT detected is
protecting against that.
"""

from __future__ import annotations

import unittest

import pandas as pd

from app import corpactions


def series(closes, volumes=None):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    volumes = volumes if volumes is not None else [1000.0] * len(closes)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": [float(c) for c in closes],
                         "volume": [float(v) for v in volumes]}, index=idx)


class DetectTest(unittest.TestCase):
    def test_a_one_for_five_split_is_detected(self) -> None:
        """Price /5, share count x5, so volume x5 and turnover is unchanged."""
        frame = series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000])
        self.assertEqual(len(corpactions.detect(frame)), 1)

    def test_a_one_for_two_bonus_is_detected(self) -> None:
        frame = series([200, 202, 100, 99], volumes=[1000, 1000, 2000, 2000])
        self.assertEqual(len(corpactions.detect(frame)), 1)

    def test_a_reverse_split_is_detected(self) -> None:
        frame = series([100, 101, 500, 495], volumes=[5000, 5000, 1000, 1000])
        self.assertEqual(len(corpactions.detect(frame)), 1)

    def test_a_real_crash_is_left_alone(self) -> None:
        """-32% on bad news is not a clean ratio. Rewriting it would erase a
        genuine loss from the record."""
        frame = series([100, 100, 68, 66])
        self.assertEqual(corpactions.detect(frame), [])

    def test_a_crash_that_lands_near_a_ratio_is_saved_by_turnover(self) -> None:
        """-50% AND turnover collapsed -> a real crash, not a 1:2 split. A split
        leaves value traded roughly intact."""
        frame = series([100, 100, 50, 49], volumes=[10_000, 10_000, 200, 200])
        self.assertEqual(corpactions.detect(frame), [])

    def test_an_extreme_move_with_held_turnover_qualifies_without_a_clean_ratio(self) -> None:
        """KOTYARK -91%, ZFCVINDIA -83%: real actions whose ratios (1:6 and the
        like) never land within tolerance. No ordinary stock loses that much
        price while value traded holds up."""
        frame = series([1000, 1010, 89, 90], volumes=[1000, 1000, 11_000, 11_000])
        self.assertEqual(len(corpactions.detect(frame)), 1)

    def test_an_extreme_move_with_collapsed_turnover_is_still_a_crash(self) -> None:
        """The turnover test is what keeps a genuine collapse in the record."""
        frame = series([1000, 1010, 89, 90], volumes=[10_000, 10_000, 50, 50])
        self.assertEqual(corpactions.detect(frame), [])

    def test_a_mid_sized_move_still_needs_a_clean_ratio(self) -> None:
        """Between 25% and 60% a real crash is entirely plausible, so turnover
        alone must not be enough to rewrite it."""
        frame = series([100, 100, 65, 64], volumes=[1000, 1000, 1600, 1600])
        self.assertEqual(corpactions.detect(frame), [])

    def test_ordinary_volatility_is_ignored(self) -> None:
        frame = series([100, 108, 96, 103, 91])
        self.assertEqual(corpactions.detect(frame), [])

    def test_a_short_or_empty_series_is_safe(self) -> None:
        self.assertEqual(corpactions.detect(series([100])), [])
        self.assertEqual(corpactions.detect(None), [])

    def test_zero_prices_do_not_crash_it(self) -> None:
        frame = series([0, 100, 20])
        corpactions.detect(frame)          # must not raise


class CleanTest(unittest.TestCase):
    def test_history_is_made_continuous(self) -> None:
        """After adjustment the split step is gone and the earlier bars sit on
        the new price scale."""
        frame = series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000])
        out, n = corpactions.clean(frame)
        self.assertEqual(n, 1)
        rets = out["close"].pct_change().dropna().abs()
        self.assertLess(rets.max(), 0.25)
        # ratio is 100/505, so 500 rescales to 99.01 — the point is that the
        # -80% step is gone, not that the number lands on a round figure.
        self.assertAlmostEqual(out["close"].iloc[0], 500 * (100 / 505))

    def test_the_bar_after_the_action_is_untouched(self) -> None:
        """Only the PAST is rescaled — today's traded price is real and must
        never be rewritten."""
        frame = series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000])
        out, _ = corpactions.clean(frame)
        self.assertEqual(out["close"].iloc[-1], 101.0)
        self.assertEqual(out["close"].iloc[-2], 100.0)

    def test_turnover_is_preserved_across_the_adjustment(self) -> None:
        """The liquidity screens rank on price x volume, so the adjustment must
        not make a name look 5x less tradeable than it was."""
        frame = series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000])
        out, _ = corpactions.clean(frame)
        before = out["close"].iloc[0] * out["volume"].iloc[0]
        after = out["close"].iloc[-1] * out["volume"].iloc[-1]
        self.assertAlmostEqual(before / after, 1.0, places=1)

    def test_ohlc_are_all_adjusted_together(self) -> None:
        frame = series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000])
        out, _ = corpactions.clean(frame)
        row = out.iloc[0]
        expected = 500 * (100 / 505)
        self.assertAlmostEqual(row["open"], expected)
        self.assertAlmostEqual(row["high"], expected)
        self.assertAlmostEqual(row["low"], expected)

    def test_a_clean_series_is_returned_unchanged(self) -> None:
        frame = series([100, 101, 102, 103])
        out, n = corpactions.clean(frame)
        self.assertEqual(n, 0)
        self.assertTrue(out["close"].equals(frame["close"]))

    def test_clean_all_reports_what_it_touched(self) -> None:
        panel = {"SPLIT": series([500, 505, 100, 101], volumes=[1000, 1000, 5000, 5000]),
                 "FINE": series([100, 101, 102])}
        out, fixed = corpactions.clean_all(panel)
        self.assertEqual(list(fixed), ["SPLIT"])
        self.assertEqual(len(out), 2)

    def test_clean_all_survives_a_broken_frame(self) -> None:
        """One malformed symbol must not cost the whole panel."""
        panel = {"BAD": pd.DataFrame({"close": ["x"]}), "FINE": series([100, 101])}
        out, _ = corpactions.clean_all(panel)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
