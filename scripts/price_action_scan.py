"""Which price-action readings actually predict index direction?

The first direction engine blended five readings and scored 36% — worse than
chance. Judging the blend was the error: a composite hides which parts carry
information, exactly as a portfolio return hides which factors work. The equity
side only made progress once factors were scored individually, so this does the
same for index price action.

Each reading votes -1, 0 or +1 on each session. The reading is then scored on
the forward return of the index when it voted +1 versus when it voted -1. A
reading with an edge separates those two; a reading without one does not, no
matter how sensible it sounds.

Read `edge` as the gap between the two, in percent of index move. It has to
clear roughly 0.5%/3d before it can pay for an option's time decay, so a
positive number is necessary but nowhere near sufficient.

Read-only.
  .venv/bin/python scripts/price_action_scan.py --all --hold 3
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import index_direction, price_action  # noqa: E402

FO_DB = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")


def load(symbol, db=FO_DB):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    bars = con.execute(
        "SELECT date, open, high, low, close, volume FROM ("
        "  SELECT date, open, high, low, close, volume,"
        "         ROW_NUMBER() OVER (PARTITION BY date ORDER BY expiry) rn"
        "    FROM fo_bhav WHERE symbol=? AND instrument='FUT' AND close>0"
        ") WHERE rn=1 ORDER BY date", (symbol,)).fetchall()
    oi = {d: (p or 0, c or 0) for d, p, c in con.execute(
        "SELECT date, SUM(CASE WHEN opt_type='PE' THEN oi ELSE 0 END),"
        " SUM(CASE WHEN opt_type='CE' THEN oi ELSE 0 END)"
        " FROM fo_bhav WHERE symbol=? AND instrument='OPT' GROUP BY date", (symbol,))}
    con.close()
    return bars, oi


def readings_for(o, h, l, c, v, put_oi, call_oi):
    """Every candidate reading, old and new, on one session."""
    out = {}
    for name, fn in price_action.READINGS.items():
        try:
            out[name] = fn(o, h, l, c, v)[0]
        except Exception:
            out[name] = 0
    # the original five, so old and new are compared on identical bars
    out["trend"] = index_direction.trend_vote(c)[0]
    out["pattern"] = index_direction.pattern_vote(o, h, l, c)[0]
    out["volume"] = index_direction.volume_vote(v, c)[0]
    out["location"] = index_direction.location_vote(o[-1], h[-1], l[-1], c[-1])[0]
    out["positioning"] = index_direction.positioning_vote(put_oi, call_oi)[0]
    return out


def scan(symbols, hold):
    votes: dict = {}
    for symbol in symbols:
        bars, oi = load(symbol)
        if len(bars) < 90:
            continue
        o = [float(b[1] or 0) for b in bars]
        h = [float(b[2] or 0) for b in bars]
        l = [float(b[3] or 0) for b in bars]
        c = [float(b[4] or 0) for b in bars]
        v = [float(b[5] or 0) for b in bars]
        dates = [b[0] for b in bars]
        for i in range(60, len(bars) - hold - 1):
            entry = o[i + 1]                      # next open — never today's close
            if entry <= 0:
                continue
            fwd = (c[min(i + hold, len(c) - 1)] / entry - 1) * 100
            put_oi, call_oi = oi.get(dates[i], (0, 0))
            for name, vote in readings_for(o[:i + 1], h[:i + 1], l[:i + 1],
                                           c[:i + 1], v[:i + 1], put_oi, call_oi).items():
                votes.setdefault(name, {1: [], -1: [], 0: []})[vote].append(fwd)
    return votes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hold", type=int, default=3)
    args = ap.parse_args()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"] if args.all else [args.symbol]

    votes = scan(symbols, args.hold)
    print(f"forward {args.hold}d index move by reading  ({', '.join(symbols)})")
    print(f"  {'reading':<14} {'n+':>5} {'mean+':>8} {'n-':>5} {'mean-':>8} {'edge':>8}")
    rows = []
    for name, buckets in votes.items():
        up, down = buckets[1], buckets[-1]
        if len(up) < 25 or len(down) < 25:
            print(f"  {name:<14} too few votes ({len(up)}+ / {len(down)}-)")
            continue
        mu, md = statistics.mean(up), statistics.mean(down)
        edge = mu - md
        rows.append((edge, name, len(up), mu, len(down), md))
    for edge, name, nu, mu, nd, md in sorted(rows, reverse=True):
        flag = "  <== EDGE" if edge > 0.30 else ("  <== INVERTED" if edge < -0.30 else "")
        print(f"  {name:<14} {nu:>5} {mu:>+8.3f} {nd:>5} {md:>+8.3f} {edge:>+8.3f}{flag}")
    print()
    print("  edge = mean forward move when the reading said UP, minus when it said DOWN.")
    print("  A reading needs roughly +0.5%/3d before it can pay for an option's decay.")


if __name__ == "__main__":
    main()
