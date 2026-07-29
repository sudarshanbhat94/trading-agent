"""Rebuild the ranking score, and prove it beats the current one before shipping.

The factor scan showed the engine ranks on a score that barely separates:
`conv` spreads +0.17% between its worst and best decile while individual
factors spread 1.7-3.0%. Two design faults explain it.

CLAMPING. Every component is squashed into 0..1 over a fixed range, e.g.
`clamp(-dist_hi20 / 0.10)`, so every name more than 10% below its 20-day high
scores an identical 1.0. In a market where dips of 20-40% are common, most of
the universe saturates and stops being ranked at all. A score that cannot tell
its candidates apart is not a ranking.

THE FALLING-KNIFE GUARD. `conviction()` multiplies the score by 0.3 when
dist_hi20 < -0.35. The scan says that cohort is the BEST performer (+1.94%
forward vs +0.19% for names at their highs), so the guard is systematically
demoting the strongest group.

The replacement uses cross-sectional percentile ranks: on each date every
factor is ranked across the universe and mapped to 0..1. Ranks cannot saturate,
are immune to outliers and to a factor's distribution shape, and make weights
comparable across factors measured in different units.

HONESTY ABOUT FITTING: the weights come from spreads measured on this sample,
so an in-sample comparison would be rigged. The report therefore splits the
period in half and quotes the SECOND half, where the weights were fixed by data
the test never sees. Read that number, not the in-sample one.

Read-only.
  .venv/bin/python scripts/score_rebuild.py --topn 300 --hold 10
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

# Direction and strength taken from the cleaned factor scan (spread D10-D1, and
# how monotonic it was). A negative spread means LOW values of the factor
# predict high forward returns, so the rank is inverted.
FACTOR_WEIGHTS = {
    "atr_pct":      +2.96,      # mono +0.93 — volatility is the strongest single edge
    "pre":          -2.27,      # mono -0.72 — the premove detector reads BACKWARDS
    "dist_hi20":    -1.76,      # mono -0.82 — deeper below the 20d high is better
    "vol_contract": +1.74,      # mono +0.95 — ATR expanding vs 20 sessions ago
    "mom20":        -1.06,      # mono -0.49 — recent momentum predicts worse
    "rs20":         +0.71,      # mono +0.70
    "rvol":         +0.56,      # mono +0.86
}
# Deliberately excluded: sent/sent5 (6% coverage) and deliv_pct/deliv_spike
# (16%). Those are not weak signals, they are unmeasured ones, and scoring on a
# factor that is absent for 84% of observations imports its missingness as if
# it were information.


def rank_score(frame, weights=FACTOR_WEIGHTS):
    """Cross-sectional percentile-rank score per (date, symbol), 0..1."""
    total = sum(abs(w) for w in weights.values())
    score = pd.Series(0.0, index=frame.index)
    coverage = pd.Series(0.0, index=frame.index)
    for name, weight in weights.items():
        if name not in frame:
            continue
        column = frame[name]
        # rank within the date, so the score compares names against the market
        # on that day rather than against a fixed absolute threshold
        pct = column.groupby(frame["date"]).rank(pct=True)
        if weight < 0:
            pct = 1.0 - pct
        contribution = pct * abs(weight)
        score = score.add(contribution.fillna(0.0), fill_value=0.0)
        coverage = coverage.add(column.notna() * abs(weight), fill_value=0.0)
    # normalise by the weight actually present, so a name missing one factor is
    # not silently penalised as though it scored zero on it
    return (score / coverage.replace(0, np.nan)) * (total / total)


def deciles(frame, column, fwd="fwd", n=10):
    sub = frame[[column, fwd]].dropna()
    if len(sub) < n * 50:
        return None
    try:
        buckets = pd.qcut(sub[column].rank(method="first"), n, labels=False)
    except ValueError:
        return None
    means = sub.groupby(buckets)[fwd].mean()
    if len(means) < n:
        return None
    spread = float(means.iloc[-1] - means.iloc[0])
    # monotonicity: correlation of decile index with decile mean
    mono = float(np.corrcoef(range(len(means)), means.values)[0, 1])
    return dict(n=len(sub), d1=float(means.iloc[0]), d10=float(means.iloc[-1]),
                spread=spread, mono=mono)


def report(label, stats):
    if not stats:
        print(f"  {label:<16} insufficient data")
        return
    print(f"  {label:<16} n={stats['n']:>7,}  D1={stats['d1']:+.3f}%  D10={stats['d10']:+.3f}%  "
          f"spread={stats['spread']:+.3f}%  mono={stats['mono']:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="IN")
    ap.add_argument("--topn", type=int, default=300)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--cost", type=float, default=None)
    args = ap.parse_args()
    cost = args.cost if args.cost is not None else DEFAULT_COST[args.market]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    syms, market_df, eligible_at = load_market(con, args.market, args.topn, asof=True)
    con.close()

    rows = []
    for sym, g in syms.items():
        if len(g) < 120:
            continue
        g = features(g, market_df)
        g["conv"] = g.apply(conviction, axis=1)
        # forward return: enter NEXT open, exit `hold` sessions later, net of cost
        entry = g["open"].shift(-1)
        exit_px = g["close"].shift(-args.hold)
        g["fwd"] = (exit_px / entry - 1) * 100 - cost
        g["symbol"] = sym
        g["date"] = g.index
        if eligible_at is not None:
            g = g[[sym in eligible_at(d) for d in g.index]]
        rows.append(g)
    if not rows:
        print("no data")
        return
    panel = pd.concat(rows, ignore_index=True).dropna(subset=["fwd"])
    panel["conv2"] = rank_score(panel)

    dates = np.sort(panel["date"].unique())
    split = dates[len(dates) // 2]
    first = panel[panel["date"] < split]
    second = panel[panel["date"] >= split]

    print(f"[{args.market}] score rebuild — {len(panel):,} observations, "
          f"fwd {args.hold}d net of {cost}%")
    print(f"  split at {pd.Timestamp(split).date()}: "
          f"{len(first):,} in-sample / {len(second):,} out-of-sample")
    print()
    print("  FULL SAMPLE (weights fitted here — expect flattery)")
    report("conv  (current)", deciles(panel, "conv"))
    report("conv2 (ranks)", deciles(panel, "conv2"))
    print()
    print("  OUT-OF-SAMPLE second half  <== the number that counts")
    a = deciles(second, "conv")
    b = deciles(second, "conv2")
    report("conv  (current)", a)
    report("conv2 (ranks)", b)
    if a and b:
        print()
        gain = b["spread"] - a["spread"]
        print(f"  conv2 vs conv out-of-sample: {gain:+.3f}pp spread, "
              f"mono {a['mono']:+.2f} -> {b['mono']:+.2f}")
        if gain > 0.3 and b["mono"] > a["mono"]:
            print("  ==> conv2 ranks better out-of-sample. Worth shipping.")
        else:
            print("  ==> conv2 does NOT clearly beat conv. Do not ship it.")


if __name__ == "__main__":
    main()
