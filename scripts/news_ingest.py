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


def fetch_earnings(rows):
    """Next-earnings dates from Yahoo's calendar (cookie+crumb session), stored in
    v2_paper.db earnings_calendar for the engine's earnings-proximity gate."""
    import json
    import time as _t
    import urllib.request
    UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        req = urllib.request.Request("https://fc.yahoo.com", headers=UA)
        try:
            opener.open(req, timeout=10)
        except Exception:
            pass                       # 404 is fine; we only need the cookie
        req = urllib.request.Request("https://query2.finance.yahoo.com/v1/test/getcrumb", headers=UA)
        crumb = opener.open(req, timeout=10).read().decode().strip()
    except Exception as exc:
        print(f"earnings: crumb failed: {exc}", flush=True)
        return 0
    v2 = sqlite3.connect(V2_DB, timeout=30)
    v2.execute("CREATE TABLE IF NOT EXISTS earnings_calendar(symbol TEXT PRIMARY KEY, market TEXT,"
               " next_earnings TEXT, fetched_at TEXT)")
    from datetime import datetime, timezone
    got = 0
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        ysym = r.get("yahoo_symbol") or (sym + ".NS" if str(r.get("exchange", "")).upper() in ("NSE", "BSE") else sym)
        market = "IN" if str(r.get("exchange", "")).upper() in ("NSE", "BSE") else "US"
        try:
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ysym}"
                   f"?modules=calendarEvents&crumb={crumb}")
            data = json.loads(opener.open(urllib.request.Request(url, headers=UA), timeout=10).read().decode())
            res = data["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"]
            if res:
                ts = res[0].get("raw")
                if ts:
                    nxt = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                    v2.execute("INSERT INTO earnings_calendar VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET"
                               " market=excluded.market, next_earnings=excluded.next_earnings,"
                               " fetched_at=excluded.fetched_at",
                               (sym, market, nxt, datetime.now(timezone.utc).isoformat()))
                    got += 1
        except Exception:
            pass                        # missing calendar for a symbol is normal
        _t.sleep(0.25)                  # gentle on Yahoo
    v2.commit(); v2.close()
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-earnings", action="store_true")
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
    if not a.skip_earnings:
        n = fetch_earnings(rows)
        print(f"earnings calendar: {n}/{len(rows)} symbols updated", flush=True)


if __name__ == "__main__":
    main()
