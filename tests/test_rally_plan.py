from __future__ import annotations

import unittest

from app.market_day_regime import REGIME_BROAD_RALLY, REGIME_FADE_RISK
from app.rally_plan import build_rally_plan, build_rally_plan_by_market


class RallyPlanBuilderTests(unittest.TestCase):
    def test_pre_catalyst_candidate_includes_why_what_how_contract(self) -> None:
        plan = build_rally_plan(
            market_region="IN",
            market_day_regime={"state": REGIME_BROAD_RALLY, "score": 72.0},
            pre_catalyst={
                "market_region": "IN",
                "candidates": [
                    {
                        "symbol": "NUVL",
                        "market_region": "IN",
                        "name": "Nuvalent",
                        "score": 0.82,
                        "setup": "pre_catalyst_pressure",
                        "key_reasons": ["volume pressure", "near breakout"],
                        "trigger_price": 91.2,
                        "max_entry": 92.0,
                        "stop_loss": 88.4,
                        "target1": 94.8,
                    }
                ],
            },
        )

        [item] = plan["sections"]["t1_pressure"]

        self.assertEqual(item["symbol"], "NUVL")
        self.assertEqual(item["action"], "WATCH")
        self.assertIn("volume pressure", item["why"])
        self.assertTrue(item["what"])
        self.assertTrue(item["how"])
        self.assertEqual(item["trigger_price"], 91.2)
        self.assertEqual(item["blockers"], [])

    def test_market_action_live_mover_is_watch_when_regime_blocks_momentum(self) -> None:
        plan = build_rally_plan(
            market_region="IN",
            market_day_regime={"state": REGIME_FADE_RISK, "score": -12.0},
            market_action_radar={
                "market_region": "IN",
                "events": [
                    {
                        "symbol": "FAST",
                        "market_region": "IN",
                        "event_types": ["TOP_GAINER"],
                        "market_action_score": 86.0,
                        "pct_change": 4.2,
                        "price": 120.0,
                        "reason": "top gainer with volume",
                    }
                ],
            },
        )

        [item] = plan["sections"]["live_momentum"]

        self.assertEqual(item["action"], "WATCH")
        self.assertEqual(item["section"], "live_momentum")
        self.assertEqual(item["blockers"][0]["reason"], "market_day_regime_not_supportive_for_live_momentum")
        self.assertIn("confirmation evidence", item["what"])

    def test_overextended_market_action_goes_to_avoid(self) -> None:
        plan = build_rally_plan(
            market_region="IN",
            market_day_regime={"state": REGIME_BROAD_RALLY, "score": 72.0},
            market_action_radar={
                "market_region": "IN",
                "events": [
                    {
                        "symbol": "CHASE",
                        "market_region": "IN",
                        "event_types": ["TOP_GAINER"],
                        "market_action_score": 90.0,
                        "pct_change": 9.1,
                        "price": 45.0,
                        "reason": "extended top gainer",
                    }
                ],
            },
        )

        [item] = plan["sections"]["avoid"]

        self.assertEqual(item["symbol"], "CHASE")
        self.assertEqual(item["action"], "AVOID")
        self.assertEqual(item["blockers"][0]["reason"], "do_not_chase_extended_market_action")

    def test_by_market_respects_explicit_us_market_region(self) -> None:
        plan = build_rally_plan_by_market(
            market_day_regime={"by_market": {"IN": {"state": REGIME_BROAD_RALLY}, "US": {"state": REGIME_BROAD_RALLY}}},
            pre_catalyst={
                "candidates": [
                    {"symbol": "AAPL", "market_region": "US", "score": 0.7, "reason": "relative strength"},
                    {"symbol": "RELIANCE", "market_region": "IN", "score": 0.7, "reason": "relative strength"},
                ]
            },
        )

        us_symbols = {item["symbol"] for item in plan["by_market"]["US"]["items"]}
        in_symbols = {item["symbol"] for item in plan["by_market"]["IN"]["items"]}

        self.assertIn("AAPL", us_symbols)
        self.assertNotIn("RELIANCE", us_symbols)
        self.assertIn("RELIANCE", in_symbols)


if __name__ == "__main__":
    unittest.main()
