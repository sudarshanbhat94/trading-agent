"""Find — and optionally repair — trade rows whose P&L was never charged costs.

Exits once recorded the GROSS move while the cash ledger was charged on both
sides. Equity was therefore honest but every statistic derived from v2_trades —
realised P&L, win rate, profit factor, per-lane attribution — was flattered by
exactly the cost of trading. net_trade_pnl() fixed the write path on
2026-07-29; rows written before it kept the gross number.

On the live book that is two rows, and one of them matters more than its size:

    PARADEEP  stored -500.18  actual -605.61
    TATACAP   stored  +14.20  actual  -91.18   <- a LOSS recorded as a WIN

A losing trade counted as a winner does not just shift the average, it moves the
win rate and the profit factor — the two numbers a subscriber would be shown.

Default is --check: report and change nothing. --fix rewrites pnl and
return_pct from net_trade_pnl(), the same function the engine and both manual
sell paths use, so the repaired rows agree with the cash ledger by construction.

Run:  python scripts/audit_trade_costs.py [--fix] [--db path]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.v2_live import net_trade_pnl          # noqa: E402  the ONE definition

V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
TOLERANCE = 0.01                                # rupees; float noise, not a defect


def scan(con):
    """Rows whose stored P&L disagrees with the shared cost math."""
    out = []
    for row in con.execute(
            "SELECT id, market, strategy, symbol, exit_date, entry_price, exit_price,"
            " shares, pnl, return_pct FROM v2_trades ORDER BY id"):
        rid, market, strat, sym, xdate, entry, exit_price, shares, pnl, ret = row
        if not (entry and shares):
            continue
        want, want_pct = net_trade_pnl(market, shares, entry, exit_price)
        if abs((pnl or 0.0) - want) > TOLERANCE:
            out.append(dict(id=rid, market=market, strategy=strat, symbol=sym,
                            exit_date=xdate, stored=pnl or 0.0, actual=want,
                            stored_pct=ret or 0.0, actual_pct=want_pct,
                            flips=(pnl or 0.0) > 0 >= want))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="rewrite the rows (default: report only)")
    ap.add_argument("--db", default=V2_DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    bad = scan(con)
    if not bad:
        print("all trade rows agree with net_trade_pnl() — nothing to repair")
        con.close()
        return

    print(f"{len(bad)} row(s) carrying GROSS P&L:\n")
    print(f"{'id':>5} {'date':<12}{'symbol':<14}{'stored':>10}{'actual':>10}{'diff':>10}  ")
    delta = 0.0
    for r in bad:
        delta += r["actual"] - r["stored"]
        flag = "  <- LOSS SHOWN AS WIN" if r["flips"] else ""
        print(f"{r['id']:>5} {r['exit_date']:<12}{r['symbol']:<14}"
              f"{r['stored']:>10.2f}{r['actual']:>10.2f}{r['actual'] - r['stored']:>10.2f}{flag}")
    print(f"\nrealised P&L is overstated by Rs {-delta:.2f}")
    flips = sum(1 for r in bad if r["flips"])
    if flips:
        print(f"{flips} trade(s) counted as winners that lost money — this moves win rate and PF")

    if not args.fix:
        print("\n--check only. Re-run with --fix to repair.")
        con.close()
        return

    for r in bad:
        con.execute("UPDATE v2_trades SET pnl=?, return_pct=? WHERE id=?",
                    (round(r["actual"], 4), round(r["actual_pct"], 6), r["id"]))
    con.commit()
    left = scan(con)
    con.close()
    print(f"\nrepaired {len(bad)} row(s); {len(left)} still disagree")


if __name__ == "__main__":
    main()
