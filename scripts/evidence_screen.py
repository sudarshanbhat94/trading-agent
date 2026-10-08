"""Refresh independent research evidence and a dated equity/index screen.

Reads market DB; writes ONLY dedicated screening DB. Never imports broker,
strategy loop or application startup. Fundamentals are secondary sourced.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import pandas as pd
from app.screening import providers, store
from app.screening.screen import build, _features, MIN_TURNOVER
from app.screening.financials import valid_income_history
from app.sleeves.reference import refresh_membership, snapshot
from app.v2_engine import load_panel

LOG = logging.getLogger("openstocks.screening")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = os.getenv("SCREENING_DB", str(ROOT / "var" / "screening.db"))


def statement_due(f,asof):
    """Recollect incompatible caches promptly without refetching honest gaps."""
    return (not f or f.get('financial_contract_version')!='annual-statements-v2'
            or not valid_income_history(f,str(asof)[:10]))


def statement_queue(symbols, cached, features, attempts, now):
    """Repair current setups first; failed sources rotate behind untried names.

    This orders evidence requests only. It never changes selection predicates.
    A recent failed request waits an hour rather than consuming every refresh.
    """
    due = [s for s in symbols if statement_due(cached.get(s), now.astimezone(providers.IST).date())]
    def recent_failure(s):
        attempt = attempts.get(s) or {}
        if attempt.get('status') != 'failed': return False
        try:
            age = (now-store.timestamp(attempt['known_at'])).total_seconds()
            return 0 <= age < 3600
        except (KeyError,ValueError,TypeError):
            return False
    def priority(s):
        f = features.get(s) or {}
        setup = f.get('setup') in ('pullback','controlled breakout')
        return (s in attempts, not setup, (attempts.get(s) or {}).get('known_at',''), s)
    return sorted((s for s in due if not recent_failure(s)), key=priority)


def capture_statements(http, con, symbols, errors, limit, budget_seconds=480, min_interval=1.):
    """Bound both request concurrency and wall time, including a cold cache.

    Keep at most three requests in flight. At the deadline drain those requests
    and archive their actual capture times; do not submit the rest of the queue.
    Stop new requests on source throttling. The next scheduled run resumes.
    """
    if not 0 <= limit <= 400 or budget_seconds < 0 or min_interval < 0:
        raise ValueError('bounded statement refresh required')
    queue = iter(symbols[:limit])
    deadline = monotonic()+budget_seconds
    next_request = monotonic()
    submitted = captured = 0
    paused = False
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {}
        while True:
            while not paused and len(tasks)<3 and monotonic()<deadline:
                symbol = next(queue,None)
                if symbol is None: break
                delay = max(0.,next_request-monotonic())
                if monotonic()+delay >= deadline: break
                if delay: sleep(delay)
                tasks[pool.submit(providers.fetch_statements,http,symbol,datetime.now(timezone.utc))] = symbol
                next_request = monotonic()+min_interval
                submitted += 1
            if not tasks: break
            done, _ = wait(tasks,return_when=FIRST_COMPLETED)
            for task in done:
                symbol = tasks.pop(task)
                seen = datetime.now(timezone.utc)
                outcome = 'captured'
                try:
                    data = task.result()
                    store.save(con,symbol,'fundamentals','Yahoo Finance financial-statement timeseries',data,seen.isoformat(),seen)
                    captured += 1
                except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
                    outcome = 'failed'
                    errors.append(symbol+' fundamentals unavailable: '+type(exc).__name__)
                    if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (429,503):
                        paused = True
                store.save(con,symbol,'fundamental_attempt','screen refresh',dict(status=outcome),seen.isoformat(),seen)
    return dict(requested=submitted,captured=captured,deferred=len(symbols)-submitted,source_paused=paused)


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def capture_participation(main, evidence, symbols, asof, now, fresh_delivery=None, fresh_flows=None):
    stamp = str(asof)[:10]
    for symbol in symbols:
        rows = _rows(main, "SELECT date,delivery_pct FROM delivery_data WHERE symbol=? "
                     "AND date<=? AND date>=? ORDER BY date DESC LIMIT 21", (symbol, stamp,
                     (asof-timedelta(days=45)).date().isoformat()))
        valid = [float(p) for d, p in rows if d < stamp and p is not None and 0 <= float(p) <= 100][:20]
        current = (fresh_delivery or {}).get(symbol)
        if current is None and rows and rows[0][0] == stamp:
            current = rows[0][1]
        deals = _rows(main, "SELECT date,side,quantity,price FROM bulk_deals "
                      "WHERE symbol=? AND date<=? AND date>=?", (symbol, stamp,
                      (asof-timedelta(days=7)).date().isoformat()))
        payload = dict(session=stamp if current is not None else None,
            delivery_pct=current if current is not None and 0 <= current <= 100 else None,
            delivery_avg20_pct=sum(valid)/len(valid) if len(valid) == 20 else None,
            bulk_deals=[dict(session=d, side=s, quantity=q, price=p) for d,s,q,p in deals],
            note="Completed-session delivery; bulk deals are disclosures, not proof of accumulation")
        seen = datetime.now(timezone.utc)
        store.save(evidence, symbol, "participation", "NSE reports captured from market database", payload, seen.isoformat(), seen)
    flows = _rows(main, "SELECT date,category,net_value FROM fii_dii_flows WHERE date<=? "
                  "ORDER BY date DESC LIMIT 2", (stamp,))
    vix = _rows(main, "SELECT date,value FROM india_vix WHERE date<=? ORDER BY date DESC LIMIT 1", (stamp,))
    oi = _rows(main, "SELECT date,client_type,fut_idx_long,fut_idx_short FROM participant_oi "
               "WHERE date=(SELECT MAX(date) FROM participant_oi WHERE date<=?)", (stamp,))
    seen = datetime.now(timezone.utc)
    store.save(evidence, "MARKET", "market", "NSE daily market reports", dict(
        fii_dii=fresh_flows if fresh_flows is not None else [dict(session=d, category=c, net_inr_crore=v) for d,c,v in flows],
        india_vix=dict(session=vix[0][0], value=vix[0][1]) if vix else None,
        participant_oi=[dict(session=d, category=c, futures_long=l, futures_short=s) for d,c,l,s in oi]), seen.isoformat(), seen)


def capture_events(http, con, symbols, now, errors):
    params = dict(index="equities", from_date=(now-timedelta(days=7)).strftime("%d-%m-%Y"),
                  to_date=now.astimezone(providers.IST).strftime("%d-%m-%Y"))
    try:
        r = http.get(providers.NSE+"/api/corporate-announcements", params=params)
        r.raise_for_status()
        announcements = r.json()
        if not isinstance(announcements, list): raise ValueError("unexpected announcements format")
    except (httpx.HTTPError, ValueError) as exc:
        errors.append("NSE announcements unavailable: " + type(exc).__name__)
        announcements = None
    try:
        r = http.get(providers.NSE+"/api/corporate-board-meetings", params={"index":"equities"})
        r.raise_for_status()
        meetings = r.json()
        if not isinstance(meetings, list): raise ValueError("unexpected calendar format")
    except (httpx.HTTPError, ValueError) as exc:
        errors.append("NSE earnings calendar unavailable: " + type(exc).__name__)
        meetings = None
    seen = datetime.now(timezone.utc)
    news, calendars = providers.events(announcements or [], meetings or [], seen)
    for symbol in symbols:
        if announcements is not None:
            items = sorted(news.get(symbol, []), key=lambda e: e["published_at"], reverse=True)
            store.save(con, symbol, "news", "NSE corporate announcements", dict(
                events=items[:10], event_count=len(items), checked_at=seen.isoformat(),
                note="No matching filing is not a guarantee of no adverse news"), seen.isoformat(), seen)
        if meetings is not None:
            store.save(con, symbol, "earnings", "NSE financial-results board meetings",
                calendars.get(symbol, dict(date=None, note="No announced results meeting found; date remains unknown")), seen.isoformat(), seen)


def capture_options(http, con, now, errors):
    # Optional. Cached stale chains cannot acquire a new availability timestamp.
    from app.options_intelligence import _analyze_option_chain
    for symbol in ("NIFTY", "BANKNIFTY"):
        try:
            spot, rows, published, expiry = providers.fetch_chain(http, symbol, now)
            result = _analyze_option_chain(symbol, rows, spot,
                                          "NSE nearest unexpired index option chain", -.08)
            result["published_at"] = published.isoformat()
            result["expiry"] = expiry
            seen = datetime.now(timezone.utc)
            store.save(con, symbol, "options_snapshot", "NSE index option chain", result, seen.isoformat(), seen)
            if 0 <= (seen-published).total_seconds() <= 300:
                store.save(con, symbol, "options", "NSE index option chain", result, seen.isoformat(), seen)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            errors.append(symbol+" options unavailable: "+type(exc).__name__)


def run(args):
    now = datetime.now(timezone.utc)
    output = Path(args.output_db).resolve()
    main_path = Path(args.market_db).resolve()
    if output == main_path or output.name in ("v2_paper.db", "trading_agent.db"):
        raise ValueError("screening output must be a dedicated database")
    output.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(output, timeout=10)
    # Refuse an accidental paper DB even if it has a different file name.
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables - {"evidence", "screens"}:
        con.close()
        raise ValueError("refusing non-screening output database")
    store.initialise(con)
    con.execute("PRAGMA journal_mode=WAL")
    main = sqlite3.connect(f"file:{main_path}?mode=ro", uri=True, timeout=10)
    errors = []
    try:
        refresh_membership(args.reference_db)
        now = datetime.now(timezone.utc)
        eligible, _ = snapshot(now, args.reference_db)
        tails, _ = load_panel(main, "IN", topn=3000, min_bars=127)
        cutoff = now.astimezone(providers.IST).date()
        if now.astimezone(providers.IST).hour < 16: cutoff -= timedelta(days=1)
        tails = {s:g.loc[g.index <= pd.Timestamp(cutoff)] for s,g in tails.items()}
        dates = [g.index[-1] for g in tails.values() if not g.empty]
        if not dates: raise ValueError("completed-session prices unavailable")
        asof = max(dates)
        from app.index_history import capture as capture_index_history
        tails.update(capture_index_history(con,asof,errors))
        sectors = {}
        wanted = []
        features = {}
        for s in sorted(eligible or []):
            f = _features(tails[s], asof) if s in tails else None
            if f and f["turnover"] >= MIN_TURNOVER and f["price"] >= 50:
                wanted.append(s);features[s]=f
        headers = {"User-Agent":"Mozilla/5.0", "Referer":providers.NSE+"/"}
        with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as http:
            try: http.get(providers.NSE)
            except httpx.HTTPError: pass
            try:
                sector_file = providers.fetch_sectors(http)
                seen = datetime.now(timezone.utc)
                for symbol in wanted:
                    if symbol in sector_file:
                        store.save(con, symbol, "sector", "NSE Nifty 500 constituent industry", dict(
                            industry=sector_file[symbol]), seen.isoformat(), seen)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append("NSE sectors unavailable: "+type(exc).__name__)
            seen = datetime.now(timezone.utc)
            for symbol in wanted:
                sector = store.latest(con, symbol, "sector", seen, 7)
                if sector: sectors[symbol] = sector["industry"]
            try:
                delivery = providers.fetch_delivery(http, asof)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                delivery = None
                errors.append("NSE delivery unavailable: "+type(exc).__name__)
            try:
                flows = providers.fetch_flows(http, asof)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                flows = None
                errors.append("NSE current-session flows unavailable: "+type(exc).__name__)
            capture_participation(main, con, wanted, asof, now, delivery, flows)
            capture_events(http, con, wanted, now, errors)
            capture_options(http, con, now, errors)
            now = datetime.now(timezone.utc)
            cached = {s:store.latest(con,s,'fundamentals',now,7) for s in wanted}
            previous_attempts = {s:store.latest(con,s,'fundamental_attempt',now,14) for s in wanted}
            previous_attempts = {s:r for s,r in previous_attempts.items() if r}
            missing = statement_queue(wanted,cached,features,previous_attempts,now)
            refresh = capture_statements(http,con,missing,errors,args.fundamentals_limit)
        now = datetime.now(timezone.utc)
        result = build(tails, eligible, sectors, con, now, asof)
        result["feed_errors"] = errors
        result['fundamentals_refresh'] = refresh
        result["price_stale"] = (now.astimezone(providers.IST).date()-asof.date()).days > 4
        con.execute("INSERT INTO screens VALUES(?,?)", (now.isoformat(), json.dumps(result, allow_nan=False)))
        con.commit()
        print(json.dumps(dict(status=result["status"], generated_at=result["generated_at"],
            price_asof=result["price_asof"], liquid=result["liquid_count"],
            fundamentals=sum(bool(r["fundamentals"]) for r in result["equities"]),
            earnings_history_ready=sum(valid_income_history(r['fundamentals'],now.astimezone(providers.IST).date().isoformat()) for r in result['equities']),
            refresh=refresh,feed_errors=errors, output=str(output))))
        return result
    finally:
        main.close()
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", default=os.getenv("OPENSTOCKS_DB", str(ROOT/"var"/"trading_agent.db")))
    parser.add_argument("--output-db", default=DEFAULT_DB)
    parser.add_argument("--reference-db", default=os.getenv("SLEEVE_REFERENCE_DB", str(ROOT/"var"/"sleeve_reference.db")))
    parser.add_argument("--fundamentals-limit", type=int, default=400,
                        help="Maximum statement requests per refresh (3 concurrent, paced 1/s, 8-minute submission budget)")
    args = parser.parse_args()
    if not 0 <= args.fundamentals_limit <= 400: parser.error("fundamentals limit must be 0..400")
    run(args)


if __name__ == "__main__":
    main()
