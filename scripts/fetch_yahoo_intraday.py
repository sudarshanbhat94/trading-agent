"""Fetch real intraday history from Yahoo Finance (free public API) to bootstrap
an intraday backtest - independent of what the box has collected.

Yahoo gives ~60 days of 5-minute bars. We pull the most-liquid symbols per
market into a SEPARATE db (var/intraday_yahoo.db), so nothing touches the live
service DB. Polite: cookie session + delay + 429 backoff.

  python3 fetch_yahoo_intraday.py --markets IN,US --topn 150 --interval 5m --range 60d
"""
from __future__ import annotations

import argparse
import sqlite3
import time

import httpx

MAIN_DB = "/opt/opentrade/var/trading_agent.db"
OUT_DB = "/opt/opentrade/var/intraday_yahoo.db"
DAILY = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  market TEXT, symbol TEXT, ts INTEGER, open REAL, high REAL, low REAL,
  close REAL, volume REAL, PRIMARY KEY(market, symbol, ts));
CREATE TABLE IF NOT EXISTS fetch_log(market TEXT, symbol TEXT, bars INTEGER, status TEXT, at TEXT);
"""


def liquid_symbols(con, market, topn):
    src = DAILY[market]
    rows = con.execute(
        "SELECT symbol, AVG(close*volume) t, COUNT(*) n FROM candles WHERE source=? "
        "GROUP BY symbol HAVING n>=120", (src,)).fetchall()
    rows = [r for r in rows if r[1]]
    rows.sort(key=lambda r: -r[1])
    return [r[0] for r in rows[:topn]]


def yahoo_ticker(sym, market):
    return f"{sym}.NS" if market == "IN" else sym


def fetch_one(cl, ticker, interval, rng, retries=3):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
    for attempt in range(retries):
        try:
            r = cl.get(url, params={"interval": interval, "range": rng})
            if r.status_code == 429:
                time.sleep(3 + attempt * 3)
                continue
            if r.status_code != 200:
                return None, f"http{r.status_code}"
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(ts):
                o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
                if None in (o, h, l, c):
                    continue
                out.append((int(t), float(o), float(h), float(l), float(c), float(v or 0)))
            return out, "ok"
        except Exception as e:
            if attempt == retries - 1:
                return None, f"err:{type(e).__name__}"
            time.sleep(2)
    return None, "rate_limited"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="IN,US")
    ap.add_argument("--topn", type=int, default=150)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--range", default="60d")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=0, help="cap symbols (test)")
    ap.add_argument("--prune-days", type=int, default=0,
                    help="janitor: delete bars older than N days, cap fetch_log, VACUUM")
    args = ap.parse_args()
    main_con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True, timeout=60)
    out = sqlite3.connect(OUT_DB, timeout=60)
    out.executescript(SCHEMA)
    out.commit()
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as cl:
        try:
            cl.get("https://finance.yahoo.com")  # cookies
        except Exception:
            pass
        time.sleep(1)
        for market in [m.strip().upper() for m in args.markets.split(",") if m.strip()]:
            syms = liquid_symbols(main_con, market, args.topn)
            if args.limit:
                syms = syms[: args.limit]
            print(f"[{market}] fetching {len(syms)} symbols  {args.interval}/{args.range}")
            ok = total = 0
            for i, sym in enumerate(syms, 1):
                data, status = fetch_one(cl, yahoo_ticker(sym, market), args.interval, args.range)
                n = len(data) if data else 0
                if data:
                    out.executemany(
                        "INSERT OR REPLACE INTO bars(market,symbol,ts,open,high,low,close,volume) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        [(market, sym, *row) for row in data])
                    ok += 1
                    total += n
                out.execute("INSERT INTO fetch_log VALUES(?,?,?,?,datetime('now'))",
                            (market, sym, n, status))
                if i % 25 == 0:
                    out.commit()
                    print(f"  {market} {i}/{len(syms)}  ok={ok} bars={total:,}")
                time.sleep(args.delay)
            out.commit()
            print(f"[{market}] DONE ok={ok}/{len(syms)} total_bars={total:,}")
    if args.prune_days > 0:
        # janitor: keep the DB bounded (retention window + capped log + vacuum)
        cutoff = int(time.time()) - args.prune_days * 86400
        n1 = out.execute("DELETE FROM bars WHERE ts < ?", (cutoff,)).rowcount
        n2 = out.execute("DELETE FROM fetch_log WHERE at < datetime('now','-30 days')").rowcount
        out.commit()
        out.execute("VACUUM")
        print(f"janitor: pruned {n1:,} old bars, {n2:,} log rows, vacuumed")
    out.close()
    main_con.close()
    print("FETCH_COMPLETE")


if __name__ == "__main__":
    main()
