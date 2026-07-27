"""Structured recommendations.

The brief's binding constraint is "never hallucinate", so the property that
matters most is traceability: every statement must be backed by an evidence
entry naming the metric it came from, and missing facts must lower confidence
rather than be filled in.

Expected ratings are hand-computed from the documented weights and band
cut-points, not captured from the implementation.
"""

from __future__ import annotations

import json
import unittest

from app import recommendation as rec


def _facts(**overrides):
    """A deliberately complete, mildly bullish fact set."""
    base = dict(
        symbol="TESTCO", price=105.0, close=105.0, conviction=0.70,
        sma20=100.0, sma50=95.0, rs20=0.04, rvol=1.4, atr_pct=0.02,
        regime_on=True, entry=105.0, stop=99.0, target=111.0, held=False,
        news=[{"title": "Q1 results beat estimates", "label": "results",
               "when": "28 Jul 09:00", "score": 0.6}],
        news_score=0.6,
        technicals={
            "supertrend": {"direction": "up", "value": 98.0},
            "ichimoku": {"kijun": 100.0},
            "pivot_points": {"s1": 102.0, "s2": 99.0, "r1": 108.0, "r2": 112.0},
            "stale": False, "as_of": "2026-07-27",
        },
    )
    base.update(overrides)
    return base


class RatingLadderTest(unittest.TestCase):
    def test_seven_levels_in_order(self) -> None:
        self.assertEqual(len(rec.RATINGS), 7)
        self.assertEqual(rec.RATINGS[0], "Strong Sell")
        self.assertEqual(rec.RATINGS[-1], "Strong Buy")
        self.assertEqual(rec.RATINGS[rec.HOLD_INDEX], "Hold")

    def test_band_boundaries_are_hand_checked(self) -> None:
        # Cut-points: -0.60 / -0.35 / -0.12 / 0.12 / 0.35 / 0.60
        self.assertEqual(rec.RATINGS[rec._band(-0.9)], "Strong Sell")
        self.assertEqual(rec.RATINGS[rec._band(-0.5)], "Sell")
        self.assertEqual(rec.RATINGS[rec._band(-0.2)], "Reduce")
        self.assertEqual(rec.RATINGS[rec._band(0.0)], "Hold")
        self.assertEqual(rec.RATINGS[rec._band(0.2)], "Accumulate")
        self.assertEqual(rec.RATINGS[rec._band(0.5)], "Buy")
        self.assertEqual(rec.RATINGS[rec._band(0.9)], "Strong Buy")

    def test_ladder_is_symmetric(self) -> None:
        for value in (0.13, 0.36, 0.61, 0.8):
            with self.subTest(value=value):
                bullish = rec._band(value)
                bearish = rec._band(-value)
                self.assertEqual(bullish - rec.HOLD_INDEX, rec.HOLD_INDEX - bearish)

    def test_bullish_facts_rate_above_hold(self) -> None:
        result = rec.build_recommendation(_facts())
        self.assertGreater(result["rating_score"], rec.HOLD_INDEX)
        self.assertIn(result["rating"], ("Accumulate", "Buy", "Strong Buy"))

    def test_bearish_facts_rate_below_hold(self) -> None:
        result = rec.build_recommendation(_facts(
            conviction=0.15, close=90.0, price=90.0, sma20=100.0, sma50=105.0,
            rs20=-0.06, news_score=-0.5,
            technicals={"supertrend": {"direction": "down", "value": 104.0},
                        "ichimoku": {"kijun": 101.0}, "pivot_points": {}, "stale": False},
        ))
        self.assertLess(result["rating_score"], rec.HOLD_INDEX)

    def test_conflicting_facts_land_near_hold(self) -> None:
        """Strong engine conviction against a broken chart must not produce a
        confident call in either direction."""
        result = rec.build_recommendation(_facts(
            conviction=0.85, close=90.0, price=90.0, sma20=100.0, sma50=105.0,
            rs20=-0.05, news_score=-0.4,
            technicals={"supertrend": {"direction": "down", "value": 104.0},
                        "ichimoku": {"kijun": 101.0}, "pivot_points": {}, "stale": False},
        ))
        self.assertIn(result["rating"], ("Reduce", "Hold", "Accumulate"))
        self.assertLess(result["confidence"], 0.75)


class EvidenceTest(unittest.TestCase):
    """The anti-hallucination guarantee."""

    def test_every_reasoning_line_has_backing_evidence(self) -> None:
        result = rec.build_recommendation(_facts())
        claims = {e["claim"] for e in result["evidence"]}
        self.assertTrue(result["reasoning"])
        for line in result["reasoning"]:
            self.assertIn(line, claims, f"unbacked statement: {line}")

    def test_bull_and_bear_cases_are_drawn_from_evidence(self) -> None:
        result = rec.build_recommendation(_facts())
        claims = {e["claim"] for e in result["evidence"]}
        for line in result["bull_case"] + result["bear_case"]:
            self.assertIn(line, claims)

    def test_evidence_entries_name_metric_value_and_source(self) -> None:
        for entry in rec.build_recommendation(_facts())["evidence"]:
            for key in ("claim", "metric", "value", "source"):
                self.assertIn(key, entry)
            self.assertTrue(str(entry["source"]).strip())
            self.assertIsNotNone(entry["value"])

    def test_a_signal_disappears_when_its_fact_is_missing(self) -> None:
        with_news = rec.build_recommendation(_facts())
        without = rec.build_recommendation(_facts(news_score=None))
        metrics_with = {e["metric"] for e in with_news["evidence"]}
        metrics_without = {e["metric"] for e in without["evidence"]}
        self.assertIn("news_score", metrics_with)
        self.assertNotIn("news_score", metrics_without)

    def test_catalysts_come_from_stored_news_only(self) -> None:
        result = rec.build_recommendation(_facts())
        self.assertEqual(len(result["catalysts"]), 1)
        self.assertEqual(result["catalysts"][0]["headline"], "Q1 results beat estimates")

    def test_no_catalysts_when_there_is_no_news(self) -> None:
        self.assertEqual(rec.build_recommendation(_facts(news=[]))["catalysts"], [])


class ConfidenceTest(unittest.TestCase):
    def test_full_agreement_beats_partial_data(self) -> None:
        full = rec.build_recommendation(_facts())["confidence"]
        sparse = rec.build_recommendation(
            dict(symbol="X", price=105.0, conviction=0.70)
        )["confidence"]
        self.assertGreater(full, sparse)

    def test_stale_indicators_reduce_confidence(self) -> None:
        fresh = _facts()
        stale = _facts()
        stale["technicals"] = dict(stale["technicals"], stale=True, as_of="2026-07-24")
        self.assertLess(
            rec.build_recommendation(stale)["confidence"],
            rec.build_recommendation(fresh)["confidence"],
        )

    def test_disagreement_reduces_confidence(self) -> None:
        agree = rec.build_recommendation(_facts())["confidence"]
        conflict = rec.build_recommendation(_facts(
            rs20=-0.06, news_score=-0.6,
            technicals={"supertrend": {"direction": "down", "value": 110.0},
                        "ichimoku": {"kijun": 108.0}, "pivot_points": {}, "stale": False},
        ))["confidence"]
        self.assertLess(conflict, agree)

    def test_confidence_is_bounded(self) -> None:
        for facts in (_facts(), _facts(conviction=1.0), _facts(conviction=0.0)):
            value = rec.build_recommendation(facts)["confidence"]
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_thin_evidence_cannot_produce_an_extreme_call(self) -> None:
        """One weak signal must not yield Strong Buy / Strong Sell."""
        result = rec.build_recommendation(dict(symbol="X", price=100.0, conviction=1.0))
        self.assertNotIn(result["rating"], ("Strong Buy", "Strong Sell"))


class LevelsAndTargetsTest(unittest.TestCase):
    def test_support_is_below_and_resistance_above_spot(self) -> None:
        result = rec.build_recommendation(_facts())
        for level in result["support"]:
            self.assertLess(level["price"], 105.0)
        for level in result["resistance"]:
            self.assertGreater(level["price"], 105.0)

    def test_levels_are_labelled_with_their_origin(self) -> None:
        result = rec.build_recommendation(_facts())
        labels = {l["label"] for l in result["support"] + result["resistance"]}
        self.assertIn("pivot S1", labels)
        self.assertIn("pivot R1", labels)
        self.assertIn("SuperTrend", labels)   # up-trend -> support side

    def test_supertrend_is_resistance_in_a_downtrend(self) -> None:
        facts = _facts(price=95.0)
        facts["technicals"] = dict(facts["technicals"],
                                   supertrend={"direction": "down", "value": 104.0})
        result = rec.build_recommendation(facts)
        self.assertIn("SuperTrend", {l["label"] for l in result["resistance"]})

    def test_targets_are_above_spot_with_upside(self) -> None:
        result = rec.build_recommendation(_facts())
        self.assertTrue(result["targets"])
        for target in result["targets"]:
            self.assertGreater(target["price"], 105.0)
            self.assertGreater(target["upside_pct"], 0)

    def test_entry_and_stoploss_are_carried_through(self) -> None:
        result = rec.build_recommendation(_facts())
        self.assertEqual(result["entry"], 105.0)
        self.assertEqual(result["stoploss"], 99.0)


class RiskTest(unittest.TestCase):
    def test_high_volatility_is_flagged(self) -> None:
        risks = rec.build_recommendation(_facts(atr_pct=0.06))["risks"]
        self.assertTrue(any("volatility" in r.lower() for r in risks))

    def test_risk_off_regime_is_flagged(self) -> None:
        risks = rec.build_recommendation(_facts(regime_on=False))["risks"]
        self.assertTrue(any("risk-off" in r for r in risks))

    def test_stale_indicators_are_flagged_with_the_date(self) -> None:
        facts = _facts()
        facts["technicals"] = dict(facts["technicals"], stale=True, as_of="2026-07-24")
        risks = rec.build_recommendation(facts)["risks"]
        self.assertTrue(any("2026-07-24" in r for r in risks))

    def test_held_position_warns_about_the_gap_limitation(self) -> None:
        risks = rec.build_recommendation(_facts(held=True))["risks"]
        self.assertTrue(any("gap" in r.lower() for r in risks))

    def test_thin_volume_is_flagged(self) -> None:
        risks = rec.build_recommendation(_facts(rvol=0.4))["risks"]
        self.assertTrue(any("Thin participation" in r for r in risks))


class RobustnessTest(unittest.TestCase):
    def test_no_facts_returns_insufficient_data(self) -> None:
        result = rec.build_recommendation({})
        self.assertTrue(result["insufficient_data"])
        self.assertEqual(result["rating"], "Hold")
        self.assertEqual(result["confidence"], 0.0)

    def test_nan_and_none_facts_are_ignored(self) -> None:
        result = rec.build_recommendation(_facts(conviction=float("nan"), rs20=None))
        metrics = {e["metric"] for e in result["evidence"]}
        self.assertNotIn("conviction", metrics)
        self.assertNotIn("rs20", metrics)

    def test_one_malformed_field_costs_only_its_own_signal(self) -> None:
        """A bad `technicals` value must not discard the valid conviction,
        trend and relative-strength signals — degrade, don't collapse."""
        result = rec.build_recommendation(_facts(
            technicals="not a dict", news="not a list"
        ))
        self.assertFalse(result["insufficient_data"])
        metrics = {e["metric"] for e in result["evidence"]}
        self.assertIn("conviction", metrics)          # survived
        self.assertIn("rs20", metrics)                # survived
        self.assertNotIn("supertrend", metrics)       # correctly dropped
        self.assertEqual(result["catalysts"], [])
        self.assertIn(result["rating"], rec.RATINGS)

    def test_garbage_numeric_does_not_raise(self) -> None:
        result = rec.build_recommendation(_facts(conviction="abc"))
        self.assertIn(result["rating"], rec.RATINGS)
        self.assertNotIn("conviction", {e["metric"] for e in result["evidence"]})

    def test_output_is_json_serialisable(self) -> None:
        json.dumps(rec.build_recommendation(_facts()))

    def test_every_documented_field_is_present(self) -> None:
        result = rec.build_recommendation(_facts())
        for field in ("rating", "rating_score", "confidence", "reasoning",
                      "bull_case", "bear_case", "risks", "catalysts", "support",
                      "resistance", "entry", "stoploss", "targets",
                      "time_horizon", "evidence"):
            self.assertIn(field, result)

    def test_time_horizon_is_stated(self) -> None:
        self.assertTrue(rec.build_recommendation(_facts())["time_horizon"].strip())


if __name__ == "__main__":
    unittest.main()
