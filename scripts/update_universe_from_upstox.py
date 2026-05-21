#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.request
from pathlib import Path
from typing import Any


UPSTOX_INSTRUMENTS = {
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenTrade universe.csv from Upstox instrument masters.")
    parser.add_argument("--exchange", choices=["NSE", "BSE", "both"], default="NSE")
    parser.add_argument("--output", default="data/universe.csv")
    parser.add_argument("--include-bse", action="store_true", help="Shortcut for --exchange both")
    args = parser.parse_args()

    exchanges = ["NSE", "BSE"] if args.include_bse or args.exchange == "both" else [args.exchange]
    rows: list[dict[str, Any]] = []
    for exchange in exchanges:
        rows.extend(_rows_for_exchange(exchange))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "name",
                "exchange",
                "yahoo_symbol",
                "kite_symbol",
                "upstox_instrument_key",
                "sector",
                "industry",
                "base_price",
                "enabled",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} enabled equity instruments to {output}")


def _rows_for_exchange(exchange: str) -> list[dict[str, Any]]:
    payload = _download_json_gz(UPSTOX_INSTRUMENTS[exchange])
    rows: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for item in payload:
        if item.get("segment") != f"{exchange}_EQ":
            continue
        if str(item.get("instrument_type", "")).upper() not in {"EQ", "BE"}:
            continue
        trading_symbol = str(item.get("trading_symbol") or item.get("symbol") or "").strip()
        instrument_key = str(item.get("instrument_key") or "").strip()
        name = str(item.get("name") or item.get("short_name") or trading_symbol).strip()
        if not trading_symbol or not instrument_key or trading_symbol in seen_symbols:
            continue
        seen_symbols.add(trading_symbol)
        yahoo_suffix = "NS" if exchange == "NSE" else "BO"
        rows.append(
            {
                "symbol": trading_symbol,
                "name": name,
                "exchange": exchange,
                "yahoo_symbol": f"{trading_symbol}.{yahoo_suffix}",
                "kite_symbol": f"{exchange}:{trading_symbol}",
                "upstox_instrument_key": instrument_key,
                "sector": str(item.get("sector") or "").strip(),
                "industry": str(item.get("industry") or "").strip(),
                "base_price": _base_price(item),
                "enabled": 1,
            }
        )
    return sorted(rows, key=lambda row: (row["exchange"], row["symbol"]))


def _download_json_gz(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenTrade/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(gzip.decompress(raw).decode("utf-8"))


def _base_price(item: dict[str, Any]) -> float:
    for key in ("last_price", "close_price"):
        value = item.get(key)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return round(numeric, 2)
    return 100.0


if __name__ == "__main__":
    main()
