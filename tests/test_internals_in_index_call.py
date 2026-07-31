"""Live market internals feeding the index CE/PE call.

The five original readings are all computed from DAILY bars, so an intraday
decision was being made on yesterday's candle. Every input that actually
explains an index move — which heavyweights are pulling it, whether the rest of
the market is following, where institutions are positioned, whether the cash
market is even participating — was already in the database and unused.

The concrete failure: on 2026-07-30 a NIFTY CE was bought on a tape that was
32% advancing with FIIs 10:1 short in index futures. Neither fact reached the
decision.

WHY VOTES AND NOT A VETO. A veto reads as the safer choice and is not. On
30 Jul the internals were bearish and WRONG — that contract closed +21.7%, and
the 31 Jul CE ran +33.9% intraday. A veto would have blocked both. Risk on a
long option is bounded by the premium and held by size and the stop, which are
enforced at entry.

UNVALIDATED. The internals are live-only; there is no history to backtest them
against, so this ships on reasoning and is tagged so it can be read back.
"""

from __future__ import annotations

import unittest

from app import index_direction as idx
from app import market_internals as mi


def ramp(n, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def series(n=60, step=1.0, start=100.0):
    closes = ramp(n, start, step)
    opens = [c - step * 0.5 for c in closes]
    highs = [c + abs(step) * 0.2 for c in closes]
    lows = [o - abs(step) * 0.2 for o in opens]
    return opens, highs, lows, closes, [1000.0] * n


class ExtraVotesTest(unittest.TestCase):
    def test_internals_are_counted_as_readings(self) -> None:
        o, h, l, c, v = series()
        base = idx.decide(o, h, l, c, v)
        withx = idx.decide(o, h, l, c, v, extra_votes={
            "breadth": (-1, "771up/1400down"), "fii": (-1, "0.10 long/short")})
        self.assertEqual(base["n_readings"], 5)
        self.assertEqual(withx["n_readings"], 7)
        self.assertEqual(withx["bearish"], base["bearish"] + 2)

    def test_a_bearish_tape_can_flip_a_marginal_call_to_no_trade(self) -> None:
        """The 30 Jul shape: trend and location bullish off yesterday's bar,
        while the live tape is broad-based negative. Two against two is a tie,
        and a tie is no information."""
        o, h, l, c, v = series()
        h[-1] = c[-1] + 0.1                 # closed strong -> location bullish
        base = idx.decide(o, h, l, c, v)
        self.assertEqual(base["call"], "CE")
        withx = idx.decide(o, h, l, c, v, extra_votes={
            "breadth": (-1, "32% advancing"), "fii": (-1, "10:1 short")})
        self.assertIsNone(withx["call"])

    def test_a_confirming_tape_leaves_the_call_standing(self) -> None:
        o, h, l, c, v = series()
        h[-1] = c[-1] + 0.1
        withx = idx.decide(o, h, l, c, v, extra_votes={
            "heavyweights": (1, "+1.56% turnover-weighted"),
            "breadth": (0, "51% advancing")})
        self.assertEqual(withx["call"], "CE")

    def test_internals_cannot_open_a_position_on_their_own(self) -> None:
        """A flat daily read plus one internals vote is a single reading, and a
        single reading is noise."""
        flat = [100.0] * 60
        r = idx.decide(flat[:], flat[:], flat[:], flat[:], [1000.0] * 60,
                       extra_votes={"heavyweights": (1, "+0.9%")})
        self.assertIsNone(r["call"])

    def test_every_reading_is_still_explained(self) -> None:
        o, h, l, c, v = series()
        r = idx.decide(o, h, l, c, v, extra_votes={"breadth": (-1, "32% advancing")})
        self.assertEqual(len(r["reasons"]), 6)
        self.assertTrue(any("32% advancing" in x for x in r["reasons"]))

    def test_malformed_extras_are_ignored_not_fatal(self) -> None:
        o, h, l, c, v = series()
        r = idx.decide(o, h, l, c, v, extra_votes={"bad": None, "worse": (1,),
                                                   "good": (-1, "reason")})
        self.assertEqual(r["n_readings"], 6)
        self.assertIn("good", r["votes"])

    def test_absent_internals_reproduce_the_original_call_exactly(self) -> None:
        """The engine must degrade to the previous behaviour when the internals
        scan fails, not decline to trade."""
        o, h, l, c, v = series()
        h[-1] = c[-1] + 0.1
        for empty in (None, {}):
            r = idx.decide(o, h, l, c, v, extra_votes=empty)
            self.assertEqual(r["n_readings"], 5)
            self.assertEqual(r["call"], "CE")


class IndexSpecificReadingTest(unittest.TestCase):
    """`contribution` ranks NIFTY 50 constituents, so it explains a NIFTY move
    and nothing else. Handing BANKNIFTY a reading built from Nifty 50
    heavyweights would be a confident answer to the wrong question."""

    DATA = dict(
        contribution=dict(weighted_move=1.56, n=15,
                          leaders=[dict(symbol="BAJFINANCE", chg=4.7)]),
        breadth=dict(advances=1231, declines=967, flat=196, ratio=1.27, pct_up=51.4),
        vix=dict(value=12.16, change=1.2),
        fii=dict(long=1.0, short=9.0, ratio=0.11),
        cash_volume=dict(turnover=1.0, ratio=0.78))

    def test_nifty_gets_the_heavyweight_reading(self) -> None:
        self.assertIn("heavyweights", mi.votes_for("NIFTY", self.DATA))

    def test_other_indices_do_not(self) -> None:
        for symbol in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            votes = mi.votes_for(symbol, self.DATA)
            self.assertNotIn("heavyweights", votes, symbol)
            # the market-wide readings still apply
            self.assertIn("breadth", votes)
            self.assertIn("fii", votes)

    def test_a_failed_read_yields_no_votes_rather_than_raising(self) -> None:
        self.assertEqual(mi.votes_for("NIFTY", {}), {})

    def test_the_fii_reading_is_bearish_when_institutions_are_short(self) -> None:
        """0.11 long/short is roughly 9:1 short — the reading that was missing
        from the 30 Jul CE."""
        vote, why = mi.votes_for("NIFTY", self.DATA)["fii"]
        self.assertEqual(vote, -1)
        self.assertIn("0.11", why)


class ReachableThresholdTest(unittest.TestCase):
    """THE INVARIANT: a call that just clears MIN_AGREEING must always clear the
    confidence gate derived from it. Break this and the lane refuses every call
    it generates, which looks like "index options are broken" rather than like a
    threshold that cannot be reached.

    It has now broken twice. First a stored 0.60 survived MIN_AGREEING dropping
    from 3 to 2. Then, once internals made the denominator 9, `confidence` came
    back rounded to 0.22 while the exact ceiling was 0.2222 — so a 2-of-9 call
    was refused by a number computed from its own vote. Caught live on
    2026-07-31 with FINNIFTY and MIDCPNIFTY.
    """

    def test_a_minimum_call_clears_its_own_gate_at_every_reading_count(self) -> None:
        from app.v2_web import _effective_min_conf
        for n in range(2, 15):
            confidence = round(idx.MIN_AGREEING / n, 2)   # exactly what decide() reports
            with self.subTest(n_readings=n):
                self.assertGreaterEqual(confidence, idx.max_confidence(n))
                self.assertGreaterEqual(
                    confidence, _effective_min_conf({"min_confidence": 0.6}, n),
                    "a stale high setting must not make the gate unreachable")

    def test_the_rounding_case_that_broke_it(self) -> None:
        self.assertAlmostEqual(idx.max_confidence(9), 0.22)
        self.assertGreaterEqual(round(2 / 9, 2), idx.max_confidence(9))


class WiringTest(unittest.TestCase):
    def test_the_engine_passes_internals_into_the_decision(self) -> None:
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("market_internals.votes_for(", src)
        self.assertIn("extra_votes=extra", src)

    def test_the_internals_reading_is_recorded_with_the_entry(self) -> None:
        """No history exists to backtest this against, so the only way to learn
        whether it helps is to store what it said at the moment of each trade."""
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("internals={k: vote", src)
        self.assertIn("internals_in_index_call", src)

    def test_the_page_and_the_engine_use_the_same_helper(self) -> None:
        """They have already disagreed once over a threshold; showing the call
        without internals would put yesterday's reasoning beside a position
        taken on today's tape."""
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web.api_index_call)
        self.assertIn("market_internals.votes_for(", src)

    def test_internals_are_read_once_per_pass_not_once_per_index(self) -> None:
        """It scans ~2,400 live quotes and every index shares the same
        market-wide reading."""
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.index_options_pass)
        before = src.index("internals = market_internals.read()")
        loop = src.index('for symbol in cfg.get("instruments"')
        self.assertLess(before, loop)


if __name__ == "__main__":
    unittest.main()
