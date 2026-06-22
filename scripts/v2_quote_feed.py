"""Standalone lean quote feeder for the v2 engine.

Replaces the heavy old agent as the live-price source. Runs as its OWN process,
so it can't block the web app. Every `interval` seconds, for each OPEN market, it
fetches quotes for the enabled (liquid) universe via the existing market-data
provider and upserts them to latest_quotes - exactly what the v2 engine reads.

  python3 scripts/v2_quote_feed.py --once          # test one fetch
  python3 scripts/v2_quote_feed.py --loop --interval 10
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
from app import market_regions                              # noqa: E402

import sqlite3
import threading

MARKETS = {"IN": "upstox", "US": "alpaca"}
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")


def _build():
    base = Settings()
    db = Database(base.database_path)
    settings = settings_from_overrides(base, db.runtime_settings())
    providers, rows, symmap = {}, {}, {}
    for m, prov in MARKETS.items():
        try:
            providers[m] = build_market_data_provider(replace(settings, market_region=m, market_data_provider=prov))
            rows[m] = db.get_universe(enabled_only=True, market_region=m)
            symmap[m] = {str(r.get("symbol") or "").upper(): r for r in rows[m]}
        except Exception as exc:
            print(f"[{m}] provider build failed: {exc}")
    return db, providers, rows, symmap


def _held():
    """Symbols we currently hold, per market — the 'hot' set polled every tick so
    open-position prices and P&L move in near real time."""
    out = {m: set() for m in MARKETS}
    try:
        c = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True, timeout=5)
        for m, sym in c.execute("SELECT market,symbol FROM v2_positions"):
            out.setdefault(m, set()).add(str(sym).upper())
        c.close()
    except Exception:
        pass
    return out


def _poll(db, providers, rows_for, label):
    for m, rws in rows_for.items():
        if m not in providers or not rws:
            continue
        try:
            if not market_regions.market_session_for_region(m).get("is_open"):
                continue
            quotes = asyncio.run(providers[m].get_quotes(rws))
            if quotes:
                db.upsert_quotes(quotes)
            if label:
                print(f"  [{m}/{label}] {len(quotes)} quotes @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        except Exception as exc:
            print(f"  [{m}] fetch error: {exc}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)        # hot (held) cadence
    ap.add_argument("--full-interval", type=float, default=15.0)  # whole-universe cadence
    a = ap.parse_args()

    if a.once:
        db, providers, rows, symmap = _build()
        print(f"feeder once: universe={ {m: len(rows.get(m, [])) for m in providers} }", flush=True)
        _poll(db, providers, rows, "full")
        return

    # Two independent workers, each with its OWN db+providers (thread isolation):
    #  - hot: re-polls only the symbols we HOLD every ~1s -> per-second P&L.
    #  - full: refreshes the whole universe on a slower cadence (movers/analysis).
    # The slow full poll runs in its own thread so it can NEVER stall the hot lane.
    def _hot_worker():
        db, providers, _rows, symmap = _build()
        print(f"hot worker up @ {a.interval}s", flush=True)
        while True:
            t0 = time.time()
            held = _held()
            hot = {m: [symmap[m][s] for s in held.get(m, ()) if s in symmap.get(m, {})] for m in MARKETS}
            if any(hot.values()):
                _poll(db, providers, hot, "")
            time.sleep(max(0.2, a.interval - (time.time() - t0)))

    def _full_worker():
        db, providers, rows, _symmap = _build()
        print(f"full worker up @ {a.full_interval}s universe={ {m: len(rows.get(m, [])) for m in providers} }", flush=True)
        n = 0
        while True:
            t0 = time.time()
            _poll(db, providers, rows, "full" if n % 8 == 0 else "")
            n += 1
            time.sleep(max(1.0, a.full_interval - (time.time() - t0)))

    t = threading.Thread(target=_hot_worker, daemon=True)
    t.start()
    _full_worker()


if __name__ == "__main__":
    main()
