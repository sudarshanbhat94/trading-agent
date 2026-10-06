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
from contextlib import closing
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings, settings_from_overrides   # noqa: E402
from app.db import Database                                 # noqa: E402
from app.market_data import build_market_data_provider      # noqa: E402
from app.v2_live import ENABLED_MARKETS                     # noqa: E402

# Only ingest candles for markets the engine actually trades (US parked -> no US
# ingestion). Re-enable US in one place: v2_live.ENABLED_MARKETS.
MARKETS = {m: p for m, p in {"IN": "upstox", "US": "alpaca"}.items() if m in ENABLED_MARKETS}
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
TOPN = 100000   # ingest the whole enabled universe (held names are forced to the front)


def _held(market):
    """Symbols we currently hold — must always be ingested fresh, regardless of
    where they fall in the (alphabetical) universe order."""
    from scripts.v2_quote_feed import _held as exposure
    return exposure(path=V2_DB).get(market, set())


def _daily_only(candles_by_symbol, target=None):
    """Keep only DAILY bars (source endswith ':day') — discard any intraday the
    provider also returns, so we don't bloat the DB with minute candles."""
    out = {}
    for sym, candles in candles_by_symbol.items():
        def completed(c):
            try:
                at = datetime.fromisoformat(str(c.ts).replace('Z', '+00:00'))
                day = at.astimezone(IST).date() if at.tzinfo else at.date()
                return not target or day.isoformat() <= target
            except (ValueError, TypeError, AttributeError):
                return False
        daily = [c for c in candles if str(getattr(c, "source", "")).endswith(":day") and completed(c)]
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


IST = timezone(timedelta(hours=5, minutes=30))
SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE = 15, 30    # NSE closes 15:30 IST


def expected_session(now=None):
    """The most recent trading session that has CLOSED, as YYYY-MM-DD.

    This is the freshness target. It must come from the calendar, not from the
    database — see _fresh_symbols for why.

    Uses the shared notified NSE holiday list for regular sessions. Exceptional
    sessions and execution permission still require the sourced catalogue.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    closed_today = (now.hour, now.minute) >= (SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE)
    candidate = now.date() if closed_today else (now.date() - timedelta(days=1))
    from app.market_regions import INDIA_TRADING_HOLIDAYS
    while candidate.weekday() >= 5 or candidate.isoformat() in INDIA_TRADING_HOLIDAYS:
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def _fresh_symbols(db, market, target=None, symbols=None):
    """Symbols already current to `target` (default: the last closed session).

    History of this function, because it has now been wrong twice in different
    ways:

    1. 'has a bar within 4 CALENDAR days' — perpetually lagged. On a Tuesday the
       window reached back to Friday, so a symbol holding Friday's bar counted
       as fresh and was never re-fetched.
    2. 'the newest trading day ANY symbol in this source has' — a SELF-
       REFERENTIAL target. Once the table sits at day D every symbol matches D,
       so `todo` is empty and NOTHING is fetched, which keeps the table at D
       forever. The only escape was the held-symbol force pass happening to pull
       a newer bar, so ingestion advanced only when a position was open and the
       provider had already published. Observed live: both of 2026-07-23's runs
       fetched nothing, and Friday 07-24's bar did not land until Monday 16:00
       IST — meaning the engine scored Monday's session on Thursday's closes.

    The target is now the calendar's last closed session, so a run that is
    behind always has work to do.
    """
    out = set()
    target = target or expected_session()
    try:
        import sqlite3 as _sq
        with closing(_sq.connect(f"file:{db.path}?mode=ro", uri=True, timeout=30)) as con:
            # A future/partial session cannot stand in for the target session.
            next_day = (datetime.fromisoformat(target).date()+timedelta(days=1)).isoformat()
            selected = list(symbols) if symbols is not None else None
            chunks = [None] if selected is None else [selected[i:i+500] for i in range(0,len(selected),500)]
            for chunk in chunks:
                suffix = '' if chunk is None else ' AND symbol IN ('+','.join('?' for _ in chunk)+')'
                for (sym,) in con.execute("SELECT DISTINCT symbol FROM candles WHERE source=? AND ts>=? AND ts<?"+suffix,
                    [DAILY_SRC[market], target, next_day]+(chunk or [])):
                    out.add(str(sym).upper())
    except Exception as exc:
        # Returning an empty set means "nothing is fresh", so the run re-fetches
        # everything. That is the safe direction to fail, but say so — silently
        # doing a full pass has looked like healthy behaviour before.
        print(f"[{market}] freshness check failed ({exc}); treating all symbols as stale", flush=True)
    return out


def _source_max_ts(db, market):
    """Newest daily bar currently stored, for before/after reporting."""
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{db.path}?mode=ro", uri=True, timeout=30)
        row = con.execute("SELECT MAX(ts) FROM candles WHERE source=?", (DAILY_SRC[market],)).fetchone()
        con.close()
        return str(row[0])[:10] if row and row[0] else None
    except Exception:
        return None


def _fetch_upsert(provider, db, batch, target=None):
    """One batch: fetch, keep :day, chunked upsert. Returns set of covered symbols
    and the candle count."""
    fetched = asyncio.run(provider.get_candles(batch))
    daily = _daily_only(fetched, target)
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
    target = expected_session()
    rows = db.get_universe(enabled_only=True, market_region=market)[: (limit or TOPN)]
    held = _held(market)
    from scripts.v2_quote_feed import _quote_map
    all_rows = _quote_map(db.get_universe(enabled_only=False, market_region=market))
    missing_held = held - set(all_rows)
    present = {str(r.get('symbol','')).upper() for r in rows}
    rows += [all_rows[s] for s in sorted(held - present - missing_held)]
    if not rows:
        print(f"[{market}] no universe rows", flush=True)
        return dict(market=market,status='unavailable',target=target,requested=0,fresh=0,missing_held=len(missing_held))
    t0 = time.time()
    candles_done = 0

    held_rows = [r for r in rows if str(r.get("symbol", "")).upper() in held]
    if held_rows:
        for attempt in range(2):
            got, n = _fetch_upsert(provider, db, held_rows, target)
            candles_done += n
            got = _fresh_symbols(db,market,target,{str(r['symbol']).upper() for r in held_rows})
            if len(got) == len(held_rows):
                break
            time.sleep(3)
        print(f"[{market}] held pass: {len(got)}/{len(held_rows)} held symbols fresh", flush=True)

    before = _source_max_ts(db, market)
    fresh = _fresh_symbols(db, market, target)
    todo = [r for r in rows if str(r.get("symbol", "")).upper() not in fresh]
    print(f"[{market}] target={target} stored={before} universe={len(rows)} "
          f"already-fresh={len(rows)-len(todo)} to-fetch={len(todo)}", flush=True)
    covered = set()
    for pass_no in range(1, max_passes + 1):
        if not todo:
            break
        missed = []
        for b in range(0, len(todo), fetch_batch):
            batch = todo[b:b + fetch_batch]
            try:
                got, n = _fetch_upsert(provider, db, batch, target)
            except Exception as exc:
                print(f"[{market}] pass{pass_no} batch {b} error: {type(exc).__name__}", flush=True)
                got, n = set(), 0
            # History returned != current-session coverage. Retry the actual
            # stale tail even if the provider returned years of older bars.
            got = _fresh_symbols(db,market,target,{str(r['symbol']).upper() for r in batch})
            candles_done += n
            covered |= got
            missed.extend(r for r in batch if str(r.get("symbol", "")).upper() not in got)
            if (b // fetch_batch) % 10 == 0:
                print(f"[{market}] pass{pass_no} {b + len(batch)}/{len(todo)} -> covered {len(covered)}", flush=True)
            time.sleep(pause)
        print(f"[{market}] pass{pass_no} done: covered {len(covered)}/{len(todo) if pass_no == 1 else '-'} · missed {len(missed)}", flush=True)
        todo = missed
        pause = min(pause * 1.5, 6.0)      # back off between retry passes
    after = _source_max_ts(db, market)
    print(f"[{market}] DONE {candles_done} candles · covered {len(covered)} · "
          f"unrecoverable {len(todo)} · {time.time()-t0:.1f}s · stored {before} -> {after}", flush=True)
    # A run that fetches hundreds of thousands of candles and still does not
    # advance the newest stored session used to look like success. Name it.
    if after and after < target:
        print(f"[{market}] WARNING: still behind — newest stored bar is {after}, "
              f"expected {target}. The provider has probably not published "
              f"{target} yet (its daily history has been observed to lag by a "
              f"session); the next run should pick it up.", flush=True)
    requested = {str(r['symbol']).upper() for r in rows}
    actual_fresh = requested & _fresh_symbols(db,market,target)
    return dict(market=market,status='complete' if actual_fresh==requested and not missing_held else 'partial',
                target=target,requested=len(requested),fresh=len(actual_fresh),missing=len(requested-actual_fresh),
                missing_held=len(missing_held),candles_written=candles_done)


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
    results = []
    for m in targets:
        try:
            if m not in MARKETS:
                results.append(dict(market=m,status='unsupported')); continue
            results.append(ingest(m, MARKETS[m], db, settings, limit=a.limit))
        except Exception as exc:
            print(f"[{m}] ingest failed: {type(exc).__name__}", flush=True)
            results.append(dict(market=m,status='failed',error_type=type(exc).__name__))
    print("candle ingest done", flush=True)
    print(json.dumps(dict(markets=results),sort_keys=True))
    # Partial history is retained, but a green job must mean current coverage.
    return 0 if results and all(r['status']=='complete' for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
