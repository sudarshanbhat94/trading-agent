"""Standalone daily-candle ingestion for the v2 engine.

The v2 engine scores every signal from the `candles` table (DAILY_SOURCE =
upstox-live:day / alpaca-iex-live:day). That table was only ever refreshed by the
old heavy agent; when the agent was disabled (for CPU), candle ingestion silently
died and the engine has been running on a frozen history snapshot.

This replaces it with a lean, run-once batch (like scripts/v2_quote_feed.py): for
each market it fetches DAILY candles for the top-liquid universe and upserts them.
Run it once per session close via a systemd timer; it exits when done, so it can
never peg CPU like the in-loop agent did.

  python3 scripts/candle_ingest.py --once --limit 40        # validate on a small batch
  python3 scripts/candle_ingest.py --market IN              # full IN ingest
  python3 scripts/candle_ingest.py                          # both markets, full
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings, settings_from_overrides   # noqa: E402
from app.db import Database                                 # noqa: E402
from app.market_data import build_market_data_provider      # noqa: E402

MARKETS = {"IN": "upstox", "US": "alpaca"}
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
TOPN = 100000   # ingest the whole enabled universe (held names are forced to the front)


def _held(market):
    """Symbols we currently hold — must always be ingested fresh, regardless of
    where they fall in the (alphabetical) universe order."""
    out = set()
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True, timeout=5)
        for (sym,) in c.execute("SELECT symbol FROM v2_positions WHERE market=?", (market,)):
            out.add(str(sym).upper())
        c.close()
    except Exception:
        pass
    return out


def _daily_only(candles_by_symbol):
    """Keep only DAILY bars (source endswith ':day') — discard any intraday the
    provider also returns, so we don't bloat the DB with minute candles."""
    out = {}
    for sym, candles in candles_by_symbol.items():
        daily = [c for c in candles if str(getattr(c, "source", "")).endswith(":day")]
        if daily:
            out[sym] = daily
    return out


def _market_settings(settings, market, prov):
    """Force DAILY candles as the provider's primary interval (the engine reads
    `:day`), and turn off multi-timeframe so we don't also pull heavy intraday."""
    s = replace(settings, market_region=market, market_data_provider=prov)
    if prov == "upstox":
        # multi-timeframe ON yields the full 420-day :day series (the engine's
        # source); we discard the 30min/week bars on upsert via _daily_only.
        s = replace(s, upstox_candle_interval="30minute",
                    enable_upstox_multi_timeframe_candles=True,
                    upstox_daily_candle_lookback_days=420)
    return s


def ingest(market, prov, db, settings, limit=0, fetch_batch=150, pause=2.0):
    provider = build_market_data_provider(_market_settings(settings, market, prov))
    rows = db.get_universe(enabled_only=True, market_region=market)
    held = _held(market)
    t0 = time.time()
    syms_done = candles_done = 0

    # 1) GUARANTEED held pass: positions we hold must be fresh. Small dedicated
    # batch with a retry, since large universe runs drop ~30-50% of symbols.
    held_rows = [r for r in rows if str(r.get("symbol", "")).upper() in held]
    if held_rows:
        for attempt in range(2):
            daily = _daily_only(asyncio.run(provider.get_candles(held_rows)))
            if daily:
                db.upsert_candles(daily)
                print(f"[{market}] held pass: {len(daily)}/{len(held_rows)} held symbols fresh", flush=True)
                break
            time.sleep(3)

    # 2) broad universe pass (held first so any partial coverage still favours them)
    rows.sort(key=lambda r: 0 if str(r.get("symbol", "")).upper() in held else 1)
    rows = rows[: (limit or TOPN)]
    if not rows:
        print(f"[{market}] no universe rows", flush=True)
        return 0
    # Fetch in batches with a short pause so the historical API doesn't rate-limit
    # the whole burst (Upstox capped a 1600-symbol burst at ~70 symbols).
    for b in range(0, len(rows), fetch_batch):
        batch = rows[b:b + fetch_batch]
        try:
            fetched = asyncio.run(provider.get_candles(batch))
        except Exception as exc:
            print(f"[{market}] batch {b} fetch error: {exc}", flush=True)
            continue
        daily = _daily_only(fetched)
        items = list(daily.items())
        for i in range(0, len(items), 40):   # chunked upsert -> short DB locks
            part = dict(items[i:i + 40])
            db.upsert_candles(part)
            candles_done += sum(len(v) for v in part.values())
            time.sleep(0.05)
        syms_done += len(daily)
        print(f"[{market}] {b + len(batch)}/{len(rows)} fetched -> {syms_done} symbols so far", flush=True)
        time.sleep(pause)
    print(f"[{market}] DONE {candles_done} daily candles for {syms_done}/{len(rows)} symbols in {time.time()-t0:.1f}s", flush=True)
    return syms_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["IN", "US"], default=None)
    ap.add_argument("--limit", type=int, default=0, help="cap symbols (for testing)")
    ap.add_argument("--once", action="store_true", help="no-op flag for clarity; this script is always run-once")
    a = ap.parse_args()
    base = Settings()
    db = Database(base.database_path)
    settings = settings_from_overrides(base, db.runtime_settings())
    targets = [a.market] if a.market else list(MARKETS)
    print(f"candle ingest start {datetime.now(timezone.utc).isoformat()[:19]}Z markets={targets} limit={a.limit or TOPN}", flush=True)
    for m in targets:
        try:
            ingest(m, MARKETS[m], db, settings, limit=a.limit)
        except Exception as exc:
            print(f"[{m}] ingest failed: {exc}", flush=True)
    print("candle ingest done", flush=True)


if __name__ == "__main__":
    main()
