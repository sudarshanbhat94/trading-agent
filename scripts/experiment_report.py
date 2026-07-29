"""What the live book is actually teaching us.

Most of what this engine now does could not be backtested — there is no intraday
history for the surge lanes, no pre-open auction archive, and no way to replay a
partial-bar signal. The decision was to ship on reasoning and learn from the
live ledger instead. That only works if the experiments are readable, so every
unvalidated path writes a tag into the position's `why`:

    late_entry       filled outside the validated 09:15-09:45 window
    intraday_bar     scored on today's forming bar, not the last completed one
    preopen_seeded   qualified on auction participation instead of live rvol

This splits closed trades by those tags and by lane, so each change can be
judged on its own evidence rather than on the book's overall direction.

Read the numbers with the sample size in view: a tag with five trades tells you
nothing, and the tool prints n first for exactly that reason. P&L is net of
costs (recorded that way since 2026-07-29); trades closed before that date are
gross and are flagged.

Read-only.
  .venv/bin/python scripts/experiment_report.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict

DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
NET_FROM = "2026-07-29"          # date the ledger switched to net-of-cost P&L
TAGS = ("late_entry", "intraday_bar", "preopen_seeded")


def load(db=DB):
    """Closed trades joined to the tags recorded at entry.

    v2_trades does not carry `why`, so the tags are matched from the open
    position only while it is open. For closed trades we fall back to the lane,
    which still answers the per-lane question. Positions that are still open are
    reported separately — counting an unrealised mark as a result is how you
    talk yourself into a strategy.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    closed = con.execute(
        "SELECT strategy,symbol,entry_date,exit_date,pnl,return_pct,reason FROM v2_trades"
    ).fetchall()
    open_rows = con.execute(
        "SELECT strategy,symbol,entry_price,shares,why FROM v2_positions"
    ).fetchall()
    con.close()
    return closed, open_rows


def summarise(rows):
    """rows: [(pnl, return_pct)] -> dict of the only stats worth quoting."""
    if not rows:
        return None
    rets = [r[1] for r in rows]
    pnl = sum(r[0] for r in rows)
    wins = [r for r in rets if r > 0]
    gain = sum(r for r in rets if r > 0)
    loss = abs(sum(r for r in rets if r <= 0))
    return dict(n=len(rets), pnl=pnl, win=len(wins) / len(rets) * 100,
                avg=sum(rets) / len(rets),
                pf=(gain / loss) if loss > 0 else float("inf") if gain else 0.0)


def line(label, s):
    if not s:
        print(f"  {label:<22} no trades yet")
        return
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"  {label:<22} n {s['n']:3d}  net Rs {s['pnl']:+8.0f}  "
          f"win {s['win']:5.1f}%  avg {s['avg']:+6.2f}%  PF {pf}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    closed, open_rows = load(args.db)

    print(f"CLOSED TRADES ({len(closed)})")
    if not closed:
        print("  none yet — nothing to learn from a book with no exits.")
    else:
        # The net-of-cost fix landed mid-session on NET_FROM, so trades that
        # closed that same day straddle the change and cannot be told apart by
        # date. Say so rather than implying a precision the data does not have.
        if any(str(t[3]) <= NET_FROM for t in closed):
            print(f"  NOTE: P&L became net of costs during {NET_FROM}. Trades closed on "
                  "or before that date may be GROSS, so early figures flatter slightly.")
        by_lane = defaultdict(list)
        for strat, _sym, _ed, _xd, pnl, ret, _r in closed:
            by_lane[strat].append((pnl, ret))
        print()
        print("  by lane")
        for lane in sorted(by_lane):
            line(lane, summarise(by_lane[lane]))
        print()
        print("  by exit reason")
        by_reason = defaultdict(list)
        for _s, _sym, _ed, _xd, pnl, ret, reason in closed:
            by_reason[reason or "?"].append((pnl, ret))
        for reason in sorted(by_reason):
            line(reason, summarise(by_reason[reason]))
        print()
        line("ALL", summarise([(t[4], t[5]) for t in closed]))

    # Open positions: show which experiment each belongs to, but never fold an
    # unrealised mark into the result table above.
    print()
    print(f"OPEN POSITIONS ({len(open_rows)}) — experiment tags, not results")
    if not open_rows:
        print("  none")
    for strat, sym, entry, shares, why in open_rows:
        tags = []
        try:
            d = json.loads(why) if why else {}
            tags = [t for t in TAGS if d.get(t)]
        except Exception:
            pass
        print(f"  {sym:<12} {strat:<15} {shares:>6.0f} @ {entry:8.2f}  "
              f"{'· '.join(tags) if tags else 'validated path'}")

    print()
    print("Tags in play (all unbacktestable, all reversible by one flag):")
    print("  late_entry     REENTRY_WINDOW_SEC   — fills after the validated open window")
    print("  intraday_bar   INTRADAY_SIGNAL_BAR  — scored on today's partial bar")
    print("  preopen_seeded PREOPEN_SEED         — qualified on auction, not live rvol")


if __name__ == "__main__":
    main()
