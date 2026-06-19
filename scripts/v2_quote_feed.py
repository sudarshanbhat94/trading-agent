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

MARKETS = {"IN": "upstox", "US": "alpaca"}


def _build():
    base = Settings()
    db = Database(base.database_path)
    settings = settings_from_overrides(base, db.runtime_settings())
    providers, rows = {}, {}
    for m, prov in MARKETS.items():
        try:
            providers[m] = build_market_data_provider(replace(settings, market_region=m, market_data_provider=prov))
            rows[m] = db.get_universe(enabled_only=True, market_region=m)
        except Exception as exc:
            print(f"[{m}] provider build failed: {exc}")
    return db, providers, rows


def _poll(db, providers, rows):
    for m in MARKETS:
        if m not in providers:
            continue
        try:
            if not market_regions.market_session_for_region(m).get("is_open"):
                continue
            quotes = asyncio.run(providers[m].get_quotes(rows[m]))
            if quotes:
                db.upsert_quotes(quotes)
            print(f"  [{m}] {len(quotes)} quotes @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        except Exception as exc:
            print(f"  [{m}] fetch error: {exc}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args()
    db, providers, rows = _build()
    print(f"feeder up: providers={list(providers)} universe="
          f"{ {m: len(rows.get(m, [])) for m in providers} }", flush=True)
    while True:
        t0 = time.time()
        _poll(db, providers, rows)
        if not a.loop:
            break
        time.sleep(max(2, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
