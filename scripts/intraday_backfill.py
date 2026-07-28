"""Backfill 5-minute NSE history from Yahoo, so intraday strategies can be tested.

This system trades intraday (`volume_surge`) with no intraday history at all,
which means the lane cannot be backtested and nobody can say whether it works.
`intraday_recorder.py` fixes that going forward, but forward-only data means
waiting months before any question can be answered.

Yahoo's chart endpoint serves roughly 60 days of 5-minute bars per symbol with
volume, and is reachable from the deploy host. That is enough to test the
question that actually matters here: can a system reliably take a point or two
out of the day's strongest movers, after costs?

  python3 scripts/intraday_backfill.py --limit 20        # validation run
  python3 scripts/intraday_backfill.py --top 400         # liquid universe

Writes to the same `candles` table under source `yahoo:5m`, so existing
tooling reads it for free. Deliberately polite: one symbol at a time with a
pause, because this is somebody else's free endpoint.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
SOURCE = "yahoo:5m"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def parse_chart(payload):
    """Yahoo chart JSON -> [(iso_ts, open, high, low, close, volume)].

    Yahoo pads the series with nulls for bars it has no data for; those rows
    are dropped rather than stored as zeros, which would look like real
    trades at a price of nothing.
    """
    try:
        result = payload["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars = []
    for i, stamp in enumerate(stamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue
        volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        moment = datetime.fromtimestamp(int(stamp), timezone.utc)
        bars.append((moment.isoformat(), float(o), float(h), float(l), float(c), float(volume)))
    return bars


def ensure_schema(con):
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS candles(
        symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, source TEXT, PRIMARY KEY(symbol, ts, source))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_candles_src_ts ON candles(source, ts)")
    con.commit()


def liquid_symbols(con, limit):
    """Most-traded names first. Intraday momentum is only tradeable where there
    is enough turnover to get filled, so backfilling illiquid tickers wastes
    requests on names no strategy would touch.
    """
    rows = con.execute(
        "SELECT symbol, AVG(close * volume) AS turnover FROM candles "
        "WHERE source='upstox-live:day' AND ts >= date('now','-30 day') "
        "GROUP BY symbol HAVING turnover > 0 ORDER BY turnover DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [r[0] for r in rows]


def store(con, symbol, bars):
    if not bars:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO candles(symbol,ts,open,high,low,close,volume,source) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(symbol, ts, o, h, l, c, v, SOURCE) for ts, o, h, l, c, v in bars])
    con.commit()
    return len(bars)


def backfill(top=400, limit=0, interval="5m", span="60d", pause=0.6, verbose=True):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=20000")
    ensure_schema(con)
    symbols = liquid_symbols(con, top)
    if limit:
        symbols = symbols[:limit]
    if not symbols:
        print("no symbols found — is the daily candle table populated?", flush=True)
        con.close()
        return 0

    stored = failed = 0
    started = time.time()
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        for index, symbol in enumerate(symbols, 1):
            try:
                response = client.get(CHART.format(symbol=symbol),
                                      params={"range": span, "interval": interval})
                if response.status_code != 200:
                    failed += 1
                else:
                    stored += store(con, symbol, parse_chart(response.json()))
            except Exception as exc:
                failed += 1
                if verbose and failed <= 3:
                    print(f"   {symbol}: {type(exc).__name__} {str(exc)[:60]}", flush=True)
            if verbose and index % 25 == 0:
                print(f"   {index}/{len(symbols)} symbols · {stored:,} bars · {failed} failed",
                      flush=True)
            time.sleep(pause)
    total = con.execute("SELECT COUNT(*) FROM candles WHERE source=?", (SOURCE,)).fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT substr(ts,1,10)) FROM candles WHERE source=?",
                       (SOURCE,)).fetchone()[0]
    con.close()
    if verbose:
        print(f"[backfill] {stored:,} bars from {len(symbols)} symbols · {failed} failed "
              f"· table now {total:,} bars across {days} days · {time.time()-started:.0f}s",
              flush=True)
    return stored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=400, help="most-liquid N symbols")
    parser.add_argument("--limit", type=int, default=0, help="cap symbols (validation)")
    parser.add_argument("--interval", default="5m", choices=["1m", "5m", "15m"])
    parser.add_argument("--span", default="60d")
    parser.add_argument("--pause", type=float, default=0.6)
    args = parser.parse_args()
    backfill(top=args.top, limit=args.limit, interval=args.interval,
             span=args.span, pause=args.pause)


if __name__ == "__main__":
    main()
