"""Index futures & options history from the NSE derivatives bhavcopy.

Data BEFORE strategy, deliberately. Every intraday capability built this week
had to ship unvalidated because no history existed; F&O is different — NSE
publishes a full end-of-day bhavcopy that is backfillable, so this can be
measured properly before a rupee is risked. Given options are leveraged, that
ordering is not optional.

Each file carries every contract's OHLC, settlement, open interest, change in
OI, volume, the underlying price and the lot size. Scoped to index derivatives
(NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) that is ~2,700 rows a session, about
70 MB a year, against ~1 GB for the whole F&O segment.

SENSEX is not here: it trades on BSE, which publishes its own bhavcopy in a
different format. Adding it is a separate source, not a parameter change.

  .venv/bin/python scripts/fo_ingest.py --backfill-days 120
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import zipfile
from datetime import date, datetime, timedelta

import httpx

DB = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
URL = ("https://nsearchives.nseindia.com/content/fo/"
       "BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip")
HOME = "https://www.nseindia.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Referer": HOME + "/"}

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")


def _num(value, default=None):
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text not in ("", "-") else default
    except (TypeError, ValueError):
        return default


def schema(con):
    con.execute("PRAGMA journal_mode=WAL")
    # One row per contract per session. instrument is FUT or OPT; for options
    # `strike` and `opt_type` are set, for futures they are NULL.
    con.execute("""CREATE TABLE IF NOT EXISTS fo_bhav(
        date TEXT NOT NULL, symbol TEXT NOT NULL, expiry TEXT NOT NULL,
        instrument TEXT NOT NULL, strike REAL, opt_type TEXT,
        open REAL, high REAL, low REAL, close REAL, settle REAL,
        underlying REAL, oi REAL, oi_change REAL, volume REAL, value REAL,
        lot_size REAL)""")
    # Uniqueness via a COALESCE index, NOT a plain primary key. Futures carry no
    # strike or option type, and SQLite treats NULLs as DISTINCT in a key — so a
    # PRIMARY KEY over these columns lets every futures row insert again on each
    # re-run, silently multiplying exactly the contracts a strategy would size
    # against. The expression index keeps NULL in the data model (a future
    # genuinely has no strike) while still collapsing duplicates.
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fo_contract ON fo_bhav("
                "date, symbol, expiry, instrument, COALESCE(strike, -1),"
                " COALESCE(opt_type, ''))")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fo_sym_date ON fo_bhav(symbol, date)")
    con.commit()


def client():
    c = httpx.Client(headers=UA, timeout=60, follow_redirects=True)
    try:
        c.get(HOME)
    except Exception:
        pass
    return c


def parse(content, symbols=INDEX_SYMBOLS):
    """Bhavcopy zip bytes -> list of row tuples for the wanted symbols."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return []
    name = archive.namelist()[0]
    reader = csv.DictReader(io.TextIOWrapper(archive.open(name), "utf-8"))
    wanted = set(symbols)
    out = []
    for row in reader:
        symbol = (row.get("TckrSymb") or "").strip().upper()
        if symbol not in wanted:
            continue
        opt_type = (row.get("OptnTp") or "").strip().upper()
        # NSE leaves OptnTp blank on futures rather than marking them, so the
        # instrument type has to be derived rather than read.
        instrument = "OPT" if opt_type in ("CE", "PE") else "FUT"
        strike = _num(row.get("StrkPric")) if instrument == "OPT" else None
        traded = (row.get("TradDt") or "").strip()[:10]
        expiry = (row.get("XpryDt") or "").strip()[:10]
        if not traded or not expiry:
            continue
        out.append((
            traded, symbol, expiry, instrument,
            strike, opt_type if instrument == "OPT" else None,
            _num(row.get("OpnPric")), _num(row.get("HghPric")), _num(row.get("LwPric")),
            _num(row.get("ClsPric")), _num(row.get("SttlmPric")),
            _num(row.get("UndrlygPric")), _num(row.get("OpnIntrst"), 0.0),
            _num(row.get("ChngInOpnIntrst"), 0.0), _num(row.get("TtlTradgVol"), 0.0),
            _num(row.get("TtlTrfVal"), 0.0), _num(row.get("NewBrdLotQty")),
        ))
    return out


def ingest_day(con, http, day, symbols=INDEX_SYMBOLS):
    """One session. Returns rows written; 0 when NSE has no file (holiday)."""
    try:
        response = http.get(URL % day.strftime("%Y%m%d"))
    except Exception as exc:
        print(f"  {day}: {exc}")
        return 0
    if response.status_code != 200 or not response.content:
        return 0
    rows = parse(response.content, symbols)
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO fo_bhav(date,symbol,expiry,instrument,strike,opt_type,"
            "open,high,low,close,settle,underlying,oi,oi_change,volume,value,lot_size)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--backfill-days", type=int, default=30)
    ap.add_argument("--symbols", default=",".join(INDEX_SYMBOLS))
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    con = sqlite3.connect(args.db, timeout=120)
    schema(con)
    http = client()
    total = sessions = 0
    for back in range(1, args.backfill_days + 1):
        day = date.today() - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        n = ingest_day(con, http, day, symbols)
        if n:
            sessions += 1
            total += n
            print(f"  {day}: {n:,} contracts")
    print(f"\nwrote {total:,} rows across {sessions} sessions")
    stats = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) FROM fo_bhav").fetchone()
    print(f"fo_bhav: {stats[0]:,} rows, {stats[1]} sessions, {stats[2]}..{stats[3]}")
    for sym, n, fut, opt in con.execute(
            "SELECT symbol, COUNT(*), SUM(instrument='FUT'), SUM(instrument='OPT')"
            " FROM fo_bhav GROUP BY symbol ORDER BY 2 DESC"):
        print(f"  {sym:<12} {n:>8,} rows  ({fut:,} futures / {opt:,} options)")
    size = os.path.getsize(args.db) if os.path.exists(args.db) else 0
    print(f"database: {size / 1e6:.1f} MB")
    con.close()


if __name__ == "__main__":
    main()
