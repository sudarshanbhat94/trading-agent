"""Ingest the NSE datasets the engine was missing or had lost.

Three of these were believed to exist and did not:

  delivery   `delivery_data` stopped on 2026-06-17 with no script, timer or
             service anywhere in the repo — it had been populated once and
             abandoned. Delivery percentage is one of the few genuinely Indian
             signals (high delivery = real accumulation rather than intraday
             churn), and it was silently dead for six weeks.
  sector     2,594 NSE symbols all carried the single label "NSE Listed
             Equity", so the engine's sector concentration cap could never
             bind — it was comparing every stock to the same bucket.
  fii_dii    absent entirely. Foreign and domestic institutional net flows are
             a real regime input in India and are published daily.
  bulk_deals absent entirely. Shows an institution's actual footprint on a
             specific name, rather than inferred from volume.

Everything here is free and official from NSE, and every write is idempotent
(INSERT OR REPLACE on a primary key), so a re-run repairs rather than
duplicates. Run after the close.

  .venv/bin/python scripts/nse_reference_ingest.py --backfill-days 30
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
from datetime import date, datetime, timedelta

import httpx

DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
HOME = "https://www.nseindia.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": HOME + "/"}

BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_%s.csv"
FIIDII_URL = HOME + "/api/fiidiiTradeReact"
BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
ALLINDICES_URL = HOME + "/api/allIndices"
# Participant-wise open interest: FII / DII / Client / Pro positioning in index
# futures and options. This is the cleanest institutional read available in
# India and has no equivalent in most markets.
PARTICIPANT_OI_URL = ("https://nsearchives.nseindia.com/content/nsccl/"
                      "fao_participant_oi_%s.csv")
# Index constituent lists carry a real Industry column. Together these cover the
# large/mid/small-cap universe the engine actually trades.
INDEX_LISTS = {
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "niftytotalmarket": "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
}


def client():
    c = httpx.Client(headers=UA, timeout=45, follow_redirects=True)
    try:
        c.get(HOME)                       # bootstrap cookies, as NSE requires
    except Exception:
        pass
    return c


def _schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS delivery_data("
                "symbol TEXT, date TEXT, close REAL, total_volume REAL,"
                " delivery_volume REAL, delivery_pct REAL)")
    # The original table had no key, so a re-run would duplicate every row.
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_delivery_sym_date "
                "ON delivery_data(symbol, date)")
    con.execute("CREATE TABLE IF NOT EXISTS fii_dii_flows("
                "date TEXT, category TEXT, buy_value REAL, sell_value REAL,"
                " net_value REAL, PRIMARY KEY(date, category))")
    con.execute("CREATE TABLE IF NOT EXISTS india_vix("
                "date TEXT PRIMARY KEY, value REAL, pct_change REAL,"
                " day_high REAL, day_low REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS participant_oi("
                "date TEXT, client_type TEXT,"
                " fut_idx_long REAL, fut_idx_short REAL,"
                " opt_idx_call_long REAL, opt_idx_put_long REAL,"
                " opt_idx_call_short REAL, opt_idx_put_short REAL,"
                " PRIMARY KEY(date, client_type))")
    con.execute("CREATE TABLE IF NOT EXISTS bulk_deals("
                "date TEXT, symbol TEXT, client TEXT, side TEXT, quantity REAL,"
                " price REAL, PRIMARY KEY(date, symbol, client, side, quantity))")
    con.commit()


def _num(value, default=None):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def ingest_delivery(con, http, day):
    """One session's bhavcopy. Returns rows written (0 if NSE has no file)."""
    url = BHAV_URL % day.strftime("%d%m%Y")
    try:
        response = http.get(url)
    except Exception as exc:
        print(f"  delivery {day}: {exc}")
        return 0
    if response.status_code != 200 or not response.text.strip():
        return 0                          # weekend/holiday — no file is normal
    rows = []
    for row in csv.DictReader(io.StringIO(response.text)):
        clean = {k.strip(): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items() if k}
        if clean.get("SERIES") != "EQ":   # EQ only: exclude bonds, ETFs, SME
            continue
        symbol = clean.get("SYMBOL")
        pct = _num(clean.get("DELIV_PER"))
        if not symbol or pct is None:     # NSE writes '-' when not applicable
            continue
        rows.append((symbol.upper(), day.isoformat(), _num(clean.get("CLOSE_PRICE")),
                     _num(clean.get("TTL_TRD_QNTY")), _num(clean.get("DELIV_QTY")), pct))
    if rows:
        con.executemany("INSERT OR REPLACE INTO delivery_data"
                        "(symbol,date,close,total_volume,delivery_volume,delivery_pct)"
                        " VALUES(?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def ingest_fii_dii(con, http):
    try:
        payload = http.get(FIIDII_URL).json()
    except Exception as exc:
        print(f"  fii/dii: {exc}")
        return 0
    rows = []
    for item in payload or []:
        raw = str(item.get("date") or "").strip()
        try:
            day = datetime.strptime(raw, "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        rows.append((day, str(item.get("category") or "").strip(),
                     _num(item.get("buyValue"), 0.0), _num(item.get("sellValue"), 0.0),
                     _num(item.get("netValue"), 0.0)))
    if rows:
        con.executemany("INSERT OR REPLACE INTO fii_dii_flows"
                        "(date,category,buy_value,sell_value,net_value) VALUES(?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def ingest_vix(con, http, day=None):
    """India VIX from the all-indices snapshot.

    VIX is what makes IV rank computable: knowing an option costs 2% of spot is
    meaningless without knowing whether that is high or low for this market.
    """
    try:
        payload = http.get(ALLINDICES_URL).json()
    except Exception as exc:
        print(f"  vix: {exc}")
        return 0
    for item in (payload or {}).get("data") or []:
        if "VIX" not in str(item.get("index") or "").upper():
            continue
        stamp = (day or date.today()).isoformat()
        con.execute("INSERT OR REPLACE INTO india_vix(date,value,pct_change,day_high,day_low)"
                    " VALUES(?,?,?,?,?)",
                    (stamp, _num(item.get("last")), _num(item.get("percentChange")),
                     _num(item.get("high")), _num(item.get("low"))))
        con.commit()
        return 1
    return 0


def ingest_participant_oi(con, http, day):
    """FII / DII / Client / Pro positioning in index futures and options.

    Read as positioning rather than as a signal to copy: FIIs being heavily long
    index futures says the move has already been paid for, not that it will
    continue. It is the second row of the file (the header is a title line) that
    carries the real column names.
    """
    url = PARTICIPANT_OI_URL % day.strftime("%d%m%Y")
    try:
        response = http.get(url)
    except Exception as exc:
        print(f"  participant oi {day}: {exc}")
        return 0
    if response.status_code != 200 or not response.text.strip():
        return 0
    lines = response.text.strip().splitlines()
    if len(lines) < 2:
        return 0
    reader = csv.DictReader(io.StringIO("\n".join(lines[1:])))
    rows = []
    for row in reader:
        clean = {(k or "").strip(): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items()}
        who = clean.get("Client Type")
        if not who:
            continue
        rows.append((day.isoformat(), who,
                     _num(clean.get("Future Index Long"), 0.0),
                     _num(clean.get("Future Index Short"), 0.0),
                     _num(clean.get("Option Index Call Long"), 0.0),
                     _num(clean.get("Option Index Put Long"), 0.0),
                     _num(clean.get("Option Index Call Short"), 0.0),
                     _num(clean.get("Option Index Put Short"), 0.0)))
    if rows:
        con.executemany("INSERT OR REPLACE INTO participant_oi(date,client_type,"
                        "fut_idx_long,fut_idx_short,opt_idx_call_long,opt_idx_put_long,"
                        "opt_idx_call_short,opt_idx_put_short) VALUES(?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def ingest_bulk_deals(con, http):
    try:
        text = http.get(BULK_URL).text
    except Exception as exc:
        print(f"  bulk deals: {exc}")
        return 0
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        clean = {k.strip(): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items() if k}
        raw = clean.get("Date")
        try:
            day = datetime.strptime(raw, "%d-%b-%Y").date().isoformat()
        except (TypeError, ValueError):
            continue
        symbol = (clean.get("Symbol") or "").upper()
        if not symbol:
            continue
        rows.append((day, symbol, clean.get("Client Name") or "",
                     (clean.get("Buy/Sell") or "").upper(),
                     _num(clean.get("Quantity Traded"), 0.0),
                     _num(clean.get("Trade Price / Wght. Avg. Price"), 0.0)))
    if rows:
        con.executemany("INSERT OR REPLACE INTO bulk_deals"
                        "(date,symbol,client,side,quantity,price) VALUES(?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def ingest_sectors(con, http):
    """Replace the useless single 'NSE Listed Equity' label with real industry."""
    mapping = {}
    for name, url in INDEX_LISTS.items():
        try:
            text = http.get(url).text
        except Exception as exc:
            print(f"  sectors {name}: {exc}")
            continue
        for row in csv.DictReader(io.StringIO(text)):
            clean = {k.strip(): (v.strip() if isinstance(v, str) else v)
                     for k, v in row.items() if k}
            symbol = (clean.get("Symbol") or "").upper()
            industry = clean.get("Industry") or ""
            if symbol and industry:
                mapping.setdefault(symbol, industry)
    updated = 0
    for symbol, industry in mapping.items():
        cur = con.execute("UPDATE universe SET sector=? WHERE UPPER(symbol)=? AND exchange!=?",
                          (industry, symbol, "US"))
        updated += cur.rowcount
    con.commit()
    return updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--backfill-days", type=int, default=7,
                    help="how many sessions of delivery data to (re)fetch")
    ap.add_argument("--skip-sectors", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    _schema(con)
    http = client()

    total = 0
    for back in range(1, args.backfill_days + 1):
        day = date.today() - timedelta(days=back)
        if day.weekday() >= 5:            # NSE is shut at weekends
            continue
        n = ingest_delivery(con, http, day)
        total += n
        if n:
            print(f"  delivery {day}: {n:,} symbols")
    print(f"delivery rows written: {total:,}")
    print(f"fii/dii rows written : {ingest_fii_dii(con, http):,}")
    print(f"india vix            : {ingest_vix(con, http):,}")
    poi = 0
    for back in range(1, args.backfill_days + 1):
        day = date.today() - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        poi += ingest_participant_oi(con, http, day)
    print(f"participant OI rows  : {poi:,}")
    print(f"bulk deal rows       : {ingest_bulk_deals(con, http):,}")
    if not args.skip_sectors:
        print(f"sectors updated      : {ingest_sectors(con, http):,}")

    freshness = con.execute("SELECT MAX(date), COUNT(DISTINCT symbol) FROM delivery_data").fetchone()
    print(f"delivery_data now    : newest {freshness[0]}, {freshness[1]:,} symbols")
    sectors = con.execute("SELECT COUNT(DISTINCT sector) FROM universe WHERE exchange!='US'").fetchone()[0]
    print(f"distinct IN sectors  : {sectors}")
    con.close()


if __name__ == "__main__":
    main()
