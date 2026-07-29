"""Does a fixed target help or hurt the 52-week-high breakout lane?

mom_breakout was given atr_target=4.0 on request. The concern is structural
rather than stylistic: trend-following lanes earn from a small number of
positions that run a long way, so capping the upside can remove the very trades
that pay for all the losers. This measures that directly instead of arguing it.

Entry approximates the live lane: price at/near its 252-session high, in an
uptrend, bought at the NEXT session's open. Every variant shares the same
entries — only the exit differs, so any difference IS the exit.

Variants:
  trail-only            2xATR stop + 2.5xATR trail, 40-session cap (the old lane)
  target NxATR          same, plus a fixed target at N x ATR

Honest by construction:
  * universe is the point-in-time screen, so no hindsight name selection;
  * entry at the next open, never at the close the signal used;
  * if a bar spans both stop and target, the STOP is taken;
  * delivery-style cost (~0.25% round trip) since these are multi-day holds.

Read-only. On the OCI box:
  .venv/bin/python scripts/breakout_target_bt.py --topn 300
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backtest_v2 import DB, load_market  # noqa: E402

HIGH_LOOKBACK = 252         # sessions in a "52-week" high
NEAR_HIGH = 0.99            # within 1% of it counts as a breakout
TREND_MA = 50               # uptrend filter, as the live lane requires
ATR_WINDOW = 14


def atr(g, window=ATR_WINDOW):
    high, low, close = g["high"], g["low"], g["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def market_regime(market_df, ma=50):
    """date -> bool: synthetic index above its own MA, shifted one day so the
    decision never uses the session it acts on."""
    cum = market_df["mkt_cum"]
    reg = (cum > cum.rolling(ma).mean()).shift(1).fillna(False)
    return {d: bool(v) for d, v in reg.items()}


def find_entries(syms, eligible_at, regime=None):
    """[(date, symbol, entry_index)] — signal bars, entry is the NEXT bar."""
    out = []
    for sym, g in syms.items():
        if len(g) < HIGH_LOOKBACK + 5:
            continue
        g = g.sort_index()
        close = g["close"]
        # rolling high EXCLUDING today would be stricter; including today is what
        # "at a new high" means, and the entry is still the next open.
        hi = close.rolling(HIGH_LOOKBACK).max()
        ma = close.rolling(TREND_MA).mean()
        a = atr(g)
        breakout = (close >= hi * NEAR_HIGH) & (close > ma) & a.notna() & (a > 0)
        idx = np.flatnonzero(breakout.to_numpy())
        for i in idx:
            if i + 1 >= len(g):
                continue
            date = g.index[i]
            if eligible_at is not None and sym not in eligible_at(date):
                continue
            if regime is not None and not regime.get(date, False):
                continue        # live lane requires a STRONG uptrend to buy at all
            out.append((date, sym, i, float(a.iloc[i])))
    out.sort(key=lambda x: x[0])
    return out


def simulate(g, i, atr_v, stop_mult, target_mult, trail_mult, max_days, cost):
    """Enter at bar i+1's open. Returns net % return."""
    entry = float(g["open"].iloc[i + 1])
    if entry <= 0:
        return None
    stop = entry - stop_mult * atr_v
    target = entry + target_mult * atr_v if target_mult else None
    peak = entry
    end = min(i + 1 + max_days, len(g))
    for j in range(i + 1, end):
        high = float(g["high"].iloc[j])
        low = float(g["low"].iloc[j])
        close = float(g["close"].iloc[j])
        if low <= stop:                       # stop first — see module docstring
            return (stop / entry - 1) * 100 - cost
        if target is not None and high >= target:
            return (target / entry - 1) * 100 - cost
        peak = max(peak, high)
        if trail_mult:                        # trail rides behind the peak
            stop = max(stop, peak - trail_mult * atr_v)
    last = float(g["close"].iloc[end - 1])
    return (last / entry - 1) * 100 - cost


def summarise(name, rets):
    if not rets:
        print(f"  {name:<18} no trades")
        return None
    arr = np.array(rets)
    wins = arr[arr > 0]
    # NOT compounded: these signals overlap heavily, so chaining 2000+ trades as
    # if each used the whole book produces a meaningless number. Mean per trade
    # is the comparable statistic when every variant shares the same entries.
    top5 = float(np.sort(arr)[-5:].sum())
    ex_top5 = float(np.sort(arr)[:-5].mean()) if len(arr) > 5 else 0.0
    print(f"  {name:<18} n {len(arr):4d}  win {len(wins) / len(arr) * 100:4.1f}%  "
          f"avg {arr.mean():+6.3f}%  ex-top5 {ex_top5:+6.3f}%  "
          f"best {arr.max():+6.1f}%  top5-sum {top5:+6.1f}%")
    return dict(n=len(arr), avg=float(arr.mean()), ex_top5=ex_top5,
                best=float(arr.max()), top5=top5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--topn", type=int, default=300)
    ap.add_argument("--stop", type=float, default=2.0, help="stop in ATRs")
    ap.add_argument("--trail", type=float, default=2.5, help="trail in ATRs")
    ap.add_argument("--max-days", type=int, default=40)
    ap.add_argument("--cost", type=float, default=0.25)
    ap.add_argument("--regime", choices=["off", "on", "both"], default="both",
                    help="gate entries on the market regime, as the live lane does")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    syms, market_df, eligible_at = load_market(con, args.market, args.topn, asof=True)
    con.close()
    reg = market_regime(market_df)
    modes = ["off", "on"] if args.regime == "both" else [args.regime]
    for mode in modes:
        entries = find_entries(syms, eligible_at, reg if mode == "on" else None)
        print(f"\n### market-regime gate: {mode.upper()} — {len(entries)} entries")
        run_variants(entries, syms, args)
    return


def run_variants(entries, syms, args):
    if not entries:
        print("  no entries")
        return
    print(f"  stop {args.stop}xATR, trail {args.trail}xATR, max hold {args.max_days}d, "
          f"cost {args.cost}%\n")

    variants = [("trail-only", None)] + [(f"target {m:g}xATR", m) for m in (3, 4, 6, 8)]
    results = {}
    for name, target_mult in variants:
        rets = []
        for date, sym, i, atr_v in entries:
            r = simulate(syms[sym].sort_index(), i, atr_v, args.stop, target_mult,
                         args.trail, args.max_days, args.cost)
            if r is not None:
                rets.append(r)
        results[name] = summarise(name, rets)

    base = results.get("trail-only")
    four = results.get("target 4xATR")
    if base and four:
        print()
        d = four["avg"] - base["avg"]
        print(f"  4xATR target vs trail-only: {d:+.3f}%/trade "
              f"({d / base['avg'] * 100:+.0f}% of the edge)")
        print(f"  biggest single winner: {base['best']:+.1f}% uncapped "
              f"-> {four['best']:+.1f}% capped")
        print()
        if d < 0:
            print("  ==> the cap COSTS money: it clips winners that paid for the losers.")
        else:
            print("  ==> the cap does not hurt on this data.")


if __name__ == "__main__":
    main()
