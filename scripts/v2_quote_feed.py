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
import json
import math
import os
import sys
import time
from dataclasses import replace
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
            # Selection membership must not determine exit/valuation coverage.
            # Disabled names stay quote-only; the full lane still uses rows[m].
            symmap[m] = _quote_map(db.get_universe(enabled_only=False, market_region=m))
        except Exception as exc:
            print(f"[{m}] provider build failed: {exc}")
    return db, providers, rows, symmap


_last_exposure_warning = None


def _quote_map(rows):
    """Do not silently choose between conflicting venue/alias identities."""
    result, ambiguous = {}, set()
    for row in rows:
        symbol = str(row.get('symbol') or '').strip().upper()
        if not symbol:
            continue
        old = result.get(symbol)
        identity = lambda r: (str(r.get('exchange') or '').upper(),
                              r.get('upstox_instrument_key'), r.get('isin'))
        if old is not None and identity(old) != identity(row):
            ambiguous.add(symbol)
        else:
            result[symbol] = row
    return {s: r for s, r in result.items() if s not in ambiguous}


def _exposure_warning(value):
    global _last_exposure_warning
    if value != _last_exposure_warning:
        if value:
            # Counts/table names only, never owner identities or credentials.
            print('exposure quote coverage incomplete: ' + value, flush=True)
        elif _last_exposure_warning:
            print('exposure quote coverage recovered', flush=True)
        _last_exposure_warning = value


def _held(path=None, *, strict=False):
    """Symbols we currently hold, per market — the 'hot' set polled every tick so
    open-position prices and P&L move in near real time."""
    out = {m: set() for m in MARKETS}
    c = None
    try:
        c = sqlite3.connect(f"file:{path or V2_DB}?mode=ro", uri=True, timeout=5)
        queries = {
            'house': 'SELECT market,symbol FROM v2_positions',
            'personal': 'SELECT market,symbol FROM user_positions',
            'paper-pending': "SELECT 'IN',json_extract(i.payload,'$.plan.symbol') FROM paper_order_intents i "
                             "JOIN paper_order_state s ON s.order_id=i.id WHERE s.status='pending'",
            'broker': "SELECT market,symbol FROM v2_live_orders WHERE status IN "
                      "('pending','submitted','partial','unknown','sent') UNION "
                      "SELECT market,symbol FROM v2_live_orders GROUP BY user_id,market,symbol,instrument_key "
                      "HAVING SUM(CASE WHEN side='BUY' THEN COALESCE(filled_qty,0) "
                      "ELSE -COALESCE(filled_qty,0) END)>0",
            'protection': "SELECT 'IN',symbol FROM v2_live_protection",
        }
        missing = []
        for scope, query in queries.items():
            try:
                for m, sym in c.execute(query):
                    out.setdefault(m, set()).add(str(sym).upper())
            except sqlite3.Error:
                missing.append(scope)
        _exposure_warning('unavailable inventory scopes: ' + ','.join(missing) if missing else None)
        if missing and strict:
            raise ValueError('Complete exposure inventory required for identity changes')
    except (sqlite3.Error, OSError):
        _exposure_warning('inventory database unavailable')
        if strict:raise ValueError('Exposure inventory unavailable') from None
    finally:
        if c is not None:
            c.close()
    return out


def _tracked():
    """Issued research plans need the hot feed even when they are not positions.

    Read only the separate journals; never invoke a strategy or create a book.
    This adds quote coverage, not candidates to the trading engine.
    """
    from app.screening import automation, tracking
    symbols = set()
    # These are separate journals. A research UI visit is not a prerequisite
    # for an automation publication to receive fast quotes.
    for scope, resolve in (('research', lambda: tracking.default_path(MAIN_DB)),
                           ('automation', automation.default_path)):
        try:
            path = resolve()
            if os.path.exists(path):
                symbols.update(tracking.symbols(path))
        except (sqlite3.Error, OSError, ValueError):
            print(f'{scope} issued idea quote watch unavailable; tracker coverage must be checked', flush=True)
    return symbols


def _hot_symbols(market, held, tracked):
    return set(held.get(market, ())) | set(WATCH_HOT.get(market, ())) | (set(tracked) if market == 'IN' else set())


def _hot_rows(symmap,held,tracked):
    return {m:[symmap[m][s] for s in sorted(_hot_symbols(m, held, tracked))
               if s in symmap.get(m,{})] for m in MARKETS}


def _refresh_quote_map(db, previous):
    """Refresh quote-only identities without adding names to entry selection."""
    result = dict(previous)
    for market in MARKETS:
        try:
            result[market] = _quote_map(db.get_universe(enabled_only=False, market_region=market))
        except Exception:
            # Retain the previous watch; stale quotes still fail entry/exit
            # freshness checks. Do not let this read kill the fast worker.
            print('quote-only universe refresh unavailable for ' + market, flush=True)
    return result


def _refresh_full_rows(db, previous):
    """Selection can change after startup; keep the broad lane current."""
    result = dict(previous)
    for market in MARKETS:
        try:
            result[market] = db.get_universe(enabled_only=True, market_region=market)
        except Exception:
            print('full quote universe refresh unavailable for ' + market, flush=True)
    return result


_cooldown: dict = {}   # market -> unix time to resume after a rate-limit (429)
_coverage_gaps: dict = {}  # lane/market -> last reported missing/stale sets
_coverage_archive_last: dict = {}
_coverage_archive_lock = threading.Lock()
_coverage_warnings: set = set()
COVERAGE_ARCHIVE_INTERVAL = 60
COVERAGE_ARCHIVE_DAYS = 14
COVERAGE_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024
QUOTE_MAX_AGE_SECONDS = 120
MAX_FULL_INTERVAL = QUOTE_MAX_AGE_SECONDS / 2


def _full_interval(value):
    """Leave room for broker batches inside the existing freshness contract.

    A deployed 300s override caused repeated gaps against a 120s reader gate.
    Bound cadence at runtime too: a stale systemd override cannot restore it.
    """
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Positive finite full quote interval required')
    if value > MAX_FULL_INTERVAL:
        print(f'full quote interval {value:g}s exceeds freshness budget; using {MAX_FULL_INTERVAL:g}s', flush=True)
    return min(value, MAX_FULL_INTERVAL)

# The hot lane always keeps this small liquid base fresh (every `interval`s) even
# when the book is EMPTY — otherwise latest_quotes only refreshes on the slow full
# poll, so MAX(ts) drifts to ~full-interval and the "live feed" health check + the
# tape/movers go stale on a fresh book. Held symbols are polled on top of these.
WATCH_HOT = {
    "IN": ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
           "ITC", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA"],
    "US": [],
}

# Quote-only instruments. Never pass these to db.upsert_quotes or universe.
BENCHMARK_ROWS = [
    {'symbol':'NIFTY','upstox_instrument_key':'NSE_INDEX|Nifty 50'},
    {'symbol':'BANKNIFTY','upstox_instrument_key':'NSE_INDEX|Nifty Bank'},
]


def _deliver(db, provider, market, quotes):
    indices = {s: q for s, q in quotes.items() if market == 'IN' and s in ('NIFTY', 'BANKNIFTY')}
    equities = {s: q for s, q in quotes.items() if s not in indices}
    if equities:
        db.upsert_quotes(equities)
    execution = getattr(provider, 'execution_quotes', {})
    if execution:
        from app import executable_quotes
        try:
            with db.connect() as con:
                for snapshot in execution.values():
                    executable_quotes.write(con, snapshot)
        except sqlite3.Error:
            print('executable depth capture unavailable; valuation quotes delivered', flush=True)
    if indices:
        from app.screening import tracking
        try:
            tracking.observe_benchmarks(tracking.default_path(MAIN_DB),
                {s: dict(price=q.price, ts=q.asof, source=q.source) for s, q in indices.items()})
        except (sqlite3.Error, OSError, ValueError):
            print('benchmark capture unavailable; equity quotes delivered', flush=True)


def _quote_coverage(market, rows, quotes, now):
    """Report per-symbol source age using the existing 120s observation contract.

    Returned quotes are not necessarily fresh. Never substitute receipt time or
    change their source labels, including on retry.
    """
    requested = {r['symbol'] for r in rows}
    fresh = set()
    for symbol in requested & quotes.keys():
        q = quotes[symbol]
        try:
            at = datetime.fromisoformat(q.asof.replace('Z', '+00:00'))
            if (at.tzinfo is not None and math.isfinite(q.price) and q.price > 0
                    and (q.source == 'upstox-live' if market == 'IN'
                         else q.source.startswith('alpaca-') and q.source.endswith('-live'))
                    and 0 <= (now - at).total_seconds() <= QUOTE_MAX_AGE_SECONDS
                    and market_regions.market_session_for_region(market, at).get('is_open')):
                fresh.add(symbol)
        except (AttributeError, TypeError, ValueError):
            pass
    returned = requested & quotes.keys()
    return dict(requested=len(requested), returned=len(returned), fresh_symbols=sorted(fresh),
                missing_symbols=sorted(requested - returned), stale_symbols=sorted(returned - fresh),
                source_counts=dict(Counter(quotes[s].source for s in returned)))


def _coverage_summary(report, now, status):
    """Bound stdout/storage without dropping detail from the caller's report.

    checked_at dates this poll receipt, never the broker's quote. Sources count
    only returned requested equities, including stale/last-traded labels.
    """
    return dict(checked_at=now.isoformat(), status=status,
                coverage_source='quote_provider_response' if status == 'polled' else 'quote_provider_error',
                requested=report['requested'], returned=report['returned'],
                fresh=len(report['fresh_symbols']), missing=len(report['missing_symbols']),
                stale=len(report['stale_symbols']), source_counts=report['source_counts'],
                missing_sample=report['missing_symbols'][:10], stale_sample=report['stale_symbols'][:10])


def _archive_coverage(market, lane, summary, now):
    """At most one market-hour sample/minute/lane, with bounded private files.

    Skips/closed hours are not observations. This is sampled telemetry, not
    proof of continuous coverage between checks. No paper database is used.
    """
    if not market_regions.market_session_for_region(market, now).get('is_open'):
        return
    with _coverage_archive_lock:
        tick = time.monotonic()
        key = (market, lane)
        previous = _coverage_archive_last.get(key)
        if previous is not None and tick - previous < COVERAGE_ARCHIVE_INTERVAL:
            return
        # Failed filesystem writes are paced too; the current DB receipt is
        # independent and still attempted on every actual poll.
        _coverage_archive_last[key] = tick
        folder = Path(os.getenv('QUOTE_FEED_COVERAGE_DIR', str(Path(MAIN_DB).parent / 'quote_feed_coverage')))
        folder.mkdir(parents=True, exist_ok=True)
        prefix = f'coverage_{market}_{lane}_'
        path = folder / f"{prefix}{now.date().isoformat()}.jsonl"
        files = sorted(set(folder.glob(prefix + '????-??-??.jsonl')) | {path})
        for old in files[:-COVERAGE_ARCHIVE_DAYS]:
            old.unlink(missing_ok=True)
        if path not in files[-COVERAGE_ARCHIVE_DAYS:]:
            return
        record = dict(summary, market=market, lane=lane)
        data = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        size = path.stat().st_size if path.exists() else 0
        if size + len(data) <= COVERAGE_ARCHIVE_MAX_BYTES:
            with path.open('ab') as archive:
                archive.write(data)


def _record_coverage(db, market, lane, summary, now):
    # Each destination fails independently, after quote delivery. An unavailable
    # telemetry sink must never turn a successful fetch into an all-missing one.
    # Only actual polls renew checked_at; closed/cooldown skips leave it dated.
    for sink, write in (
        ('state', lambda: db.set_state(f'quote_feed_coverage_{market}_{lane}', summary)),
        ('archive', lambda: _archive_coverage(market, lane, summary, now)),
    ):
        key = (market, lane, sink)
        try:
            write()
            _coverage_warnings.discard(key)
        except Exception:
            if key not in _coverage_warnings:
                print(f'  [{market}/{lane}] coverage {sink} unavailable; quote delivery unaffected', flush=True)
            _coverage_warnings.add(key)


def _rate_limited(message):
    return any(code in message for code in ('429', 'Too Many Request', 'UDAPI10005'))


def _poll(db, providers, rows_for, label, benchmarks=False):
    coverage = {}
    lane_label = 'hot' if benchmarks else 'full'
    for m, rws in rows_for.items():
        if m not in providers or not rws:
            continue
        if time.time() < _cooldown.get(m, 0):   # backing off after a 429 — don't burn more quota
            continue
        try:
            if not market_regions.market_session_for_region(m).get("is_open"):
                continue
            requested=rws+BENCHMARK_ROWS if benchmarks and m=='IN' else rws
            quotes = asyncio.run(providers[m].get_quotes(requested))
            # Portfolio marks get written first. Research capture failures must
            # never discard or delay delivery of already fetched equity quotes.
            _deliver(db, providers[m], m, quotes)
            diagnostics = getattr(providers[m], 'last_quote_diagnostics', {})
            limited = _rate_limited(str(diagnostics.get('errors', [])))
            # One extra broker batch at most. Stale trades are source evidence,
            # not transport failures, and must not trigger repeated requests.
            missing = [row for row in rws if row['symbol'] not in quotes][:100]
            if missing and not limited and market_regions.market_session_for_region(m).get('is_open'):
                try:
                    retried = asyncio.run(providers[m].get_quotes(missing))
                    retry_symbols = {r['symbol'] for r in missing}
                    retried = {s: q for s, q in retried.items() if s in retry_symbols}
                    _deliver(db, providers[m], m, retried)
                    quotes.update(retried)
                    limited = _rate_limited(str(getattr(providers[m], 'last_quote_diagnostics', {}).get('errors', [])))
                except Exception as exc:
                    limited = _rate_limited(str(exc))
                    print(f'  [{m}] missing-symbol retry failed: {str(exc)[:180]}', flush=True)
            if limited:
                _cooldown[m] = time.time() + 45
                print(f'  [{m}] rate-limited by broker (429) — backing off 45s', flush=True)
            now = datetime.now(timezone.utc)
            report = _quote_coverage(m, rws, quotes, now)
            coverage[m] = report
            summary = _coverage_summary(report, now, 'polled')
            _record_coverage(db, m, lane_label, summary, now)
            gaps = (tuple(report['missing_symbols']), tuple(report['stale_symbols']))
            lane = (m, benchmarks)
            changed = gaps != _coverage_gaps.get(lane, ((), ()))
            _coverage_gaps[lane] = gaps
            if label or changed:
                print(f"  [{m}/{lane_label}] coverage={json.dumps(summary, separators=(',', ':'))}", flush=True)
        except Exception as exc:
            # An all-symbol transport failure is still an explicit coverage gap.
            now = datetime.now(timezone.utc)
            coverage[m] = _quote_coverage(m, rws, {}, now)
            msg = str(exc)
            if _rate_limited(msg):
                _cooldown[m] = time.time() + 45   # Upstox rate-limited us: pause both lanes ~45s
                print(f"  [{m}] rate-limited by broker (429) — backing off 45s", flush=True)
            else:
                print(f"  [{m}] fetch error: {msg[:180]}", flush=True)
            summary = _coverage_summary(coverage[m], now, 'fetch_error')
            _record_coverage(db, m, lane_label, summary, now)
            print(f"  [{m}/{lane_label}] coverage={json.dumps(summary, separators=(',', ':'))}", flush=True)
    return coverage


def _full_worker(interval):
    interval = _full_interval(interval)
    db, providers, rows, _symmap = _build()
    print(f"full worker up @ {interval}s universe={ {m: len(rows.get(m, [])) for m in providers} }", flush=True)
    last_refresh = time.monotonic()
    n = 0
    while True:
        t0 = time.monotonic()
        if t0 - last_refresh >= 60:
            rows = _refresh_full_rows(db, rows)
            last_refresh = t0
        _poll(db, providers, rows, 'full' if n % 8 == 0 else '')
        n += 1
        time.sleep(max(1.0, interval - (time.monotonic() - t0)))


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
    """{index: spot} used to CENTRE the ATM watch window.

    The LIVE level first, falling back to the last bhavcopy close.

    Centring on the bhavcopy alone is what put a held position outside the
    window and stopped it being quoted. On 2026-08-03 BANKNIFTY's bhavcopy
    close was 57,147.5 while the index was actually at 57,249 — 102 points, one
    strike at ATM +/- 3:

        centred on the bhavcopy   56800 .. 57400   <- held 57500CE OUTSIDE
        centred on the live level 56900 .. 57500   <- inside

    The error is systematically worst when the index has moved a long way,
    which is exactly when an option position is winning and most needs pricing.

    The live level comes from put-call parity on the strikes already being
    polled, so this is self-correcting rather than circular: even a window
    aimed slightly wrong still quotes strikes near the money, and their parity
    recentres the next pass.
    """
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
    try:
        from app import index_spot
        for sym in list(out) or []:
            live = index_spot.spot(sym)
            # A thin or far-from-the-money reading is refused by spot() itself,
            # so this only ever replaces the stale close with something better.
            if live and live.get("price"):
                out[sym] = float(live["price"])
    except Exception as exc:
        print(f"  [NFO] live spot unavailable, using bhavcopy: {str(exc)[:80]}", flush=True)
    return out


def _nfo_prune():
    """Drop quotes for contracts that have expired. Returns rows removed.

    This table was insert-only, so rows for dead expiries stayed forever, frozen
    at whatever price they last printed. An expired contract still reports a
    healthy underlying, strike, lot size and volume, so nothing downstream could
    tell it apart from a live one by looking at the row: on 2026-08-07 the engine
    bought and sold one such contract 83 times in a session for -Rs 7,721.

    The engine has its own guards now (it refuses expired and frozen quotes at
    entry), but those defend one reader. Not writing the garbage defends every
    reader, including the two in v2_web and index_spot that nobody has audited
    for this.

    SEPARATE FROM THE WRITE, and called unconditionally, because `_nfo_write`
    returns early when there is nothing to write — which is every tick outside
    market hours. Pruning only when quotes happen to arrive means the dead rows
    from a Friday expiry sit there all weekend, exactly when nobody is watching.

    Compared against the IST trading date, not UTC: for the five and a half
    hours after 18:30 IST, UTC is still on the previous day, and using it would
    keep an expired contract alive through the whole evening. Expiry day itself
    is KEPT — the lane trades 0-DTE on purpose.
    """
    try:
        ist_today = (datetime.now(timezone.utc)
                     + timedelta(hours=5, minutes=30)).date().isoformat()
        con = sqlite3.connect(MAIN_DB, timeout=20)
        cur = con.execute("DELETE FROM nfo_quotes WHERE expiry IS NOT NULL"
                          " AND expiry <> '' AND substr(expiry,1,10) < ?", (ist_today,))
        n = cur.rowcount or 0
        con.commit(); con.close()
        if n:
            print(f"  [NFO] pruned {n} expired contract rows", flush=True)
        return n
    except Exception as exc:
        print(f"  [NFO] prune failed: {str(exc)[:120]}", flush=True)
        return 0


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
        _nfo_prune()          # see _nfo_prune: also called on ticks that write nothing
        return len(rows)
    except Exception as exc:
        print(f"  [NFO] write failed: {str(exc)[:120]}", flush=True)
        return 0


def _nfo_held(contracts):
    """Option contracts we actually HOLD, matched to the live contract rows.

    A held option is the one position whose price genuinely cannot wait: it is
    leveraged, it decays, and the exit decision is worthless if it is acting on
    a price from twenty seconds ago.
    """
    try:
        con = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True, timeout=5)
        held = {str(r[0]).upper() for r in con.execute(
            "SELECT symbol FROM v2_positions WHERE market=?", ("IN",))}
        con.close()
    except Exception:
        return []
    if not held:
        return []
    have = {c["symbol"].upper(): c for c in contracts}
    out = [have[s] for s in held if s in have]
    # A held contract that has fallen OUT of the ATM window is looked up by
    # name instead of being dropped. Filtering the watch list was the bug:
    # the underlying moves, the window slides, and the position you own stops
    # being quoted — BANKNIFTY26AUG57500CE went unpriced from 07-31 to 08-03
    # while the book kept marking it at the last price it ever saw.
    missing = [s for s in held if s not in have]
    if missing:
        try:
            from app import nfo_contracts
            out.extend(nfo_contracts.by_symbols(missing))
        except Exception as exc:
            print(f"  [NFO] held lookup failed: {str(exc)[:100]}", flush=True)
    return out


def _nfo_worker(interval, hot_interval):
    """Two cadences, mirroring the equity lanes.

    HELD contracts are polled every `hot_interval` — the same reasoning as the
    equity hot lane, only more urgent, because an option can lose a third of its
    value inside a minute and the stop is meaningless if it is evaluated against
    a stale price.

    The ATM WATCH window refreshes every `interval`. Those are candidates, not
    positions; nothing is riding on them being a few seconds old, and Upstox
    rate-limits — a 429 puts BOTH equity lanes into a 45s cooldown, so spending
    quota on contracts nobody holds would degrade the rest of the engine.
    """
    try:
        from app import nfo_contracts
    except Exception as exc:
        print(f"  [NFO] disabled: {exc}", flush=True)
        return
    db, providers, _rows, _symmap = _build()
    if "IN" not in providers:
        return
    print(f"nfo worker up @ {hot_interval}s held / {interval}s watch", flush=True)
    contracts, last_watch, last_prune = [], 0.0, 0.0
    while True:
        t0 = time.time()
        try:
            # OUTSIDE the is_open gate on purpose. Everything else in this loop
            # is skipped when the market is shut, so pruning in there would
            # leave a Friday expiry's dead rows sitting in the table until
            # Monday's open — the whole weekend, unwatched. Throttled to ten
            # minutes because it is a DELETE against the live DB, not a read.
            if time.time() - last_prune >= 600:
                _nfo_prune()
                last_prune = time.time()
            if market_regions.market_session_for_region("IN").get("is_open"):
                due = (time.time() - last_watch) >= interval
                if due or not contracts:
                    contracts = nfo_contracts.select_many(_nfo_spots())
                    last_watch = time.time()
                    poll = contracts
                    label = "watch"
                else:
                    poll = _nfo_held(contracts)
                    label = "held"
                if poll:
                    quotes = asyncio.run(providers["IN"].get_quotes(poll))
                    n = _nfo_write(quotes, contracts or poll)
                    if label == "watch":
                        print(f"  [NFO/{label}] {n} option quotes @ "
                              f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}", flush=True)
        except Exception as exc:
            print(f"  [NFO] {str(exc)[:160]}", flush=True)
        time.sleep(max(0.5, hot_interval - (time.time() - t0)))



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=8.0)  # hot publications/held cadence
    ap.add_argument("--full-interval", type=_full_interval, default=MAX_FULL_INTERVAL)
    ap.add_argument("--nfo-interval", type=float, default=20.0)      # ATM watch window
    ap.add_argument("--nfo-hot-interval", type=float, default=1.0)   # contracts we HOLD
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
        last_map_refresh = time.monotonic()
        missing_before = None
        while True:
            t0 = time.time()
            if time.monotonic() - last_map_refresh >= 60:
                symmap = _refresh_quote_map(db, symmap)
                last_map_refresh = time.monotonic()
            held = _held()
            # Held symbols, base names and BOTH publication journals.
            tracked = _tracked()
            hot = _hot_rows(symmap, held, tracked)
            missing = tuple(sorted((m, s) for m in MARKETS for s in _hot_symbols(m, held, tracked)
                                   if s not in symmap.get(m, {})))
            if missing != missing_before:
                if missing:
                    print(f'hot quote identities unresolved: count={len(missing)} sample={missing[:10]}; quote coverage requires review', flush=True)
                elif missing_before:
                    print('hot quote identities recovered', flush=True)
                missing_before = missing
            if any(hot.values()):
                _poll(db, providers, hot, "", benchmarks=True)
            time.sleep(max(0.2, a.interval - (time.time() - t0)))

    t = threading.Thread(target=_hot_worker, daemon=True)
    t.start()
    if not a.no_nfo:
        threading.Thread(target=_nfo_worker, args=(a.nfo_interval, a.nfo_hot_interval),
                         daemon=True).start()
    _full_worker(a.full_interval)


if __name__ == "__main__":
    main()
