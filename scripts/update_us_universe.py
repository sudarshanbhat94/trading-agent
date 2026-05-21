#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import urllib.request
from pathlib import Path
from typing import Iterable


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
FIELDNAMES = [
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
]
EXCHANGE_MAP = {
    "A": "AMEX",
    "N": "NYSE",
    "P": "ARCA",
    "Z": "BATS",
}
SKIP_NAME_TOKENS = (
    " warrant",
    " warrants",
    " right",
    " rights",
    " unit",
    " units",
    " preferred",
    " preference",
    " depositary",
    " note due",
    " notes due",
    " debenture",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a broad US OpenTrade universe from NASDAQ Trader files.")
    parser.add_argument("--output", default="data/us_universe.csv")
    parser.add_argument("--nasdaq-url", default=NASDAQ_LISTED_URL)
    parser.add_argument("--other-url", default=OTHER_LISTED_URL)
    args = parser.parse_args()

    nasdaq_text = _download_text(args.nasdaq_url)
    other_text = _download_text(args.other_url)
    rows = build_us_universe_rows(nasdaq_text, other_text)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} enabled US symbols to {output}")


def build_us_universe_rows(nasdaq_text: str, other_text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in _nasdaq_rows(nasdaq_text):
        _append_row(rows, seen, item)
    for item in _other_rows(other_text):
        _append_row(rows, seen, item)
    return sorted(rows, key=lambda row: (str(row["exchange"]), str(row["symbol"])))


def _nasdaq_rows(text: str) -> Iterable[dict[str, object]]:
    for row in _pipe_rows(text):
        symbol = _yahoo_symbol(row.get("Symbol", ""))
        name = str(row.get("Security Name") or symbol).strip()
        if str(row.get("Test Issue") or "").upper() == "Y":
            continue
        if str(row.get("Financial Status") or "N").upper() not in {"", "N"}:
            continue
        if not _is_tradeable_symbol(symbol, name):
            continue
        is_etf = str(row.get("ETF") or "").upper() == "Y"
        yield _row(symbol, name, "NASDAQ", is_etf)


def _other_rows(text: str) -> Iterable[dict[str, object]]:
    for row in _pipe_rows(text):
        symbol = _yahoo_symbol(row.get("ACT Symbol", ""))
        name = str(row.get("Security Name") or symbol).strip()
        exchange = EXCHANGE_MAP.get(str(row.get("Exchange") or "").upper())
        if not exchange:
            continue
        if str(row.get("Test Issue") or "").upper() == "Y":
            continue
        if not _is_tradeable_symbol(symbol, name):
            continue
        is_etf = str(row.get("ETF") or "").upper() == "Y"
        yield _row(symbol, name, exchange, is_etf)


def _append_row(rows: list[dict[str, object]], seen: set[str], row: dict[str, object]) -> None:
    symbol = str(row["symbol"])
    if symbol in seen:
        return
    seen.add(symbol)
    rows.append(row)


def _row(symbol: str, name: str, exchange: str, is_etf: bool) -> dict[str, object]:
    sector = "ETF" if is_etf else "US Equity"
    industry = "Exchange Traded Fund" if is_etf else "US Listed Equity"
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "yahoo_symbol": symbol,
        "kite_symbol": "",
        "upstox_instrument_key": "",
        "sector": sector,
        "industry": industry,
        "base_price": 100,
        "enabled": 1,
    }


def _pipe_rows(text: str) -> Iterable[dict[str, str]]:
    lines = [
        line
        for line in str(text or "").splitlines()
        if line.strip() and not line.startswith("File Creation Time")
    ]
    yield from csv.DictReader(lines, delimiter="|")


def _yahoo_symbol(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def _is_tradeable_symbol(symbol: str, name: str) -> bool:
    if not symbol or len(symbol) > 12:
        return False
    if any(part in symbol for part in ("$", "^", "/")):
        return False
    lower_name = f" {str(name or '').lower()}"
    return not any(token in lower_name for token in SKIP_NAME_TOKENS)


def _download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenTrade/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
