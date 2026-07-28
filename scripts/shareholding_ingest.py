"""NSE shareholding-pattern ingester — promoter and public holding per symbol.

The brief asks for promoter and institutional holding. Most of the fundamentals
list (revenue, EPS, PE, ROE) is not reachable cheaply: NSE's quarterly-results
index carries only metadata, with the actual figures behind a per-record
detail link — 3,800+ fetches per quarter — and Yahoo's quoteSummary endpoint,
the usual free substitute, now returns 401 without a crumb.

Shareholding is the exception: NSE publishes it with the percentages INLINE, so
one request yields the promoter and public split for ~2,275 companies. That is
what this ingests.

  python3 scripts/shareholding_ingest.py --once
  python3 scripts/shareholding_ingest.py --limit 50      # small validation run

Quarterly data, so a weekly timer is ample. Read-only on everything except its
own table. Unlike the announcements feed this does NOT prune: shareholding
history is the point — a promoter stake falling over three quarters is a
signal, and you cannot see it from one row.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
URL = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.nseindia.com/"}


def _pct(value):
    """A holding percentage, or None. NSE sends these as strings, sometimes
    blank, occasionally with stray commas."""
    if value in (None, "", "-"):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if number != number or not 0.0 <= number <= 100.0:
        return None          # a holding outside 0-100 is corrupt, not a datum
    return round(number, 2)


def _as_of(value):
    """"30-JUN-2026" -> "2026-06-30". Returns None when unparseable."""
    # strptime already matches month names case-insensitively, so "30-JUN-2026"
    # parses with %b directly. Upper-casing the FORMAT would turn %b into %B and
    # change what it means.
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date().isoformat()
        except (TypeError, ValueError):
            continue
    return None


def parse_record(row):
    """One API record -> a storable row, or None if it is not usable.

    Pure, so the parsing is testable without touching the network.
    """
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol") or "").upper().strip()
    as_of = _as_of(row.get("date"))
    if not symbol or not as_of:
        return None
    promoter = _pct(row.get("pr_and_prgrp"))
    public = _pct(row.get("public_val"))
    if promoter is None and public is None:
        return None          # nothing worth storing
    return {
        "symbol": symbol,
        "as_of": as_of,
        "promoter_pct": promoter,
        "public_pct": public,
        "employee_trust_pct": _pct(row.get("employeeTrusts")),
        "company": str(row.get("name") or "")[:120],
        "submitted_at": str(row.get("submissionDate") or "")[:40],
    }


def ensure_schema(con):
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS shareholding(
        symbol TEXT, as_of TEXT, promoter_pct REAL, public_pct REAL,
        employee_trust_pct REAL, company TEXT, submitted_at TEXT,
        ingested_at TEXT, PRIMARY KEY(symbol, as_of))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shareholding_sym ON shareholding(symbol, as_of)")
    con.commit()


def fetch(client):
    client.get("https://www.nseindia.com")       # bootstrap cookies
    time.sleep(1)
    response = client.get(URL)
    if response.status_code != 200:
        print(f"NSE shareholding fetch failed: HTTP {response.status_code}", flush=True)
        return []
    try:
        payload = response.json()
    except Exception as exc:
        print(f"NSE shareholding response was not JSON: {str(exc)[:120]}", flush=True)
        return []
    return payload if isinstance(payload, list) else (payload.get("data") or [])


def ingest(limit=0, verbose=True):
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=10000")
    ensure_schema(con)
    try:
        with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
            rows = fetch(client)
    except Exception as exc:
        print("NSE shareholding error:", str(exc)[:140], flush=True)
        con.close()
        return 0

    if limit:
        rows = rows[:limit]
    now = datetime.now(timezone.utc).isoformat()
    stored = skipped = 0
    for row in rows:
        record = parse_record(row)
        if not record:
            skipped += 1
            continue
        try:
            # A revised filing for the same quarter should replace the original,
            # so this is REPLACE rather than IGNORE.
            con.execute(
                "INSERT OR REPLACE INTO shareholding(symbol,as_of,promoter_pct,public_pct,"
                "employee_trust_pct,company,submitted_at,ingested_at) VALUES(?,?,?,?,?,?,?,?)",
                (record["symbol"], record["as_of"], record["promoter_pct"], record["public_pct"],
                 record["employee_trust_pct"], record["company"], record["submitted_at"], now))
            stored += 1
        except Exception:
            skipped += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM shareholding").fetchone()[0]
    quarters = con.execute("SELECT COUNT(DISTINCT as_of) FROM shareholding").fetchone()[0]
    con.close()
    if verbose:
        print(f"[shareholding] pulled {len(rows)} · stored {stored} · unusable {skipped} "
              f"· table now {total} rows across {quarters} quarter(s)", flush=True)
    return stored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="no-op flag; this script is always run-once")
    parser.add_argument("--limit", type=int, default=0, help="cap records (for validation)")
    args = parser.parse_args()
    print(f"shareholding ingest start {datetime.now(timezone.utc).isoformat()[:19]}Z", flush=True)
    ingest(limit=args.limit)
    print("shareholding ingest done", flush=True)


if __name__ == "__main__":
    main()
