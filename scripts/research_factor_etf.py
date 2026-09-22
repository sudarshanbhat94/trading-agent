"""Frozen Nifty200 Momentum ETF test for a small delivery account.

The factor index supplies diversified stock momentum in one low-priced unit,
which avoids splitting a Rs 10,000 book into fee-heavy stock tickets.  Signals
use completed NIFTYBEES/MOMENTUM closes and fills occur at the next open.
This script is research only and has no production or broker imports.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pandas as pd

from app.costs import round_trip
from scripts.audit_market_prices import bars_from_response


SPEC = {
    "version": 1,
    "symbols": ["NIFTYBEES.NS", "MOMENTUM.NS"],
    "warmup_start": "2021-08-01",
    "development": ["2023-01-01", "2024-12-31"],
    "holdout": ["2025-01-01", "2026-09-22"],
    "capital": 10_000,
    "allocation": 0.50,
    "slippage_bps": [5, 20],
    "rules": ["buy_hold_factor", "factor_under_nifty_trend",
              "factor_and_nifty_trend"],
    "review": "first session of each month",
    "cost_model": "OpenStocks delivery round-trip schedule",
    "selection": "must be net-positive with drawdown <=10% in both windows at 20 bps",
}


def _desired(rule: str, factor: pd.DataFrame, nifty: pd.DataFrame, asof: str) -> bool:
    f = factor.loc[:asof, "close"]
    n = nifty.loc[:asof, "close"]
    if len(f) < 200 or len(n) < 200:
        return False
    if rule == "buy_hold_factor":
        return True
    nifty_on = float(n.iloc[-1]) > float(n.tail(200).mean())
    if rule == "factor_under_nifty_trend":
        return nifty_on
    return nifty_on and float(f.iloc[-1]) > float(f.tail(200).mean())


def replay(factor: pd.DataFrame, nifty: pd.DataFrame, rule: str,
           start: str, end: str, slip_bps: int) -> dict:
    capital = float(SPEC["capital"])
    cash, qty, entry = capital, 0, 0.0
    trades, curve = [], []
    slip = slip_bps / 10_000
    dates = [d for d in factor.index if start <= d <= end and d in nifty.index]
    for date_s in dates:
        i = factor.index.get_loc(date_s)
        if not isinstance(i, int) or i < 200:
            continue
        previous = pd.Timestamp(factor.index[i - 1])
        date = pd.Timestamp(date_s)
        review = (date.year, date.month) != (previous.year, previous.month)
        desired = _desired(rule, factor, nifty, factor.index[i - 1]) if review else None
        row = factor.loc[date_s]
        if desired is False and qty:
            price = float(row.open) * (1 - slip)
            fee = round_trip(qty * entry, qty * price)
            pnl = qty * (price - entry) - fee
            cash += qty * price - fee
            trades.append({"date": date_s, "pnl": pnl})
            qty = 0
        elif desired is True and not qty:
            price = float(row.open) * (1 + slip)
            qty = max(0, int(min(cash - 100, capital * SPEC["allocation"]) // price))
            if qty:
                entry = price
                cash -= qty * price
        value = qty * float(row.close)
        curve.append((date_s, cash + value - (round_trip(qty * entry, value) if qty else 0)))
    if qty and curve:
        price = float(factor.loc[curve[-1][0], "close"]) * (1 - slip)
        fee = round_trip(qty * entry, qty * price)
        pnl = qty * (price - entry) - fee
        cash += qty * price - fee
        trades.append({"date": curve[-1][0], "pnl": pnl, "research_close": True})
        curve[-1] = (curve[-1][0], cash)
    peak, drawdown = capital, 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {"return_pct": round((cash / capital - 1) * 100, 3),
            "max_drawdown_pct": round(drawdown * 100, 3),
            "trades": len(trades), "wins": sum(t["pnl"] > 0 for t in trades),
            "net_pnl": round(cash - capital, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--cached", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "frozen-spec.json"
    encoded = json.dumps(SPEC, sort_keys=True, indent=2)
    if manifest.exists() and manifest.read_text() != encoded:
        raise SystemExit("Frozen specification differs; choose another output directory")
    manifest.write_text(encoded)

    frames, sources = {}, {}
    for symbol in SPEC["symbols"]:
        path = args.out / f"{symbol}.json"
        if not args.cached:
            response = httpx.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
                params={"period1": int(pd.Timestamp(SPEC["warmup_start"], tz="UTC").timestamp()),
                        "period2": int(pd.Timestamp(args.end, tz="UTC").timestamp()),
                        "interval": "1d", "events": "splits,div"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            response.raise_for_status()
            path.write_text(json.dumps(response.json()))
        bars, splits = bars_from_response(json.loads(path.read_text()))
        frames[symbol] = pd.DataFrame.from_dict(bars, orient="index").sort_index()
        sources[symbol] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "bars": len(frames[symbol]), "first": frames[symbol].index[0],
                           "last": frames[symbol].index[-1], "splits": splits}
    factor, nifty = frames["MOMENTUM.NS"], frames["NIFTYBEES.NS"]
    results = {}
    for window in ("development", "holdout"):
        start, end = SPEC[window]
        results[window] = {rule: {str(s): replay(factor, nifty, rule, start, end, s)
                                  for s in SPEC["slippage_bps"]}
                           for rule in SPEC["rules"]}
    report = {"spec": SPEC, "sources": sources, "results": results,
              "promotion": "NOT AUTHORIZED BY THIS REPORT"}
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
