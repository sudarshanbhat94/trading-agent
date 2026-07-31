"""Are the index-option stop and target reachable? Measured, not assumed.

INDEX_OPTIONS ships stop_pct 0.35 and target_pct 0.60 — get out at -35% of the
premium, take profit at +60%. Neither number was measured against anything;
they were chosen because options move a lot.

This walks every index option contract in fo.db forward from an entry, exactly
the way exit_monitor would, and reports what actually happened.

WHAT IT MODELS, and where it is honest about not matching production:

  * Entry at the day's OPEN. The lane enters intraday; daily bhavcopy is the
    only history with 405 sessions behind it. Entry price is the one input
    this cannot reproduce faithfully.
  * PREMIUM CEILING. _pick_contract buys the nearest strike whose ONE LOT fits
    the per-position cap, so the lane cannot buy an expensive option. On NIFTY
    at a Rs 10k cap and a 65 lot that means premium <= ~Rs 154 — well out of
    the money. Measuring all strikes would describe a lane we do not run, so
    contracts are filtered to what the cap can actually buy.
  * Stop checked BEFORE target on the same bar, matching evaluate_exit's
    precedence. Where both are touched in one session the outcome is recorded
    separately as ambiguous, because daily bars cannot order them.
  * Held to expiry or HOLD_DAYS, whichever comes first — the lane has no
    intraday square-off for options.

Run:  python scripts/index_option_target_study.py [--stop 0.35] [--target 0.60]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict

FO_DB = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
# Per-position rupee cap and lot size, from INDEX_OPTIONS. The premium ceiling
# is what makes this a study of the lane we run rather than of options at large.
CAP = {"NIFTY": 10000.0, "BANKNIFTY": 30000.0,
       "FINNIFTY": 30000.0, "MIDCPNIFTY": 30000.0}
MIN_PREMIUM = 5.0          # below this a tick is a percentage move; not tradeable
MIN_VOLUME = 100.0         # a contract nobody traded has no fill to speak of
HOLD_DAYS = 10


def load(con):
    """{(symbol, expiry, strike, opt_type): [(date, o, h, l, c, lot, underlying)]}"""
    rows = con.execute(
        "SELECT symbol, expiry, strike, opt_type, date, open, high, low, close,"
        " lot_size, underlying, volume FROM fo_bhav"
        " WHERE instrument='OPT' AND open>0 AND high>0 AND low>0"
        f" AND symbol IN ({','.join('?' * len(INDICES))})"
        " ORDER BY symbol, expiry, strike, opt_type, date", INDICES).fetchall()
    out = defaultdict(list)
    for sym, exp, strike, opt, date, o, h, l, c, lot, und, vol in rows:
        out[(sym, exp, strike, opt)].append(
            (date, float(o), float(h), float(l), float(c),
             float(lot or 0), float(und or 0), float(vol or 0)))
    return out


def affordable(symbol, premium, lot):
    return lot > 0 and premium * lot <= CAP.get(symbol, 10000.0)


def walk(bars, i, stop_pct, target_pct, hold=HOLD_DAYS):
    """Outcome of entering at bars[i]'s open. Returns (label, return_pct, mfe, mae)."""
    entry = bars[i][1]
    stop, target = entry * (1 - stop_pct), entry * (1 + target_pct)
    mfe = mae = 0.0
    for j in range(i, min(i + hold, len(bars))):
        _d, _o, high, low, close, *_ = bars[j]
        mfe = max(mfe, high / entry - 1)
        mae = min(mae, low / entry - 1)
        hit_stop, hit_target = low <= stop, high >= target
        if hit_stop and hit_target:
            return "ambiguous", -stop_pct, mfe, mae      # daily bar cannot order them
        if hit_stop:
            return "stop", -stop_pct, mfe, mae
        if hit_target:
            return "target", target_pct, mfe, mae
    close = bars[min(i + hold, len(bars)) - 1][4]
    return "expiry_or_time", close / entry - 1, mfe, mae


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=float, default=0.35)
    ap.add_argument("--target", type=float, default=0.60)
    ap.add_argument("--db", default=FO_DB)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    contracts = load(con)
    con.close()

    trades = []
    for (symbol, _exp, _strike, _opt), bars in contracts.items():
        for i in range(len(bars)):
            _d, o, _h, _l, _c, lot, _und, vol = bars[i]
            if o < MIN_PREMIUM or vol < MIN_VOLUME or not affordable(symbol, o, lot):
                continue
            label, ret, mfe, mae = walk(bars, i, args.stop, args.target)
            trades.append((symbol, label, ret, mfe, mae))

    if not trades:
        print("no trades matched the filters")
        return

    print(f"index option stop/target study — stop -{args.stop:.0%}, target +{args.target:.0%}")
    print(f"entries: {len(trades):,} (affordable, liquid, near-dated)\n")

    print(f"{'index':<12}{'n':>8}{'target':>9}{'stop':>8}{'ambig':>8}{'held':>8}"
          f"{'avg ret':>10}{'reach+60':>10}{'reach+30':>10}")
    by = defaultdict(list)
    for symbol, label, ret, mfe, mae in trades:
        by[symbol].append((label, ret, mfe, mae))
        by["ALL"].append((label, ret, mfe, mae))
    for symbol in list(INDICES) + ["ALL"]:
        rows = by.get(symbol)
        if not rows:
            continue
        n = len(rows)
        share = lambda k: sum(1 for x in rows if x[0] == k) / n * 100
        avg = sum(x[1] for x in rows) / n * 100
        r60 = sum(1 for x in rows if x[2] >= 0.60) / n * 100
        r30 = sum(1 for x in rows if x[2] >= 0.30) / n * 100
        print(f"{symbol:<12}{n:>8,}{share('target'):>8.1f}%{share('stop'):>7.1f}%"
              f"{share('ambiguous'):>7.1f}%{share('expiry_or_time'):>7.1f}%"
              f"{avg:>9.1f}%{r60:>9.1f}%{r30:>9.1f}%")

    # How far a winner actually runs, and how deep the average trade digs first.
    rows = by["ALL"]
    mfes = sorted(x[2] for x in rows)
    maes = sorted(x[3] for x in rows)
    pct = lambda seq, p: seq[int(len(seq) * p)] * 100
    print("\nmax favourable excursion (how high the premium got, ever):")
    for p in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
        print(f"   p{int(p * 100):<3} {pct(mfes, p):>7.1f}%")
    print("max adverse excursion (how far it fell first):")
    for p in (0.5, 0.3, 0.2, 0.1, 0.05, 0.01):
        print(f"   p{int(p * 100):<3} {pct(maes, p):>7.1f}%")

    # The grid: which pair actually pays, on the same entries.
    print("\nexpectancy grid — avg net return per trade, same entries")
    print(f"{'stop \\\\ target':<14}" + "".join(f"{t:>9.0%}" for t in (0.20, 0.30, 0.40, 0.60, 0.80)))
    for s in (0.20, 0.25, 0.35, 0.50):
        cells = []
        for t in (0.20, 0.30, 0.40, 0.60, 0.80):
            rets = []
            for (symbol, _exp, _strike, _opt), bars in contracts.items():
                for i in range(len(bars)):
                    _d, o, _h, _l, _c, lot, _und, vol = bars[i]
                    if o < MIN_PREMIUM or vol < MIN_VOLUME or not affordable(symbol, o, lot):
                        continue
                    rets.append(walk(bars, i, s, t)[1])
            cells.append(sum(rets) / len(rets) * 100 if rets else 0.0)
        print(f"-{s:<13.0%}" + "".join(f"{c:>8.1f}%" for c in cells))


if __name__ == "__main__":
    main()
