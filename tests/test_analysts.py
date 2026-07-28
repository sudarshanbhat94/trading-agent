"""Independent analysts and CIO reconciliation.

The property that justifies this module existing — as opposed to the single
blended score in `recommendation.py` — is that genuine disagreement stays
visible. So the tests that matter most are the ones asserting a bull and a bear
produce reported dissent and reduced confidence, rather than cancelling into a
confident-looking neutral.

Consensus values are hand-computed from the documented confidence weighting.
"""

from __future__ import annotations

import json
import unittest

from app import analysts


BULL_FACTS = {
    "price": 105.0, "close": 105.0, "sma20": 100.0, "sma50": 95.0,
    "rvol": 1.4, "atr_pct": 0.02, "regime_on": True, "news_score": 0.6,
    "catalysts": [{"headline": "Q1 results beat", "type": "results"}],
    "technicals": {"supertrend": {"direction": "up", "value": 98.0},
                   "ichimoku": {"kijun": 100.0}, "stale": False},
}

BEAR_FACTS = {
    "price": 90.0, "close": 90.0, "sma20": 100.0, "sma50": 105.0,
    "rvol": 0.5, "atr_pct": 0.06, "regime_on": False, "news_score": -0.6,
    "catalysts": [{"headline": "Order cancelled", "type": "order"}],
    "technicals": {"supertrend": {"direction": "down", "value": 104.0},
                   "ichimoku": {"kijun": 101.0}, "stale": False},
}


class TechnicalAnalystTest(unittest.TestCase):
    def test_bullish_structure(self) -> None:
        o = analysts.technical_analyst(BULL_FACTS)
        self.assertGreater(o["stance"], 0.5)
        self.assertFalse(o["abstained"])

    def test_bearish_structure(self) -> None:
        self.assertLess(analysts.technical_analyst(BEAR_FACTS)["stance"], -0.5)

    def test_abstains_without_price_data(self) -> None:
        o = analysts.technical_analyst({})
        self.assertTrue(o["abstained"])
        self.assertEqual(o["confidence"], 0.0)

    def test_fewer_checks_means_lower_confidence(self) -> None:
        """Only the moving-average check available -> one of three."""
        partial = analysts.technical_analyst(
            {"close": 105.0, "sma20": 100.0, "sma50": 95.0})
        self.assertLess(partial["confidence"], analysts.technical_analyst(BULL_FACTS)["confidence"])

    def test_stale_indicators_cut_confidence(self) -> None:
        stale = dict(BULL_FACTS)
        stale["technicals"] = dict(BULL_FACTS["technicals"], stale=True, as_of="2026-07-24")
        self.assertLess(analysts.technical_analyst(stale)["confidence"],
                        analysts.technical_analyst(BULL_FACTS)["confidence"])

    def test_evidence_names_its_sources(self) -> None:
        for item in analysts.technical_analyst(BULL_FACTS)["evidence"]:
            self.assertIn("metric", item)
            self.assertIn("source", item)


class CatalystAnalystTest(unittest.TestCase):
    def test_positive_tone_with_filing(self) -> None:
        o = analysts.catalyst_analyst(BULL_FACTS)
        self.assertGreater(o["stance"], 0.0)
        self.assertIn("filing", o["rationale"])

    def test_abstains_with_no_news_or_filings(self) -> None:
        self.assertTrue(analysts.catalyst_analyst({})["abstained"])

    def test_filing_adds_confidence_not_direction(self) -> None:
        """A material filing means 'pay attention', not 'buy'. Stance must be
        unchanged by its presence; only confidence moves."""
        with_filing = analysts.catalyst_analyst(
            {"news_score": 0.4, "catalysts": [{"headline": "x", "type": "results"}]})
        without = analysts.catalyst_analyst({"news_score": 0.4, "catalysts": []})
        self.assertEqual(with_filing["stance"], without["stance"])
        self.assertGreater(with_filing["confidence"], without["confidence"])

    def test_procedural_filings_do_not_count_as_material(self) -> None:
        o = analysts.catalyst_analyst(
            {"news_score": 0.2, "catalysts": [{"headline": "AGM notice", "type": "noise"}]})
        self.assertIn("no material filing", o["rationale"])


class RiskAnalystTest(unittest.TestCase):
    def test_never_argues_to_buy(self) -> None:
        """Structural guarantee: risk can veto, never endorse."""
        for facts in (BULL_FACTS, BEAR_FACTS, {"atr_pct": 0.001, "rvol": 5.0, "regime_on": True}):
            with self.subTest(facts=list(facts)[:2]):
                self.assertLessEqual(analysts.risk_analyst(facts)["stance"], 0.0)

    def test_calm_conditions_are_neutral(self) -> None:
        o = analysts.risk_analyst({"atr_pct": 0.015, "rvol": 1.2, "regime_on": True})
        self.assertEqual(o["stance"], 0.0)
        self.assertIn("No elevated risk", o["rationale"])

    def test_flags_volatility_regime_and_liquidity(self) -> None:
        o = analysts.risk_analyst(BEAR_FACTS)
        self.assertLess(o["stance"], -0.5)
        for phrase in ("daily range", "risk-off", "thin volume"):
            self.assertIn(phrase, o["rationale"])

    def test_abstains_with_no_inputs(self) -> None:
        self.assertTrue(analysts.risk_analyst({})["abstained"])


class PositionAnalystTest(unittest.TestCase):
    def test_abstains_when_not_held(self) -> None:
        self.assertTrue(analysts.position_analyst({})["abstained"])

    def test_extended_winner_argues_against_adding(self) -> None:
        o = analysts.position_analyst({"held": {"strategy": "swing", "pnl": 22.0}})
        self.assertLess(o["stance"], 0.0)
        self.assertIn("concentration", o["rationale"])

    def test_loser_points_at_the_stop(self) -> None:
        o = analysts.position_analyst({"held": {"strategy": "swing", "pnl": -8.0}})
        self.assertLess(o["stance"], 0.0)
        self.assertIn("stop", o["rationale"])

    def test_small_move_is_neutral(self) -> None:
        self.assertEqual(
            analysts.position_analyst({"held": {"strategy": "swing", "pnl": 2.0}})["stance"], 0.0)


class CIOTest(unittest.TestCase):
    def test_unanimous_bulls_produce_a_bullish_consensus(self) -> None:
        cio = analysts.analyse(BULL_FACTS)["cio"]
        self.assertIn(cio["consensus"], ("bullish", "leaning bullish"))
        self.assertEqual(cio["dissent"], [])

    def test_unanimous_bears_produce_a_bearish_consensus(self) -> None:
        cio = analysts.analyse(BEAR_FACTS)["cio"]
        self.assertIn(cio["consensus"], ("bearish", "leaning bearish"))

    def test_conflict_is_reported_not_averaged_away(self) -> None:
        """The whole reason this module exists. A strong chart against a
        risk-off tape and bad news must surface BOTH sides."""
        conflicted = dict(BULL_FACTS)
        conflicted.update(news_score=-0.8, regime_on=False, atr_pct=0.07, rvol=0.4,
                          catalysts=[{"headline": "Order cancelled", "type": "order"}])
        cio = analysts.analyse(conflicted)["cio"]
        self.assertTrue(cio["dissent"], "conflicting analysts must produce dissent")
        agents = " ".join(cio["dissent"])
        self.assertIn("technical", agents)
        self.assertIn("risk", agents)

    def test_conflict_lowers_confidence(self) -> None:
        agree = analysts.analyse(BULL_FACTS)["cio"]["confidence"]
        conflicted = dict(BULL_FACTS)
        conflicted.update(news_score=-0.8, regime_on=False, atr_pct=0.07, rvol=0.4)
        self.assertLess(analysts.analyse(conflicted)["cio"]["confidence"], agree)

    def test_abstainers_are_excluded_not_counted_as_neutral(self) -> None:
        """A missing analyst must not dilute a strong consensus the way a
        neutral vote would."""
        cio = analysts.analyse(BULL_FACTS)["cio"]
        self.assertIn("position", cio["abstained"])     # nothing held
        self.assertEqual(cio["participating"], 3)
        self.assertGreater(cio["stance"], 0.1)

    def test_all_abstaining_gives_no_view(self) -> None:
        cio = analysts.analyse({})["cio"]
        self.assertEqual(cio["consensus"], "no view")
        self.assertEqual(cio["confidence"], 0.0)
        self.assertEqual(cio["participating"], 0)

    def test_consensus_is_confidence_weighted(self) -> None:
        """Hand-computed: stance +1.0 at confidence 0.8 against -1.0 at 0.2
        gives (0.8 - 0.2) / 1.0 = +0.6."""
        cio = analysts.chief_investment_officer([
            {"agent": "a", "stance": 1.0, "confidence": 0.8, "rationale": "x",
             "evidence": [], "abstained": False},
            {"agent": "b", "stance": -1.0, "confidence": 0.2, "rationale": "y",
             "evidence": [], "abstained": False},
        ])
        self.assertAlmostEqual(cio["stance"], 0.6, places=3)

    def test_dissent_names_both_sides(self) -> None:
        cio = analysts.chief_investment_officer([
            {"agent": "technical", "stance": 0.9, "confidence": 0.7,
             "rationale": "chart strong", "evidence": [], "abstained": False},
            {"agent": "risk", "stance": -0.9, "confidence": 0.7,
             "rationale": "tape weak", "evidence": [], "abstained": False},
        ])
        self.assertEqual(len(cio["dissent"]), 2)
        self.assertTrue(any("bullish" in d for d in cio["dissent"]))
        self.assertTrue(any("bearish" in d for d in cio["dissent"]))


class RobustnessTest(unittest.TestCase):
    def test_analyse_never_raises_on_garbage(self) -> None:
        for facts in (None, "nonsense", {"technicals": "bad", "catalysts": "bad",
                                         "held": "bad", "atr_pct": "bad"}):
            with self.subTest(facts=facts):
                result = analysts.analyse(facts)
                self.assertIn("cio", result)
                self.assertEqual(len(result["opinions"]), len(analysts.ANALYSTS))

    def test_one_failing_analyst_does_not_silence_the_panel(self) -> None:
        def boom(facts):
            raise RuntimeError("analyst exploded")

        original = analysts.ANALYSTS
        try:
            analysts.ANALYSTS = (boom, analysts.technical_analyst)
            result = analysts.analyse(BULL_FACTS)
            self.assertEqual(len(result["opinions"]), 2)
            self.assertTrue(result["opinions"][0]["abstained"])
            self.assertFalse(result["opinions"][1]["abstained"])
        finally:
            analysts.ANALYSTS = original

    def test_output_is_json_serialisable(self) -> None:
        json.dumps(analysts.analyse(BULL_FACTS))

    def test_stance_and_confidence_stay_in_range(self) -> None:
        for facts in (BULL_FACTS, BEAR_FACTS, {}):
            result = analysts.analyse(facts)
            for o in result["opinions"]:
                self.assertGreaterEqual(o["stance"], -1.0)
                self.assertLessEqual(o["stance"], 1.0)
                self.assertGreaterEqual(o["confidence"], 0.0)
                self.assertLessEqual(o["confidence"], 1.0)
            self.assertGreaterEqual(result["cio"]["stance"], -1.0)
            self.assertLessEqual(result["cio"]["stance"], 1.0)


if __name__ == "__main__":
    unittest.main()
