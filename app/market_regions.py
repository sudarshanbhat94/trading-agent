from __future__ import annotations

from typing import Any


INDIA_EXCHANGES = {"NSE", "BSE"}
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"}


def normalize_market_region(value: Any, default: str = "IN") -> str:
    region = str(value or default).strip().upper()
    return region if region in {"IN", "US", "BOTH"} else default


def exchange_for_row(row: dict[str, Any]) -> str:
    return str(row.get("exchange") or "").strip().upper()


def market_region_for_row(row: dict[str, Any]) -> str:
    exchange = exchange_for_row(row)
    if exchange in INDIA_EXCHANGES:
        return "IN"
    if exchange in US_EXCHANGES:
        return "US"
    yahoo_symbol = str(row.get("yahoo_symbol") or "").strip().upper()
    if yahoo_symbol.endswith(".NS") or yahoo_symbol.endswith(".BO"):
        return "IN"
    return "US" if exchange else "IN"


def row_matches_market_region(row: dict[str, Any], market_region: str | None) -> bool:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        return True
    return market_region_for_row(row) == region


def filter_universe_for_market(
    universe: list[dict[str, Any]],
    market_region: str | None,
) -> list[dict[str, Any]]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        return universe
    return [row for row in universe if market_region_for_row(row) == region]
