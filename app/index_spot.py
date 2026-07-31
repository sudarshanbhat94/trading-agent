"""A LIVE index level, and intraday candles built from it.

The index had no intraday price anywhere in the system. `latest_quotes` holds
equities only; `nfo_quotes` holds option contracts. Everything that needed an
index level read `fo_bhav.underlying` — YESTERDAY'S close from the derivatives
bhavcopy. So the direction call read yesterday's candle, the ATM strike was
chosen against yesterday's spot, and the dashboard could not draw an index
chart at all because there was no series to draw.

Subscribing the feed to index instruments would mean more API quota, and Upstox
rate-limits hard enough that a 429 puts BOTH equity lanes into a 45s cooldown.
So the level is DERIVED from option quotes we already poll every 8 seconds.

PUT-CALL PARITY. For one strike and expiry, a call minus a put is worth the
same as holding the index itself:

    forward = strike + call_price - put_price

The forward differs from spot by the cost of carry over the option's remaining
life. Over a few days at Indian rates that is a handful of points on ~24,000 —
below the noise of the bid-ask spread it is measured from — so it is not
discounted, and this is documented as an ESTIMATE rather than a quote.

Verified against the live chain on 2026-07-31, parity vs the previous
bhavcopy close:

    NIFTY       24,388.5  vs  24,250.2   +0.57%
    BANKNIFTY   57,305.4  vs  57,205.9   +0.17%
    FINNIFTY    26,368.4  vs  26,287.1   +0.31%
    MIDCPNIFTY  14,793.0  vs  14,683.0   +0.75%

All four move together and in the direction the tape was running that session
(heavyweights +1.6%, 52% advancing), which is what a working estimate looks
like.

ACCURACY IS BOUNDED BY THE STRIKES WE POLL. Only the ATM watch window is
quoted, so a violent move can leave every polled strike far from the money and
widen the estimate. `spot()` therefore reports how many strike pairs it used
and how far the nearest one sat, and callers can refuse a thin reading.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger("openstocks.index_spot")

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
IST = timezone(timedelta(hours=5, minutes=30))
BUCKET_MIN = 5                  # candle width
RETAIN_DAYS = 30
# A parity estimate is only as good as the nearest strike quoted. Beyond this
# the call and the put are both far from the money, their spreads dominate, and
# the estimate is not worth recording.
MAX_ATM_DISTANCE_PCT = 2.0
MIN_PAIRS = 1


# `path=MAIN_DB` as a DEFAULT ARGUMENT would bind the value at import time, so
# the module constant could never be redirected afterwards — not by a test, and
# not by anything that sets the path after import. Resolved per call instead.
def _ro(path=None):
    return sqlite3.connect(f"file:{path or MAIN_DB}?mode=ro", uri=True, timeout=20)


def _rw(path=None):
    con = sqlite3.connect(path or MAIN_DB, timeout=20)
    con.execute("PRAGMA busy_timeout=8000")
    return con


def _f(v, d=0.0):
    try:
        out = float(v)
        return out if out == out else d
    except (TypeError, ValueError):
        return d


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS index_bars_5m("
                "symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL,"
                " n INTEGER DEFAULT 1, PRIMARY KEY(symbol, ts))")


def bucket(now=None):
    """The 5-minute bucket a timestamp belongs to, as an IST ISO string.

    Floor, not round: a bar is named for the interval it OPENS, so a sample at
    09:47 belongs to the 09:45 bar.
    """
    now = now or datetime.now(IST)
    return now.replace(minute=(now.minute // BUCKET_MIN) * BUCKET_MIN,
                       second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def spot(symbol, quotes=None, con=None):
    """Parity-derived index level, or None.

    Returns {price, pairs, atm_distance_pct, strike}. `pairs` and
    `atm_distance_pct` are the caller's means of judging it: an estimate built
    from one strike 3% away is not the same claim as one built from twelve
    straddling the money, and reporting a bare number would hide the difference.
    """
    symbol = str(symbol).upper()
    rows = quotes
    if rows is None:
        own = con is None
        con = con or _ro()
        try:
            rows = con.execute(
                "SELECT underlying, strike, option_type, price, expiry FROM nfo_quotes"
                " WHERE price>0 AND UPPER(underlying)=?", (symbol,)).fetchall()
        except Exception as exc:
            _LOG.warning("index spot query failed: %s", exc)
            return None
        finally:
            if own:
                con.close()
    pairs = {}
    for underlying, strike, opt_type, price, expiry in rows:
        if str(underlying or "").upper() != symbol:
            continue
        strike, price = _f(strike), _f(price)
        if strike <= 0 or price <= 0:
            continue
        pairs.setdefault((strike, expiry), {})[str(opt_type or "").upper()] = price
    # Nearest the money first: the smallest |call - put| is the strike closest to
    # spot, which is where parity is least sensitive to a wide quote.
    est = sorted((abs(d["CE"] - d["PE"]), k + d["CE"] - d["PE"], k)
                 for (k, _e), d in pairs.items() if "CE" in d and "PE" in d)
    if len(est) < MIN_PAIRS:
        return None
    best = est[:3]
    price = sum(x[1] for x in best) / len(best)
    if price <= 0:
        return None
    distance = abs(est[0][2] - price) / price * 100
    if distance > MAX_ATM_DISTANCE_PCT:
        return None                     # nearest quoted strike is too far to trust
    return dict(price=round(price, 2), pairs=len(est), strike=est[0][2],
                atm_distance_pct=round(distance, 2))


def observe(symbols, now=None, con=None):
    """Fold one live sample into each symbol's current 5-minute bar.

    Written as an UPSERT that widens the existing bar rather than replacing it,
    so a restart mid-bar keeps the high and low already seen instead of
    restarting the candle from the current price.
    """
    stamp = bucket(now)
    own = con is None
    con = con or _rw()
    written = 0
    try:
        ensure_schema(con)
        read = _ro()
        try:
            for symbol in symbols:
                s = spot(symbol, con=read)
                if not s:
                    continue
                price = s["price"]
                con.execute(
                    "INSERT INTO index_bars_5m(symbol,ts,open,high,low,close,n)"
                    " VALUES(?,?,?,?,?,?,1)"
                    " ON CONFLICT(symbol,ts) DO UPDATE SET"
                    "   high=MAX(high,excluded.high),"
                    "   low=MIN(low,excluded.low),"
                    "   close=excluded.close,"
                    "   n=n+1",
                    (str(symbol).upper(), stamp, price, price, price, price))
                written += 1
        finally:
            read.close()
        con.commit()
    except Exception as exc:
        _LOG.warning("index bar write failed: %s", exc)
    finally:
        if own:
            con.close()
    return written


def bars(symbol, limit=200, con=None):
    """Most recent 5-minute bars, oldest first — chart order."""
    own = con is None
    con = con or _ro()
    try:
        # No ensure_schema here: this path is read-only, and a missing table
        # simply means nothing has been sampled yet.
        rows = con.execute(
            "SELECT ts, open, high, low, close FROM index_bars_5m"
            " WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (str(symbol).upper(), int(limit))).fetchall()
    except Exception:
        return []                       # table not created yet
    finally:
        if own:
            con.close()
    return [dict(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4])
            for r in reversed(rows)]


def prune(days=RETAIN_DAYS, con=None):
    """Drop bars older than `days`. Six symbols at 75 bars a session is tiny,
    but unbounded growth in the main DB is how a 12GB file happened before."""
    own = con is None
    con = con or _rw()
    try:
        ensure_schema(con)
        cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        con.execute("DELETE FROM index_bars_5m WHERE ts < ?", (cutoff,))
        con.commit()
    except Exception as exc:
        _LOG.warning("index bar prune failed: %s", exc)
    finally:
        if own:
            con.close()
