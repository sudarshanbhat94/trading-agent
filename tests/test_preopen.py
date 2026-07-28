"""NSE pre-open call-auction parsing and the gapper screen.

The engine previously knew only yesterday's close, so at 09:15 it discovered
gaps from live ticks — after the move. This reads the 09:00-09:08 auction.

The parsing cases are the ones that bite in production: NSE returns rows with
no auction match (price 0), nulls in numeric fields, and occasional malformed
entries. None of those may take down the batch or, worse, be read as a real
price. The fetch itself is best-effort by design — NSE 503s under load, and a
failed pre-market call must leave the engine exactly as it was.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app import preopen

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime(2026, 7, 29, 9, 10, tzinfo=IST)


def row(symbol, last, prev, qty=1000, turnover=5e6):
    return {"metadata": {"symbol": symbol, "lastPrice": last, "previousClose": prev,
                         "finalQuantity": qty, "totalTurnover": turnover}}


class ParseTest(unittest.TestCase):
    def test_computes_the_gap_percent(self) -> None:
        out = preopen.parse({"data": [row("TCS", 106.0, 100.0)]})
        self.assertAlmostEqual(out["TCS"]["gap_pct"], 6.0)

    def test_negative_gaps_are_kept(self) -> None:
        """A gap DOWN is information too — it must not be silently dropped."""
        out = preopen.parse({"data": [row("INFY", 94.0, 100.0)]})
        self.assertAlmostEqual(out["INFY"]["gap_pct"], -6.0)

    def test_a_symbol_with_no_auction_match_is_skipped(self) -> None:
        """NSE returns lastPrice 0 when nothing crossed. Treating that as a
        price would read as a -100% gap."""
        out = preopen.parse({"data": [row("NOMATCH", 0, 100.0)]})
        self.assertEqual(out, {})

    def test_zero_previous_close_is_skipped(self) -> None:
        self.assertEqual(preopen.parse({"data": [row("X", 100.0, 0)]}), {})

    def test_nulls_in_numeric_fields_do_not_crash(self) -> None:
        out = preopen.parse({"data": [row("TCS", 106.0, 100.0, qty=None, turnover=None)]})
        self.assertEqual(out["TCS"]["qty"], 0.0)

    def test_a_malformed_row_does_not_lose_the_batch(self) -> None:
        payload = {"data": [{"metadata": {"symbol": "BAD", "lastPrice": "abc",
                                          "previousClose": 100}},
                            row("GOOD", 110.0, 100.0)]}
        out = preopen.parse(payload)
        self.assertIn("GOOD", out)
        self.assertNotIn("BAD", out)

    def test_symbols_are_upper_cased_and_trimmed(self) -> None:
        out = preopen.parse({"data": [row(" tcs ", 106.0, 100.0)]})
        self.assertIn("TCS", out)

    def test_empty_and_missing_payloads_are_safe(self) -> None:
        for payload in ({}, {"data": None}, {"data": []}, None):
            with self.subTest(payload=payload):
                self.assertEqual(preopen.parse(payload), {})


class GapperScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        preopen._CACHE.clear()
        preopen._CACHE[TODAY.date().isoformat()] = {
            "BIG":    dict(open=110.0, prev_close=100.0, gap_pct=10.0, qty=1e5, value=5e7),
            "SMALL":  dict(open=103.0, prev_close=100.0, gap_pct=3.0, qty=1e4, value=5e6),
            "THIN":   dict(open=150.0, prev_close=100.0, gap_pct=50.0, qty=5.0, value=750.0),
            "FLAT":   dict(open=100.5, prev_close=100.0, gap_pct=0.5, qty=1e4, value=5e6),
            "DOWN":   dict(open=90.0, prev_close=100.0, gap_pct=-10.0, qty=1e4, value=5e6),
        }

    def tearDown(self) -> None:
        preopen._CACHE.clear()

    def test_ranks_biggest_gap_first(self) -> None:
        names = [r["symbol"] for r in preopen.gappers(now=TODAY)]
        self.assertEqual(names[0], "BIG")

    def test_an_illiquid_auction_print_is_excluded(self) -> None:
        """THIN 'gapped' 50% on 5 shares. That price does not survive the open,
        and buying it books a fill that never existed."""
        names = [r["symbol"] for r in preopen.gappers(now=TODAY)]
        self.assertNotIn("THIN", names)

    def test_small_moves_are_below_the_threshold(self) -> None:
        self.assertNotIn("FLAT", [r["symbol"] for r in preopen.gappers(now=TODAY)])

    def test_down_gaps_are_not_returned_as_buy_candidates(self) -> None:
        self.assertNotIn("DOWN", [r["symbol"] for r in preopen.gappers(now=TODAY)])

    def test_a_penny_stock_is_excluded(self) -> None:
        """Taken from a real snapshot: DHARAN auctions at Rs 0.16, where a single
        tick is a 6% 'gap'. Matches the engine's MIN_PRICE floor."""
        preopen._CACHE[TODAY.date().isoformat()]["DHARAN"] = dict(
            open=0.16, prev_close=0.15, gap_pct=6.67, qty=1e5, value=5e7)
        self.assertNotIn("DHARAN", [r["symbol"] for r in preopen.gappers(now=TODAY)])

    def test_limit_is_respected(self) -> None:
        self.assertLessEqual(len(preopen.gappers(limit=1, now=TODAY)), 1)

    def test_cached_is_empty_for_a_different_session(self) -> None:
        """Yesterday's auction must never be served as today's."""
        other = TODAY.replace(day=30)
        self.assertEqual(preopen.cached(now=other), {})
        self.assertEqual(preopen.gappers(now=other), [])


class FetchSafetyTest(unittest.TestCase):
    def tearDown(self) -> None:
        preopen._CACHE.clear()

    def test_fetch_returns_empty_rather_than_raising(self) -> None:
        """NSE 503s under load; the open must not depend on this call."""
        original = preopen.httpx.Client
        preopen.httpx.Client = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(preopen.fetch(), {})
        finally:
            preopen.httpx.Client = original

    def test_a_failed_fetch_is_not_cached_so_it_retries(self) -> None:
        calls = []

        def failing():
            calls.append(1)
            return {}

        original, preopen.fetch = preopen.fetch, failing
        try:
            preopen.refresh(now=TODAY)
            preopen.refresh(now=TODAY)
        finally:
            preopen.fetch = original
        self.assertEqual(len(calls), 2, "an empty result must not be cached")

    def test_a_good_fetch_is_cached_once(self) -> None:
        calls = []

        def ok():
            calls.append(1)
            return {"TCS": dict(open=110.0, prev_close=100.0, gap_pct=10.0,
                                qty=1e5, value=5e7)}

        original, preopen.fetch = preopen.fetch, ok
        try:
            preopen.refresh(now=TODAY)
            preopen.refresh(now=TODAY)
        finally:
            preopen.fetch = original
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
