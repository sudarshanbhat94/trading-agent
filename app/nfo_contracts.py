"""Pick the handful of index option contracts worth streaming live.

The quote feed had no derivatives at all, so the options engine could open a
position and then never see it again until the next day's bhavcopy. A weekly
option moves 30-50% on a 1% index move, so that is not a gap in reporting — it
means a position cannot be exited.

Deliberately NOT solved by adding contracts to the `universe` table. That table
feeds `get_universe()`, which is what the equity lanes screen over: a NIFTY
option sitting in it would become a buy candidate for swing_meanrev. These rows
are built in memory and handed straight to the provider, so options are visible
to the options code and invisible to everything else.

Scope is a few strikes around the money on the nearest expiry — roughly 14
contracts per index rather than the 1,549 NIFTY has listed. The feed is already
rate-limited by Upstox (429s trigger a 45s cooldown for BOTH lanes), so asking
for everything would cost equity quotes to serve options nobody is holding.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import time

import httpx

_LOG = logging.getLogger("openstocks.nfo")

MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
MASTER_TTL = 12 * 3600          # contracts change on expiry, not intraday
STRIKES_EACH_SIDE = 3           # ATM +/- 3, both CE and PE

_MASTER: tuple = (0.0, [])      # (fetched_at, rows)


def fetch_master(url=MASTER_URL, timeout=120):
    """All NSE_FO instruments from Upstox. Returns [] on any failure."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code != 200:
            _LOG.warning("instrument master HTTP %s", response.status_code)
            return []
        text = gzip.decompress(response.content).decode("utf-8", "replace")
        return [row for row in csv.DictReader(io.StringIO(text))
                if (row.get("exchange") or "").strip() == "NSE_FO"]
    except Exception as exc:
        _LOG.warning("instrument master failed: %s", exc)
        return []


def master(now=None):
    """Cached NSE_FO instrument rows. Keeps the last good copy on failure, so a
    single fetch error does not blank the option feed for the rest of the day."""
    global _MASTER
    stamp = now or time.time()
    if _MASTER[1] and stamp - _MASTER[0] < MASTER_TTL:
        return _MASTER[1]
    rows = fetch_master()
    if rows:
        _MASTER = (stamp, rows)
    return _MASTER[1]


def _f(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def nearest_expiry(rows, today=None):
    """Earliest expiry not already past. Options expire; a stale expiry would
    stream contracts that no longer trade."""
    today = today or time.strftime("%Y-%m-%d")
    future = sorted({(r.get("expiry") or "")[:10] for r in rows
                     if (r.get("expiry") or "")[:10] >= today})
    return future[0] if future else None


def select(symbol, spot, rows=None, each_side=STRIKES_EACH_SIDE, today=None):
    """Rows for the provider: ATM +/- `each_side` strikes, CE and PE, nearest expiry.

    Each row carries `exchange: NSE` so the region router sends it to the India
    provider — the routing keys off exchange, and NSE_FO is not in
    INDIA_EXCHANGES.
    """
    rows = rows if rows is not None else master()
    if not rows or not spot or spot <= 0:
        return []
    mine = [r for r in rows if (r.get("name") or "").strip().upper() == symbol.upper()
            and (r.get("option_type") or "").strip() in ("CE", "PE")]
    if not mine:
        return []
    expiry = nearest_expiry(mine, today)
    if not expiry:
        return []
    mine = [r for r in mine if (r.get("expiry") or "")[:10] == expiry]
    strikes = sorted({_f(r.get("strike")) for r in mine if _f(r.get("strike"))})
    if not strikes:
        return []
    atm = min(strikes, key=lambda k: abs(k - spot))
    index = strikes.index(atm)
    wanted = set(strikes[max(0, index - each_side): index + each_side + 1])
    out = []
    for r in mine:
        strike = _f(r.get("strike"))
        key = (r.get("instrument_key") or "").strip()
        ticker = (r.get("tradingsymbol") or "").strip().upper()
        if strike not in wanted or not key or not ticker:
            continue
        out.append({
            "symbol": ticker,
            "upstox_instrument_key": key,
            "exchange": "NSE",           # routes to the India provider
            "expiry": expiry,
            "strike": strike,
            "option_type": (r.get("option_type") or "").strip(),
            "lot_size": _f(r.get("lot_size"), 0.0),
            "underlying": symbol.upper(),
        })
    return sorted(out, key=lambda r: (r["strike"], r["option_type"]))


def select_many(spots, each_side=STRIKES_EACH_SIDE, today=None):
    """{symbol: spot} -> flat list of contract rows for every index."""
    rows = master()
    out = []
    for symbol, spot in (spots or {}).items():
        out.extend(select(symbol, spot, rows=rows, each_side=each_side, today=today))
    return out
