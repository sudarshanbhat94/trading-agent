from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from app.main import (
    WebSocketHub,
    _compact_rally_plan_item,
    _effective_position_quote_refresh_seconds,
    _rally_plan_is_cached,
    _rally_plan_market_view,
    _follow_history_order_events,
)


class DashboardJournalContractTests(unittest.TestCase):
    def test_websocket_broadcast_tolerates_connection_set_mutation(self) -> None:
        class MutatingSocket:
            def __init__(self, hub: WebSocketHub) -> None:
                self.hub = hub
                self.messages: list[str] = []

            async def send_text(self, message: str) -> None:
                self.messages.append(message)
                self.hub.connections.add(object())  # type: ignore[arg-type]

        async def run() -> MutatingSocket:
            hub = WebSocketHub()
            socket = MutatingSocket(hub)
            hub.connections.add(socket)  # type: ignore[arg-type]
            await hub.broadcast({"ok": True})
            return socket

        socket = asyncio.run(run())

        self.assertEqual(len(socket.messages), 1)

    def test_paper_follow_events_are_labeled_simulated_not_broker_orders(self) -> None:
        events = _follow_history_order_events(
            [
                {
                    "follow_id": 143,
                    "symbol": "AEROFLEX",
                    "market_region": "IN",
                    "exchange": "NSE",
                    "mode": "PAPER",
                    "mode_label": "Paper",
                    "status": "EXITED",
                    "state": "CLOSED",
                    "entry_qty": 19,
                    "entry_price": 410.5,
                    "opened_at": "2026-06-02T07:45:17+00:00",
                    "closed_qty": 19,
                    "exit_price": 410.65,
                    "closed_at": "2026-06-02T07:47:03+00:00",
                    "exit_reason": "active_follow_severe_risk_flags",
                }
            ]
        )

        by_side = {event["side"]: event for event in events}
        buy = by_side["BUY"]
        sell = by_side["SELL"]
        self.assertEqual(buy["record_type"], "paper_follow_event")
        self.assertFalse(buy["is_broker_order"])
        self.assertTrue(buy["is_paper"])
        self.assertEqual(buy["status"], "PAPER_OPENED")
        self.assertEqual(buy["status_label"], "PAPER ENTRY")
        self.assertEqual(buy["product"], "PAPER FOLLOW")
        self.assertEqual(buy["order_type"], "SIMULATED")
        self.assertIn("Simulated paper", buy["reason"])
        self.assertEqual(sell["status"], "PAPER_EXITED")
        self.assertEqual(sell["status_label"], "PAPER EXIT")

    def test_rally_plan_cache_rejects_stale_or_wrong_market_state(self) -> None:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        stale_us_plan = {
            "enabled": True,
            "market_region": "US",
            "generated_at": yesterday,
            "items": [{"symbol": "AAPL"}],
            "sections": {},
        }

        self.assertFalse(_rally_plan_is_cached(stale_us_plan, "IN"))

        fresh_us_plan = {**stale_us_plan, "generated_at": datetime.now(timezone.utc).isoformat()}
        self.assertFalse(_rally_plan_is_cached(fresh_us_plan, "IN"))
        self.assertTrue(_rally_plan_is_cached(fresh_us_plan, "US"))
        self.assertEqual(_rally_plan_market_view(fresh_us_plan, "IN")["items"], [])

    def test_rally_plan_compact_preserves_entry_and_exit_plan(self) -> None:
        item = _compact_rally_plan_item(
            {
                "symbol": "NUVL",
                "action": "BUY CHECK",
                "entry_plan": {"status": "entry_check", "when": "Enter above trigger", "trigger_price": 91.2},
                "exit_plan": {"summary": "Partial at T1", "stop_loss": 88.4, "target1": 94.8},
                "evidence": {"large": {"ignored": True}},
            },
            include_evidence=False,
        )

        self.assertEqual(item["entry_plan"]["status"], "entry_check")
        self.assertEqual(item["exit_plan"]["stop_loss"], 88.4)
        self.assertNotIn("evidence", item)

    def test_position_quote_refresh_backs_off_for_large_books(self) -> None:
        self.assertEqual(_effective_position_quote_refresh_seconds(1, 0), 1.0)
        self.assertEqual(_effective_position_quote_refresh_seconds(1, 12), 5.0)
        self.assertEqual(_effective_position_quote_refresh_seconds(1, 30), 5.0)
        self.assertEqual(_effective_position_quote_refresh_seconds(1, 97), 10.0)
        self.assertEqual(_effective_position_quote_refresh_seconds(1, 4, closed_us_polling=True), 5.0)


if __name__ == "__main__":
    unittest.main()
