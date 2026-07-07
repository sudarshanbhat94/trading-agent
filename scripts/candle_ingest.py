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


DAILY_SRC = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}


def _fresh_symbols(db, market, days=4):
    """Symbols whose daily candles are already current (a bar within `days`).
    Lets reruns/retry passes target only the stale tail."""
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    out = set()
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{db.path}?mode=ro", uri=True, timeout=30)
        for sym, mx in con.execute("SELECT symbol, MAX(ts) FROM candles WHERE source=? GROUP BY symbol",
                                   (DAILY_SRC[market],)):
            if mx and str(mx)[:10] >= cutoff:
                out.add(str(sym).upper())
        con.close()
    except Exception:
        pass
    return out


def _fetch_upsert(provider, db, batch):
    """One batch: fetch, keep :day, chunked upsert. Returns set of covered symbols
    and the candle count."""
    fetched = asyncio.run(provider.get_candles(batch))
    daily = _daily_only(fetched)
    items = list(daily.items())
    n = 0
    for i in range(0, len(items), 40):     # chunked upsert -> short DB locks
        part = dict(items[i:i + 40])
        db.upsert_candles(part)
        n += sum(len(v) for v in part.values())
        time.sleep(0.05)
    return {str(s).upper() for s in daily}, n


def ingest(market, prov, db, settings, limit=0, fetch_batch=40, pause=2.0, max_passes=3):
    """Provider bursts get silently truncated (a 150-symbol batch returns ~70), so:
    small batches + skip already-fresh symbols + RETRY PASSES over the stale tail
    until coverage converges. Held positions are always done first."""
    provider = build_market_data_provider(_market_settings(settings, market, prov))
    rows = db.get_universe(enabled_only=True, market_region=market)[: (limit or TOPN)]
    held = _held(market)
    if not rows:
        print(f"[{market}] no universe rows", flush=True)
        return 0
    t0 = time.time()
    candles_done = 0

    held_rows = [r for r in rows if str(r.get("symbol", "")).upper() in held]
    if held_rows:
        for attempt in range(2):
            got, n = _fetch_upsert(provider, db, held_rows)
            candles_done += n
            if len(got) == len(held_rows):
                break
            time.sleep(3)
        print(f"[{market}] held pass: {len(got)}/{len(held_rows)} held symbols fresh", flush=True)

    fresh = _fresh_symbols(db, market)
    todo = [r for r in rows if str(r.get("symbol", "")).upper() not in fresh]
    print(f"[{market}] universe={len(rows)} already-fresh={len(rows)-len(todo)} to-fetch={len(todo)}", flush=True)
    covered = set()
    for pass_no in range(1, max_passes + 1):
        if not todo:
            break
        missed = []
        for b in range(0, len(todo), fetch_batch):
            batch = todo[b:b + fetch_batch]
            try:
                got, n = _fetch_upsert(provider, db, batch)
            except Exception as exc:
                print(f"[{market}] pass{pass_no} batch {b} error: {exc}", flush=True)
                got, n = set(), 0
            candles_done += n
            covered |= got
            missed.extend(r for r in batch if str(r.get("symbol", "")).upper() not in got)
            if (b // fetch_batch) % 10 == 0:
                print(f"[{market}] pass{pass_no} {b + len(batch)}/{len(todo)} -> covered {len(covered)}", flush=True)
            time.sleep(pause)
        print(f"[{market}] pass{pass_no} done: covered {len(covered)}/{len(todo) if pass_no == 1 else '-'} · missed {len(missed)}", flush=True)
        todo = missed
        pause = min(pause * 1.5, 6.0)      # back off between retry passes
    print(f"[{market}] DONE {candles_done} candles · covered {len(covered)} · unrecoverable {len(todo)} · {time.time()-t0:.1f}s", flush=True)
    return len(covered)


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
