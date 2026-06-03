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

    def test_big_runner_candidate_includes_why_what_how_and_levels(self) -> None:
        plan = build_rally_plan(
            market_region="IN",
            market_day_regime={"state": REGIME_BROAD_RALLY, "score": 72.0},
            opportunity_scan={
                "market_region": "IN",
                "top_big_runner_candidates": [
                    {
                        "symbol": "PRERUN",
                        "name": "Pre Runner",
                        "market_region": "IN",
                        "score": 0.78,
                        "setup": "big_runner_watch",
                        "big_runner": {
                            "available": True,
                            "stage": "t1_pressure",
                            "setup": "big_runner_watch",
                            "action": "WATCH",
                            "score": 0.74,
                            "why": "tight base near breakout; relative strength leadership",
                            "what": "Prepare levels and wait for pre-open or first-hour ignition.",
                            "how": "No buy from pressure alone; act only after confirmation and supportive regime.",
                            "trigger_price": 121.0,
                            "max_entry": 122.21,
                            "stop_loss": 116.3,
                            "target1": 126.2,
                            "invalidation": "Invalid below 116.3 or if volume/regime confirmation fails.",
                            "evidence": {"rs_rank": 93.0},
                            "blockers": [],
                        },
                    }
                ],
            },
        )

        [item] = plan["sections"]["t1_pressure"]

        self.assertEqual(item["symbol"], "PRERUN")
        self.assertEqual(item["strategy"], "big_runner_watch")
        self.assertIn("tight base", item["why"])
        self.assertIn("wait", item["what"])
        self.assertIn("confirmation", item["how"])
        self.assertEqual(item["trigger_price"], 121.0)
        self.assertEqual(item["blockers"], [])
        self.assertIn("big_runner", item["evidence"])

    def test_big_runner_live_momentum_is_watch_when_regime_blocks(self) -> None:
        plan = build_rally_plan(
            market_region="IN",
            market_day_regime={"state": REGIME_FADE_RISK, "score": -12.0},
            opportunity_scan={
                "market_region": "IN",
                "top_big_runner_candidates": [
                    {
                        "symbol": "IGNITE",
                        "market_region": "IN",
                        "score": 0.83,
                        "setup": "big_runner_ignition",
                        "big_runner": {
                            "available": True,
                            "stage": "live_momentum",
                            "setup": "big_runner_ignition",
                            "action": "BUY CHECK",
                            "score": 0.82,
                            "why": "volume participation is starting",
                            "what": "Confirm broad/selective rally regime, VWAP hold, opening-range hold, and volume pace.",
                            "how": "Entry authority may promote only while price holds trigger and stays below max entry.",
                            "trigger_price": 123.4,
                            "max_entry": 124.14,
                            "stop_loss": 119.1,
                            "target1": 128.2,
                            "blockers": [],
                        },
                    }
                ],
            },
        )

        [item] = plan["sections"]["live_momentum"]

        self.assertEqual(item["symbol"], "IGNITE")
        self.assertEqual(item["action"], "WATCH")
        self.assertEqual(item["blockers"][0]["reason"], "market_day_regime_not_supportive_for_live_momentum")

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
