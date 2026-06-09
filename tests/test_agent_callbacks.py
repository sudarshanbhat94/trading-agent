from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.agent import TradingAgentService


class _LogDB:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def insert_agent_log(self, level: str, component: str, event: str, message: str, details=None) -> None:
        self.logs.append(
            {
                "level": level,
                "component": component,
                "event": event,
                "message": message,
                "details": details or {},
            }
        )


class _SlowNotifier:
    async def notify_cycle_events(self) -> dict:
        await asyncio.sleep(1.0)
        return {"enabled": True}


class _FastNotifier:
    async def notify_cycle_events(self) -> dict:
        return {"enabled": True}


class PostCycleCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_optional_callback_timeouts_are_not_repeated_warns(self) -> None:
        db = _LogDB()
        agent = TradingAgentService(
            db=db,  # type: ignore[arg-type]
            market_data=SimpleNamespace(),
            broker=SimpleNamespace(),
            strategy=SimpleNamespace(settings=SimpleNamespace()),
            macro=None,
            institutional_feeds=None,
            delivery_service=None,
            market_breadth=None,
            sector_rotation=None,
            macro_calendar=None,
            options_intelligence=None,
            interval_seconds=1,
            cycle_timeout_seconds=30,
            openclaw_notifier=_SlowNotifier(),
        )
        agent._remaining_cycle_seconds = lambda: 0.55  # type: ignore[method-assign]

        await agent._run_post_cycle_callbacks({})
        await agent._run_post_cycle_callbacks({})

        timeout_logs = [log for log in db.logs if log["event"] == "openclaw_notifications_timeout"]
        self.assertEqual([log["level"] for log in timeout_logs], ["WARN", "INFO"])
        self.assertEqual(timeout_logs[1]["details"]["consecutive_timeout_count"], 2)

        agent.openclaw_notifier = _FastNotifier()
        await agent._run_post_cycle_callbacks({})

        self.assertNotIn("openclaw_notifications", agent._post_cycle_callback_timeouts)


class OptionalPhaseTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_optional_phase_timeouts_are_not_repeated_warns(self) -> None:
        logs: list[tuple] = []
        agent = TradingAgentService.__new__(TradingAgentService)
        agent.strategy = SimpleNamespace(settings=SimpleNamespace(optional_phase_timeout_seconds=0.01))
        agent.cycle_timeout_seconds = 120
        agent._cycle_phase = "global_intelligence"
        agent._log = lambda *args, **kwargs: logs.append(args)

        async def slow_context() -> dict:
            await asyncio.sleep(1.05)
            return {"status": "late"}

        await agent._run_optional_phase(
            component="macro",
            event="global_context",
            description="Global intelligence",
            awaitable=slow_context(),
            default={},
        )
        await agent._run_optional_phase(
            component="macro",
            event="global_context",
            description="Global intelligence",
            awaitable=slow_context(),
            default={},
        )

        timeout_logs = [log for log in logs if log[2] == "global_context_timeout"]
        self.assertEqual([log[0] for log in timeout_logs], ["WARN", "INFO"])
        self.assertEqual(timeout_logs[1][4]["consecutive_timeout_count"], 2)
        self.assertTrue(timeout_logs[1][4]["optional_phase"])

        async def fast_context() -> dict:
            return {"status": "ok"}

        result = await agent._run_optional_phase(
            component="macro",
            event="global_context",
            description="Global intelligence",
            awaitable=fast_context(),
            default={},
        )

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("macro:global_context", agent._optional_phase_timeouts)


if __name__ == "__main__":
    unittest.main()
