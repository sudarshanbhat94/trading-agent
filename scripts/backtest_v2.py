"""OpenStocks v2 engine - brand-new deterministic scoring/decision core + backtest.

Old logic (raw_opportunity_v1 chase scorer, the 3 competing scores, the
conjunctive gate wall) is scrapped. This is a clean factor engine built only on
what the validated backtest showed the data rewards:
  - never chase extension (>4% intraday / far above base = proven loser)
  - favour trend + relative strength + volume confirmation + moderate momentum
  - buy pullbacks inside uptrends, not breakouts at the high
  - a separate "pre-move" detector (volatility squeeze + accumulation) for
    catching the big move early

Everything is deterministic (no LLM). The backtest enters at next-day OPEN (no
look-ahead), simulates an ATR stop / target / time exit, and charges round-trip
transaction costs. The headline test: is the conviction score MONOTONIC with
forward net return? (The old overall_score_pct was not.)

Run read-only on the OCI DB:
  python3 backtest_v2.py --market IN --topn 700
  python3 backtest_v2.py --market US --topn 700 --mode premove
"""
from __future__ import annotations

import argparse
import sqlite3
import numpy as np
import pandas as pd

DB = "/opt/opentrade/var/trading_agent.db"
DAILY = {"IN": "upstox-live:day", "US": "alpaca-iex-live:day"}
# realistic round-trip cost (brokerage+taxes+slippage), in %.
DEFAULT_COST = {"IN": 0.30, "US": 0.12}


def load_market(con, market, topn, min_bars=120):
    src = DAILY[market]
    df = pd.read_sql_query(
        "SELECT symbol, ts, open, high, low, close, volume FROM candles WHERE source=?",
        con, params=(src,),
    )
    if df.empty:
        return {}, None
    df["date"] = pd.to_datetime(df["ts"].str[:10])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "high", "low"])
    df = df.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"])
    # liquidity filter: keep top-N symbols by median daily turnover
    turn = df.assign(t=df["close"] * df["volume"]).groupby("symbol")["t"].median()
    cnt = df.groupby("symbol")["close"].count()
    eligible = cnt[cnt >= min_bars].index
    turn = turn.loc[turn.index.isin(eligible)].sort_values(ascending=False)
    keep = set(turn.head(topn).index)
    df = df[df["symbol"].isin(keep)].copy()
    # synthetic market index = median daily return across the liquid universe
    df["ret1"] = df.groupby("symbol")["close"].pct_change()
    mkt = df.groupby("date")["ret1"].median().rename("mkt_ret1")
    mkt_cum = (1 + mkt.fillna(0)).cumprod().rename("mkt_cum")
    market_df = pd.concat([mkt, mkt_cum], axis=1)
    return {s: g.set_index("date") for s, g in df.groupby("symbol")}, market_df


def load_delivery(con, market):
    """{symbol: Series(date -> delivery_pct)}. Column names detected at runtime."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(delivery_data)")]
    if not cols:
        return {}
    datecol = "date" if "date" in cols else ("ts" if "ts" in cols else None)
    pctc = [c for c in cols if "deliv" in c.lower() and ("pct" in c.lower() or "perc" in c.lower())]
    if not pctc:
        pctc = [c for c in cols if "deliv" in c.lower()]
    if not datecol or not pctc:
        print(f"  (delivery_data: could not detect date/pct columns in {cols})")
        return {}
    df = pd.read_sql_query(f"SELECT symbol, {datecol} AS d, {pctc[0]} AS dp FROM delivery_data", con)
    df["date"] = pd.to_datetime(df["d"].astype(str).str[:10], errors="coerce")
    df["dp"] = pd.to_numeric(df["dp"], errors="coerce")
    df = df.dropna(subset=["date", "dp"])
    return {s: g.set_index("date")["dp"].sort_index() for s, g in df.groupby("symbol")}


def load_sentiment(con):
    """{symbol: Series(date -> mean sentiment score)} aggregated per day."""
    df = pd.read_sql_query("SELECT symbol, substr(ts,1,10) AS d, score FROM sentiment_events", con)
    df["date"] = pd.to_datetime(df["d"], errors="coerce")
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df = df.dropna(subset=["date", "score"])
    g = df.groupby(["symbol", "date"])["score"].mean().reset_index()
    return {s: gg.set_index("date")["score"].sort_index() for s, gg in g.groupby("symbol")}


def features(g: pd.DataFrame, market_df: pd.DataFrame, deliv_s=None, sent_s=None) -> pd.DataFrame:
    g = g.copy()
    c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
    g["sma20"] = c.rolling(20).mean()
    g["sma50"] = c.rolling(50).mean()
    g["mom20"] = c / c.shift(20) - 1
    g["mom63"] = c / c.shift(63) - 1
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    g["atr14"] = tr.rolling(14).mean()
    g["atr_pct"] = g["atr14"] / c
    g["hi20"] = h.rolling(20).max()
    g["lo20"] = l.rolling(20).min()
    g["dist_hi20"] = c / g["hi20"] - 1                 # 0 = at 20d high, <0 below
    g["rng_pos"] = (c - g["lo20"]) / (g["hi20"] - g["lo20"]).replace(0, np.nan)
    g["rvol"] = v / v.rolling(20).mean()
    g["vol_contract"] = g["atr14"] / g["atr14"].shift(20)   # <1 = squeezing
    # relative strength vs synthetic market over 20d
    mkt20 = market_df["mkt_cum"] / market_df["mkt_cum"].shift(20) - 1
    g = g.join(mkt20.rename("mkt_mom20"))
    g["rs20"] = g["mom20"] - g["mkt_mom20"]
    # delivery % (institutional footprint): level + spike vs its own 20d norm
    if deliv_s is not None and len(deliv_s):
        dp = deliv_s.reindex(g.index).ffill(limit=5)
        g["deliv_pct"] = dp
        g["deliv_spike"] = dp - dp.rolling(20, min_periods=5).mean()
    else:
        g["deliv_pct"] = np.nan
        g["deliv_spike"] = np.nan
    # news sentiment: carry a few days (news persists), plus a 5d average
    if sent_s is not None and len(sent_s):
        sc = sent_s.reindex(g.index).ffill(limit=3)
        g["sent"] = sc
        g["sent5"] = sc.rolling(5, min_periods=1).mean()
    else:
        g["sent"] = np.nan
        g["sent5"] = np.nan
    return g


def conviction(row) -> float:
    """Mean-reversion conviction (0..1), built from the SIGN of the discovered
    factor edges: this universe rewards buying beaten-down dips in relatively
    strong, high-volume, volatile names - NOT chasing momentum at the high.
      + dip below 20d high      (dist_hi20 low  -> edge)
      + oversold recent move    (mom20 low/neg  -> edge)
      + high relative strength  (rs20 high      -> edge)
      + volume waking up        (rvol high      -> edge)
      + sufficient volatility   (atr_pct high   -> edge; the movers)
      quality guard: still structurally alive (above sma50 OR positive RS) to
      avoid catching pure falling knives."""
    def clamp(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))
    if any(pd.isna(row.get(k)) for k in ("sma50", "mom20", "atr_pct", "rs20", "rvol", "dist_hi20")):
        return 0.0
    if pd.isna(row.get("vol_contract")):
        return 0.0
    # built from the cleanest monotonic factors found in the scan
    dip = clamp(-row["dist_hi20"] / 0.10)                 # 10% below 20d high -> 1.0  (mono -0.74)
    volq = clamp(row["atr_pct"] / 0.05)                   # higher vol up to 5%        (mono +0.92)
    vexp = clamp((row["vol_contract"] - 0.8) / 0.6)       # ATR expanding vs 20d ago   (mono +0.98)
    rs = clamp(0.5 + row["rs20"] * 5.0)                   # +10% RS vs market -> 1.0   (mono +0.67)
    vol = clamp((row["rvol"] - 0.8) / 1.0)                # volume waking up           (mono +0.79)
    score = 0.22 * dip + 0.24 * volq + 0.20 * vexp + 0.18 * rs + 0.16 * vol
    # falling-knife guard
    if row["mom20"] < -0.25 or row["dist_hi20"] < -0.35:
        score *= 0.3
    return clamp(score)


def premove(row) -> float:
    """Pre-breakout 'big move coming' detector: volatility squeeze + volume
    waking up + coiled near a base with positive relative strength."""
    keys = ("vol_contract", "rvol", "dist_hi20", "rs20", "atr_pct", "sma50", "close")
    if any(pd.isna(row.get(k)) for k in keys):
        return 0.0
    squeeze = 1.0 if row["vol_contract"] < 0.8 else (0.5 if row["vol_contract"] < 1.0 else 0.0)
    waking = 1.0 if row["rvol"] > 1.3 else (0.5 if row["rvol"] > 1.0 else 0.0)
    coiled = 1.0 if -0.06 <= row["dist_hi20"] <= -0.005 else 0.0   # just under a base high
    rs_ok = 1.0 if row["rs20"] > 0 else 0.0
    above = 1.0 if row["close"] > row["sma50"] else 0.0
    return 0.30 * squeeze + 0.25 * waking + 0.20 * coiled + 0.15 * rs_ok + 0.10 * above


def simulate_trade(g, i, atr_stop=2.0, atr_target=3.5, max_days=10, cost_pct=0.30):
    """Enter at NEXT day open; exit on ATR stop, ATR target, or time stop.
    Conservative intrabar: stop checked before target."""
    if i + 1 >= len(g):
        return None
    entry = g["open"].iloc[i + 1]
    atr = g["atr14"].iloc[i]
    if not (entry > 0) or not (atr > 0):
        return None
    stop = entry - atr_stop * atr
    target = entry + atr_target * atr
    for j in range(i + 1, min(i + 1 + max_days, len(g))):
        hi, lo, cl = g["high"].iloc[j], g["low"].iloc[j], g["close"].iloc[j]
        if lo <= stop:
            gross = (stop - entry) / entry * 100
            return gross - cost_pct, "stop", j - i
        if hi >= target:
            gross = (target - entry) / entry * 100
            return gross - cost_pct, "target", j - i
    last = g["close"].iloc[min(i + max_days, len(g) - 1)]
    return (last - entry) / entry * 100 - cost_pct, "time", min(max_days, len(g) - 1 - i)


def run(market, topn, mode, cost_pct, thresh, hold, start, con):
    syms, market_df = load_market(con, market, topn)
    print(f"[{market}] liquid universe: {len(syms)} symbols; "
          f"market history {market_df.index.min().date()}..{market_df.index.max().date()}")
    trades = []
    scorer = {"factor": conviction, "premove": premove}[mode]
    for sym, g in syms.items():
        if len(g) < 80:
            continue
        g = features(g, market_df)
        for i in range(60, len(g) - 1):
            if start and g.index[i] < pd.Timestamp(start):
                continue
            row = g.iloc[i]
            sc = scorer(row)
            if sc < thresh:
                continue
            res = simulate_trade(g, i, max_days=hold, cost_pct=cost_pct)
            if res is None:
                continue
            ret, reason, days = res
            trades.append({"sym": sym, "date": g.index[i], "score": sc,
                           "ret": ret, "reason": reason, "days": days,
                           "mom20": row["mom20"], "rs20": row["rs20"]})
    if not trades:
        print("  no trades generated")
        return
    t = pd.DataFrame(trades)
    n = len(t)
    wr = (t["ret"] > 0).mean() * 100
    exp = t["ret"].mean()
    pf = t.loc[t.ret > 0, "ret"].sum() / abs(t.loc[t.ret <= 0, "ret"].sum() or 1)
    print(f"  mode={mode} thresh={thresh} cost={cost_pct}% hold={hold}d")
    print(f"  TRADES n={n:,}  win={wr:.1f}%  expectancy(net)={exp:+.3f}%/trade  "
          f"profit_factor={pf:.2f}  avgW={t.loc[t.ret>0,'ret'].mean():+.2f}% avgL={t.loc[t.ret<=0,'ret'].mean():+.2f}%")
    print(f"  exit mix: {t['reason'].value_counts().to_dict()}")
    # HEADLINE: is conviction monotonic with forward net return?
    print("  --- conviction-bucket monotonicity (the test the old score failed) ---")
    t["bucket"] = pd.cut(t["score"], [0, 0.4, 0.5, 0.6, 0.7, 1.01])
    g2 = t.groupby("bucket", observed=True)["ret"].agg(["count", "mean"])
    for b, row in g2.iterrows():
        wrr = (t.loc[t.bucket == b, "ret"] > 0).mean() * 100
        print(f"    score {str(b):14s} n={int(row['count']):>5} win={wrr:4.1f}% net_exp={row['mean']:+.3f}%")
    # equity-ish: total net P&L if 1 unit per trade
    print(f"  total net P&L (1 unit/trade): {t['ret'].sum():+.1f} units over {n} trades")


def scan_factors(market, topn, cost_pct, hold, con):
    """Factor discovery: for every (symbol, day), compute the realised forward
    return (enter next open, exit `hold` days later, minus cost) and report mean
    forward return by decile of each individual feature. This reveals which
    factors actually predict, before any weighting/overfitting."""
    syms, market_df = load_market(con, market, topn)
    deliv = load_delivery(con, market)
    sent = load_sentiment(con)
    print(f"[{market}] scanning {len(syms)} symbols for factor edge "
          f"(fwd {hold}d, cost {cost_pct}%); delivery symbols={len(deliv)}, sentiment symbols={len(sent)}")
    frames = []
    for sym, g in syms.items():
        if len(g) < 90:
            continue
        g = features(g, market_df, deliv.get(sym), sent.get(sym))
        nopen = g["open"].shift(-1)               # next-day open (entry)
        fexit = g["close"].shift(-(hold + 1))      # close `hold` days after entry
        g["fwd"] = (fexit / nopen - 1) * 100 - cost_pct
        g["conv"] = g.apply(conviction, axis=1)
        g["pre"] = g.apply(premove, axis=1)
        frames.append(g)
    allf = pd.concat(frames).dropna(subset=["fwd"])
    print(f"  observations: {len(allf):,}  baseline mean fwd (any day) = {allf['fwd'].mean():+.3f}%")
    feats = ["mom20", "mom63", "rs20", "atr_pct", "rvol", "dist_hi20",
             "vol_contract", "rng_pos", "deliv_pct", "deliv_spike", "sent", "sent5",
             "conv", "pre"]
    for f in feats:
        sub = allf[[f, "fwd"]].dropna()
        if len(sub) < 1000:
            continue
        try:
            sub["d"] = pd.qcut(sub[f], 10, labels=False, duplicates="drop")
        except ValueError:
            continue
        gg = sub.groupby("d")["fwd"].agg(["count", "mean"])
        lo = gg["mean"].iloc[0]
        hi = gg["mean"].iloc[-1]
        spread = hi - lo
        # monotonicity: correlation of decile rank vs mean fwd return (numpy, no scipy)
        ranks = np.arange(len(gg))
        mono = float(np.corrcoef(ranks, gg["mean"].values)[0, 1]) if len(gg) > 2 else 0.0
        flag = "  <== EDGE" if abs(spread) > 0.5 and abs(mono) > 0.6 else ""
        print(f"  {f:13s} n={len(sub):>7,} D1={lo:+.3f}% D10={hi:+.3f}% spread={spread:+.3f}% mono={mono:+.2f}{flag}")


def portfolio(market, topn, cost_pct, thresh, hold, max_pos, regime_on,
              atr_stop, atr_target, start, con):
    """Event-driven portfolio sim: ranks daily signals by conviction, holds at
    most `max_pos` equal-weight positions, enters next-day open, exits on ATR
    stop / target / time. Optional market-regime filter blocks new dip-buys when
    the synthetic index is below its own 50d average (downtrend protection).
    Reports the real equity curve: total return, CAGR, max drawdown, Sharpe."""
    syms, market_df = load_market(con, market, topn)
    # market regime: synthetic index above its 50d average
    reg = (market_df["mkt_cum"] > market_df["mkt_cum"].rolling(50).mean())
    bars = {}          # sym -> {date: (o,h,l,c,atr,conv)}
    all_dates = set()
    for sym, g in syms.items():
        if len(g) < 90:
            continue
        g = features(g, market_df)
        g["conv"] = g.apply(conviction, axis=1)
        d = {}
        for ts, r in g.iterrows():
            if pd.isna(r["close"]) or pd.isna(r["open"]):
                continue
            d[ts] = (r["open"], r["high"], r["low"], r["close"], r["atr14"], r["conv"])
            all_dates.add(ts)
        bars[sym] = d
    dates = sorted(all_dates)
    if start:
        dates = [d for d in dates if d >= pd.Timestamp(start)]
    equity = 100000.0
    cash = equity
    pos = {}           # sym -> {shares, entry, stop, target, eday}
    pending = []       # [(sym, conv)] queued at close d, executed at open d+1
    curve = []
    n_trades = wins = 0
    cside = cost_pct / 200.0   # half the round-trip per side, as fraction
    for di, d in enumerate(dates):
        # 1) execute pending entries at today's open
        slots = max_pos - len(pos)
        for sym, conv in pending:
            if slots <= 0:
                break
            b = bars.get(sym, {}).get(d)
            if not b or sym in pos:
                continue
            o, _, _, _, atr, _ = b
            if not (o > 0 and atr > 0):
                continue
            alloc = min(equity / max_pos, cash)
            if alloc <= 0:
                continue
            shares = alloc / o
            cash -= shares * o * (1 + cside)
            pos[sym] = {"shares": shares, "entry": o,
                        "stop": o - atr_stop * atr, "target": o + atr_target * atr,
                        "eday": di}
            slots -= 1
        pending = []
        # 2) check exits on today's bar
        for sym in list(pos.keys()):
            b = bars.get(sym, {}).get(d)
            if not b:
                continue
            o, h, l, c, atr, conv = b
            p = pos[sym]
            exit_px = None
            if l <= p["stop"]:
                exit_px = min(p["stop"], o)
            elif h >= p["target"]:
                exit_px = max(p["target"], o)
            elif di - p["eday"] >= hold:
                exit_px = c
            if exit_px is not None:
                cash += p["shares"] * exit_px * (1 - cside)
                n_trades += 1
                if exit_px > p["entry"]:
                    wins += 1
                del pos[sym]
        # 3) mark-to-market equity
        mtm = sum(pb["shares"] * (bars.get(s, {}).get(d, (0, 0, 0, pb["entry"]))[3] or pb["entry"])
                  for s, pb in pos.items())
        equity = cash + mtm
        curve.append((d, equity))
        # 4) generate tomorrow's entry queue (regime-gated)
        if regime_on and not bool(reg.get(d, False)):
            continue
        cands = []
        for sym, dd in bars.items():
            if sym in pos:
                continue
            b = dd.get(d)
            if b and b[5] >= thresh:
                cands.append((sym, b[5]))
        cands.sort(key=lambda x: -x[1])
        pending = cands[: max_pos]
    # stats
    eq = pd.Series([e for _, e in curve], index=[d for d, _ in curve])
    rets = eq.pct_change().dropna()
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    ndays = len(eq)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (252.0 / max(ndays, 1)) - 1
    peak = eq.cummax()
    mdd = ((eq - peak) / peak).min()
    sharpe = (rets.mean() / rets.std() * (252 ** 0.5)) if rets.std() else 0.0
    print(f"[{market}] PORTFOLIO  regime_filter={'ON' if regime_on else 'off'}  "
          f"max_pos={max_pos} thresh={thresh} hold={hold}d cost={cost_pct}%")
    print(f"  period {eq.index[0].date()}..{eq.index[-1].date()} ({ndays} days)")
    print(f"  total return: {total_ret*100:+.1f}%   CAGR: {cagr*100:+.1f}%   "
          f"max drawdown: {mdd*100:.1f}%   Sharpe: {sharpe:.2f}")
    print(f"  closed trades: {n_trades:,}  win rate: {wins/max(n_trades,1)*100:.1f}%   "
          f"end equity: {eq.iloc[-1]:,.0f}")
    # benchmark: equal-weight buy & hold of the universe over the same window
    mc = market_df["mkt_cum"].reindex(eq.index).ffill()
    if mc.notna().sum() > 2:
        b_ret = mc.iloc[-1] / mc.iloc[0] - 1
        b_peak = mc.cummax()
        b_mdd = ((mc - b_peak) / b_peak).min()
        edge = total_ret - b_ret
        print(f"  BENCHMARK (buy&hold universe): return {b_ret*100:+.1f}%  max drawdown {b_mdd*100:.1f}%")
        print(f"  ==> ENGINE vs BUY&HOLD: {edge*100:+.1f}pp  ({'ALPHA: beats hold' if edge>0 else 'just beta: hold won'}); "
              f"engine DD {mdd*100:.1f}% vs hold DD {b_mdd*100:.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["IN", "US"], default="IN")
    ap.add_argument("--topn", type=int, default=700)
    ap.add_argument("--mode", choices=["factor", "premove", "scan", "portfolio"], default="factor")
    ap.add_argument("--max-pos", type=int, default=20)
    ap.add_argument("--no-regime", action="store_true")
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--thresh", type=float, default=0.6)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--start", default=None, help="only trade signals on/after this date (out-of-sample)")
    args = ap.parse_args()
    cost = args.cost if args.cost is not None else DEFAULT_COST[args.market]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    if args.mode == "scan":
        scan_factors(args.market, args.topn, cost, args.hold, con)
    elif args.mode == "portfolio":
        portfolio(args.market, args.topn, cost, args.thresh, args.hold, args.max_pos,
                  not args.no_regime, 2.0, 3.5, args.start, con)
    else:
        run(args.market, args.topn, args.mode, cost, args.thresh, args.hold, args.start, con)


if __name__ == "__main__":
    main()
