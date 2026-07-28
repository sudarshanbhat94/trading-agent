"""NSE pre-open call-auction data — what is gapping BEFORE the bell.

NSE runs a call auction 09:00-09:08 and publishes an indicative opening price
per symbol. Until now the engine only knew yesterday's close, so at 09:15 it
had to wait for live ticks to discover that a stock had gapped 6% — by which
time the move it was ranking had already happened.

This reads the auction result once during the pre-open window and caches it in
memory for the session. Deliberately memory-only: the figures are indicative,
they are superseded by real ticks minutes later, and writing them into
latest_quotes would let a stale auction price masquerade as a live quote.

Fetching is best-effort. NSE rate-limits and returns 503 under load, so every
failure degrades to "no pre-open data" and the engine behaves exactly as it did
before. It must never be able to block or break the open.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

_LOG = logging.getLogger("openstocks.preopen")

IST = timezone(timedelta(hours=5, minutes=30))
URL = "https://www.nseindia.com/api/market-data-pre-open?key=ALL"
HOME = "https://www.nseindia.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.nseindia.com/"}

# session date -> {symbol: {...}}. One session's worth; replaced next morning.
_CACHE: dict = {}


def parse(payload):
    """NSE's pre-open JSON -> {symbol: {open, prev_close, gap_pct, qty, value}}.

    Shape is `{"data": [{"metadata": {...}}, ...]}`. Every field is coerced and
    a row that cannot be read is skipped rather than aborting the batch — one
    malformed entry must not cost us the other 1,900.
    """
    out = {}
    for row in (payload or {}).get("data") or []:
        meta = row.get("metadata") or row
        symbol = str(meta.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        try:
            price = float(meta.get("lastPrice") or 0)
            prev = float(meta.get("previousClose") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or prev <= 0:
            continue          # no auction match for this name — not a zero gap
        try:
            qty = float(meta.get("finalQuantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            value = float(meta.get("totalTurnover") or 0)
        except (TypeError, ValueError):
            value = 0.0
        out[symbol] = dict(open=price, prev_close=prev,
                           gap_pct=round((price / prev - 1) * 100, 2),
                           qty=qty, value=value)
    return out


def fetch(timeout=15):
    """Pull the auction snapshot. Returns {} on any failure — never raises."""
    try:
        with httpx.Client(headers=UA, timeout=timeout, follow_redirects=True) as client:
            client.get(HOME)                      # bootstrap cookies, as NSE requires
            response = client.get(URL)
            if response.status_code != 200:
                _LOG.warning("pre-open fetch HTTP %s", response.status_code)
                return {}
            return parse(response.json())
    except Exception as exc:                      # network, JSON, cookie wall
        _LOG.warning("pre-open fetch failed: %s", exc)
        return {}


def refresh(now=None):
    """Fetch once per session and cache. Returns the cached map."""
    session = (now or datetime.now(IST)).date().isoformat()
    if session in _CACHE:
        return _CACHE[session]
    data = fetch()
    if data:                        # only cache a real result, so a 503 retries
        _CACHE.clear()              # one session at a time
        _CACHE[session] = data
        _LOG.info("pre-open: %d symbols, %d gapping >2%%", len(data),
                  sum(1 for v in data.values() if abs(v["gap_pct"]) >= 2))
    return data


def cached(now=None):
    """Today's pre-open map, or {} if it was never fetched."""
    return _CACHE.get((now or datetime.now(IST)).date().isoformat(), {})


def gappers(min_gap=2.0, min_value=1e6, min_price=50.0, limit=25, now=None):
    """Biggest UP gaps with real auction participation, strongest first.

    Two filters, both learned from the live snapshot rather than assumed:

    * min_value drops names whose "gap" came from a handful of shares crossing
      in the auction — PPSL printed +15.6% on Rs 3.5 lakh of turnover. Those
      prices do not survive the open, and chasing them is how a paper book
      records fills that never existed.
    * min_price drops sub-Rs 50 names (DHARAN auctions at Rs 0.16, where one
      tick IS a 6% move). This mirrors MIN_PRICE in the engine; it is repeated
      here rather than imported to keep this module free of engine imports.
    """
    rows = [dict(symbol=s, **v) for s, v in cached(now).items()
            if v["gap_pct"] >= min_gap and v["value"] >= min_value
            and v["open"] >= min_price]
    rows.sort(key=lambda r: -r["gap_pct"])
    return rows[:limit]
