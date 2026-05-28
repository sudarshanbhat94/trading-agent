from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.market_regions import market_session_for_region


class MarketRegionSessionTests(unittest.TestCase):
    def test_india_session_is_closed_on_2026_bakri_id_holiday(self) -> None:
        session = market_session_for_region(
            "IN",
            datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(session["is_open"])
        self.assertEqual(session["reason"], "trading_holiday")
        self.assertEqual(session["holiday"], "Bakri Id")
        self.assertEqual(session["next_open"], "2026-05-29T09:15:00+05:30")

    def test_india_session_opens_on_next_non_holiday_weekday(self) -> None:
        session = market_session_for_region(
            "IN",
            datetime(2026, 5, 29, 6, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(session["is_open"])
        self.assertEqual(session["reason"], "regular_session")


if __name__ == "__main__":
    unittest.main()
