"""Capital-accurate portfolio backtest to DECIDE (on real OCI data) three things
the user asked to fix/upgrade, before any of them ship:

  1. position count  : MAXPOS 6 vs 8 vs 10 vs 14  (was changed on preference)
  2. bet sizing      : equal-weight  vs  probability-weighted (meta P(win))
                       vs  volatility-normalized (1/atr_pct)
  3. selection       : rank by conviction  vs  rank by meta P(win)

Honest method:
  - events come from meta_label_research.py (one row per signal the LIVE engine
    would take, with net_ret AND hold_days = trading days capital is committed).
  - meta P(win) is OUT-OF-FOLD: purged walk-forward, embargo >= max hold, so a
    trade's probability is never fit on overlapping-outcome data (same protocol
    as meta_label_train.py).
  - a real cash-tracked book: each trading day, free slots are filled from that
    day's eligible signals; a slot is locked for the trade's hold_days; equity
    is marked daily; Sharpe/maxDD come from the daily equity curve.

  python3 scripts/meta_portfolio_eval.py --data /opt/opentrade/var/meta_events_IN.csv
"""
from __future__ import annotations

import argparse
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

BASE_FEATURES = ["conviction", "mom20", "mom63", "rs20", "atr_pct", "dist_hi20",
                 "rng_pos", "rvol", "vol_contract", "mkt_mom20", "gap_pct",
                 "regime_on", "regime_neutral", "regime_strong", "is_gap",
                 "day_rank", "day_breadth", "dow"]
ALPHA_COLS = ["a_kmid", "a_klen", "a_kup", "a_klow", "a_roc5", "a_roc10",
              "a_std5", "a_std20", "a_std60", "a_corr20", "a_cntp20",
              "a_sump20", "a_vstd20", "a_wvma20"]
FEATURES = BASE_FEATURES        # overridden in main() per --features
TD_YEAR = 252


def oos_probabilities(df, folds=5, embargo_days=30):
    """Assign every event an out-of-fold P(win) via purged walk-forward."""
    df = df.sort_values("date").reset_index(drop=True)
    dates = np.array(sorted(df.date.unique()))
    blocks = np.array_split(dates, folds + 1)
    p = pd.Series(np.nan, index=df.index)
    for k in range(1, folds + 1):
        test_dates = blocks[k]
        test_start = pd.Timestamp(test_dates.min())
        train_cut = test_start - pd.Timedelta(days=embargo_days)
        tr = df[df.date <= train_cut]
        te_mask = (df.date >= test_start) & (df.date <= pd.Timestamp(test_dates.max()))
        te = df[te_mask]
        if len(tr) < 200 or len(te) < 30 or tr.label.nunique() < 2:
            continue
        clf = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=42)
        clf.fit(tr[FEATURES], tr.label)
        p.loc[te.index] = clf.predict_proba(te[FEATURES])[:, 1]
    df["p"] = p
    return df[df.p.notna()].copy()


def run_book(df, maxpos, floor, sizing, selection, start_equity=1_000_000.0):
    """Cash-tracked daily portfolio sim. Returns (equity_series, trades taken)."""
    df = df[df.p >= floor].copy()
    if selection == "p":
        df["rank_key"] = -df["p"]
    else:
        df["rank_key"] = -df["conviction"]
    by_day = {d: g.sort_values("rank_key") for d, g in df.groupby("date")}
    all_days = sorted(pd.to_datetime(sorted(df.date.unique())))
    cash = start_equity
    open_slots = []   # list of dicts: {free_on_idx, capital, ret}
    equity_curve, taken = [], []
    for i, day in enumerate(all_days):
        # settle positions whose hold elapsed (capital + P&L returns to cash)
        still = []
        for s in open_slots:
            if s["free_on"] <= i:
                cash += s["capital"] * (1 + s["ret"] / 100.0)
            else:
                still.append(s)
        open_slots = still
        free = maxpos - len(open_slots)
        if free > 0:
            cand = by_day.get(np.datetime64(day.date()), None)
            if cand is None:
                cand = by_day.get(str(day.date()), None)
            if cand is not None and len(cand):
                picks = cand.head(free)
                # size like the live engine: each position ~ equity/maxpos, so N
                # candidates deploy N*(equity/maxpos) and the rest stays in cash
                # (NOT the whole free-slot budget concentrated into a few names).
                # weight_factor scales AROUND 1 (mean 1), so avg slot = per_slot.
                if sizing == "prob":
                    wf = np.clip(picks["p"].to_numpy() - 0.5, 1e-3, None)
                elif sizing == "vol":
                    wf = 1.0 / np.clip(picks["atr_pct"].to_numpy(), 0.01, None)
                else:
                    wf = np.ones(len(picks))
                wf = wf / wf.mean()                       # mean 1 -> avg slot = per_slot
                cur_equity = cash + sum(s["capital"] for s in open_slots)
                per_slot = cur_equity / maxpos
                for (_, row), fi in zip(picks.iterrows(), wf):
                    cap = min(per_slot * fi, cash)
                    if cap < 1:
                        continue
                    cash -= cap
                    open_slots.append(dict(free_on=i + int(row["hold_days"]),
                                           capital=cap, ret=row["net_ret"]))
                    taken.append(row["net_ret"])
        mtm = cash + sum(s["capital"] * (1 + s["ret"] / 100.0) for s in open_slots)
        equity_curve.append(mtm)
    return pd.Series(equity_curve, index=all_days), taken


def perf(eq, taken, start=1_000_000.0):
    if len(eq) < 3:
        return dict(ret=0, sharpe=0, maxdd=0, n=len(taken), wr=0)
    total = eq.iloc[-1] / start - 1
    daily = eq.pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * np.sqrt(TD_YEAR)) if daily.std() > 0 else 0
    roll_max = eq.cummax()
    maxdd = ((eq - roll_max) / roll_max).min()
    wr = (np.array(taken) > 0).mean() * 100 if taken else 0
    return dict(ret=total * 100, sharpe=sharpe, maxdd=maxdd * 100, n=len(taken), wr=wr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/opt/opentrade/var/meta_events_IN.csv")
    ap.add_argument("--floor", type=float, default=0.60)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--features", choices=["base", "all"], default="base")
    a = ap.parse_args()

    global FEATURES
    have_alpha = all(c in pd.read_csv(a.data, nrows=1).columns for c in ALPHA_COLS)
    FEATURES = BASE_FEATURES + (ALPHA_COLS if (a.features == "all" and have_alpha) else [])
    print("feature set: %s (%d features)" % (a.features, len(FEATURES)))

    df = pd.read_csv(a.data)
    df["date"] = pd.to_datetime(df["date"])
    if "hold_days" not in df.columns:
        print("ERROR: events CSV lacks hold_days — regenerate with updated meta_label_research.py")
        return
    print("events=%d  %s..%s" % (len(df), df.date.min().date(), df.date.max().date()))
    df = oos_probabilities(df, folds=a.folds)
    print("with OOS prob=%d  (floor P>=%.2f)\n" % (len(df), a.floor))

    def line(tag, eq, tk):
        s = perf(eq, tk)
        print("  %-40s ret=%+7.1f%%  Sharpe=%5.2f  maxDD=%6.1f%%  trades=%4d  win=%4.1f%%"
              % (tag, s["ret"], s["sharpe"], s["maxdd"], s["n"], s["wr"]))

    print("=== 1) POSITION COUNT (equal-weight, rank by conviction) ===")
    for mp in (6, 8, 10, 14):
        eq, tk = run_book(df, mp, a.floor, "equal", "conviction")
        line("MAXPOS=%2d" % mp, eq, tk)

    print("\n=== 2) BET SIZING (best MAXPOS, rank by conviction) ===")
    bestmp = 10
    for sz, name in (("equal", "equal-weight"), ("prob", "probability-weighted"), ("vol", "volatility-normalized")):
        eq, tk = run_book(df, bestmp, a.floor, sz, "conviction")
        line("MAXPOS=%d  %s" % (bestmp, name), eq, tk)

    print("\n=== 3) SELECTION (best MAXPOS, equal-weight) ===")
    for sel, name in (("conviction", "rank by conviction"), ("p", "rank by meta P(win)")):
        eq, tk = run_book(df, bestmp, a.floor, "equal", sel)
        line("MAXPOS=%d  %s" % (bestmp, name), eq, tk)

    print("\n=== 4) COMBINED BEST vs CURRENT-LIVE ===")
    eq, tk = run_book(df, 6, a.floor, "equal", "conviction")
    line("CURRENT LIVE: MAXPOS=6 equal conviction", eq, tk)
    for mp in (8, 10, 14):
        for sz in ("prob", "vol"):
            for sel in ("conviction", "p"):
                eq, tk = run_book(df, mp, a.floor, sz, sel)
                line("MAXPOS=%d %s %s" % (mp, sz, sel), eq, tk)


if __name__ == "__main__":
    main()
