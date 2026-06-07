"""Read-only real-data backtest of the strategy-preset engine using public Yahoo daily candles.

No production access, no broker token. Fetches daily OHLCV from Yahoo's public chart API
for a basket of NSE symbols and drives app.strategy_backtest.strategy_backtest_snapshot,
then aggregates per-strategy performance across the basket with a realistic India cost in bps.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Candle
from app.strategy_backtest import strategy_backtest_snapshot

BASKET = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK",
    "LT", "ITC", "HINDUNILVR", "BHARTIARTL", "KOTAKBANK", "MARUTI", "TITAN",
    "SUNPHARMA", "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "BAJFINANCE",
    "HCLTECH", "ASIANPAINT", "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC",
    "COALINDIA", "JSWSTEEL", "GRASIM", "CIPLA",
]

# India round-trip cost estimate (bps of notional): brokerage+STT+exchange+GST+stamp+slippage.
COST_BPS = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
RANGE = sys.argv[2] if len(sys.argv) > 2 else "3y"


def fetch_daily(symbol: str) -> list[Candle]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    params = {"range": RANGE, "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()["chart"]["result"][0]
        ts = data["timestamp"]
        q = data["indicators"]["quote"][0]
        out: list[Candle] = []
        for i, t in enumerate(ts):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c):
                continue
            out.append(Candle(symbol, str(t), float(o), float(h), float(l), float(c), float(v or 0), "yahoo-1d"))
        return out
    except Exception as exc:
        print(f"  ! {symbol}: {exc.__class__.__name__}", file=sys.stderr)
        return []


def main() -> None:
    agg: dict[str, list[dict]] = defaultdict(list)
    total_candles = 0
    covered = 0
    for sym in BASKET:
        candles = fetch_daily(sym)
        if len(candles) < 120:
            continue
        covered += 1
        total_candles += len(candles)
        snap = strategy_backtest_snapshot(candles, execution_cost_bps=COST_BPS)
        for row in snap.get("strategy_backtests", []):
            if row.get("trades", 0) > 0:
                agg[row["strategy"]].append(row)
        time.sleep(0.2)

    print(f"\n{'='*92}")
    print(f"REAL-DATA PRESET BACKTEST | basket={covered}/{len(BASKET)} symbols | "
          f"~{total_candles} daily candles | range={RANGE} | cost={COST_BPS}bps round-trip")
    print(f"{'='*92}")
    print(f"{'strategy':32} {'trades':>7} {'win%':>7} {'exp%':>8} {'PF':>6} {'avgWin':>7} {'avgLoss':>8} {'maxDD%':>8}")
    print("-" * 92)

    summary = []
    for name, rows in agg.items():
        trades = sum(r["trades"] for r in rows)
        if trades == 0:
            continue
        # trade-weighted aggregation
        wr = sum(r["win_rate"] * r["trades"] for r in rows) / trades
        exp = sum(r["expectancy_pct"] * r["trades"] for r in rows) / trades
        avg_win = sum(r.get("avg_win_pct", 0) * r["trades"] for r in rows) / trades
        avg_loss = sum(r.get("avg_loss_pct", 0) * r["trades"] for r in rows) / trades
        gross_win = sum(r.get("avg_win_pct", 0) * r["win_rate"] * r["trades"] for r in rows)
        gross_loss = abs(sum(r.get("avg_loss_pct", 0) * (1 - r["win_rate"]) * r["trades"] for r in rows))
        pf = gross_win / gross_loss if gross_loss else float("inf")
        worst_dd = min(r.get("max_drawdown_pct", 0) for r in rows)
        summary.append((name, trades, wr, exp, pf, avg_win, avg_loss, worst_dd))

    summary.sort(key=lambda x: x[3], reverse=True)
    port_trades = sum(s[1] for s in summary)
    port_exp = sum(s[3] * s[1] for s in summary) / port_trades if port_trades else 0.0
    for name, trades, wr, exp, pf, aw, al, dd in summary:
        print(f"{name:32} {trades:>7} {wr*100:>6.1f} {exp:>8.3f} {pf:>6.2f} {aw:>7.2f} {al:>8.2f} {dd:>8.2f}")
    print("-" * 92)
    print(f"{'ALL PRESETS (trade-weighted)':32} {port_trades:>7} {'':>7} {port_exp:>8.3f}")
    print(f"\nNet expectancy is per-trade % after {COST_BPS}bps round-trip cost. "
          f"PF<1 or exp%<=0 means the preset loses money on this basket/window.")


if __name__ == "__main__":
    main()
