"""Is the exit logic destroying a good entry signal?

The entry ranking is strong: the top conviction decile returns +3.93% over 10
sessions out-of-sample, monotonic +0.87, net of costs. Live results are
nothing like that. The difference between the two is everything that happens
AFTER the buy — stops, trails, targets and the hold clock — because the
backtest that produced +3.93% simply buys at the next open and holds.

So this takes the SAME entries and varies only the exit, which makes any
difference attributable to the exit and nothing else:

  hold-only        buy next open, sell `hold` sessions later. The benchmark the
                   entry signal was measured with.
  stop only        + a fixed ATR stop
  stop+target      + an ATR profit target
  stop+trail       + an ATR trailing stop
  live-equivalent  everything the swing lane actually applies

Conservative throughout: a bar that spans both stop and target is counted as
the stop; entry is the next session's open, never the close the signal used;
costs are charged both sides.

Read-only.
  .venv/bin/python scripts/exit_cost_analysis.py --topn 300
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backtest_v2 import DB, DEFAULT_COST, conviction, features, load_market  # noqa: E402

THRESHOLD = 0.55            # the swing lane's live entry threshold


def simulate(bars, i, atr, hold, cost, stop_atr=None, target_atr=None, trail_atr=None):
    """One trade from bar i's signal. Entry at i+1's open. Returns net %."""
    if i + 1 >= len(bars):
        return None
    o = bars["open"].to_numpy()
    h = bars["high"].to_numpy()
    low = bars["low"].to_numpy()
    c = bars["close"].to_numpy()
    entry = float(o[i + 1])
    if entry <= 0 or not np.isfinite(atr) or atr <= 0:
        return None
    stop = entry - stop_atr * atr if stop_atr else None
    target = entry + target_atr * atr if target_atr else None
    peak = entry
    end = min(i + 1 + hold, len(bars))
    for j in range(i + 1, end):
        if stop is not None and low[j] <= stop:
            return (stop / entry - 1) * 100 - cost      # stop first — see docstring
        if target is not None and h[j] >= target:
            return (target / entry - 1) * 100 - cost
        peak = max(peak, h[j])
        if trail_atr:
            lifted = peak - trail_atr * atr
            stop = lifted if stop is None else max(stop, lifted)
    return (float(c[end - 1]) / entry - 1) * 100 - cost


def summarise(name, rets):
    if not rets:
        print(f"  {name:<18} no trades")
        return None
    arr = np.array(rets)
    wins = arr[arr > 0]
    gain = arr[arr > 0].sum()
    loss = abs(arr[arr <= 0].sum())
    pf = (gain / loss) if loss > 0 else float("inf")
    print(f"  {name:<18} n={len(arr):>6,}  avg={arr.mean():+6.3f}%  win={len(wins) / len(arr) * 100:5.1f}%  "
          f"PF={pf:5.2f}  best={arr.max():+6.1f}%  worst={arr.min():+6.1f}%")
    return dict(n=len(arr), avg=float(arr.mean()), win=len(wins) / len(arr) * 100, pf=pf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--topn", type=int, default=300)
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--cost", type=float, default=None)
    args = ap.parse_args()
    cost = args.cost if args.cost is not None else DEFAULT_COST[args.market]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    syms, market_df, eligible_at = load_market(con, args.market, args.topn, asof=True)
    con.close()

    signals = []            # (symbol, positional index, atr)
    frames = {}
    for sym, g in syms.items():
        if len(g) < 120:
            continue
        g = features(g, market_df)
        g["conv"] = g.apply(conviction, axis=1)
        frames[sym] = g
        conv = g["conv"].to_numpy()
        atr = g["atr14"].to_numpy()
        for i in np.flatnonzero(conv >= args.threshold):
            if eligible_at is not None and sym not in eligible_at(g.index[i]):
                continue
            signals.append((sym, int(i), float(atr[i])))
    print(f"[{args.market}] {len(signals):,} entries at conv >= {args.threshold}, "
          f"hold {args.hold}d, cost {cost}%\n")
    if not signals:
        return

    variants = [
        ("hold-only", dict()),
        ("stop 2ATR", dict(stop_atr=2.0)),
        ("stop 3ATR", dict(stop_atr=3.0)),
        ("stop2 + tgt3.5", dict(stop_atr=2.0, target_atr=3.5)),
        ("stop2 + trail2.5", dict(stop_atr=2.0, trail_atr=2.5)),
        ("live-equivalent", dict(stop_atr=2.0, target_atr=3.5, trail_atr=2.5)),
    ]
    results = {}
    for name, kwargs in variants:
        rets = []
        for sym, i, atr in signals:
            r = simulate(frames[sym], i, atr, args.hold, cost, **kwargs)
            if r is not None:
                rets.append(r)
        results[name] = summarise(name, rets)

    base, live = results.get("hold-only"), results.get("live-equivalent")
    if base and live:
        print()
        diff = live["avg"] - base["avg"]
        print(f"  live exits vs plain hold: {diff:+.3f}%/trade "
              f"({diff / base['avg'] * 100:+.0f}% of the entry edge)"
              if base["avg"] else f"  live exits vs plain hold: {diff:+.3f}%/trade")
        if diff < -0.05:
            print("  ==> the EXITS are destroying the entry edge, not the signal.")
        else:
            print("  ==> the exits are not the problem; the entry edge is the limit.")


if __name__ == "__main__":
    main()
