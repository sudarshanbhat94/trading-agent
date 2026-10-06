#!/usr/bin/env python3
"""Import a dated official Upstox BOD file. Does not enable any trading route.

Run daily before the session. --input accepts an archived JSON/JSON.gz file;
--download uses only the fixed official source. Retain the source in var/.
Unknown series/tick units/settlement remain UNKNOWN and cannot certify orders.
"""
import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.instrument_catalog import InstrumentError, import_snapshot, upstox_contract

SOURCE = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


def sync(con, content, source_day, observed_at, source=SOURCE):
    raw = gzip.decompress(content) if content[:2]==b'\x1f\x8b' else content
    data = json.loads(raw)
    if not isinstance(data,list) or not data:
        raise InstrumentError("provider catalogue must contain contract rows")
    contracts, quarantine = [], []
    for row in data:
        try:
            contracts.append(upstox_contract(row))
        except (ValueError,TypeError,KeyError,ArithmeticError):
            quarantine.append(str(row.get("instrument_key","unidentified")) if isinstance(row,dict) else "invalid-row")
    sid = import_snapshot(con,contracts,provider="upstox",source=source,
                          source_day=source_day,observed_at=observed_at)
    return dict(snapshot_id=sid,source_sha256=hashlib.sha256(content).hexdigest(),
                imported=len(contracts),quarantined=len(quarantine),observed_at=observed_at,
                source_day_declared=source_day,execution_enabled=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input",type=Path)
    group.add_argument("--download",action="store_true")
    parser.add_argument("--db",type=Path,required=True)
    parser.add_argument("--source-day",required=True,help="YYYY-MM-DD source trading date; never assume this proves provider freshness")
    parser.add_argument("--archive-dir",type=Path,default=Path("var/instrument_evidence"))
    args = parser.parse_args()
    if args.download:
        import httpx
        response = httpx.get(SOURCE,timeout=60,follow_redirects=False)
        response.raise_for_status()
        content = response.content
    else:
        content = args.input.read_bytes()
    observed = datetime.now(timezone.utc).isoformat()
    args.archive_dir.mkdir(parents=True,exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    (args.archive_dir/(digest+".source")).write_bytes(content)
    with sqlite3.connect(args.db) as con:
        result = sync(con,content,args.source_day,observed)
    (args.archive_dir/(digest+".review.json")).write_text(json.dumps(result,indent=2))
    print(json.dumps(result,sort_keys=True))


if __name__ == "__main__":
    main()
