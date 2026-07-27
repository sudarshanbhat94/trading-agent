"""Train + honestly evaluate the meta-label model on the event dataset produced
by meta_label_research.py.

Evaluation = PURGED WALK-FORWARD with a calendar embargo >= max hold, so no
training event's outcome window overlaps the test period (prevents the classic
overlapping-label leak that fakes high win-rates).

For each out-of-sample fold we compare, on the SAME candidate signals:
  BASELINE : take every signal the engine would take
  META     : take only signals whose predicted P(win) >= a threshold chosen on
             the training fold (never on test)
and report win-rate, avg net %/trade, profit factor, trade count, and the LIFT.

  python3 scripts/meta_label_train.py --data /tmp/meta_IN.csv
"""
from __future__ import annotations

import argparse
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

FEATURES = ["conviction", "mom20", "mom63", "rs20", "atr_pct", "dist_hi20",
            "rng_pos", "rvol", "vol_contract", "mkt_mom20", "gap_pct",
            "regime_on", "regime_neutral", "regime_strong", "is_gap",
            "day_rank", "day_breadth", "dow"]


def stats(sub):
    if len(sub) == 0:
        return dict(n=0, wr=float("nan"), avg=float("nan"), pf=float("nan"), total=0.0)
    wins = sub[sub.net_ret > 0].net_ret.sum()
    losses = sub[sub.net_ret <= 0].net_ret.sum()
    pf = (wins / abs(losses)) if losses < 0 else float("inf")
    return dict(n=len(sub), wr=(sub.label.mean() * 100), avg=sub.net_ret.mean(),
                pf=pf, total=sub.net_ret.sum())


def pick_threshold(train_df, proba, grid):
    """Choose the P(win) cutoff that maximizes TOTAL net return on the training
    fold while keeping a reasonable trade count (>= 20% of candidates)."""
    best, best_thr = -1e9, 0.5
    n = len(train_df)
    for t in grid:
        take = train_df[proba >= t]
        if len(take) < max(20, 0.15 * n):
            continue
        score = take.net_ret.sum()
        if score > best:
            best, best_thr = score, t
    return best_thr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/tmp/meta_events.csv")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--embargo-days", type=int, default=30)  # >= max hold (20 td)
    ap.add_argument("--fixed-thr", type=float, default=0.0)   # >0 = use this cutoff, skip auto-pick
    ap.add_argument("--save", default="")                     # path to persist the final model bundle
    ap.add_argument("--floor", type=float, default=0.58)      # ship probability floor stored in the bundle
    a = ap.parse_args()

    df = pd.read_csv(a.data)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print("events=%d  %s..%s  base win-rate=%.1f%%  base avg net=%+.2f%%"
          % (len(df), df.date.min().date(), df.date.max().date(),
             df.label.mean() * 100, df.net_ret.mean()))

    # contiguous date blocks -> expanding-window folds
    dates = np.array(sorted(df.date.unique()))
    blocks = np.array_split(dates, a.folds + 1)  # first block = initial train only
    grid = np.round(np.arange(0.30, 0.86, 0.02), 2)

    oos_base, oos_meta, oos_pred = [], [], []
    for k in range(1, a.folds + 1):
        test_dates = blocks[k]
        test_start = pd.Timestamp(test_dates.min())
        train_cut = test_start - pd.Timedelta(days=a.embargo_days)
        tr = df[df.date <= train_cut]
        te = df[(df.date >= test_start) & (df.date <= pd.Timestamp(test_dates.max()))]
        if len(tr) < 200 or len(te) < 30 or tr.label.nunique() < 2:
            continue
        clf = GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                         learning_rate=0.05, subsample=0.8,
                                         random_state=42)
        clf.fit(tr[FEATURES], tr.label)
        p_tr = clf.predict_proba(tr[FEATURES])[:, 1]
        thr = a.fixed_thr if a.fixed_thr > 0 else pick_threshold(tr, p_tr, grid)
        p_te = clf.predict_proba(te[FEATURES])[:, 1]
        te_meta = te[p_te >= thr]
        b, m = stats(te), stats(te_meta)
        oos_base.append(te.assign(_take=1))
        oos_meta.append(te[p_te >= thr])
        oos_pred.append(te.assign(p=p_te))
        print("\n-- fold %d  test %s..%s  thr=%.2f --"
              % (k, test_start.date(), pd.Timestamp(test_dates.max()).date(), thr))
        print("   BASELINE n=%4d  win=%5.1f%%  avg=%+.2f%%  PF=%.2f  total=%+.0f%%"
              % (b["n"], b["wr"], b["avg"], b["pf"], b["total"]))
        print("   META     n=%4d  win=%5.1f%%  avg=%+.2f%%  PF=%.2f  total=%+.0f%%"
              % (m["n"], m["wr"], m["avg"], m["pf"], m["total"]))

    if not oos_base:
        print("\nnot enough data for walk-forward")
        return
    B = stats(pd.concat(oos_base)); M = stats(pd.concat(oos_meta))
    pooled = pd.concat(oos_pred)  # every test event with its OOS predicted prob

    print("\n===== POOLED OOS THRESHOLD SWEEP (selectivity vs edge) =====")
    print(" thr   trades  keep%   win%    avg net   PF")
    for t in np.round(np.arange(0.40, 0.75, 0.05), 2):
        s = stats(pooled[pooled.p >= t])
        keep = 100.0 * s["n"] / len(pooled)
        print("%.2f  %6d  %4.0f%%  %5.1f%%  %+.2f%%   %.2f"
              % (t, s["n"], keep, s["wr"], s["avg"], s["pf"]))
    print("\n-- per-strategy at the fold-chosen thresholds (pooled OOS) --")
    taken = pd.concat(oos_meta)
    for strat, g in taken.groupby("strategy"):
        s = stats(g)
        print("   %-14s trades=%5d  win=%5.1f%%  avg=%+.2f%%  PF=%.2f" % (strat, s["n"], s["wr"], s["avg"], s["pf"]))
    print("\n================ POOLED OUT-OF-SAMPLE ================")
    print("BASELINE  trades=%4d  win-rate=%5.1f%%  avg net/trade=%+.2f%%  PF=%.2f"
          % (B["n"], B["wr"], B["avg"], B["pf"]))
    print("META      trades=%4d  win-rate=%5.1f%%  avg net/trade=%+.2f%%  PF=%.2f"
          % (M["n"], M["wr"], M["avg"], M["pf"]))
    print("LIFT      win-rate %+.1f pts   avg net/trade %+.2f pts   kept %.0f%% of trades"
          % (M["wr"] - B["wr"], M["avg"] - B["avg"], 100.0 * M["n"] / max(B["n"], 1)))

    # feature importance from a full-sample fit (insight only, not evaluation)
    clf = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                     subsample=0.8, random_state=42).fit(df[FEATURES], df.label)
    imp = sorted(zip(FEATURES, clf.feature_importances_), key=lambda x: -x[1])
    print("\ntop features:", ", ".join("%s=%.2f" % (f, w) for f, w in imp[:8]))

    if a.save and len(df) < 15000:
        # retrain guard: a shrunken dataset means the pipeline broke upstream —
        # keep serving the previous model rather than shipping a degenerate one
        print("\nREFUSING to save: only %d events (< 15000) — data pipeline problem?" % len(df))
        return
    if a.save:
        import joblib
        bundle = dict(model=clf, features=FEATURES, floor=a.floor,
                      trained_on=len(df), date_range=[str(df.date.min().date()), str(df.date.max().date())])
        joblib.dump(bundle, a.save)
        print("\nsaved model bundle -> %s  (floor=%.2f, trained on %d events)" % (a.save, a.floor, len(df)))


if __name__ == "__main__":
    main()
