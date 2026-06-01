from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

from app.agent import TradingAgentService
from app.db import Database
from app.models import Quote
from app.pre_catalyst_engine import _missed_move_min_pct_for_universe


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

    def test_agent_persists_us_missed_move_review_when_running_both_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            agent = TradingAgentService.__new__(TradingAgentService)
            agent.db = db
            agent.market_region = "BOTH"
            agent.strategy = SimpleNamespace(
                settings=SimpleNamespace(missed_move_review_enabled=True, missed_move_review_market="BOTH")
            )
            summary = {
                "generated_at": "2026-06-01T14:30:00+00:00",
                "candidates": [{"symbol": "NVDA", "market_region": "US"}],
                "candidate_pool": [{"symbol": "NVDA", "market_region": "US"}],
                "candidate_pool_count": 1,
                "live_confirmations": [],
                "label_counts": {"READY_AT_OPEN": 1},
                "missed_move_review": {
                    "enabled": True,
                    "generated_at": "2026-06-01T14:30:00+00:00",
                    "reviewed_movers": 1,
                    "items": [{"symbol": "NVDA", "status": "absent_from_prior_watchlist", "move_pct": 3.4}],
                },
            }

            agent._persist_missed_move_review(summary)
            rows = db.latest_missed_move_reviews(5, market_region="US")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_region"], "US")
        self.assertEqual(rows[0]["details"]["market_region"], "US")

    def test_missed_move_threshold_uses_market_specific_setting(self) -> None:
        settings = SimpleNamespace(missed_move_min_move_pct_in=3.0, missed_move_min_move_pct_us=2.5)

        self.assertEqual(
            _missed_move_min_pct_for_universe(settings, [{"symbol": "NVDA", "exchange": "NASDAQ"}]),
            2.5,
        )
        self.assertEqual(
            _missed_move_min_pct_for_universe(settings, [{"symbol": "RELIANCE", "exchange": "NSE"}]),
            3.0,
        )
        self.assertEqual(
            _missed_move_min_pct_for_universe(
                settings,
                [{"symbol": "RELIANCE", "exchange": "NSE"}, {"symbol": "NVDA", "exchange": "NASDAQ"}],
            ),
            2.5,
        )

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

    def test_optional_phase_timeout_logs_and_continues_with_empty_context(self) -> None:
        async def slow_context() -> dict:
            await asyncio.sleep(1.05)
            return {"status": "late"}

        logs = []
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.strategy = SimpleNamespace(settings=SimpleNamespace(optional_phase_timeout_seconds=0.01))
        agent.cycle_timeout_seconds = 120
        agent._cycle_phase = "global_intelligence"
        agent._log = lambda *args, **kwargs: logs.append(args)

        result = asyncio.run(
            agent._run_optional_phase(
                component="macro",
                event="global_context",
                description="Global intelligence",
                awaitable=slow_context(),
                default={},
            )
        )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["timeout_seconds"], 1.0)
        self.assertTrue(any(args[2] == "global_context_timeout" for args in logs))

    def test_strategy_timeout_writes_safe_hold_decisions(self) -> None:
        async def slow_evaluate(*args, **kwargs) -> list:
            await asyncio.sleep(1.05)
            return []

        logs = []
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.strategy = SimpleNamespace(
            settings=SimpleNamespace(strategy_eval_timeout_seconds=0.01),
            evaluate=slow_evaluate,
        )
        agent.cycle_timeout_seconds = 120
        agent._cycle_started_at = datetime.now(timezone.utc).isoformat()
        agent._log = lambda *args, **kwargs: logs.append(args)
        universe = [{"symbol": "AAPL", "exchange": "NASDAQ"}]
        quotes = {"AAPL": Quote(symbol="AAPL", price=200.0, source="unit-test", asof="2026-06-01T14:30:00+00:00")}

        decisions = asyncio.run(
            agent._run_strategy_evaluation(
                universe,
                quotes,
                {},
                {},
                {},
                {},
                {},
                None,
                {},
                {},
                None,
                {},
                100_000.0,
            )
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "HOLD")
        self.assertIn("strategy_eval_timeout", decisions[0].details_json)
        self.assertTrue(any(args[2] == "strategy_eval_timeout" for args in logs))

    def test_pre_strategy_candle_fetch_defaults_to_deferred(self) -> None:
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.strategy = SimpleNamespace(settings=SimpleNamespace())

        self.assertFalse(agent._pre_strategy_candle_fetch_enabled())

    def test_market_closed_cycle_state_clears_stale_open_diagnostics(self) -> None:
        states = {}
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.db = SimpleNamespace(set_state=lambda key, value: states.__setitem__(key, value))
        agent.market_region = "BOTH"

        agent._write_market_closed_cycle_state(
            [
                {"symbol": "RELIANCE", "exchange": "NSE"},
                {"symbol": "AAPL", "exchange": "NASDAQ"},
            ],
            {"open_regions": [], "closed_regions": ["IN", "US"], "data_policy": {"IN": "closed", "US": "closed"}},
        )

        self.assertEqual(states["opportunity_scan"]["mode"], "market_closed")
        self.assertEqual(states["opportunity_scan"]["selected_symbols"], 0)
        self.assertEqual(states["decision_diagnostics"]["mode"], "market_closed")
        self.assertEqual(states["decision_diagnostics"]["health_flags"], [])


if __name__ == "__main__":
    unittest.main()
