"""Standalone news/sentiment ingestion for the v2 engine's news veto.

The engine's "never buy into fraud/regulatory/downgrade news" gate reads
sentiment_events, which was written only by the old agent — when the agent was
disabled the pipeline silently died (last row 2026-06-19) and the veto became a
no-op. This lean batch revives it: fetch + classify headlines (lexical only, no
LLM) for the symbols that actually matter — held positions, current engine
candidates, and the user watchlist — and let SentimentService persist the events.

Run every ~45 min via opentrade-news.timer. Bounded (<100 symbols) and run-once,
so it can never peg CPU or starve the web app.

  python3 scripts/news_ingest.py            # one pass
  python3 scripts/news_ingest.py --limit 10 # test on a few symbols
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings, settings_from_overrides   # noqa: E402
from app.db import Database                                 # noqa: E402
from app.sentiment import SentimentService                  # noqa: E402

V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")


def target_symbols():
    """Held + engine radar (latest signals) + user watchlist — the only names
    whose news can change a buy/sell decision."""
    syms = set()
    try:
        v2 = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True, timeout=10)
        for (s,) in v2.execute("SELECT symbol FROM v2_positions"):
            syms.add(str(s).upper())
        try:
            for (s,) in v2.execute("SELECT symbol FROM v2_watch_user"):
                syms.add(str(s).upper())
        except sqlite3.OperationalError:
            pass
        try:
            d = v2.execute("SELECT MAX(date) FROM v2_signals").fetchone()[0]
            if d:
                for (s,) in v2.execute("SELECT DISTINCT symbol FROM v2_signals WHERE date=?", (d,)):
                    syms.add(str(s).upper())
        except sqlite3.OperationalError:
            pass
        v2.close()
    except Exception as exc:
        print(f"target symbols failed: {exc}", flush=True)
    return syms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    base = Settings()
    db = Database(base.database_path)
    settings = settings_from_overrides(base, db.runtime_settings())
    # force-enable: runtime overrides may have news sentiment off from the old-agent
    # cleanup, but the v2 veto depends on these events existing
    settings = replace(settings, enable_news_sentiment=True)
    svc = SentimentService(settings, db)
    want = target_symbols()
    rows = [r for r in db.get_universe(enabled_only=True) if str(r.get("symbol", "")).upper() in want]
    if a.limit:
        rows = rows[: a.limit]
    print(f"news ingest: {len(rows)} symbols (held+radar+watchlist)", flush=True)
    if not rows:
        return
    res = asyncio.run(svc.refresh_watchlist_news(rows, limit=len(rows), allow_llm=False, reason="v2_news_ingest"))
    print({k: res.get(k) for k in ("symbols_requested", "symbols_refreshed", "events_found", "headlines_found")},
          flush=True)


if __name__ == "__main__":
    main()
