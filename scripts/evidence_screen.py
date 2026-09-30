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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import pandas as pd
from app.screening import providers, store
from app.screening.screen import build, _features, MIN_TURNOVER
from app.sleeves.reference import refresh_membership, snapshot
from app.v2_engine import load_panel

LOG = logging.getLogger("openstocks.screening")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = os.getenv("SCREENING_DB", str(ROOT / "var" / "screening.db"))


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def capture_participation(main, evidence, symbols, asof, now):
    stamp = str(asof)[:10]
    for symbol in symbols:
        rows = _rows(main, "SELECT date,delivery_pct FROM delivery_data WHERE symbol=? "
                     "AND date<=? ORDER BY date DESC LIMIT 21", (symbol, stamp))
        valid = [float(p) for _, p in rows[1:] if p is not None and 0 <= float(p) <= 100]
        deals = _rows(main, "SELECT date,side,quantity,price FROM bulk_deals "
                      "WHERE symbol=? AND date<=? AND date>=?", (symbol, stamp,
                      (asof-timedelta(days=7)).date().isoformat()))
        payload = dict(session=rows[0][0] if rows else None,
            delivery_pct=rows[0][1] if rows and rows[0][1] is not None and 0 <= rows[0][1] <= 100 else None,
            delivery_avg20_pct=sum(valid)/len(valid) if len(valid) >= 10 else None,
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
        fii_dii=[dict(session=d, category=c, net_inr_crore=v) for d,c,v in flows],
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
        sectors = {s:sec for s,sec in _rows(main, "SELECT symbol,sector FROM universe WHERE exchange='NSE'")}
        wanted = []
        for s in sorted(eligible or []):
            f = _features(tails[s], asof) if s in tails else None
            if f and f["turnover"] >= MIN_TURNOVER and f["price"] >= 50: wanted.append(s)
        capture_participation(main, con, wanted, asof, now)
        headers = {"User-Agent":"Mozilla/5.0", "Referer":providers.NSE+"/"}
        with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as http:
            try: http.get(providers.NSE)
            except httpx.HTTPError: pass
            capture_events(http, con, wanted, now, errors)
            capture_options(http, con, now, errors)
            missing = [s for s in wanted if store.latest(con, s, "fundamentals", now, 7) is None]
            # Oldest/never-fetched first. Bound traffic; successful captures rotate
            # out of the queue, failures do not permanently starve other names.
            previous_attempts = {s:r for s,r in _rows(con,
                "SELECT symbol,MAX(known_at) FROM evidence WHERE kind='fundamental_attempt' GROUP BY symbol")}
            missing.sort(key=lambda s:(previous_attempts.get(s, ""), s))
            with ThreadPoolExecutor(max_workers=3) as pool:
                tasks = {pool.submit(providers.fetch_statements, http, s, now):s for s in missing[:args.fundamentals_limit]}
                for task in as_completed(tasks):
                    symbol = tasks[task]
                    seen = datetime.now(timezone.utc)
                    try:
                        data = task.result()
                        store.save(con, symbol, "fundamentals", "Yahoo Finance financial-statement timeseries", data, seen.isoformat(), seen)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        errors.append(symbol+" fundamentals unavailable: "+type(exc).__name__)
                    store.save(con, symbol, "fundamental_attempt", "screen refresh", {}, seen.isoformat(), seen)
        now = datetime.now(timezone.utc)
        result = build(tails, eligible, sectors, con, now, asof)
        result["feed_errors"] = errors
        result["price_stale"] = (now.astimezone(providers.IST).date()-asof.date()).days > 4
        con.execute("INSERT INTO screens VALUES(?,?)", (now.isoformat(), json.dumps(result, allow_nan=False)))
        con.commit()
        print(json.dumps(dict(status=result["status"], generated_at=result["generated_at"],
            price_asof=result["price_asof"], liquid=result["liquid_count"],
            fundamentals=sum(bool(r["fundamentals"]) for r in result["equities"]),
            feed_errors=errors, output=str(output))))
        return result
    finally:
        main.close()
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", default=os.getenv("OPENSTOCKS_DB", str(ROOT/"var"/"trading_agent.db")))
    parser.add_argument("--output-db", default=DEFAULT_DB)
    parser.add_argument("--reference-db", default=os.getenv("SLEEVE_REFERENCE_DB", str(ROOT/"var"/"sleeve_reference.db")))
    parser.add_argument("--fundamentals-limit", type=int, default=40)
    args = parser.parse_args()
    if not 0 <= args.fundamentals_limit <= 100: parser.error("fundamentals limit must be 0..100")
    run(args)


if __name__ == "__main__":
    main()
