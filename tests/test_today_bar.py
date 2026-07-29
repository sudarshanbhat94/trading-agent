"""Today's forming bar, appended from the live feed.

The daily lanes scored `dates[-1]` — the last COMPLETED session — so at midday
they ranked yesterday's close and bought at today's price. A signal a full
session stale is the reason the picks looked wrong.

The risk in fixing it is writing bad data into the history: a frozen quote, a
duplicate of a bar the nightly job already ingested, or a market index that
advances for the symbols but not for the regime filter. Each is pinned here.
"""

from __future__ import annotations

import unittest
from datetime import datetime

import pandas as pd

from app import v2_live

IST = v2_live.IST
TODAY = pd.Timestamp(datetime.now(IST).date())
COLS = ["symbol", "ts", "open", "high", "low", "close", "volume", "ret1"]


def hist(last_close=100.0, days=5, end=None):
    end = end or (TODAY - pd.Timedelta(days=1))
    idx = pd.date_range(end=end, periods=days, freq="D")
    rows = [{"symbol": "TCS", "ts": str(d.date()), "open": last_close, "high": last_close,
             "low": last_close, "close": last_close, "volume": 1000.0, "ret1": 0.0}
            for d in idx]
    return pd.DataFrame(rows, index=idx, columns=COLS)


def mkt(end=None):
    end = end or (TODAY - pd.Timedelta(days=1))
    idx = pd.date_range(end=end, periods=5, freq="D")
    return pd.DataFrame({"mkt_ret1": [0.0] * 5, "mkt_cum": [1.0] * 5}, index=idx)


QUOTE = {"price": 110.0, "open": 101.0, "high": 112.0, "low": 99.0, "vol": 5000.0}


class AppendTodayBarTest(unittest.TestCase):
    def test_todays_bar_is_appended(self) -> None:
        out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {"TCS": QUOTE})
        self.assertEqual(out["TCS"].index[-1], TODAY)
        self.assertEqual(out["TCS"]["close"].iloc[-1], 110.0)

    def test_the_bar_uses_the_real_session_ohlc(self) -> None:
        """Upstox sends a true session open/high/low, so the partial bar is real
        rather than a flat line at the last print."""
        out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {"TCS": QUOTE})
        row = out["TCS"].iloc[-1]
        self.assertEqual((row["open"], row["high"], row["low"]), (101.0, 112.0, 99.0))

    def test_ret1_is_measured_against_the_previous_close(self) -> None:
        out, _ = v2_live.append_today_bar({"TCS": hist(100.0)}, mkt(), "IN", {"TCS": QUOTE})
        self.assertAlmostEqual(out["TCS"]["ret1"].iloc[-1], 0.10)

    def test_a_frozen_quote_is_not_written_in(self) -> None:
        """A stale price recorded as today's action is the same failure the
        entry guard exists to prevent."""
        out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {"TCS": QUOTE},
                                          stale={"TCS"})
        self.assertLess(out["TCS"].index[-1], TODAY)

    def test_a_missing_quote_leaves_history_untouched(self) -> None:
        out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {})
        self.assertLess(out["TCS"].index[-1], TODAY)

    def test_a_zero_or_negative_price_is_rejected(self) -> None:
        for px in (0.0, -5.0, None):
            with self.subTest(price=px):
                out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN",
                                                  {"TCS": dict(QUOTE, price=px)})
                self.assertLess(out["TCS"].index[-1], TODAY)

    def test_no_duplicate_when_the_nightly_job_already_ingested_today(self) -> None:
        """Otherwise the same session appears twice and every rolling window is
        computed over a doubled bar."""
        already = hist(end=TODAY)
        out, _ = v2_live.append_today_bar({"TCS": already}, mkt(), "IN", {"TCS": QUOTE})
        self.assertEqual(len(out["TCS"]), len(already))
        self.assertEqual((out["TCS"].index == TODAY).sum(), 1)

    def test_the_market_index_advances_too(self) -> None:
        """The regime reads mdf. If only the symbols advanced, the lanes would
        score today while the market filter still described yesterday."""
        _out, m = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {"TCS": QUOTE})
        self.assertEqual(m.index[-1], TODAY)
        self.assertAlmostEqual(m["mkt_ret1"].iloc[-1], 0.10)
        self.assertAlmostEqual(m["mkt_cum"].iloc[-1], 1.10)

    def test_the_market_index_is_not_advanced_twice(self) -> None:
        _o, m = v2_live.append_today_bar({"TCS": hist()}, mkt(end=TODAY), "IN", {"TCS": QUOTE})
        self.assertEqual((m.index == TODAY).sum(), 1)

    def test_columns_are_preserved(self) -> None:
        out, _ = v2_live.append_today_bar({"TCS": hist()}, mkt(), "IN", {"TCS": QUOTE})
        self.assertEqual(list(out["TCS"].columns), COLS)

    def test_the_cached_history_is_not_mutated(self) -> None:
        """_hist caches for 6h; mutating it would poison every later call."""
        original = hist()
        before = len(original)
        v2_live.append_today_bar({"TCS": original}, mkt(), "IN", {"TCS": QUOTE})
        self.assertEqual(len(original), before)

    def test_empty_history_is_skipped(self) -> None:
        empty = pd.DataFrame(columns=COLS)
        out, _ = v2_live.append_today_bar({"TCS": empty}, mkt(), "IN", {"TCS": QUOTE})
        self.assertTrue(out["TCS"].empty)


class WiringTest(unittest.TestCase):
    def test_poll_market_appends_and_tags(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("append_today_bar(", src)
        self.assertIn("intraday_bar=today_bar", src)

    def test_only_appended_while_the_market_is_open(self) -> None:
        """After the close the nightly ingest owns the bar; appending a stale
        one on top would duplicate it."""
        import inspect
        self.assertIn("market_open(market)", inspect.getsource(v2_live.poll_market))

    def test_the_flag_can_restore_the_old_behaviour(self) -> None:
        self.assertIsInstance(v2_live.INTRADAY_SIGNAL_BAR, bool)


if __name__ == "__main__":
    unittest.main()
