"""Frozen comparison of canonical Nifty/Bank ETF trend schedules.

This is research only.  It reads public vendor candles captured by
``research_index_baselines.py`` and never imports the production book, broker,
or order code.  Every decision uses completed data and fills at the next open.
The specification is written before the candle files are opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.costs import round_trip
from scripts.audit_market_prices import bars_from_response


SPEC = {
    "version": 1,
    "symbols": ["NIFTYBEES.NS", "BANKBEES.NS"],
    "development": ["2020-01-01", "2023-12-31"],
    "holdout": ["2024-01-01", "2026-09-22"],
    "capital": 10_000,
    "allocation": 0.50,
    "slippage_bps": [5, 20],
    "rules": [
        "daily_price_sma200",
        "daily_sma50_sma200",
        "weekly_price_sma200",
        "weekly_sma50_sma200",
        "monthly_price_sma200",
    ],
    "selection": (
        "A rule is viable only when net-positive and max drawdown <=10% in "
        "both development and holdout at 20 bps slippage. Prefer the lowest "
        "review frequency among viable rules unless a faster rule improves "
        "holdout return without increasing drawdown or trade count materially."
    ),
    "cost_model": "OpenStocks delivery round-trip schedule",
}


def _review(rule: str, date: pd.Timestamp, previous: pd.Timestamp) -> bool:
    if rule.startswith("daily_"):
        return True
    if rule.startswith("weekly_"):
        return date.isocalendar()[:2] != previous.isocalendar()[:2]
    return (date.year, date.month) != (previous.year, previous.month)


def _desired(rule: str, history: pd.DataFrame) -> bool:
    if len(history) < 200:
        return False
    close = history["close"]
    if rule.endswith("price_sma200"):
        return float(close.iloc[-1]) > float(close.tail(200).mean())
    return float(close.tail(50).mean()) > float(close.tail(200).mean())


def replay(frame: pd.DataFrame, rule: str, start: str, end: str, slip_bps: int) -> dict:
    capital = float(SPEC["capital"])
    cash, qty, entry = capital, 0, 0.0
    trades, curve = [], []
    slip = slip_bps / 10_000
    for i, (date_s, row) in enumerate(frame.iterrows()):
        date = pd.Timestamp(date_s)
        if date_s < start or date_s > end or i < 200:
            continue
        prior = pd.Timestamp(frame.index[i - 1])
        desired = _desired(rule, frame.iloc[:i]) if _review(rule, date, prior) else None
        if desired is False and qty:
            price = float(row.open) * (1 - slip)
            fee = round_trip(qty * entry, qty * price)
            pnl = qty * (price - entry) - fee
            cash += qty * price - fee
            trades.append({"entry": entry, "exit": price, "qty": qty,
                           "pnl": pnl, "date": date_s})
            qty = 0
        elif desired is True and not qty:
            price = float(row.open) * (1 + slip)
            qty = max(0, int(min(cash - 100, capital * SPEC["allocation"]) // price))
            if qty:
                entry = price
                cash -= qty * entry
        value = qty * float(row.close)
        exit_cost = round_trip(qty * entry, value) if qty else 0.0
        curve.append((date_s, cash + value - exit_cost))
    if qty and curve:
        price = float(frame.loc[curve[-1][0], "close"]) * (1 - slip)
        fee = round_trip(qty * entry, qty * price)
        pnl = qty * (price - entry) - fee
        cash += qty * price - fee
        trades.append({"entry": entry, "exit": price, "qty": qty,
                       "pnl": pnl, "date": curve[-1][0], "research_close": True})
        curve[-1] = (curve[-1][0], cash)
    peak, drawdown = capital, 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {
        "return_pct": round((cash / capital - 1) * 100, 3),
        "max_drawdown_pct": round(drawdown * 100, 3),
        "trades": len(trades),
        "wins": sum(t["pnl"] > 0 for t in trades),
        "net_pnl": round(cash - capital, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "frozen-spec.json"
    encoded = json.dumps(SPEC, sort_keys=True, indent=2)
    if manifest.exists() and manifest.read_text() != encoded:
        raise SystemExit("Frozen specification differs; choose another output directory")
    manifest.write_text(encoded)

    report = {"spec": SPEC, "sources": {}, "results": {}}
    for symbol in SPEC["symbols"]:
        source = args.source_dir / f"{symbol}.json"
        body = json.loads(source.read_text())
        bars, splits = bars_from_response(body)
        frame = pd.DataFrame.from_dict(bars, orient="index").sort_index()
        report["sources"][symbol] = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "bars": len(frame), "first": frame.index[0], "last": frame.index[-1],
            "splits": splits,
        }
        report["results"][symbol] = {}
        for window in ("development", "holdout"):
            start, end = SPEC[window]
            report["results"][symbol][window] = {
                rule: {str(slip): replay(frame, rule, start, end, slip)
                       for slip in SPEC["slippage_bps"]}
                for rule in SPEC["rules"]
            }
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    for symbol, windows in report["results"].items():
        for window, rules in windows.items():
            for rule, runs in rules.items():
                print(symbol, window, rule, runs)


if __name__ == "__main__":
    main()
