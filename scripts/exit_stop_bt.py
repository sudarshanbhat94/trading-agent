#!/usr/bin/env python3
"""Stop-loss cap + realistic-gap A/B on the REAL engine signals.

Reuses backtest_v2's universe/features/conviction (the actual daily factor
engine) and re-runs the exit with two upgrades the live engine/back-test lack:
  1. REALISTIC gap fills — if a day OPENS below the stop, you fill at the open
     (the gap-through), not magically at the stop. This exposes the true tail.
  2. A max-loss CAP — raise the stop so no single trade is designed to lose more
     than X% (bounds the -7%/-11% disasters).

Sweeps cap = none/-6/-5/-4/-3% under realistic gaps, vs the optimistic baseline,
to see whether capping the loss preserves the engine's edge while cutting the tail.

Run on OCI:  /opt/opentrade/.venv/bin/python scripts/exit_stop_bt.py --market IN --topn 500
"""
from __future__ import annotations
import argparse, sqlite3, statistics
import pandas as pd
import backtest_v2 as bt


def sim(g, i, atr_stop, atr_target, max_days, cost, max_loss, gap_real):
    if i + 1 >= len(g):
        return None
    entry = g["open"].iloc[i + 1]
    atr = g["atr14"].iloc[i]
    if not (entry > 0) or not (atr > 0):
        return None
    stop = entry - atr_stop * atr
    target = entry + atr_target * atr
    if max_loss is not None:
        stop = max(stop, entry * (1 - max_loss))     # never risk more than max_loss
    for j in range(i + 1, min(i + 1 + max_days, len(g))):
        o, hi, lo = g["open"].iloc[j], g["high"].iloc[j], g["low"].iloc[j]
        if gap_real and o <= stop:                   # gapped through the stop -> fill at open
            return (o - entry) / entry * 100 - cost, "stop_gap", j - i
        if lo <= stop:
            return (stop - entry) / entry * 100 - cost, "stop", j - i
        if hi >= target:
            return (target - entry) / entry * 100 - cost, "target", j - i
    last = g["close"].iloc[min(i + max_days, len(g) - 1)]
    return (last - entry) / entry * 100 - cost, "time", min(max_days, len(g) - 1 - i)


def run(con, market, topn, atr_stop, atr_target, hold, cost, max_loss, gap_real, thresh):
    syms, mdf = bt.load_market(con, market, topn)
    rets, reasons = [], {}
    for sym, g in syms.items():
        if len(g) < 80:
            continue
        g = bt.features(g, mdf)
        for i in range(60, len(g) - 1):
            if bt.conviction(g.iloc[i]) < thresh:
                continue
            r = sim(g, i, atr_stop, atr_target, hold, cost, max_loss, gap_real)
            if r:
                rets.append(r[0]); reasons[r[1]] = reasons.get(r[1], 0) + 1
    n = len(rets)
    wins = [x for x in rets if x > 0]; loss = [x for x in rets if x <= 0]
    pf = sum(wins) / abs(sum(loss)) if loss else float("inf")
    p05 = sorted(rets)[int(0.05 * (n - 1))] if n else 0
    worst = min(rets) if rets else 0
    return dict(n=n, win=100 * len(wins) / n if n else 0, exp=statistics.mean(rets) if n else 0,
                pf=pf, avgL=statistics.mean(loss) if loss else 0, p05=p05, worst=worst,
                total=sum(rets), reasons=reasons)


def line(name, s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    print(f"  {name:<34} n={s['n']:<6} win={s['win']:4.1f}%  exp={s['exp']:+.3f}%/t  PF={pf:<5} "
          f"avgL={s['avgL']:+.2f}%  p05={s['p05']:+.1f}  worst={s['worst']:+.1f}  totP&L={s['total']:+.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--topn", type=int, default=500)
    ap.add_argument("--db", default="/opt/opentrade/var/trading_agent.db")
    ap.add_argument("--atr-stop", type=float, default=2.0)     # swing_meanrev default
    ap.add_argument("--atr-target", type=float, default=3.5)
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--cost", type=float, default=0.30)
    ap.add_argument("--thresh", type=float, default=0.6)
    a = ap.parse_args()
    con = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    print(f"\n=== STOP-CAP A/B (swing config atr_stop={a.atr_stop} target={a.atr_target} hold={a.hold}d) ===")
    print("baseline = optimistic stop fill (no gaps). realistic = fill at open on a gap-down.\n")
    base = run(con, a.market, a.topn, a.atr_stop, a.atr_target, a.hold, a.cost, None, False, a.thresh)
    line("baseline (no gap, no cap)", base)
    real = run(con, a.market, a.topn, a.atr_stop, a.atr_target, a.hold, a.cost, None, True, a.thresh)
    line("realistic gaps, no cap", real)
    for cap in (0.06, 0.05, 0.04, 0.03):
        s = run(con, a.market, a.topn, a.atr_stop, a.atr_target, a.hold, a.cost, cap, True, a.thresh)
        line(f"realistic gaps + cap -{int(cap*100)}%", s)
    con.close()


if __name__ == "__main__":
    main()
