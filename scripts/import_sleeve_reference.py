"""Import provider-sourced point-in-time fundamentals and Nifty membership.

JSON: {"source":"provider identifier", "known_at":"<UTC ISO timestamp>",
       "fundamentals":[{"symbol":"...","roe_pct":20,"debt_equity":0.4,
                         "eps_growth_std_pct":10}],
       "membership":{"NIFTY500":["..."]}}
roe_pct and eps_growth_std_pct are percentages, debt_equity is a ratio.
Use the actual publication/availability time, not the fiscal period end.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.sleeves.reference import import_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input",type=Path)
    parser.add_argument("--db",required=True,type=Path,help="Dedicated sleeve_reference.db; never a paper book")
    args=parser.parse_args()
    data=json.loads(args.input.read_text())
    con = sqlite3.connect(args.db)
    try:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"fundamentals","membership"}:
            raise SystemExit("Refusing to import into a non-reference database")
        print(json.dumps(import_snapshot(con,data)))
    finally:
        con.close()


if __name__ == "__main__":
    main()
