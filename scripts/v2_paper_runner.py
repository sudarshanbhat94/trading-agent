"""v2 paper runner - forward paper-trades the validated v2 engines on live data.

Runs TWO deterministic strategies side by side, each its own book:
  - swing_meanrev : buy dips in strong names, ATR stop/target/time, regime-gated
  - gap_momentum  : buy gap-ups on volume, ATR stop + trailing stop, NOT regime-gated

Reads daily candles READ-ONLY from the main DB; keeps its own paper book in a
SEPARATE file (var/v2_paper.db) so it can never lock/corrupt the live service.
Each run advances every (market, strategy) book to the latest complete trading
date. Idempotent. No LLM.

  python3 scripts/v2_paper_runner.py --markets IN,US
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import v2_engine as eng  # noqa: E402

# strategy registry --------------------------------------------------------
STRATEGIES = {
    "swing_meanrev": dict(signal="conviction", regime_gated=True, threshold=0.0,
                          hold=6, atr_stop=2.0, atr_target=3.5, trail=0.0, max_pos=50),
    "gap_momentum":  dict(signal="gap", regime_gated=False, threshold=0.0,
                          hold=20, atr_stop=1.5, atr_target=0.0, trail=0.10, max_pos=50),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_state(
  market TEXT, strategy TEXT, last_date TEXT, capital REAL, max_pos INTEGER,
  started_at TEXT, PRIMARY KEY(market, strategy));
CREATE TABLE IF NOT EXISTS v2_positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, shares REAL, stop REAL, target REAL,
  trail REAL, peak REAL, conviction REAL, opened_at TEXT);
CREATE TABLE IF NOT EXISTS v2_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL, shares REAL,
  pnl REAL, return_pct REAL, reason TEXT, conviction REAL);
CREATE TABLE IF NOT EXISTS v2_pending(
  market TEXT, strategy TEXT, symbol TEXT, conviction REAL, atr REAL,
  stop_atr REAL, target_atr REAL, trail REAL, signal_date TEXT);
CREATE TABLE IF NOT EXISTS v2_equity(
  market TEXT, strategy TEXT, date TEXT, equity REAL, cash REAL,
  positions_value REAL, n_positions INTEGER, PRIMARY KEY(market, strategy, date));
CREATE TABLE IF NOT EXISTS v2_signals(
  market TEXT, strategy TEXT, date TEXT, symbol TEXT, conviction REAL,
  ref_close REAL, rank INTEGER);
"""


def ensure_schema(v2):
    v2.executescript(SCHEMA); v2.commit()


def get_state(v2, market, strat, capital, max_pos):
    r = v2.execute("SELECT last_date FROM v2_state WHERE market=? AND strategy=?",
                   (market, strat)).fetchone()
    if r:
        return r[0]
    v2.execute("INSERT INTO v2_state(market,strategy,last_date,capital,max_pos,started_at) "
               "VALUES(?,?,?,?,?,?)", (market, strat, None, capital, max_pos,
                                       datetime.now(timezone.utc).isoformat()))
    v2.commit()
    return None


def process(main, v2, market, strat, cfg, capital, dry):
    syms, market_df = eng.load_panel(main, market, topn=eng.DEFAULTS["topn"])
    if not syms:
        print(f"[{market}/{strat}] no data"); return
    cside = eng.COST_PCT[market] / 200.0
    max_pos, hold = cfg["max_pos"], cfg["hold"]
    alloc = capital / max_pos
    last_s = get_state(v2, market, strat, capital, max_pos)
    all_dates = eng.complete_trading_dates(syms, 0.5)
    if not all_dates:
        print(f"[{market}/{strat}] no complete dates"); return
    last = pd.Timestamp(last_s) if last_s else None
    todo = [d for d in all_dates if (last is None or d > last)]
    if not todo:
        print(f"[{market}/{strat}] up to date at {last_s}"); return
    if last is None:
        todo = [all_dates[-1]]
    # load open positions + pending for this book
    positions = {}
    for r in v2.execute("SELECT id,symbol,entry_date,entry_price,shares,stop,target,trail,peak,conviction "
                        "FROM v2_positions WHERE market=? AND strategy=?", (market, strat)):
        positions[r[1]] = dict(id=r[0], entry_date=r[2], entry=r[3], shares=r[4],
                               stop=r[5], target=r[6], trail=r[7], peak=r[8], conviction=r[9])
    pending = [dict(symbol=r[0], conviction=r[1], atr=r[2], stop_atr=r[3], target_atr=r[4], trail=r[5])
               for r in v2.execute("SELECT symbol,conviction,atr,stop_atr,target_atr,trail "
                                    "FROM v2_pending WHERE market=? AND strategy=?", (market, strat))]
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND strategy=?",
                          (market, strat)).fetchone()[0] or 0.0
    cash = capital - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
    for d in todo:
        di = all_dates.index(d)
        # 1) execute pending entries at open
        for cand in pending:
            if len(positions) >= max_pos:
                break
            g = syms.get(cand["symbol"])
            if g is None or d not in g.index or cand["symbol"] in positions:
                continue
            o, atr = float(g.loc[d, "open"]), cand["atr"]
            if not (o > 0 and atr > 0) or alloc > cash:
                continue
            shares = alloc / o
            cash -= shares * o * (1 + cside)
            tgt = o + cand["target_atr"] * atr if cand["target_atr"] else 0.0
            pid = None
            if not dry:
                pid = v2.execute(
                    "INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,"
                    "stop,target,trail,peak,conviction,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (market, strat, cand["symbol"], d.date().isoformat(), o, shares,
                     o - cand["stop_atr"] * atr, tgt, cand["trail"], o, cand["conviction"],
                     datetime.now(timezone.utc).isoformat())).lastrowid
            positions[cand["symbol"]] = dict(id=pid, entry_date=d.date().isoformat(), entry=o,
                                             shares=shares, stop=o - cand["stop_atr"] * atr,
                                             target=tgt, trail=cand["trail"], peak=o,
                                             conviction=cand["conviction"])
        pending = []
        if not dry:
            v2.execute("DELETE FROM v2_pending WHERE market=? AND strategy=?", (market, strat))
        # 2) exits (ATR stop / target / trailing / time)
        for sym in list(positions.keys()):
            g = syms.get(sym)
            if g is None or d not in g.index:
                continue
            o, h, l, c = (float(g.loc[d, k]) for k in ("open", "high", "low", "close"))
            p = positions[sym]
            p["peak"] = max(p["peak"], h)
            eff_stop = p["stop"]
            if p["trail"]:
                eff_stop = max(eff_stop, p["peak"] * (1 - p["trail"]))
            held = di - all_dates.index(pd.Timestamp(p["entry_date"])) if pd.Timestamp(p["entry_date"]) in all_dates else hold
            exit_px = reason = None
            if l <= eff_stop:
                exit_px, reason = eff_stop, ("trail" if p["trail"] and eff_stop > p["stop"] else "stop")
            elif p["target"] and h >= p["target"]:
                exit_px, reason = p["target"], "target"
            elif held >= hold:
                exit_px, reason = c, "time"
            if exit_px is not None:
                proceeds = p["shares"] * exit_px * (1 - cside)
                cash += proceeds
                ret = (exit_px / p["entry"] - 1) * 100
                if not dry:
                    v2.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,"
                               "exit_date,exit_price,shares,pnl,return_pct,reason,conviction) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (market, strat, sym, p["entry_date"], p["entry"], d.date().isoformat(),
                                exit_px, p["shares"], proceeds - p["shares"] * p["entry"], ret,
                                reason, p["conviction"]))
                    if p["id"] is not None:
                        v2.execute("DELETE FROM v2_positions WHERE id=?", (p["id"],))
                del positions[sym]
            elif not dry and p["id"] is not None:
                v2.execute("UPDATE v2_positions SET peak=? WHERE id=?", (p["peak"], p["id"]))
        # 3) equity
        pv = sum(p["shares"] * (float(syms[s].loc[d, "close"]) if d in syms[s].index else p["entry"])
                 for s, p in positions.items())
        equity = cash + pv
        if not dry:
            v2.execute("INSERT OR REPLACE INTO v2_equity VALUES(?,?,?,?,?,?,?)",
                       (market, strat, d.date().isoformat(), equity, cash, pv, len(positions)))
        # 4) generate signals + queue pending
        if cfg["signal"] == "conviction":
            sigs = eng.signals_for_date(syms, market_df, d, cfg["threshold"],
                                        cfg["atr_stop"], cfg["atr_target"])
            for s in sigs:
                s["trail"] = cfg["trail"]
        else:
            sigs = eng.gap_signals_for_date(syms, market_df, d, atr_stop=cfg["atr_stop"], trail=cfg["trail"])
        if not dry:
            v2.execute("DELETE FROM v2_signals WHERE market=? AND strategy=? AND date=?",
                       (market, strat, d.date().isoformat()))
            for rank, s in enumerate(sigs[:max_pos], 1):
                v2.execute("INSERT INTO v2_signals VALUES(?,?,?,?,?,?,?)",
                           (market, strat, d.date().isoformat(), s["symbol"], s["conviction"],
                            s["ref_close"], rank))
        gate_ok = (not cfg["regime_gated"]) or eng.regime_ok(market_df, d, eng.DEFAULTS["regime_lookback"])
        if gate_ok:
            free = max_pos - len(positions)
            for s in sigs:
                if free <= 0:
                    break
                if s["symbol"] in positions:
                    continue
                pending.append(dict(symbol=s["symbol"], conviction=s["conviction"], atr=s["atr"],
                                    stop_atr=s["stop_from_entry_atr"], target_atr=s["target_from_entry_atr"],
                                    trail=s.get("trail", cfg["trail"])))
                free -= 1
        if not dry:
            for cand in pending:
                v2.execute("INSERT INTO v2_pending VALUES(?,?,?,?,?,?,?,?,?)",
                           (market, strat, cand["symbol"], cand["conviction"], cand["atr"],
                            cand["stop_atr"], cand["target_atr"], cand["trail"], d.date().isoformat()))
            v2.execute("UPDATE v2_state SET last_date=? WHERE market=? AND strategy=?",
                       (d.date().isoformat(), market, strat))
            v2.commit()
    eqr = v2.execute("SELECT equity FROM v2_equity WHERE market=? AND strategy=? ORDER BY date DESC LIMIT 1",
                     (market, strat)).fetchone()
    ntr = v2.execute("SELECT COUNT(*) FROM v2_trades WHERE market=? AND strategy=?",
                     (market, strat)).fetchone()[0]
    print(f"[{market}/{strat}] -> {todo[-1].date()} equity={(eqr[0] if eqr else capital):,.0f} "
          f"open={len(positions)} pending={len(pending)} closed={ntr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-db", default="/opt/opentrade/var/trading_agent.db")
    ap.add_argument("--v2-db", default="/opt/opentrade/var/v2_paper.db")
    ap.add_argument("--markets", default="IN,US")
    ap.add_argument("--strategies", default="swing_meanrev,gap_momentum")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    main_con = sqlite3.connect(f"file:{a.main_db}?mode=ro", uri=True, timeout=120)
    v2 = sqlite3.connect(a.v2_db, timeout=60)
    ensure_schema(v2)
    print(f"v2 paper runner @ {datetime.now(timezone.utc).isoformat()} dry={a.dry}")
    for market in [m.strip().upper() for m in a.markets.split(",") if m.strip()]:
        for strat in [s.strip() for s in a.strategies.split(",") if s.strip()]:
            cfg = STRATEGIES.get(strat)
            if not cfg:
                continue
            try:
                process(main_con, v2, market, strat, cfg, a.capital, a.dry)
            except Exception as exc:
                import traceback
                print(f"[{market}/{strat}] ERROR {exc}"); traceback.print_exc()
    v2.close(); main_con.close()


if __name__ == "__main__":
    main()
