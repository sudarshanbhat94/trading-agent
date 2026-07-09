"""Walk-forward validation: run the FULL live pipeline (HYBRID + momentum sleeve)
over sequential, non-overlapping folds. If the edge only exists in one window,
the parameters are curve-fit; if every fold behaves, the engine is robust.

  /opt/opentrade/.venv/bin/python3 scripts/backtest_walkforward.py
"""
import sys
sys.path.insert(0, "/opt/opentrade")
sys.path.insert(0, "/opt/opentrade/scripts")
import sqlite3
import pandas as pd
import backtest_v2 as bt
import backtest_investigation as bi

FOLDS = 4

def main():
    con = sqlite3.connect(f"file:{bi.DB}?mode=ro", uri=True, timeout=180)
    for market in ("IN", "US"):
        syms, mdf = bt.load_market(con, market, bi.TOPN)
        M = bi.build_matrices(syms, mdf)
        idx = [d for d in M["close"].index if d >= pd.Timestamp("2024-01-01")]
        n = len(idx)
        print(f"===== {market}: {n} trading days {str(idx[0])[:10]} -> {str(idx[-1])[:10]} in {FOLDS} folds =====", flush=True)
        for f in range(FOLDS):
            a, b = idx[f * n // FOLDS], idx[min(n - 1, (f + 1) * n // FOLDS - 1)]
            mkt = mdf["mkt_cum"]
            seg = mkt[(mkt.index >= a) & (mkt.index <= b)]
            mret = (seg.iloc[-1] / seg.iloc[0] - 1) * 100 if len(seg) > 1 else 0.0
            m, _ = bi.run(market, "HYBRID", M, mdf, maxpos=14, mom=True, start=a, end=b)
            print(f"  fold{f+1} {str(a)[:10]}->{str(b)[:10]}: engine={m['ret']:+6.1f}%  market={mret:+6.1f}%  "
                  f"alpha={m['ret']-mret:+6.1f}pp  maxDD={m['maxdd']:4.1f}%  trades={m['n']:3d}  win={m['win']:.0f}%", flush=True)
    con.close()

if __name__ == "__main__":
    main()
