from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.models import utc_now


class StrategyPlanTests(unittest.TestCase):
    def test_strategy_plan_stats_are_market_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            now = utc_now()
            with db.connect() as conn:
                conn.executemany(
                    "insert into universe(symbol, name, exchange, base_price, enabled) values (?, ?, ?, 100, 1)",
                    [
                        ("INPLAN", "India Plan Stock", "NSE"),
                        ("USREJECT", "Rejected US Stock", "NASDAQ"),
                    ],
                )
                conn.executemany(
                    """
                    insert into signal_ideas (
                        first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                        entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                        confidence, combined_score, confluence, overall_score_pct, overall_grade,
                        reason, details_json
                    )
                    values (?, ?, ?, 'breakout', 'confirmed_breakout', 'BUY', ?, 100, 100,
                        ?, ?, ?, 0.8, 0.4, 20, 80, 'A', 'test', '{}')
                    """,
                    [
                        (now, now, "INPLAN", "ACTIVE", 1.76, 2.0, -0.2),
                        (now, now, "USREJECT", "REJECTED", 9.5, 10.0, -1.0),
                    ],
                )

            plan = next(item for item in db.strategy_plans() if item["code"] == "confirmed_breakout")

            self.assertEqual(plan["market_stats"]["IN"]["idea_count"], 1)
            self.assertAlmostEqual(plan["market_stats"]["IN"]["avg_return_pct"], 1.76)
            self.assertEqual(plan["market_stats"]["US"]["idea_count"], 0)
            self.assertEqual(plan["market_stats"]["US"]["avg_return_pct"], 0.0)
            self.assertEqual(plan["constituents_by_market"]["US"], [])


if __name__ == "__main__":
    unittest.main()
