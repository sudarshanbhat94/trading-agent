from __future__ import annotations

import unittest

from app.main import _follow_history_order_events


class DashboardJournalContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
