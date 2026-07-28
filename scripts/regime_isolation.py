"""Does the regime filter time the market, independent of stock selection?

The portfolio backtest conflates two things: which names it buys, and when it
is willing to buy at all. Turning the filter off changes both the trade count
and the exposure, so the A/B does not separate them.

This strips selection out entirely. It holds the WHOLE point-in-time universe
and only decides in or out, using the same rule the engine uses (synthetic
index above its own 50-day average). If regime-gated holding beats always
holding, the signal times the market. If not, the drawdown reduction seen in
the portfolio run comes from being incidentally under-invested rather than from
the signal being informative.

The regime is shifted by one day: the rule is evaluated on a close and acted on
from the next session, so today's return is never earned using today's signal.

Read-only. Run on the OCI box:
  .venv/bin/python scripts/regime_isolation.py --market IN --topn 300
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pandas as pd

from backtest_v2 import DB, load_market


def stats(returns, label, exposure=None):
    eq = (1 + returns.fillna(0)).cumprod()
    total = eq.iloc[-1] - 1
    peak = eq.cummax()
    mdd = ((eq - peak) / peak).min()
    sd = returns.std()
    sharpe = (returns.mean() / sd * np.sqrt(252)) if sd and sd > 0 else 0.0
    line = (f"  {label:<28} return {total * 100:+6.1f}%   maxDD {mdd * 100:6.1f}%   "
            f"Sharpe {sharpe:5.2f}")
    if exposure is not None:
        line += f"   in-market {exposure * 100:4.0f}%"
    print(line)
    return dict(total=total, mdd=mdd, sharpe=sharpe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["IN", "US"], default="IN")
    ap.add_argument("--topn", type=int, default=300)
    ap.add_argument("--start", default=None)
    ap.add_argument("--ma", type=int, default=50, help="regime moving average, in sessions")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    _syms, market_df, _elig = load_market(con, args.market, args.topn, asof=True)
    if market_df is None or market_df.empty:
        print("no market data")
        return

    ret = market_df["mkt_ret1"]
    regime = market_df["mkt_cum"] > market_df["mkt_cum"].rolling(args.ma).mean()
    if args.start:
        keep = market_df.index >= pd.Timestamp(args.start)
        ret, regime = ret[keep], regime[keep]

    # decide on the close, act from the next session. astype(bool) matters:
    # shift() on a bool Series yields object dtype, and pandas then treats a
    # boolean-looking object mask as LABELS, not a mask.
    active = regime.shift(1).fillna(False).astype(bool).values
    timed = ret.where(active, 0.0)

    print(f"[{args.market}] regime isolation — whole universe, no stock selection")
    print(f"  period {ret.index.min().date()}..{ret.index.max().date()} "
          f"({len(ret)} sessions), {args.ma}d regime MA")
    always = stats(ret, "always invested")
    gated = stats(timed, "regime-gated", exposure=float(active.mean()))

    print()
    print(f"  difference: {(gated['total'] - always['total']) * 100:+.1f}pp return, "
          f"{(gated['mdd'] - always['mdd']) * 100:+.1f}pp drawdown "
          f"({'less' if gated['mdd'] > always['mdd'] else 'more'} severe)")

    # Is it skill or just less exposure? A filter that is in-market X% of the
    # time mechanically takes about X% of the market's move. Compare against
    # simply scaling the always-invested return by that same exposure — if the
    # gated result is no better, the signal is not picking WHICH days to miss.
    naive = ret * float(active.mean())
    scaled = stats(naive, f"static {active.mean() * 100:.0f}% exposure")
    edge = (gated["total"] - scaled["total"]) * 100
    print()
    if edge > 0.5:
        print(f"  ==> the signal beats equivalent static exposure by {edge:+.1f}pp:")
        print("      it is choosing WHICH days to sit out, not merely sitting out.")
    else:
        print(f"  ==> the signal does NOT beat equivalent static exposure ({edge:+.1f}pp).")
        print("      Its drawdown benefit is under-investment, not market timing.")

    # how often the call was right: does an OFF day actually precede weakness?
    on_ret = ret[active].mean()
    off_ret = ret[~active].mean()
    print()
    print(f"  mean daily return when IN  : {on_ret * 100:+.3f}%")
    print(f"  mean daily return when OUT : {off_ret * 100:+.3f}%   "
          f"(the days it avoided; more negative = better call)")


if __name__ == "__main__":
    main()
