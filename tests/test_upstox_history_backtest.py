from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.models import Candle
from scripts import backtest_upstox_history_entry_authority as replay


class UpstoxHistoryBacktestTests(unittest.TestCase):
    def test_parse_candles_accepts_upstox_payload_and_skips_bad_rows(self) -> None:
        candles = replay._parse_candles(
            "ABC",
            [
                ["2026-06-01T09:15:00+05:30", 100, 104, 99, 103, 12000, 0],
                ["bad"],
                ["2026-06-01T09:45:00+05:30", "101", "bad-high", 98, 100, 8000],
            ],
            "upstox-history:30minute",
        )

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].symbol, "ABC")
        self.assertEqual(candles[0].close, 103.0)
        self.assertEqual(candles[0].source, "upstox-history:30minute")

    def test_cache_round_trips_candles_and_fetch_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            payload = {
                "daily": [
                    Candle("ABC", "2026-05-31T00:00:00+05:30", 100, 105, 95, 102, 100000, "day"),
                ],
                "intraday": [
                    Candle("ABC", "2026-06-01T09:15:00+05:30", 102, 106, 101, 105, 12000, "30minute"),
                ],
                "daily_error": None,
                "intraday_error": "HTTPStatusError:429:rate limited",
            }

            replay._write_cache(cache_dir, "ABC", payload)
            cached = replay._read_cache(cache_dir, "ABC")

        self.assertIsNotNone(cached)
        self.assertEqual(cached["daily"][0].close, 102)
        self.assertEqual(cached["intraday"][0].volume, 12000)
        self.assertEqual(cached["intraday_error"], "HTTPStatusError:429:rate limited")

    def test_sentiment_at_uses_latest_past_event_and_ignores_future_news(self) -> None:
        decision_ts = datetime(2026, 6, 1, 10, 15, tzinfo=timezone.utc)
        history = [
            {
                "asof": "2026-06-01T09:00:00+00:00",
                "score": 0.1,
                "headline_count": 1,
                "headlines": ["older"],
                "events": [],
            },
            {
                "asof": "2026-06-01T10:00:00+00:00",
                "score": 0.4,
                "headline_count": 1,
                "headlines": ["usable"],
                "events": [{"type": "order_win"}],
            },
            {
                "asof": "2026-06-01T10:30:00+00:00",
                "score": -0.8,
                "headline_count": 1,
                "headlines": ["future"],
                "events": [{"type": "bad_news"}],
            },
        ]

        sentiment = replay._sentiment_at(history, decision_ts, max_age_days=7)

        self.assertIsNotNone(sentiment)
        self.assertEqual(sentiment["score"], 0.4)
        self.assertEqual(sentiment["headlines"], ["usable"])
        self.assertEqual(sentiment["age_seconds"], 900.0)

    def test_sentiment_at_respects_max_age(self) -> None:
        decision_ts = datetime(2026, 6, 1, 10, 15, tzinfo=timezone.utc)

        sentiment = replay._sentiment_at(
            [{"asof": "2026-05-20T10:00:00+00:00", "score": 0.9, "headline_count": 1}],
            decision_ts,
            max_age_days=7,
        )

        self.assertIsNone(sentiment)

    def test_historical_sentiment_loader_reads_replay_window_without_future_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "agent.db"
            conn = sqlite3.connect(database)
            conn.execute(
                """
                create table sentiment_events (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    score real not null,
                    headline_count integer not null,
                    headlines_json text not null,
                    confidence real not null,
                    events_json text not null
                )
                """
            )
            conn.executemany(
                """
                insert into sentiment_events
                    (ts, symbol, score, headline_count, headlines_json, confidence, events_json)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("2026-05-31T10:00:00+00:00", "ABC", 0.2, 1, '["prior"]', 0.6, "[]"),
                    (
                        "2026-06-01T10:00:00+00:00",
                        "ABC",
                        0.8,
                        2,
                        '["current"]',
                        0.9,
                        '[{"type":"order_win"}]',
                    ),
                    ("2026-06-04T10:00:00+00:00", "ABC", -0.9, 1, '["future"]', 0.9, "[]"),
                ],
            )
            conn.commit()
            conn.close()

            history = replay._load_historical_sentiment(database, date(2026, 6, 1), date(2026, 6, 3), 7)
            sentiment = replay._sentiment_at(
                history["ABC"],
                datetime(2026, 6, 1, 10, 15, tzinfo=timezone.utc),
                max_age_days=7,
            )

        self.assertEqual(len(history["ABC"]), 2)
        self.assertEqual(sentiment["score"], 0.8)
        self.assertEqual(sentiment["events"], [{"type": "order_win"}])

    def test_historical_quote_states_builds_cumulative_intraday_quote(self) -> None:
        states = replay._historical_quote_states(
            {
                "ABC": {
                    "intraday": [
                        Candle("ABC", "2026-06-01T09:15:00+05:30", 100, 103, 99, 102, 1000, "upstox"),
                        Candle("ABC", "2026-06-01T09:45:00+05:30", 102, 108, 101, 107, 1500, "upstox"),
                    ]
                }
            },
            date(2026, 6, 1),
            date(2026, 6, 2),
        )

        quotes = [states[ts]["ABC"]["quote"] for ts in sorted(states)]

        self.assertEqual(quotes[0].open, 100)
        self.assertEqual(quotes[0].high, 103)
        self.assertEqual(quotes[0].low, 99)
        self.assertEqual(quotes[0].volume, 1000)
        self.assertEqual(quotes[1].high, 108)
        self.assertEqual(quotes[1].low, 99)
        self.assertEqual(quotes[1].volume, 2500)

    def test_cache_only_fetch_all_replays_available_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            replay._write_cache(
                cache_dir,
                "ABC",
                {"daily": [], "intraday": [], "daily_error": None, "intraday_error": None},
            )

            fetched = asyncio.run(
                replay._fetch_all(
                    [
                        {"symbol": "ABC", "upstox_instrument_key": "NSE_EQ|1"},
                        {"symbol": "XYZ", "upstox_instrument_key": "NSE_EQ|2"},
                    ],
                    "https://example.invalid",
                    "30minute",
                    date(2026, 6, 1),
                    date(2026, 6, 2),
                    420,
                    1,
                    cache_dir,
                    cache_only=True,
                    request_spacing_seconds=0.0,
                )
            )

        self.assertEqual(set(fetched), {"ABC"})

    def test_summary_excludes_unpriced_no_future_trades_from_performance(self) -> None:
        summary = replay._summary(
            [
                {
                    "exit_reason": "open_or_period_mark",
                    "gross_pct": 1.0,
                    "net_pct": 0.5,
                    "net_pnl": 50.0,
                    "cost": 10.0,
                },
                {
                    "exit_reason": "no_future_candles",
                    "gross_pct": -5.0,
                    "net_pct": -6.0,
                    "net_pnl": -600.0,
                    "cost": 20.0,
                },
            ]
        )

        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["priced"], 1)
        self.assertEqual(summary["no_future"], 1)
        self.assertEqual(summary["sum_net_pnl"], 50.0)
        self.assertEqual(summary["sum_cost"], 10.0)
