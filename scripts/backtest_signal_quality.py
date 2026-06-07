"""Rigorous, read-only signal-quality backtest on public Yahoo daily candles.

Goal: honestly measure the preset-signal edge and prove whether the proposed exit
improvements (ATR-adaptive stops, let-winners-run trailing) and per-setup expectancy
gating actually help -- with an out-of-sample (train/test) split so we are not fooling
ourselves.

No production access, no broker token. Survivorship caveat: uses a fixed current basket,
so results are mildly optimistic; treat as relative comparison between exit modes.

Usage:
    python scripts/backtest_signal_quality.py --mode fixed
    python scripts/backtest_signal_quality.py --mode atr_trail --gate
    python scripts/backtest_signal_quality.py --compare        # run all modes side by side
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Candle
from app.strategy_presets import evaluate_strategy_presets

BASKET = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "LT",
    "ITC", "HINDUNILVR", "BHARTIARTL", "KOTAKBANK", "MARUTI", "TITAN", "SUNPHARMA",
    "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "BAJFINANCE", "HCLTECH",
    "ASIANPAINT", "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC", "COALINDIA", "JSWSTEEL",
    "GRASIM", "CIPLA", "TECHM", "NESTLEIND", "DRREDDY", "BAJAJFINSV", "BPCL",
    "EICHERMOT", "HEROMOTOCO", "DIVISLAB", "BRITANNIA", "HINDALCO", "INDUSINDBK",
    "TATACONSUM", "APOLLOHOSP", "ADANIPORTS", "SBILIFE", "HDFCLIFE", "M&M", "DLF",
    "VEDL", "GAIL",
]

SIGNAL_MIN_SCORE = 0.52     # mirrors strategy_backtest.py entry threshold
TIME_STOP_BARS = 20
TRAIN_FRACTION = 0.6        # first 60% of trades (chronological) = train, rest = test
GATE_MIN_PF = 1.20          # keep a setup only if train profit factor clears this
GATE_MIN_TRADES = 12        # ...and it has enough train trades to be meaningful


# ----------------------------- data -----------------------------
def fetch_daily(symbol: str, rng: str) -> list[Candle]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        r = httpx.get(url, params={"range": rng, "interval": "1d"},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        d = r.json()["chart"]["result"][0]
        ts, q = d["timestamp"], d["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c):
                continue
            out.append(Candle(symbol, str(t), float(o), float(h), float(l), float(c), float(v or 0), "yahoo-1d"))
        return out
    except Exception as exc:
        print(f"  ! {symbol}: {exc.__class__.__name__}", file=sys.stderr)
        return []


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else None


# ----------------------------- simulation -----------------------------
def simulate_trade(candles: list[Candle], i: int, mode: str, cost_bps: float) -> dict[str, Any] | None:
    """Open at close[i], simulate forward. Returns one trade dict or None.

    Exit modes:
      fixed     - live authority style: -2.2% stop, +2.8% target (the dominant T1 booking).
      atr       - 1.5*ATR stop, 3*ATR target (2R), time stop.
      atr_trail - 1.5*ATR initial stop; at +1.5R move stop to breakeven, then chandelier
                  trail (high_since - 2.5*ATR). Lets winners run.
    """
    entry = candles[i].close
    if entry <= 0:
        return None
    a = atr(candles[: i + 1], 14) or entry * 0.025

    if mode == "fixed":
        risk = entry * 0.022
        stop = entry * (1.0 - 0.022)
        target = entry * (1.0 + 0.028)
        trail = False
    elif mode == "atr":
        risk = max(1.5 * a, entry * 0.005)
        stop = entry - risk
        target = entry + 2.0 * risk     # 2R
        trail = False
    elif mode == "atr_trail":
        risk = max(1.5 * a, entry * 0.005)
        stop = entry - risk
        target = None                   # no hard cap; trail instead
        trail = True
    else:
        raise ValueError(mode)

    be_trigger = entry + 1.5 * risk
    high_since = entry
    moved_be = False
    exit_price, reason = candles[-1].close, "time_stop"

    for j in range(i + 1, min(i + 1 + TIME_STOP_BARS, len(candles))):
        bar = candles[j]
        high_since = max(high_since, bar.high)
        # stop first (conservative for a long)
        if bar.low <= stop:
            exit_price, reason = stop, ("breakeven" if moved_be and stop >= entry else "stop")
            break
        if target is not None and bar.high >= target:
            exit_price, reason = target, "target"
            break
        if trail:
            if not moved_be and bar.high >= be_trigger:
                stop = max(stop, entry)
                moved_be = True
            chandelier = high_since - 2.5 * a
            if chandelier > stop:
                stop = chandelier
        if j == min(i + 1 + TIME_STOP_BARS, len(candles)) - 1:
            exit_price, reason = bar.close, "time_stop"

    gross_pct = (exit_price - entry) / entry * 100.0
    net_pct = gross_pct - cost_bps / 100.0
    r_multiple = (exit_price - entry) / risk if risk else 0.0
    return {
        "entry_index": i, "exit_reason": reason, "gross_pct": gross_pct,
        "net_pct": net_pct, "r_multiple": r_multiple, "hold": min(TIME_STOP_BARS, len(candles) - i - 1),
    }


def collect_entries(symbols_candles: dict[str, list[Candle]]) -> list[dict[str, Any]]:
    """Compute preset BUY entries ONCE (mode-independent). A given (symbol, setup) cannot
    re-enter within TIME_STOP_BARS bars of its last entry, so entries are identical across
    exit modes and the comparison isolates the exit logic."""
    entries = []
    for sym, candles in symbols_candles.items():
        if len(candles) < 120:
            continue
        last_entry: dict[str, int] = {}
        for i in range(50, len(candles) - 1):
            signals = evaluate_strategy_presets(candles[: i + 1], candles[i].close)
            for sig in signals:
                if sig.direction != "BUY" or sig.score < SIGNAL_MIN_SCORE:
                    continue
                if i - last_entry.get(sig.name, -10_000) < TIME_STOP_BARS:
                    continue
                entries.append({"symbol": sym, "setup": sig.name, "i": i, "ts": int(candles[i].ts)})
                last_entry[sig.name] = i
    entries.sort(key=lambda e: e["ts"])
    return entries


def _base_features(candles: list[Candle], i: int) -> dict[str, Any] | None:
    """Point-in-time base structure using bars strictly BEFORE i (no look-ahead)."""
    base = candles[max(0, i - 55):i]
    if len(base) < 30:
        return None
    highs = [c.high for c in base]
    lows = [c.low for c in base]
    vols = [c.volume for c in base]
    pivot = max(highs[-20:])
    base_low = min(lows[-20:])
    base_width = (pivot - base_low) / base_low * 100 if base_low else 999.0
    half = max(1, len(vols) // 2)
    early_vol = sum(vols[:half]) / half
    late_vol = sum(vols[-10:]) / min(10, len(vols))
    volume_dryup = bool(early_vol) and late_vol <= early_vol * 0.80
    thirds = [base[k * len(base) // 3:(k + 1) * len(base) // 3] for k in range(3)]
    rng = []
    for seg in thirds:
        if not seg:
            rng = []
            break
        lo = min(c.low for c in seg)
        rng.append((max(c.high for c in seg) - lo) / lo if lo else 0.0)
    progressive = len(rng) == 3 and rng[1] <= rng[0] * 0.9 and rng[2] <= rng[1] * 0.9
    avg_vol20 = sum(vols[-20:]) / min(20, len(vols))
    return {"pivot": pivot, "base_low": base_low, "base_width": base_width,
            "volume_dryup": volume_dryup, "progressive": progressive,
            "tight": base_width <= 28.0, "avg_vol20": avg_vol20}


def collect_concept_entries(symbols_candles: dict[str, list[Candle]], style: str) -> list[dict[str, Any]]:
    """Entry styles isolating WHEN you enter relative to the move:
       coil      = at the compression, before the breakout (anticipatory)
       breakout  = on the first close above the pivot (catch the start)
       chase     = after it has already run and sits near its high (today's behaviour)
    """
    entries = []
    for sym, candles in symbols_candles.items():
        if len(candles) < 120:
            continue
        last = -10_000
        for i in range(60, len(candles) - 1):
            if i - last < TIME_STOP_BARS:
                continue
            f = _base_features(candles, i)
            if not f:
                continue
            c, c_prev = candles[i].close, candles[i - 1].close
            day_gain = (c - c_prev) / c_prev * 100 if c_prev else 0.0
            day_high = candles[i].high
            hit = False
            if style == "coil":
                hit = (f["tight"] and (f["volume_dryup"] or f["progressive"])
                       and f["pivot"] * 0.96 <= c <= f["pivot"] * 1.005 and c >= c_prev)
            elif style == "breakout":
                hit = (c > f["pivot"] and c_prev <= f["pivot"]
                       and candles[i].volume > 1.3 * f["avg_vol20"])
            elif style == "chase":
                hit = (day_gain > 2.5 and day_high and c >= day_high * 0.98
                       and c > f["pivot"] * 1.03)
            if hit:
                entries.append({"symbol": sym, "setup": style, "i": i, "ts": int(candles[i].ts)})
                last = i
    entries.sort(key=lambda e: e["ts"])
    return entries


def trades_for_mode(entries: list[dict[str, Any]], symbols_candles: dict[str, list[Candle]],
                    mode: str, cost_bps: float) -> list[dict[str, Any]]:
    trades = []
    for e in entries:
        tr = simulate_trade(symbols_candles[e["symbol"]], e["i"], mode, cost_bps)
        if not tr:
            continue
        tr["symbol"] = e["symbol"]
        tr["setup"] = e["setup"]
        tr["ts"] = e["ts"]
        trades.append(tr)
    trades.sort(key=lambda t: t["ts"])
    return trades


# ----------------------------- metrics -----------------------------
def _bootstrap_ci(values: list[float], n: int = 2000) -> tuple[float, float]:
    if len(values) < 5:
        return (0.0, 0.0)
    means = []
    k = len(values)
    for _ in range(n):
        sample = [values[random.randrange(k)] for _ in range(k)]
        means.append(sum(sample) / k)
    means.sort()
    return (round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3))


def metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    net = [t["net_pct"] for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # equity curve (per-trade compounding-agnostic, additive %)
    eq, peak, max_dd = 0.0, 0.0, 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    mean = statistics.mean(net)
    std = statistics.pstdev(net) if len(net) > 1 else 0.0
    downside = statistics.pstdev([min(x, 0) for x in net]) if len(net) > 1 else 0.0
    ci = _bootstrap_ci(net)
    return {
        "n": len(trades),
        "win_pct": round(len(wins) / len(trades) * 100, 1),
        "exp_pct": round(mean, 3),
        "exp_ci95": ci,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "sharpe_per_trade": round(mean / std, 3) if std else 0.0,
        "sortino_per_trade": round(mean / downside, 3) if downside else 0.0,
        "avg_R": round(statistics.mean(t["r_multiple"] for t in trades), 3),
        "max_dd_pct": round(max_dd, 2),
        "total_net_pct": round(eq, 1),
        "edge_positive": mean > 0 and ci[0] > 0,   # lower CI bound above zero = real edge
    }


def per_setup_pf(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by[t["setup"]].append(t)
    out = {}
    for name, rows in by.items():
        net = [r["net_pct"] for r in rows]
        gw = sum(x for x in net if x > 0)
        gl = abs(sum(x for x in net if x <= 0))
        out[name] = {"n": len(rows), "pf": (gw / gl) if gl else float("inf"),
                     "exp": round(statistics.mean(net), 3)}
    return out


# ----------------------------- runner -----------------------------
def run_mode(entries, symbols_candles, mode, cost_bps, gate: bool):
    trades = trades_for_mode(entries, symbols_candles, mode, cost_bps)
    split = int(len(trades) * TRAIN_FRACTION)
    train, test = trades[:split], trades[split:]
    kept = None
    if gate:
        pf = per_setup_pf(train)
        kept = {name for name, s in pf.items() if s["pf"] >= GATE_MIN_PF and s["n"] >= GATE_MIN_TRADES}
        test = [t for t in test if t["setup"] in kept]
    return {"all": metrics(trades), "test": metrics(test), "kept_setups": sorted(kept) if kept is not None else None,
            "train_n": len(train)}


def fmt(m: dict[str, Any]) -> str:
    if not m.get("n"):
        return "no trades"
    return (f"n={m['n']:>5} win={m['win_pct']:>5}% exp={m['exp_pct']:>+6.3f}% "
            f"CI95={str(m['exp_ci95']):>16} PF={m['profit_factor']:>5} "
            f"Sharpe={m['sharpe_per_trade']:>6} Sortino={m['sortino_per_trade']:>6} "
            f"avgR={m['avg_R']:>+6.3f} maxDD={m['max_dd_pct']:>7}% "
            f"{'EDGE' if m['edge_positive'] else 'noedge'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="fixed", choices=["fixed", "atr", "atr_trail"])
    p.add_argument("--cost-bps", type=float, default=50.0)
    p.add_argument("--range", default="5y")
    p.add_argument("--gate", action="store_true")
    p.add_argument("--compare", action="store_true")
    p.add_argument("--ab", action="store_true", help="A/B entry timing: coil vs breakout vs chase")
    args = p.parse_args()

    print(f"Fetching {len(BASKET)} symbols ({args.range})...", file=sys.stderr)
    sc = {}
    for s in BASKET:
        c = fetch_daily(s, args.range)
        if len(c) >= 120:
            sc[s] = c
        time.sleep(0.12)
    total = sum(len(c) for c in sc.values())
    print(f"\n{'='*120}")
    print(f"RIGOROUS SIGNAL BACKTEST | {len(sc)} symbols | {total} candles | cost={args.cost_bps}bps | "
          f"train/test split={TRAIN_FRACTION:.0%} | gate: PF>={GATE_MIN_PF} & n>={GATE_MIN_TRADES}")
    print(f"{'='*120}")
    print("'EDGE' = lower bound of 95% bootstrap CI on per-trade expectancy is above zero (statistically real).")
    print("-" * 120)

    if args.ab:
        print("ENTRY-TIMING A/B (same ATR-trail exit, same costs, out-of-sample):")
        print("  coil     = enter at the compression, before the breakout (catch early)")
        print("  breakout = enter on first close above the pivot (catch the start)")
        print("  chase    = enter after it has already run, near its high (current behaviour)")
        print("-" * 120)
        for style in ["coil", "breakout", "chase"]:
            ents = collect_concept_entries(sc, style)
            trades = trades_for_mode(ents, sc, "atr_trail", args.cost_bps)
            split = int(len(trades) * TRAIN_FRACTION)
            print(f"\n### {style}  (entries={len(ents)})")
            print(f"  full window : {fmt(metrics(trades))}")
            print(f"  OUT-OF-SAMPLE: {fmt(metrics(trades[split:]))}")
        print("\n" + "-" * 120)
        print("Higher avg-R and PF with positive CI lower-bound = that entry timing has a real edge.")
        return

    entries = collect_entries(sc)   # computed once, reused across all exit modes
    print(f"entries (mode-independent) = {len(entries)}")

    modes = ["fixed", "atr", "atr_trail"] if args.compare else [args.mode]
    for mode in modes:
        for gate in ([False, True] if args.compare else [args.gate]):
            res = run_mode(entries, sc, mode, args.cost_bps, gate)
            tag = f"{mode}{'+gate' if gate else ''}"
            print(f"\n### {tag}")
            print(f"  full window : {fmt(res['all'])}")
            print(f"  OUT-OF-SAMPLE: {fmt(res['test'])}   (train n={res['train_n']})")
            if res["kept_setups"] is not None:
                print(f"  kept setups : {len(res['kept_setups'])} -> {', '.join(res['kept_setups'])}")
    print("\n" + "-" * 120)
    print("OUT-OF-SAMPLE row is the honest one: setups were selected on train data, measured on unseen test data.")


if __name__ == "__main__":
    main()
