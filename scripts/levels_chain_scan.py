"""Do the levels and chain readings actually predict anything?

Phases 2 and 3 built the readings. This scores them, because a reading that is
correctly implemented and useless is still useless — and every strategy this
project has killed was killed by exactly this step.

Four questions, each answerable from the 404 sessions already stored:

  1. PINNING     does the index move TOWARDS max pain? If it does, the gap to
                 max pain is a directional signal on its own.
  2. OI WALLS    when price sits at the highest-call-OI strike, does it stall?
                 At the highest-put-OI strike, does it hold?
  3. PCR         do extremes in put/call positioning precede anything?
  4. STRADDLE    is the ATM straddle priced above or below the move that
                 actually happens? That decides whether to buy or sell premium
                 and is the single most important number for an options book.

Plus the price levels from Phase 2: forward return when price is sitting at
PDH, PDL, a round number or a swing.

Conservative: every reading is computed from data up to and INCLUDING day t,
and the forward return is measured from day t+1's OPEN, so nothing uses a price
it could not have seen.

Read-only.
  .venv/bin/python scripts/levels_chain_scan.py --all --hold 3
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import levels as lv, option_chain as oc  # noqa: E402

FO_DB = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")


def load(symbol, db=FO_DB):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=90)
    fut = con.execute(
        "SELECT date, open, high, low, close FROM ("
        "  SELECT date, open, high, low, close,"
        "         ROW_NUMBER() OVER (PARTITION BY date ORDER BY expiry) rn"
        "    FROM fo_bhav WHERE symbol=? AND instrument='FUT' AND close>0"
        ") WHERE rn=1 ORDER BY date", (symbol,)).fetchall()
    chains = defaultdict(list)
    for d, strike, side, close, oi, vol in con.execute(
            "SELECT date, strike, opt_type, close, oi, volume FROM fo_bhav"
            " WHERE symbol=? AND instrument='OPT' AND expiry=("
            "   SELECT MIN(expiry) FROM fo_bhav f2 WHERE f2.symbol=fo_bhav.symbol"
            "     AND f2.date=fo_bhav.date AND f2.expiry>=fo_bhav.date)", (symbol,)):
        chains[d].append(dict(strike=strike, opt_type=side, close=close,
                              oi=oi, volume=vol))
    con.close()
    return fut, chains


def bucket_stats(buckets, label, note=""):
    print(f"\n  {label}")
    if note:
        print(f"    {note}")
    rows = [(k, v) for k, v in buckets.items() if len(v) >= 20]
    if not rows:
        print("    too few observations")
        return
    for key, vals in sorted(rows):
        mean = statistics.mean(vals)
        up = sum(1 for x in vals if x > 0) / len(vals) * 100
        print(f"    {str(key):<22} n={len(vals):>4}  mean {mean:+6.3f}%  up {up:4.1f}%")


def run(symbol, hold):
    fut, chains = load(symbol)
    if len(fut) < 80:
        print(f"[{symbol}] only {len(fut)} sessions")
        return
    dates = [b[0] for b in fut]
    o = [float(b[1] or 0) for b in fut]
    c = [float(b[4] or 0) for b in fut]

    pin, walls, pcr_b, level_b = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    straddle_implied, straddle_actual = [], []

    for i in range(25, len(fut) - hold - 1):
        entry = o[i + 1]
        if entry <= 0:
            continue
        fwd = (c[min(i + hold, len(c) - 1)] / entry - 1) * 100
        spot = c[i]
        chain = chains.get(dates[i]) or []
        bars = fut[:i + 1]

        if chain and spot > 0:
            summary = oc.summarise(chain, spot, min_volume=1)
            gap = summary.get("max_pain_gap_pct")
            if gap is not None:
                # Does price move TOWARDS max pain? Positive gap = pain above.
                band = ("pain >1% above" if gap > 1 else
                        "pain >1% below" if gap < -1 else "pain near spot")
                pin[band].append(fwd)
            res, sup = summary.get("resistance"), summary.get("support")
            if res and abs(spot / res - 1) * 100 < 0.5:
                walls["at call wall"].append(fwd)
            if sup and abs(spot / sup - 1) * 100 < 0.5:
                walls["at put wall"].append(fwd)
            p = summary.get("pcr_oi")
            if p:
                band = "pcr >1.3" if p >= 1.3 else "pcr <0.7" if p <= 0.7 else "pcr 0.7-1.3"
                pcr_b[band].append(fwd)
            st = summary.get("straddle")
            if st and st.get("pct"):
                straddle_implied.append(st["pct"])
                # realised move over the option's remaining life, same horizon
                straddle_actual.append(abs(fwd))

        summary_lv = lv.summarise(bars, spot, symbol=symbol, within_pct=0.3)
        at = summary_lv.get("at_level")
        if at:
            level_b[at["name"]].append(fwd)

    print(f"\n{'=' * 66}\n[{symbol}] {len(fut)} sessions, forward {hold}d from next open")
    bucket_stats(pin, "MAX PAIN — does price move toward it?",
                 "if pinning is real, 'pain above' should be positive and 'below' negative")
    bucket_stats(walls, "OI WALLS — does price stall at them?",
                 "call wall should be negative (resistance), put wall positive (support)")
    bucket_stats(pcr_b, "PCR (open interest)")
    bucket_stats(level_b, "PRICE LEVELS — forward return when sitting at each")
    if len(straddle_implied) >= 20:
        imp = statistics.mean(straddle_implied)
        act = statistics.mean(straddle_actual)
        print(f"\n  STRADDLE — is premium rich or cheap?")
        print(f"    implied move {imp:.2f}%  vs  realised {act:.2f}%  "
              f"({'SELLERS favoured' if imp > act else 'BUYERS favoured'})")
        print(f"    n={len(straddle_implied)}. Buying options only makes sense when "
              "realised exceeds implied.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hold", type=int, default=3)
    args = ap.parse_args()
    for symbol in (["NIFTY", "BANKNIFTY"] if args.all else [args.symbol]):
        run(symbol, args.hold)


if __name__ == "__main__":
    main()
