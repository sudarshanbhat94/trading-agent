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
from app.v2_live import ENABLED_MARKETS                     # noqa: E402

import sqlite3
import threading

# Only fetch quotes for markets the engine actually trades (US parked -> no US
# data pulled at all). Re-enable US in one place: v2_live.ENABLED_MARKETS.
MARKETS = {m: p for m, p in {"IN": "upstox", "US": "alpaca"}.items() if m in ENABLED_MARKETS}
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")


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


_cooldown: dict = {}   # market -> unix time to resume after a rate-limit (429)

# The hot lane always keeps this small liquid base fresh (every `interval`s) even
# when the book is EMPTY — otherwise latest_quotes only refreshes on the slow full
# poll, so MAX(ts) drifts to ~full-interval and the "live feed" health check + the
# tape/movers go stale on a fresh book. Held symbols are polled on top of these.
WATCH_HOT = {
    "IN": ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
           "ITC", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA"],
    "US": [],
}


def _poll(db, providers, rows_for, label):
    for m, rws in rows_for.items():
        if m not in providers or not rws:
            continue
        if time.time() < _cooldown.get(m, 0):   # backing off after a 429 — don't burn more quota
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
            msg = str(exc)
            if "429" in msg or "Too Many Request" in msg or "UDAPI10005" in msg:
                _cooldown[m] = time.time() + 45   # Upstox rate-limited us: pause both lanes ~45s
                print(f"  [{m}] rate-limited by broker (429) — backing off 45s", flush=True)
            else:
                print(f"  [{m}] fetch error: {msg[:180]}", flush=True)


# ---- index option lane -------------------------------------------------------
# Options are polled SEPARATELY and written under their own source. Two reasons,
# both about isolation rather than tidiness:
#   * they are never added to the `universe` table, because that table is what
#     get_universe() screens over — a NIFTY option sitting in it would become a
#     buy candidate for the equity lanes;
#   * they are stored under NFO_SOURCE, so _live("IN") (which selects on
#     source='upstox-live') cannot see them either.
# Only a few strikes around the money on the nearest expiry are polled: Upstox
# rate-limits, and a 429 puts BOTH equity lanes into a 45s cooldown, so asking
# for the full 1,549-contract chain would cost real quotes to serve options
# nobody holds.
NFO_SOURCE = "upstox-nfo"


def _nfo_spots():
    """{index: spot} from the most recent derivatives bhavcopy."""
    out = {}
    try:
        import os
        path = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        for (sym,) in con.execute("SELECT DISTINCT symbol FROM fo_bhav"):
            row = con.execute("SELECT underlying FROM fo_bhav WHERE symbol=? AND underlying>0"
                              " ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
            if row and row[0]:
                out[sym] = float(row[0])
        con.close()
    except Exception:
        pass
    return out


def _nfo_write(quotes, contracts):
    """Persist option quotes under NFO_SOURCE, keeping them out of the equity feed."""
    if not quotes:
        return 0
    meta = {c["symbol"]: c for c in contracts}
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for symbol, quote in quotes.items():
        d = quote.to_dict() if hasattr(quote, "to_dict") else dict(quote)
        c = meta.get(str(symbol).upper(), {})
        rows.append((str(symbol).upper(), NFO_SOURCE, now,
                     d.get("price"), d.get("open"), d.get("high"), d.get("low"),
                     d.get("close"), d.get("volume"),
                     c.get("underlying"), c.get("expiry"), c.get("strike"),
                     c.get("option_type"), c.get("lot_size")))
    try:
        con = sqlite3.connect(MAIN_DB, timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS nfo_quotes(
            symbol TEXT, source TEXT, ts TEXT, price REAL, open REAL, high REAL,
            low REAL, close REAL, volume REAL, underlying TEXT, expiry TEXT,
            strike REAL, option_type TEXT, lot_size REAL,
            PRIMARY KEY(symbol, source))""")
        con.executemany("INSERT OR REPLACE INTO nfo_quotes(symbol,source,ts,price,open,high,"
                        "low,close,volume,underlying,expiry,strike,option_type,lot_size)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit(); con.close()
        return len(rows)
    except Exception as exc:
        print(f"  [NFO] write failed: {str(exc)[:120]}", flush=True)
        return 0


def _nfo_worker(interval):
    """Poll a small ATM window of index options while the market is open."""
    try:
        from app import nfo_contracts
    except Exception as exc:
        print(f"  [NFO] disabled: {exc}", flush=True)
        return
    db, providers, _rows, _symmap = _build()
    if "IN" not in providers:
        return
    print(f"nfo worker up @ {interval}s", flush=True)
    while True:
        t0 = time.time()
        try:
            if market_regions.market_session_for_region("IN").get("is_open"):
                contracts = nfo_contracts.select_many(_nfo_spots())
                if contracts:
                    quotes = asyncio.run(providers["IN"].get_quotes(contracts))
                    n = _nfo_write(quotes, contracts)
                    print(f"  [NFO] {n} option quotes @ "
                          f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        except Exception as exc:
            print(f"  [NFO] {str(exc)[:160]}", flush=True)
        time.sleep(max(3.0, interval - (time.time() - t0)))



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)        # hot (held) cadence
    ap.add_argument("--full-interval", type=float, default=15.0)  # whole-universe cadence
    ap.add_argument("--nfo-interval", type=float, default=20.0)   # index option cadence
    ap.add_argument("--no-nfo", action="store_true")
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
            # held symbols + the always-fresh liquid base set (dedup)
            hot = {m: [symmap[m][s] for s in (set(held.get(m, ())) | set(WATCH_HOT.get(m, [])))
                       if s in symmap.get(m, {})] for m in MARKETS}
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
    if not a.no_nfo:
        threading.Thread(target=_nfo_worker, args=(a.nfo_interval,), daemon=True).start()
    _full_worker()


if __name__ == "__main__":
    main()
