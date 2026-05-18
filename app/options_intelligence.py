from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .models import Quote, utc_now


class OptionsIntelligenceService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._symbol_cursor = 0

    async def context_for_cycle(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
    ) -> dict[str, Any]:
        if not self.settings.enable_options_intelligence:
            return {
                "enabled": False,
                "source": "nse_option_chain_stock_level",
                "updated_at": utc_now(),
                "symbols": {},
                "indices": {},
            }

        stock_rows = self._select_stock_rows(universe, quotes)
        index_symbols = self._index_symbols()
        async with httpx.AsyncClient(
            timeout=max(5, self.settings.free_feed_timeout_seconds),
            headers=self._headers(),
            follow_redirects=True,
        ) as client:
            await self._bootstrap_nse(client)
            stock_results, index_results = await asyncio.gather(
                self._fetch_many(client, stock_rows, quotes, is_index=False),
                self._fetch_indices(client, index_symbols),
            )

        context = {
            "enabled": True,
            "source": "nse_option_chain_stock_level",
            "index_source": "nse_option_chain_index_level",
            "updated_at": utc_now(),
            "symbols": stock_results,
            "indices": index_results,
            "scan": {
                "cycle_stock_symbols": len(stock_rows),
                "index_symbols": index_symbols,
                "cache_seconds": self.settings.options_cache_seconds,
                "max_pain_buy_suppress_pct": self.settings.options_max_pain_buy_suppress_pct,
            },
        }
        self.db.set_state("options_intelligence_context", context)
        self.db.insert_agent_log(
            "INFO",
            "options",
            "options_intelligence_refreshed",
            "Options intelligence refreshed",
            {
                "source": context["source"],
                "stock_symbols": len(stock_results),
                "index_symbols": len(index_results),
                "ok_stock_symbols": sum(1 for item in stock_results.values() if item.get("status") == "ok"),
                "ok_index_symbols": sum(1 for item in index_results.values() if item.get("status") == "ok"),
            },
        )
        return context

    def _select_stock_rows(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
    ) -> list[dict[str, Any]]:
        rows = [row for row in universe if row.get("symbol") in quotes and row.get("exchange", "NSE") == "NSE"]
        limit = max(0, int(self.settings.options_symbols_per_cycle or 0))
        if limit <= 0 or limit >= len(rows):
            return rows
        if not rows:
            return []
        start = self._symbol_cursor % len(rows)
        selected = [rows[(start + index) % len(rows)] for index in range(limit)]
        self._symbol_cursor = (start + limit) % len(rows)
        return selected

    async def _fetch_many(
        self,
        client: httpx.AsyncClient,
        rows: list[dict[str, Any]],
        quotes: dict[str, Quote],
        is_index: bool,
    ) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(3)

        async def fetch(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            symbol = str(row["symbol"]).strip().upper()
            async with semaphore:
                quote = quotes.get(symbol)
                current_price = float(quote.price) if quote else None
                return symbol, await self._fetch_symbol(client, symbol, current_price, is_index=is_index)

        output: dict[str, Any] = {}
        results = await asyncio.gather(*(fetch(row) for row in rows), return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                continue
            symbol, context = item
            output[symbol] = context
        return output

    async def _fetch_indices(self, client: httpx.AsyncClient, symbols: list[str]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for symbol in symbols:
            output[symbol] = await self._fetch_symbol(client, symbol, None, is_index=True)
        return output

    async def _fetch_symbol(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        current_price: float | None,
        is_index: bool,
    ) -> dict[str, Any]:
        cache_key = f"{'INDEX' if is_index else 'STOCK'}:{symbol}"
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self.settings.options_cache_seconds:
            return cached[1]
        endpoint = "option-chain-indices" if is_index else "option-chain-equities"
        try:
            response = await client.get(f"https://www.nseindia.com/api/{endpoint}", params={"symbol": symbol})
            response.raise_for_status()
            payload = response.json()
            records = payload.get("records") if isinstance(payload, dict) else {}
            rows = records.get("data") if isinstance(records, dict) else []
            if not rows:
                raise ValueError("empty option-chain payload")
            underlying = _float_or_none(records.get("underlyingValue")) or current_price
            context = _analyze_option_chain(
                symbol=symbol,
                rows=rows,
                current_price=underlying,
                source="nse_option_chain_index_level" if is_index else "nse_option_chain_stock_level",
                suppress_threshold=self.settings.options_max_pain_buy_suppress_pct,
            )
        except ValueError as exc:
            if not is_index:
                context = _not_fno_context(symbol, exc)
            else:
                context = {
                    "status": "unavailable",
                    "available": False,
                    "source": "nse_option_chain_index_level",
                    "audit_label": "nse_option_chain_index_level",
                    "symbol": symbol,
                    "updated_at": utc_now(),
                    "error": f"{exc.__class__.__name__}: {str(exc)[:220]}",
                    "data_gap": "index_option_chain_unavailable",
                }
        except Exception as exc:
            context = {
                "status": "unavailable" if is_index else "option_chain_unavailable",
                "available": False,
                "source": "nse_option_chain_index_level" if is_index else "nse_option_chain_stock_level",
                "audit_label": "nse_option_chain_index_level" if is_index else "nse_option_chain_stock_level_unavailable",
                "symbol": symbol,
                "updated_at": utc_now(),
                "error": f"{exc.__class__.__name__}: {str(exc)[:220]}",
                "data_gap": "option_chain_unavailable",
            }
        self._cache[cache_key] = (now, context)
        return context

    async def _bootstrap_nse(self, client: httpx.AsyncClient) -> None:
        for url in ("https://www.nseindia.com", "https://www.nseindia.com/option-chain"):
            try:
                await client.get(url)
            except Exception:
                continue

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain",
            "User-Agent": "Mozilla/5.0 OpenStocks/1.0 (+options-intelligence)",
        }

    def _index_symbols(self) -> list[str]:
        return [
            symbol.strip().upper()
            for symbol in self.settings.options_index_symbols.split(",")
            if symbol.strip()
        ]


def _analyze_option_chain(
    symbol: str,
    rows: list[dict[str, Any]],
    current_price: float | None,
    source: str,
    suppress_threshold: float,
) -> dict[str, Any]:
    strikes = []
    total_ce_oi = 0.0
    total_pe_oi = 0.0
    for row in rows:
        strike = _float_or_none(row.get("strikePrice"))
        if strike is None:
            continue
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        ce_oi = _float_or_none(ce.get("openInterest")) or 0.0
        pe_oi = _float_or_none(pe.get("openInterest")) or 0.0
        ce_chg_oi = _float_or_none(ce.get("changeinOpenInterest")) or 0.0
        pe_chg_oi = _float_or_none(pe.get("changeinOpenInterest")) or 0.0
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        strikes.append(
            {
                "strike": strike,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_change_oi": ce_chg_oi,
                "pe_change_oi": pe_chg_oi,
                "strike_pcr": round(pe_oi / ce_oi, 4) if ce_oi else None,
                "ce_buildup": _buildup(ce_oi, ce_chg_oi, _float_or_none(ce.get("change"))),
                "pe_buildup": _buildup(pe_oi, pe_chg_oi, _float_or_none(pe.get("change"))),
            }
        )
    if not strikes:
        raise ValueError("no strikes in option-chain payload")
    current = current_price or _infer_underlying_from_strikes(strikes)
    max_pain = _max_pain(strikes)
    max_pain_distance = ((max_pain - current) / current) * 100 if max_pain and current else None
    support = _concentration_zone(strikes, current, side="PE")
    resistance = _concentration_zone(strikes, current, side="CE")
    around_price = _near_price_strikes(strikes, current)
    buy_suppressed = max_pain_distance is not None and max_pain_distance <= suppress_threshold
    return {
        "status": "ok",
        "available": True,
        "source": source,
        "symbol": symbol,
        "updated_at": utc_now(),
        "underlying_price": round(current, 4) if current else None,
        "total_ce_oi": int(total_ce_oi),
        "total_pe_oi": int(total_pe_oi),
        "pcr_oi": round(total_pe_oi / total_ce_oi, 4) if total_ce_oi else None,
        "max_pain": round(max_pain, 4) if max_pain else None,
        "max_pain_distance_pct": round(max_pain_distance, 3) if max_pain_distance is not None else None,
        "buy_suppressed": buy_suppressed,
        "buy_suppression_reason": "max_pain_8pct_below_current_price" if buy_suppressed else None,
        "oi_concentration_zones": {
            "support": support,
            "resistance": resistance,
        },
        "strike_pcr": around_price,
        "top_oi_change": _top_oi_change(strikes),
        "audit_label": source,
    }


def _not_fno_context(symbol: str, exc: Exception) -> dict[str, Any]:
    return {
        "status": "not_fno_no_stock_options",
        "available": False,
        "source": "nse_equity_non_fno_no_stock_options",
        "audit_label": "nse_equity_non_fno_no_stock_options",
        "stock_option_status": "not_fno_no_stock_options",
        "symbol": symbol,
        "updated_at": utc_now(),
        "error": f"{exc.__class__.__name__}: {str(exc)[:220]}",
        "data_gap": None,
        "note": "No stock-level PCR, Max Pain, or OI concentration is available because this equity is not in NSE F&O.",
        "buy_suppressed": False,
        "max_pain": None,
        "max_pain_distance_pct": None,
        "oi_concentration_zones": {"support": [], "resistance": []},
    }


def _max_pain(strikes: list[dict[str, Any]]) -> float | None:
    best_strike = None
    best_pain = None
    for settlement in [item["strike"] for item in strikes]:
        pain = 0.0
        for item in strikes:
            strike = item["strike"]
            pain += item["ce_oi"] * max(0.0, settlement - strike)
            pain += item["pe_oi"] * max(0.0, strike - settlement)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = settlement
    return best_strike


def _concentration_zone(strikes: list[dict[str, Any]], current: float | None, side: str) -> list[dict[str, Any]]:
    key = "pe_oi" if side == "PE" else "ce_oi"
    if current:
        filtered = [
            item for item in strikes
            if item[key] > 0 and ((side == "PE" and item["strike"] <= current) or (side == "CE" and item["strike"] >= current))
        ]
    else:
        filtered = [item for item in strikes if item[key] > 0]
    ranked = sorted(filtered, key=lambda item: item[key], reverse=True)[:3]
    return [
        {
            "strike": round(item["strike"], 4),
            "open_interest": int(item[key]),
            "change_oi": int(item["pe_change_oi" if side == "PE" else "ce_change_oi"]),
            "buildup": item["pe_buildup" if side == "PE" else "ce_buildup"],
        }
        for item in ranked
    ]


def _near_price_strikes(strikes: list[dict[str, Any]], current: float | None) -> list[dict[str, Any]]:
    ranked = sorted(strikes, key=lambda item: abs(item["strike"] - (current or item["strike"])))[:9]
    return [
        {
            "strike": round(item["strike"], 4),
            "pcr": item["strike_pcr"],
            "ce_oi": int(item["ce_oi"]),
            "pe_oi": int(item["pe_oi"]),
            "ce_change_oi": int(item["ce_change_oi"]),
            "pe_change_oi": int(item["pe_change_oi"]),
            "ce_buildup": item["ce_buildup"],
            "pe_buildup": item["pe_buildup"],
        }
        for item in ranked
    ]


def _top_oi_change(strikes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def row(item: dict[str, Any], side: str) -> dict[str, Any]:
        return {
            "strike": round(item["strike"], 4),
            "change_oi": int(item["ce_change_oi" if side == "CE" else "pe_change_oi"]),
            "open_interest": int(item["ce_oi" if side == "CE" else "pe_oi"]),
            "buildup": item["ce_buildup" if side == "CE" else "pe_buildup"],
        }

    ce = sorted(strikes, key=lambda item: abs(item["ce_change_oi"]), reverse=True)[:5]
    pe = sorted(strikes, key=lambda item: abs(item["pe_change_oi"]), reverse=True)[:5]
    return {"CE": [row(item, "CE") for item in ce], "PE": [row(item, "PE") for item in pe]}


def _buildup(open_interest: float, change_oi: float, price_change: float | None) -> str:
    if open_interest <= 0 and change_oi == 0:
        return "no_activity"
    if change_oi > 0 and (price_change or 0) >= 0:
        return "long_buildup"
    if change_oi > 0 and (price_change or 0) < 0:
        return "short_buildup"
    if change_oi < 0 and (price_change or 0) >= 0:
        return "short_covering"
    if change_oi < 0 and (price_change or 0) < 0:
        return "long_unwinding"
    return "unchanged"


def _infer_underlying_from_strikes(strikes: list[dict[str, Any]]) -> float | None:
    ordered = sorted(item["strike"] for item in strikes)
    if not ordered:
        return None
    return ordered[len(ordered) // 2]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
