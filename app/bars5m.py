"""5-minute intraday bars for the Nifty 500, built from the live Upstox feed.

The engine had daily candles and a live snapshot, and nothing in between: it
could see yesterday's close and this instant's price, but not how today got
here. That is why the daily lanes were effectively trading a stale picture.

Rather than add another external fetch, this aggregates the quote feed the
engine already polls every few seconds into proper OHLCV bars. No new API, no
rate limits, and the bars are exactly the prices the engine itself acted on.

Storage is deliberately bounded, not merely small. Measured at ~82 bytes/row,
500 symbols x 75 bars/session is ~3M rows a year; at RETAIN_DAYS the file sits
around 550 MB and stops growing. It lives in its own WAL database so pruning
and VACUUM never block the engine's reads — the 12 GB trading_agent.db is the
cautionary tale for both of those choices.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

_LOG = logging.getLogger("openstocks.bars5m")

IST = timezone(timedelta(hours=5, minutes=30))
DB = os.environ.get("INTRADAY_IN_DB", "/opt/opentrade/var/intraday_in.db")
BAR_SECONDS = 300                      # 5 minutes
RETAIN_DAYS = 180                      # ~7 months of history; keeps the file flat

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
MEMBERS_TTL = 7 * 24 * 3600            # index membership changes rarely
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Referer": "https://www.nseindia.com/"}

_MEMBERS: tuple = (0.0, frozenset())   # (fetched_at, symbols)
_ACC: dict = {}                        # symbol -> [open, high, low, close, vol_first, vol_last]
_WINDOW: int | None = None             # epoch start of the bar being accumulated


def bar_start(now=None):
    """Epoch seconds of the 5-minute window containing `now`."""
    ts = int((now or time.time()))
    return ts - (ts % BAR_SECONDS)


def fetch_members():
    """Nifty 500 symbols from NSE. Returns frozenset(); empty on any failure."""
    try:
        with httpx.Client(headers=UA, timeout=25, follow_redirects=True) as client:
            response = client.get(NIFTY500_URL)
        if response.status_code != 200:
            _LOG.warning("nifty500 list HTTP %s", response.status_code)
            return frozenset()
        rows = response.text.strip().splitlines()[1:]      # drop the header
        out = set()
        for row in rows:
            parts = row.split(",")
            if len(parts) > 2 and parts[2].strip():
                out.add(parts[2].strip().upper())
        return frozenset(out)
    except Exception as exc:
        _LOG.warning("nifty500 list failed: %s", exc)
        return frozenset()


def members(now=None):
    """Cached membership. Falls back to the last good set if NSE is down, so a
    failed refresh never silently shrinks the recorded universe to nothing."""
    global _MEMBERS
    stamp = now or time.time()
    if _MEMBERS[1] and stamp - _MEMBERS[0] < MEMBERS_TTL:
        return _MEMBERS[1]
    fetched = fetch_members()
    if fetched:
        _MEMBERS = (stamp, fetched)
        _LOG.info("nifty500 membership: %d symbols", len(fetched))
    return _MEMBERS[1]


def _connect():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS bars("
                "symbol TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL,"
                " low REAL, close REAL, volume REAL, PRIMARY KEY(symbol, ts))")
    return con


def observe(live, now=None, allowed=None):
    """Fold one quote snapshot into the bar being accumulated.

    Returns the number of completed bars written, which is non-zero only on the
    cycle that crosses a 5-minute boundary.
    """
    global _WINDOW
    written = 0
    window = bar_start(now)
    if _WINDOW is not None and window != _WINDOW:
        written = flush(_WINDOW)
    _WINDOW = window
    universe = allowed if allowed is not None else members()
    if not universe:
        return written
    for symbol, quote in (live or {}).items():
        sym = str(symbol).upper()
        if sym not in universe:
            continue
        price = quote.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        try:
            vol = float(quote.get("vol") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        row = _ACC.get(sym)
        if row is None:
            # volume in the feed is CUMULATIVE for the day, so the bar's volume
            # is the delta across the window, not the value itself.
            _ACC[sym] = [price, price, price, price, vol, vol]
        else:
            row[1] = max(row[1], price)
            row[2] = min(row[2], price)
            row[3] = price
            row[5] = vol
    return written


def flush(window, con=None):
    """Write the accumulated bars for `window` and reset. Returns rows written."""
    if not _ACC:
        return 0
    rows = [(sym, window, r[0], r[1], r[2], r[3], max(0.0, r[5] - r[4]))
            for sym, r in _ACC.items()]
    _ACC.clear()
    owned = con is None
    try:
        con = con or _connect()
        con.executemany("INSERT OR REPLACE INTO bars(symbol,ts,open,high,low,close,volume)"
                        " VALUES(?,?,?,?,?,?,?)", rows)
        con.commit()
        if owned:
            con.close()
        return len(rows)
    except Exception as exc:
        _LOG.warning("bar flush failed: %s", exc)
        return 0


def prune(retain_days=RETAIN_DAYS, now=None, con=None):
    """Drop bars older than the retention window. Returns rows deleted.

    DELETE alone does not shrink a SQLite file — vacuum() below reclaims it, and
    must only run market-closed.
    """
    cutoff = int((now or time.time())) - retain_days * 86400
    owned = con is None
    try:
        con = con or _connect()
        deleted = con.execute("DELETE FROM bars WHERE ts < ?", (cutoff,)).rowcount
        con.commit()
        if owned:
            con.close()
        return deleted
    except Exception as exc:
        _LOG.warning("bar prune failed: %s", exc)
        return 0


def vacuum():
    """Reclaim disk after pruning. Market-closed only — it locks the file."""
    try:
        con = _connect()
        con.execute("VACUUM")
        con.close()
        return True
    except Exception as exc:
        _LOG.warning("bar vacuum failed: %s", exc)
        return False


def stats():
    """(rows, symbols, oldest_ts, newest_ts, bytes) for health reporting."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
        row = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts), MAX(ts) "
                          "FROM bars").fetchone()
        con.close()
        size = os.path.getsize(DB) if os.path.exists(DB) else 0
        return dict(rows=row[0], symbols=row[1], oldest=row[2], newest=row[3], bytes=size)
    except Exception:
        return dict(rows=0, symbols=0, oldest=None, newest=None, bytes=0)
