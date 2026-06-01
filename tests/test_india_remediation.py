from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

from app.agent import TradingAgentService
from app.db import Database


class IndiaRemediationTests(unittest.TestCase):
    def test_agent_persists_missed_move_review_to_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            agent = TradingAgentService.__new__(TradingAgentService)
            agent.db = db
            agent.market_region = "IN"
            agent.strategy = SimpleNamespace(
                settings=SimpleNamespace(missed_move_review_enabled=True, missed_move_review_market="IN")
            )
            summary = {
                "generated_at": "2026-06-01T10:00:00+00:00",
                "candidates": [{"symbol": "READY"}],
                "candidate_pool_count": 3,
                "live_confirmations": [],
                "label_counts": {"PRE_CATALYST_WATCH": 1},
                "missed_move_review": {
                    "enabled": True,
                    "generated_at": "2026-06-01T10:00:00+00:00",
                    "reviewed_movers": 1,
                    "items": [{"symbol": "MISSED", "status": "absent_from_prior_watchlist", "move_pct": 4.2}],
                },
            }

            agent._persist_missed_move_review(summary)
            rows = db.latest_missed_move_reviews(5, market_region="IN")

        self.assertEqual(len(rows), 1)
        self.assertEqual(summary["missed_move_review_row_id"], rows[0]["id"])
        self.assertEqual(rows[0]["review_date"], "2026-06-01")
        self.assertEqual(rows[0]["details"]["review"]["items"][0]["symbol"], "MISSED")

    def test_manual_run_once_is_wrapped_by_cycle_timeout(self) -> None:
        async def slow_inner(self: TradingAgentService) -> dict:
            self._cycle_phase = "strategy_and_llm"
            await asyncio.sleep(0.05)
            return {"finished": True}

        logs = []
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.cycle_timeout_seconds = 0.01
        agent._cycle_phase = "market_quotes"
        agent._cycle_started_at = "2026-06-01T10:00:00+00:00"
        agent._last_error = None
        agent.on_update = None
        agent._log = lambda *args, **kwargs: logs.append(args)
        agent.snapshot = MethodType(lambda self: {"last_error": self._last_error, "phase": self._cycle_phase}, agent)
        agent._run_once_inner = MethodType(slow_inner, agent)

        result = asyncio.run(agent.run_once())

        self.assertIn("Cycle timed out", result["last_error"])
        self.assertEqual(result["phase"], "idle")
        self.assertTrue(any(args[2] == "cycle_timeout" for args in logs))


if __name__ == "__main__":
    unittest.main()
