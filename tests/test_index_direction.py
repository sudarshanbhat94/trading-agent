"""The CE / PE / no-trade call for index options.

An option loses value every day it is held, so a flat market is a losing
position rather than a neutral one. That inverts the usual bias: the expensive
mistake is trading on a weak read, not missing one. These tests are therefore
weighted towards proving the engine SAYS NO — a majority of them assert that no
call is issued.

The readings are kept separate rather than blended into one score on purpose.
A blend lets a strong trend carry a contradicted signal over the line, and for
a leveraged instrument the useful question is "does anything disagree".
"""

from __future__ import annotations

import unittest

from app import index_direction as idx


def ramp(n, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def series(n=60, step=1.0, start=100.0):
    """A clean trending series: opens/highs/lows/closes/volumes."""
    closes = ramp(n, start, step)
    opens = [c - step * 0.5 for c in closes]
    highs = [c + abs(step) * 0.2 for c in closes]
    lows = [o - abs(step) * 0.2 for o in opens]
    volumes = [1000.0] * n
    return opens, highs, lows, closes, volumes


class TrendVoteTest(unittest.TestCase):
    def test_uptrend_votes_bullish(self) -> None:
        self.assertEqual(idx.trend_vote(ramp(60))[0], 1)

    def test_downtrend_votes_bearish(self) -> None:
        self.assertEqual(idx.trend_vote(ramp(60, 200.0, -1.0))[0], -1)

    def test_short_history_abstains(self) -> None:
        self.assertEqual(idx.trend_vote(ramp(10))[0], 0)


class LocationVoteTest(unittest.TestCase):
    def test_close_at_the_high_is_bullish(self) -> None:
        self.assertEqual(idx.location_vote(100, 110, 100, 109)[0], 1)

    def test_close_at_the_low_is_bearish(self) -> None:
        self.assertEqual(idx.location_vote(110, 110, 100, 101)[0], -1)

    def test_mid_range_abstains(self) -> None:
        self.assertEqual(idx.location_vote(100, 110, 100, 105)[0], 0)

    def test_zero_range_abstains(self) -> None:
        self.assertEqual(idx.location_vote(100, 100, 100, 100)[0], 0)


class VolumeVoteTest(unittest.TestCase):
    def test_heavy_volume_confirms_the_bar_direction(self) -> None:
        volumes = [1000.0] * 19 + [3000.0]
        self.assertEqual(idx.volume_vote(volumes, [100.0, 105.0])[0], 1)
        self.assertEqual(idx.volume_vote(volumes, [105.0, 100.0])[0], -1)

    def test_ordinary_volume_abstains(self) -> None:
        """Volume confirms direction, it never sets it — heavy buying and heavy
        selling look identical in the volume column."""
        self.assertEqual(idx.volume_vote([1000.0] * 20, [100.0, 105.0])[0], 0)


class PositioningVoteTest(unittest.TestCase):
    def test_put_heavy_is_supportive(self) -> None:
        self.assertEqual(idx.positioning_vote(1300, 1000)[0], 1)

    def test_call_heavy_is_bearish(self) -> None:
        self.assertEqual(idx.positioning_vote(600, 1000)[0], -1)

    def test_balanced_positioning_says_nothing(self) -> None:
        self.assertEqual(idx.positioning_vote(1000, 1000)[0], 0)

    def test_missing_oi_abstains(self) -> None:
        self.assertEqual(idx.positioning_vote(None, None)[0], 0)
        self.assertEqual(idx.positioning_vote(0, 0)[0], 0)


class DecideTest(unittest.TestCase):
    def test_a_clean_uptrend_with_agreement_calls_ce(self) -> None:
        o, h, l, c, v = series()
        v[-1] = 3000.0                      # participation on the last bar
        h[-1] = c[-1] + 0.1                 # close near the high
        result = idx.decide(o, h, l, c, v, put_oi=1400, call_oi=1000)
        self.assertEqual(result["call"], "CE")
        self.assertGreaterEqual(result["bullish"], idx.MIN_AGREEING)

    def test_a_clean_downtrend_calls_pe(self) -> None:
        o, h, l, c, v = series(step=-1.0, start=200.0)
        v[-1] = 3000.0
        l[-1] = c[-1] - 0.1
        result = idx.decide(o, h, l, c, v, put_oi=600, call_oi=1000)
        self.assertEqual(result["call"], "PE")

    def test_a_net_majority_still_calls_despite_one_dissenter(self) -> None:
        """Was a veto: any single contradicting reading killed the call, which
        fired on only 12% of sessions. Five independent readings rarely agree
        unanimously, so demanding it made the strong case unreachable. Risk is
        held by size and the stop, not by abstaining."""
        o, h, l, c, v = series()
        v[-1] = 3000.0
        h[-1] = c[-1] + 0.1
        result = idx.decide(o, h, l, c, v, put_oi=500, call_oi=1000)
        self.assertEqual(result["call"], "CE")
        self.assertGreater(result["bullish"], result["bearish"])

    def test_one_reading_alone_is_not_enough(self) -> None:
        """Trend up but the bar closed MID-RANGE and volume was ordinary, so
        only one reading has a view. A single reading is noise."""
        o, h, l, c, v = series()
        h[-1] = c[-1] + 5.0                 # wide bar, close in the middle
        l[-1] = c[-1] - 5.0
        result = idx.decide(o, h, l, c, v)
        self.assertIsNone(result["call"])
        self.assertLess(result["bullish"], idx.MIN_AGREEING)

    def test_an_even_split_produces_no_call(self) -> None:
        """A tie is genuinely no information, so it stays out."""
        votes = {"a": 1, "b": -1}
        self.assertEqual(sum(1 for x in votes.values() if x > 0),
                         sum(1 for x in votes.values() if x < 0))

    def test_flat_market_produces_no_call(self) -> None:
        """The expensive default: an option held through a flat market bleeds
        theta, so indecision must mean no position."""
        c = [100.0] * 60
        result = idx.decide(c[:], c[:], c[:], c[:], [1000.0] * 60)
        self.assertIsNone(result["call"])

    def test_confidence_is_zero_without_a_call(self) -> None:
        c = [100.0] * 60
        self.assertEqual(idx.decide(c[:], c[:], c[:], c[:], [1000.0] * 60)["confidence"], 0.0)

    def test_confidence_never_exceeds_one(self) -> None:
        o, h, l, c, v = series()
        v[-1] = 3000.0
        h[-1] = c[-1] + 0.1
        result = idx.decide(o, h, l, c, v, put_oi=1400, call_oi=1000)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_every_reading_is_explained(self) -> None:
        """An unexplained call cannot be reviewed after it loses."""
        o, h, l, c, v = series()
        result = idx.decide(o, h, l, c, v)
        self.assertEqual(len(result["reasons"]), 5)
        self.assertEqual(set(result["votes"]), {"trend", "pattern", "volume",
                                                "location", "positioning"})

    def test_short_history_cannot_produce_a_call(self) -> None:
        o, h, l, c, v = series(n=5)
        self.assertIsNone(idx.decide(o, h, l, c, v)["call"])

    def test_threshold_requires_more_than_one_reading(self) -> None:
        """Loosened from 3 to 2, but never to 1 — a single reading is noise."""
        self.assertEqual(idx.MIN_AGREEING, 2)


if __name__ == "__main__":
    unittest.main()
