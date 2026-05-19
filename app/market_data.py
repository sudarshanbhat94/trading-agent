from __future__ import annotations

import asyncio
import csv
import io
import math
import random
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .market_regions import market_region_for_row, normalize_market_region
from .models import Candle, Quote, utc_now


class MarketDataError(RuntimeError):
    pass


def normalize_indstocks_access_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def normalize_upstox_access_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def normalize_market_data_provider_name(value: Any) -> str:
    provider = str(value or "upstox").strip().lower()
    if provider == "indstocks":
        provider = "upstox"
    elif provider == "indstocks_yahoo":
        provider = "upstox_yahoo"
    choices = {"simulated", "upstox", "upstox_yahoo", "kite", "kite_yahoo", "nubra", "yahoo"}
    return provider if provider in choices else "upstox"


def normalize_us_market_data_provider_name(value: Any) -> str:
    provider = str(value or "yahoo").strip().lower()
    choices = {"yahoo", "alpaca", "alpaca_yahoo", "polygon", "polygon_yahoo"}
    return provider if provider in choices else "yahoo"


class MarketDataProvider(ABC):
    source_name: str

    @abstractmethod
    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise NotImplementedError

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        return {}


class HistoricalCandleFallbackProvider(MarketDataProvider):
    def __init__(
        self,
        primary: MarketDataProvider,
        fallback: MarketDataProvider,
        min_candles: int = 3,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.min_candles = min_candles
        self.source_name = f"{primary.source_name}+{fallback.source_name}-fallback"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        primary_error: str | None = None
        fallback_error: str | None = None
        try:
            primary_quotes = await self.primary.get_quotes(universe)
        except Exception as exc:
            primary_quotes = {}
            primary_error = _market_data_error_summary(exc)
        missing_rows = [row for row in universe if row["symbol"] not in primary_quotes]
        if not missing_rows:
            return primary_quotes
        try:
            fallback_quotes = await self.fallback.get_quotes(missing_rows)
        except Exception as exc:
            fallback_quotes = {}
            fallback_error = _market_data_error_summary(exc)
        if not primary_quotes and not fallback_quotes and universe:
            raise MarketDataError(
                f"{self.source_name} returned no quotes. "
                f"primary={self.primary.source_name}: {primary_error or _provider_diagnostics(self.primary)}; "
                f"fallback={self.fallback.source_name}: {fallback_error or _provider_diagnostics(self.fallback)}"
            )
        return {**fallback_quotes, **primary_quotes}

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        try:
            primary_candles = await self.primary.get_candles(universe)
        except Exception:
            primary_candles = {}
        missing_rows = [
            row
            for row in universe
            if len(primary_candles.get(row["symbol"], [])) < self.min_candles
        ]
        if not missing_rows:
            return primary_candles

        try:
            fallback_candles = await self.fallback.get_candles(missing_rows)
        except Exception:
            fallback_candles = {}
        merged = dict(primary_candles)
        for row in missing_rows:
            symbol = row["symbol"]
            candles = fallback_candles.get(symbol, [])
            if len(candles) >= self.min_candles:
                merged[symbol] = candles
        return merged


class MarketRegionRoutingProvider(MarketDataProvider):
    def __init__(self, india_provider: MarketDataProvider, us_provider: MarketDataProvider) -> None:
        self.india_provider = india_provider
        self.us_provider = us_provider
        self.source_name = f"region-router:{india_provider.source_name}+{us_provider.source_name}"
        self.last_quote_diagnostics: dict[str, Any] = {}
        self.last_candle_diagnostics: dict[str, Any] = {}

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        groups = self._groups(universe)
        quotes: dict[str, Quote] = {}
        errors: dict[str, str] = {}
        for region, rows in groups.items():
            if not rows:
                continue
            provider = self.us_provider if region == "US" else self.india_provider
            try:
                quotes.update(await provider.get_quotes(rows))
            except Exception as exc:
                errors[region] = _market_data_error_summary(exc)
        self.last_quote_diagnostics = {
            "requested": len(universe),
            "returned": len(quotes),
            "groups": {region: len(rows) for region, rows in groups.items()},
            "group_errors": errors,
            "india_provider": _provider_diagnostics(self.india_provider),
            "us_provider": _provider_diagnostics(self.us_provider),
        }
        if universe and not quotes:
            raise MarketDataError(f"{self.source_name} returned no quotes; diagnostics={self.last_quote_diagnostics}")
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        groups = self._groups(universe)
        candles: dict[str, list[Candle]] = {}
        errors: dict[str, str] = {}
        for region, rows in groups.items():
            if not rows:
                continue
            provider = self.us_provider if region == "US" else self.india_provider
            try:
                candles.update(await provider.get_candles(rows))
            except Exception as exc:
                errors[region] = _market_data_error_summary(exc)
        self.last_candle_diagnostics = {
            "requested": len(universe),
            "returned": len(candles),
            "groups": {region: len(rows) for region, rows in groups.items()},
            "group_errors": errors,
        }
        return candles

    def _groups(self, universe: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        groups = {"IN": [], "US": []}
        for row in universe:
            groups["US" if market_region_for_row(row) == "US" else "IN"].append(row)
        return groups


class SimulatedMarketDataProvider(MarketDataProvider):
    source_name = "simulated"

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        now = utc_now()
        for row in universe:
            symbol = row["symbol"]
            base_price = float(row.get("base_price") or 100)
            previous = self._prices.get(symbol, base_price)
            noise = random.gauss(0, 0.003)
            drift = math.sin(datetime.now(timezone.utc).timestamp() / 1800 + len(symbol)) * 0.0008
            price = max(1.0, previous * (1 + drift + noise))
            self._prices[symbol] = price
            quotes[symbol] = Quote(
                symbol=symbol,
                price=round(price, 2),
                source=self.source_name,
                asof=now,
                open=round(base_price, 2),
                high=round(max(base_price, price) * 1.002, 2),
                low=round(min(base_price, price) * 0.998, 2),
                close=round(previous, 2),
                volume=random.randint(10_000, 3_000_000),
            )
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        now = datetime.now(timezone.utc)
        for row in universe:
            symbol = row["symbol"]
            current = self._prices.get(symbol, float(row.get("base_price") or 100))
            candles: list[Candle] = []
            price = current
            for index in range(36, 0, -1):
                close = max(1.0, price * (1 + random.gauss(0, 0.0025)))
                high = max(price, close) * (1 + abs(random.gauss(0, 0.0015)))
                low = min(price, close) * (1 - abs(random.gauss(0, 0.0015)))
                candles.append(
                    Candle(
                        symbol=symbol,
                        ts=(now - timedelta(minutes=15 * index)).isoformat(),
                        open=round(price, 2),
                        high=round(high, 2),
                        low=round(low, 2),
                        close=round(close, 2),
                        volume=random.randint(10_000, 1_000_000),
                        source=self.source_name,
                    )
                )
                price = close
            output[symbol] = candles
        return output


class YahooMarketDataProvider(MarketDataProvider):
    source_name = "yahoo-delayed"

    def __init__(self, settings: Settings) -> None:
        self.interval = settings.yahoo_candle_interval
        self.range = settings.yahoo_candle_range
        self.last_quote_diagnostics: dict[str, Any] = {}
        self._crumb: str | None = None
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 OpenStocks/1.0 (+paper-trading-dashboard)",
        }

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        symbols = [self._yahoo_symbol(row) for row in universe]
        by_yahoo = dict(zip(symbols, universe))
        quotes: dict[str, Quote] = {}
        diagnostics: dict[str, Any] = {
            "requested": len(universe),
            "chunks": 0,
            "chunk_errors": [],
            "chart_errors": 0,
            "crumb_refreshes": 0,
            "reference_data_enriched": 0,
        }
        async with httpx.AsyncClient(timeout=10, headers=self.headers, follow_redirects=True) as client:
            for i in range(0, len(symbols), 40):
                chunk = symbols[i : i + 40]
                diagnostics["chunks"] += 1
                try:
                    data = await self._fetch_quote_chunk(client, chunk, diagnostics)
                except Exception as exc:
                    diagnostics["chunk_errors"].append(_market_data_error_summary(exc))
                    continue
                for item in data.get("quoteResponse", {}).get("result", []):
                    yahoo_symbol = item.get("symbol")
                    row = by_yahoo.get(yahoo_symbol)
                    if row and self._apply_yahoo_reference_data(row, item):
                        diagnostics["reference_data_enriched"] += 1
                    price = item.get("regularMarketPrice")
                    if price is None:
                        price = item.get("postMarketPrice") or item.get("preMarketPrice")
                    if not row or price is None:
                        continue
                    symbol = row["symbol"]
                    quotes[symbol] = Quote(
                        symbol=symbol,
                        price=float(price),
                        source=self.source_name,
                        asof=self._epoch_to_iso(item.get("regularMarketTime")),
                        open=item.get("regularMarketOpen"),
                        high=item.get("regularMarketDayHigh"),
                        low=item.get("regularMarketDayLow"),
                        close=item.get("regularMarketPreviousClose"),
                        volume=item.get("regularMarketVolume"),
                    )
            missing = [row for row in universe if row["symbol"] not in quotes]
            fallback = await asyncio.gather(*(self._quote_from_chart(client, row) for row in missing), return_exceptions=True)
            for item in fallback:
                if isinstance(item, Quote):
                    quotes[item.symbol] = item
                elif isinstance(item, Exception):
                    diagnostics["chart_errors"] += 1
        diagnostics["returned"] = len(quotes)
        diagnostics["missing"] = max(len(universe) - len(quotes), 0)
        diagnostics["sample_symbols"] = symbols[:5]
        self.last_quote_diagnostics = diagnostics
        return quotes

    async def _fetch_quote_chunk(
        self,
        client: httpx.AsyncClient,
        symbols: list[str],
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        params = {"symbols": ",".join(symbols)}
        response = await client.get("https://query1.finance.yahoo.com/v7/finance/quote", params=params)
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return response.json()

        crumb = await self._ensure_crumb(client)
        if not crumb:
            response.raise_for_status()
        diagnostics["crumb_refreshes"] += 1
        response = await client.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={**params, "crumb": crumb},
        )
        if response.status_code in {401, 403}:
            self._crumb = None
        response.raise_for_status()
        return response.json()

    async def _ensure_crumb(self, client: httpx.AsyncClient) -> str | None:
        if self._crumb:
            return self._crumb
        crumb_headers = {"Accept": "*/*", "User-Agent": self.headers["User-Agent"]}
        try:
            # Yahoo sets the A3 cookie on fc.yahoo.com even though the page
            # returns 404; that cookie is required for the quote crumb.
            await client.get("https://fc.yahoo.com", headers=crumb_headers)
            response = await client.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=crumb_headers)
            response.raise_for_status()
            crumb = response.text.strip()
        except Exception:
            return None
        if not crumb or "<" in crumb or " " in crumb:
            return None
        self._crumb = crumb
        return crumb

    def _apply_yahoo_reference_data(self, row: dict[str, Any], item: dict[str, Any]) -> bool:
        if not self._is_us_row(row):
            return False
        updated = False
        numeric_fields = {
            "market_cap": "marketCap",
            "trailing_pe": "trailingPE",
            "forward_pe": "forwardPE",
            "price_to_book": "priceToBook",
            "eps_ttm": "epsTrailingTwelveMonths",
            "eps_forward": "epsForward",
            "fifty_two_week_high": "fiftyTwoWeekHigh",
            "fifty_two_week_low": "fiftyTwoWeekLow",
            "average_daily_volume_10d": "averageDailyVolume10Day",
        }
        for target, yahoo_key in numeric_fields.items():
            value = _float_any(item.get(yahoo_key))
            if value is None:
                continue
            row[target] = value
            updated = True
        if row.get("trailing_pe") is not None:
            row["pe"] = row["trailing_pe"]
        if row.get("price_to_book") is not None:
            row["pb"] = row["price_to_book"]

        quote_type = str(item.get("quoteType") or item.get("typeDisp") or "").strip().upper()
        if quote_type:
            row["yahoo_quote_type"] = quote_type
            row["security_type"] = "ETF" if quote_type == "ETF" else quote_type
        if item.get("currency"):
            row["currency"] = item.get("currency")
        if updated:
            row["fundamental_source"] = "yahoo_quote"
            row["fundamental_asof"] = self._epoch_to_iso(item.get("regularMarketTime"))
        return updated

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        semaphore = asyncio.Semaphore(6)
        async with httpx.AsyncClient(timeout=12, headers=self.headers, follow_redirects=True) as client:
            async def fetch(row: dict[str, Any]) -> tuple[str, list[Candle]]:
                async with semaphore:
                    return row["symbol"], await self._candles_from_chart(client, row)

            results = await asyncio.gather(*(fetch(row) for row in universe), return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                continue
            symbol, candles = item
            output[symbol] = candles
        return output

    async def _quote_from_chart(self, client: httpx.AsyncClient, row: dict[str, Any]) -> Quote | None:
        response = await client.get(self._chart_url(row), params={"range": "1d", "interval": "5m"})
        response.raise_for_status()
        result = self._chart_result(response.json())
        if not result:
            return None
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price is None:
            return None
        indicators = result.get("indicators", {}).get("quote", [{}])[0]
        self._apply_yahoo_chart_reference_data(row, meta)
        return Quote(
            symbol=row["symbol"],
            price=float(price),
            source=self.source_name,
            asof=self._epoch_to_iso(meta.get("regularMarketTime")),
            open=self._last_value(indicators.get("open")),
            high=self._last_value(indicators.get("high")),
            low=self._last_value(indicators.get("low")),
            close=meta.get("previousClose"),
            volume=self._last_value(indicators.get("volume")),
        )

    def _apply_yahoo_chart_reference_data(self, row: dict[str, Any], meta: dict[str, Any]) -> None:
        if not self._is_us_row(row):
            return
        for target, yahoo_key in (
            ("fifty_two_week_high", "fiftyTwoWeekHigh"),
            ("fifty_two_week_low", "fiftyTwoWeekLow"),
        ):
            value = _float_any(meta.get(yahoo_key))
            if value is not None:
                row[target] = value
        instrument_type = str(meta.get("instrumentType") or "").strip().upper()
        if instrument_type:
            row["yahoo_quote_type"] = instrument_type
            row["security_type"] = "ETF" if instrument_type == "ETF" else instrument_type
        if meta.get("currency"):
            row["currency"] = meta.get("currency")

    async def _candles_from_chart(self, client: httpx.AsyncClient, row: dict[str, Any]) -> list[Candle]:
        if self._is_us_row(row) and str(self.interval or "").lower() != "1d":
            daily = await self._candles_from_chart_params(client, row, "1y", "1d")
            if len(daily) >= 60:
                return daily[-320:]
        candles = await self._candles_from_chart_params(client, row, self.range, self.interval)
        if len(candles) < 60 and self._is_us_row(row) and str(self.interval or "").lower() != "1d":
            daily = await self._candles_from_chart_params(client, row, "1y", "1d")
            if len(daily) > len(candles):
                return daily[-320:]
        return candles[-self._candle_limit() :]

    async def _candles_from_chart_params(
        self,
        client: httpx.AsyncClient,
        row: dict[str, Any],
        range_value: str,
        interval_value: str,
    ) -> list[Candle]:
        try:
            response = await client.get(
                self._chart_url(row),
                params={"range": range_value, "interval": interval_value, "includePrePost": "false"},
            )
            response.raise_for_status()
            result = self._chart_result(response.json())
            if not result:
                return []
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            candles: list[Candle] = []
            for index, ts in enumerate(timestamps):
                values = [
                    self._value_at(opens, index),
                    self._value_at(highs, index),
                    self._value_at(lows, index),
                    self._value_at(closes, index),
                ]
                if any(value is None for value in values):
                    continue
                candles.append(
                    Candle(
                        symbol=row["symbol"],
                        ts=self._epoch_to_iso(ts),
                        open=float(values[0]),
                        high=float(values[1]),
                        low=float(values[2]),
                        close=float(values[3]),
                        volume=float(self._value_at(volumes, index) or 0),
                        source=self.source_name,
                    )
                )
            return candles
        except Exception:
            return []

    def _is_us_row(self, row: dict[str, Any]) -> bool:
        exchange = str(row.get("exchange") or "").strip().upper()
        return exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"}

    def _chart_url(self, row: dict[str, Any]) -> str:
        yahoo_symbol = self._yahoo_symbol(row)
        return f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(yahoo_symbol, safe='')}"

    def _yahoo_symbol(self, row: dict[str, Any]) -> str:
        explicit = str(row.get("yahoo_symbol") or "").strip()
        if explicit:
            return explicit
        exchange = str(row.get("exchange") or "").strip().upper()
        symbol = str(row["symbol"]).strip().upper()
        if exchange == "NSE":
            return f"{symbol}.NS"
        if exchange == "BSE":
            return f"{symbol}.BO"
        return symbol

    def _chart_result(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        results = payload.get("chart", {}).get("result") or []
        return results[0] if results else None

    def _epoch_to_iso(self, value: Any) -> str:
        if not value:
            return utc_now()
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except Exception:
            return utc_now()

    def _last_value(self, values: Any) -> float | None:
        if not isinstance(values, list):
            return None
        for value in reversed(values):
            if value is not None:
                return float(value)
        return None

    def _value_at(self, values: Any, index: int) -> float | None:
        if not isinstance(values, list) or index >= len(values):
            return None
        value = values[index]
        return float(value) if value is not None else None

    def _candle_limit(self) -> int:
        interval = str(self.interval or "").lower()
        if interval in {"1d", "1wk"}:
            return 320
        return 160


class AlpacaMarketDataProvider(MarketDataProvider):
    source_name = "alpaca-live"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.alpaca_api_key
        self.api_secret = settings.alpaca_api_secret
        self.base_url = settings.alpaca_data_base_url.rstrip("/")
        self.feed = settings.alpaca_data_feed or "iex"
        self.intraday_lookback_days = max(1, int(settings.us_intraday_candle_lookback_days or 5))
        self.daily_lookback_days = max(30, int(settings.us_daily_candle_lookback_days or 420))
        self.last_quote_diagnostics: dict[str, Any] = {}
        self.last_candle_diagnostics: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        if not self.api_key or not self.api_secret:
            raise MarketDataError("Alpaca provider needs ALPACA_API_KEY and ALPACA_API_SECRET")
        rows = [row for row in universe if market_region_for_row(row) == "US"]
        quotes: dict[str, Quote] = {}
        diagnostics = {"requested": len(rows), "chunks": 0, "errors": [], "missing_symbols": []}
        async with httpx.AsyncClient(timeout=10, headers=self._headers()) as client:
            for chunk in _chunks(rows, 100):
                diagnostics["chunks"] += 1
                symbols = [str(row["symbol"]).upper() for row in chunk]
                try:
                    response = await client.get(
                        f"{self.base_url}/v2/stocks/quotes/latest",
                        params={"symbols": ",".join(symbols), "feed": self.feed},
                    )
                    response.raise_for_status()
                    payload = response.json().get("quotes") or {}
                except Exception as exc:
                    diagnostics["errors"].append(_market_data_error_summary(exc))
                    continue
                for row in chunk:
                    symbol = str(row["symbol"]).upper()
                    item = payload.get(symbol) or {}
                    ask = _float_any(item.get("ap"))
                    bid = _float_any(item.get("bp"))
                    price = ((ask + bid) / 2.0) if ask and bid else ask or bid
                    if not price:
                        continue
                    quotes[symbol] = Quote(
                        symbol=symbol,
                        price=float(price),
                        source=self.source_name,
                        asof=str(item.get("t") or utc_now()),
                        open=None,
                        high=None,
                        low=None,
                        close=None,
                        volume=None,
                    )
        diagnostics["returned"] = len(quotes)
        diagnostics["missing_symbols"] = [row["symbol"] for row in rows if row["symbol"] not in quotes][:20]
        self.last_quote_diagnostics = diagnostics
        if rows and not quotes:
            raise MarketDataError(f"Alpaca returned no US quotes; diagnostics={diagnostics}")
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        if not self.api_key or not self.api_secret:
            raise MarketDataError("Alpaca provider needs ALPACA_API_KEY and ALPACA_API_SECRET")
        rows = [row for row in universe if market_region_for_row(row) == "US"]
        output: dict[str, list[Candle]] = {}
        diagnostics = {"requested": len(rows), "intervals": ["1Min", "1Day"], "errors": [], "completed_requests": 0}
        async with httpx.AsyncClient(timeout=18, headers=self._headers()) as client:
            for timeframe, lookback_days, source_suffix in (
                ("1Min", self.intraday_lookback_days, "1minute"),
                ("1Day", self.daily_lookback_days, "day"),
            ):
                for chunk in _chunks(rows, 50):
                    symbols = [str(row["symbol"]).upper() for row in chunk]
                    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
                    try:
                        response = await client.get(
                            f"{self.base_url}/v2/stocks/bars",
                            params={
                                "symbols": ",".join(symbols),
                                "timeframe": timeframe,
                                "start": start,
                                "limit": 10000,
                                "adjustment": "raw",
                                "feed": self.feed,
                            },
                        )
                        response.raise_for_status()
                        bars = response.json().get("bars") or {}
                    except Exception as exc:
                        diagnostics["errors"].append(_market_data_error_summary(exc))
                        continue
                    diagnostics["completed_requests"] += 1
                    for symbol, items in bars.items():
                        candles = [_alpaca_bar_to_candle(symbol, item, f"{self.source_name}:{source_suffix}") for item in items or []]
                        output.setdefault(symbol, []).extend([item for item in candles if item is not None])
        self.last_candle_diagnostics = {**diagnostics, "symbols_with_candles": len(output)}
        return output


class PolygonMarketDataProvider(MarketDataProvider):
    source_name = "polygon-live"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.polygon_api_key
        self.base_url = settings.polygon_base_url.rstrip("/")
        self.intraday_lookback_days = max(1, int(settings.us_intraday_candle_lookback_days or 5))
        self.daily_lookback_days = max(30, int(settings.us_daily_candle_lookback_days or 420))
        self.last_quote_diagnostics: dict[str, Any] = {}
        self.last_candle_diagnostics: dict[str, Any] = {}

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        if not self.api_key:
            raise MarketDataError("Polygon provider needs POLYGON_API_KEY")
        rows = [row for row in universe if market_region_for_row(row) == "US"]
        quotes: dict[str, Quote] = {}
        diagnostics = {"requested": len(rows), "chunks": 0, "errors": [], "missing_symbols": []}
        async with httpx.AsyncClient(timeout=10) as client:
            for chunk in _chunks(rows, 75):
                diagnostics["chunks"] += 1
                symbols = [str(row["symbol"]).upper() for row in chunk]
                try:
                    response = await client.get(
                        f"{self.base_url}/v2/snapshot/locale/us/markets/stocks/tickers",
                        params={"tickers": ",".join(symbols), "apiKey": self.api_key},
                    )
                    response.raise_for_status()
                    items = response.json().get("tickers") or []
                except Exception as exc:
                    diagnostics["errors"].append(_market_data_error_summary(exc))
                    continue
                for item in items:
                    symbol = str(item.get("ticker") or "").upper()
                    day = item.get("day") or {}
                    prev = item.get("prevDay") or {}
                    trade = item.get("lastTrade") or {}
                    price = _float_any(trade.get("p")) or _float_any(day.get("c")) or _float_any(prev.get("c"))
                    if not symbol or not price:
                        continue
                    quotes[symbol] = Quote(
                        symbol=symbol,
                        price=float(price),
                        source=self.source_name,
                        asof=_polygon_ts_to_iso(trade.get("t") or item.get("updated")),
                        open=_float_any(day.get("o")),
                        high=_float_any(day.get("h")),
                        low=_float_any(day.get("l")),
                        close=_float_any(prev.get("c")),
                        volume=_float_any(day.get("v")),
                    )
        diagnostics["returned"] = len(quotes)
        diagnostics["missing_symbols"] = [row["symbol"] for row in rows if row["symbol"] not in quotes][:20]
        self.last_quote_diagnostics = diagnostics
        if rows and not quotes:
            raise MarketDataError(f"Polygon returned no US quotes; diagnostics={diagnostics}")
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        if not self.api_key:
            raise MarketDataError("Polygon provider needs POLYGON_API_KEY")
        rows = [row for row in universe if market_region_for_row(row) == "US"]
        output: dict[str, list[Candle]] = {}
        diagnostics = {"requested": len(rows), "intervals": ["minute", "day"], "errors": [], "completed_requests": 0}
        semaphore = asyncio.Semaphore(6)
        async with httpx.AsyncClient(timeout=18) as client:
            tasks = [
                asyncio.create_task(self._fetch_aggs(client, semaphore, row, "minute", self.intraday_lookback_days, "1minute"))
                for row in rows
            ] + [
                asyncio.create_task(self._fetch_aggs(client, semaphore, row, "day", self.daily_lookback_days, "day"))
                for row in rows
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                diagnostics["errors"].append(_market_data_error_summary(item))
                continue
            if not item:
                continue
            symbol, candles = item
            diagnostics["completed_requests"] += 1
            output.setdefault(symbol, []).extend(candles)
        self.last_candle_diagnostics = {**diagnostics, "symbols_with_candles": len(output)}
        return output

    async def _fetch_aggs(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        row: dict[str, Any],
        span: str,
        lookback_days: int,
        source_suffix: str,
    ) -> tuple[str, list[Candle]] | None:
        async with semaphore:
            symbol = str(row["symbol"]).upper()
            end = date.today()
            start = end - timedelta(days=lookback_days)
            response = await client.get(
                f"{self.base_url}/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/{span}/{start.isoformat()}/{end.isoformat()}",
                params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self.api_key},
            )
            response.raise_for_status()
            rows = response.json().get("results") or []
            candles = [_polygon_bar_to_candle(symbol, item, f"{self.source_name}:{source_suffix}") for item in rows]
            return symbol, [item for item in candles if item is not None]


class AlpacaSetupRequiredProvider(MarketDataProvider):
    source_name = "alpaca-not-connected"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise MarketDataError("Alpaca API key/secret are not configured for US trade-decision data.")

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        raise MarketDataError("Alpaca API key/secret are not configured for US minute/daily candles.")


class PolygonSetupRequiredProvider(MarketDataProvider):
    source_name = "polygon-not-connected"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise MarketDataError("Polygon API key is not configured for US trade-decision data.")

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        raise MarketDataError("Polygon API key is not configured for US minute/daily candles.")


class IndStocksMarketDataProvider(MarketDataProvider):
    source_name = "indstocks-live"
    quote_chunk_size = 10
    quote_chunk_spacing_seconds = 0.35
    quote_rate_limit_backoff_seconds = 1.0

    def __init__(self, settings: Settings) -> None:
        self.access_token = normalize_indstocks_access_token(settings.indstocks_access_token)
        self.base_url = settings.indstocks_api_base_url.rstrip("/")
        self.interval = settings.indstocks_candle_interval
        self.lookback_days = max(1, int(settings.indstocks_candle_lookback_days or 365))
        self.candle_concurrency = max(1, int(settings.indstocks_candle_concurrency or 8))
        self.candle_request_spacing_seconds = max(
            0.0,
            float(getattr(settings, "indstocks_candle_request_spacing_ms", 450) or 0) / 1000.0,
        )
        self.candle_retry_attempts = max(1, int(getattr(settings, "indstocks_candle_retry_attempts", 4) or 4))
        self.candle_retry_backoff_seconds = max(
            0.1,
            float(getattr(settings, "indstocks_candle_retry_backoff_seconds", 1.0) or 1.0),
        )
        self.timeout_seconds = max(5, int(settings.indstocks_fetch_timeout_seconds or 20))
        self.last_quote_diagnostics: dict[str, Any] = {}
        self.last_candle_diagnostics: dict[str, Any] = {}
        self.last_resolver_diagnostics: dict[str, Any] = {}
        self._instrument_cache: tuple[float, dict[str, dict[str, str]]] | None = None
        self._candle_rate_lock = asyncio.Lock()
        self._last_candle_request_at = 0.0
        self._last_rate_limit_count = 0
        if not self.access_token:
            raise MarketDataError("INDstocks provider needs INDSTOCKS_ACCESS_TOKEN")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=self._headers(), follow_redirects=True) as client:
            resolved = await self._resolve_rows(client, universe)
            quotes: dict[str, Quote] = {}
            errors: list[str] = []
            for chunk_index, chunk in enumerate(_chunks(resolved, self.quote_chunk_size)):
                if chunk_index:
                    await asyncio.sleep(self.quote_chunk_spacing_seconds)
                data = await self._quote_data_for_chunk(client, chunk, errors)
                if len(data) < len(chunk):
                    missing_items = [item for item in chunk if item["scrip_code"] not in data]
                    for item in missing_items:
                        await asyncio.sleep(self.quote_chunk_spacing_seconds)
                        data.update(await self._quote_data_for_chunk(client, [item], errors))
                for item in chunk:
                    quote_data = data.get(item["scrip_code"]) or {}
                    price = _float_any(quote_data.get("live_price"))
                    if price is None:
                        continue
                    row = item["row"]
                    quotes[row["symbol"]] = Quote(
                        symbol=row["symbol"],
                        price=price,
                        source=self.source_name,
                        asof=utc_now(),
                        open=_float_any(quote_data.get("day_open")),
                        high=_float_any(quote_data.get("day_high")),
                        low=_float_any(quote_data.get("day_low")),
                        close=_float_any(quote_data.get("prev_close")),
                        volume=_float_any(quote_data.get("volume")),
                    )
            self.last_quote_diagnostics = {
                "requested": len(universe),
                "resolved": len(resolved),
                "returned": len(quotes),
                "missing_symbols": [row["symbol"] for row in universe if row["symbol"] not in quotes][:20],
                "errors": _unique_errors(errors)[:5],
                "source": "indstocks_market_quotes_full",
                "resolver": self.last_resolver_diagnostics,
            }
            if universe and not quotes:
                raise MarketDataError(f"INDstocks returned no quotes; diagnostics={self.last_quote_diagnostics}")
            return quotes

    async def _quote_data_for_chunk(
        self,
        client: httpx.AsyncClient,
        chunk: list[dict[str, Any]],
        errors: list[str],
        attempt: int = 1,
    ) -> dict[str, Any]:
        if not chunk:
            return {}
        try:
            response = await client.get(
                f"{self.base_url}/market/quotes/full",
                params={"scrip-codes": ",".join(item["scrip_code"] for item in chunk)},
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            return data if isinstance(data, dict) else {}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and attempt < 3:
                await asyncio.sleep(self.quote_rate_limit_backoff_seconds * attempt)
                return await self._quote_data_for_chunk(client, chunk, errors, attempt=attempt + 1)
            if exc.response.status_code == 429 and len(chunk) > 1:
                midpoint = max(1, len(chunk) // 2)
                await asyncio.sleep(self.quote_chunk_spacing_seconds)
                left = await self._quote_data_for_chunk(client, chunk[:midpoint], errors)
                await asyncio.sleep(self.quote_chunk_spacing_seconds)
                right = await self._quote_data_for_chunk(client, chunk[midpoint:], errors)
                return {**left, **right}
            errors.append(_market_data_error_summary(exc))
        except Exception as exc:
            errors.append(_market_data_error_summary(exc))
        return {}

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        failures: list[str] = []
        self._last_rate_limit_count = 0
        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=self._headers(), follow_redirects=True) as client:
            resolved = await self._resolve_rows(client, universe)
            semaphore = asyncio.Semaphore(self.candle_concurrency)

            async def fetch(item: dict[str, Any]) -> tuple[str, list[Candle]]:
                async with semaphore:
                    return item["row"]["symbol"], await self._candles_for_scrip(client, item["row"], item["scrip_code"])

            results = await asyncio.gather(*(fetch(item) for item in resolved), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failures.append(_market_data_error_summary(result))
                continue
            symbol, candles = result
            if candles:
                output[symbol] = candles
        self.last_candle_diagnostics = {
            "requested": len(universe),
            "resolved": len(resolved),
            "symbols_with_candles": len(output),
            "failures": len(failures),
            "sample_errors": _unique_errors(failures)[:5],
            "interval": self.interval,
            "concurrency": self.candle_concurrency,
            "request_spacing_ms": round(self.candle_request_spacing_seconds * 1000),
            "retry_attempts": self.candle_retry_attempts,
            "rate_limit_retries": self._last_rate_limit_count,
            "resolver": self.last_resolver_diagnostics,
        }
        return output

    async def _candles_for_scrip(self, client: httpx.AsyncClient, row: dict[str, Any], scrip_code: str) -> list[Candle]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self._effective_lookback_days())
        response = await self._get_candles_with_retry(
            client=client,
            scrip_code=scrip_code,
            start_ms=int(start.timestamp() * 1000),
            end_ms=int(end.timestamp() * 1000),
        )
        data = response.json().get("data") or {}
        symbol_payload = data.get(scrip_code) if isinstance(data, dict) else None
        raw_candles = (
            (symbol_payload or {}).get("candles")
            if isinstance(symbol_payload, dict)
            else None
        ) or (data.get("candles") if isinstance(data, dict) else None) or []
        candles: list[Candle] = []
        for candle in raw_candles:
            parsed = self._parse_candle(row["symbol"], candle)
            if parsed:
                candles.append(parsed)
        return candles[-320:]

    async def _get_candles_with_retry(
        self,
        client: httpx.AsyncClient,
        scrip_code: str,
        start_ms: int,
        end_ms: int,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.candle_retry_attempts + 1):
            await self._pace_candle_request()
            try:
                response = await client.get(
                    f"{self.base_url}/market/historical/{self.interval}",
                    params={
                        "scrip-codes": scrip_code,
                        "start_time": start_ms,
                        "end_time": end_ms,
                    },
                )
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code != 429 or attempt >= self.candle_retry_attempts:
                    raise
                self._last_rate_limit_count += 1
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self.candle_retry_attempts:
                    raise
            delay = self.candle_retry_backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            await asyncio.sleep(delay)
        if last_exc:
            raise last_exc
        raise MarketDataError(f"INDstocks historical candles failed for {scrip_code}")

    async def _pace_candle_request(self) -> None:
        if self.candle_request_spacing_seconds <= 0:
            return
        async with self._candle_rate_lock:
            now = time.monotonic()
            wait_for = self.candle_request_spacing_seconds - (now - self._last_candle_request_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_candle_request_at = time.monotonic()

    async def _resolve_rows(self, client: httpx.AsyncClient, universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        instruments: dict[str, dict[str, str]] | None = None
        resolved: list[dict[str, Any]] = []
        for row in universe:
            scrip_code = self._explicit_scrip_code(row)
            if not scrip_code:
                if instruments is None:
                    instruments = await self._instrument_index(client)
                instrument = instruments.get(self._instrument_key(row))
                if instrument:
                    scrip_code = instrument["scrip_code"]
            if not scrip_code:
                continue
            resolved.append({"row": row, "scrip_code": scrip_code})
        return resolved

    async def _instrument_index(self, client: httpx.AsyncClient) -> dict[str, dict[str, str]]:
        cached = self._instrument_cache
        if cached and time.monotonic() - cached[0] < 86400:
            return cached[1]
        try:
            response = await client.get(f"{self.base_url}/market/instruments", params={"source": "equity"})
            response.raise_for_status()
            index = self._parse_indstocks_instruments_csv(response.text)
            self.last_resolver_diagnostics = {
                "source": "indstocks_market_instruments",
                "instrument_count": len(index),
            }
            self._instrument_cache = (time.monotonic(), index)
            return index
        except Exception as primary_exc:
            try:
                index = await self._public_kite_instrument_index()
            except Exception as fallback_exc:
                self.last_resolver_diagnostics = {
                    "source": "unavailable",
                    "indstocks_error": _market_data_error_summary(primary_exc),
                    "public_fallback_error": _market_data_error_summary(fallback_exc),
                }
                raise MarketDataError(
                    "Could not resolve INDstocks scrip codes. "
                    f"INDstocks instruments failed: {_market_data_error_summary(primary_exc)}; "
                    f"public NSE/BSE resolver failed: {_market_data_error_summary(fallback_exc)}"
                ) from fallback_exc
            self.last_resolver_diagnostics = {
                "source": "kite_public_instruments_fallback",
                "instrument_count": len(index),
                "fallback_reason": _market_data_error_summary(primary_exc),
            }
            self._instrument_cache = (time.monotonic(), index)
            return index

    def _parse_indstocks_instruments_csv(self, text: str) -> dict[str, dict[str, str]]:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        index: dict[str, dict[str, str]] = {}
        for raw in reader:
            row = {str(key or "").strip().upper(): str(value or "").strip() for key, value in raw.items()}
            exchange = row.get("EXCH", "").upper()
            security_id = row.get("SECURITY_ID", "")
            if not exchange or not security_id:
                continue
            scrip_code = f"{exchange}_{security_id}"
            for symbol in {
                row.get("TRADING_SYMBOL", ""),
                row.get("SYMBOL_NAME", ""),
                row.get("CUSTOM_SYMBOL", ""),
            }:
                normalized = _normalize_trade_symbol(symbol)
                if normalized:
                    index[f"{exchange}:{normalized}"] = {"scrip_code": scrip_code, "security_id": security_id}
        return index

    async def _public_kite_instrument_index(self) -> dict[str, dict[str, str]]:
        # The INDstocks instruments endpoint may be blocked for some direct
        # tokens even when quote APIs work. Kite's public instrument dump gives
        # NSE/BSE exchange tokens, which match the INDstocks NSE_<token> /
        # BSE_<token> scrip-code shape used by quote and history endpoints.
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as public_client:
            response = await public_client.get("https://api.kite.trade/instruments")
            response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
        index: dict[str, dict[str, str]] = {}
        for raw in reader:
            row = {str(key or "").strip().upper(): str(value or "").strip() for key, value in raw.items()}
            exchange = row.get("EXCHANGE", "").upper()
            if exchange not in {"NSE", "BSE"}:
                continue
            if row.get("INSTRUMENT_TYPE", "").upper() != "EQ":
                continue
            exchange_token = row.get("EXCHANGE_TOKEN", "")
            if not exchange_token:
                continue
            scrip_code = f"{exchange}_{exchange_token}"
            for symbol in {
                row.get("TRADINGSYMBOL", ""),
                row.get("NAME", ""),
            }:
                normalized = _normalize_trade_symbol(symbol)
                if normalized:
                    index[f"{exchange}:{normalized}"] = {"scrip_code": scrip_code, "security_id": exchange_token}
        return index

    def _instrument_key(self, row: dict[str, Any]) -> str:
        exchange = str(row.get("exchange") or "NSE").strip().upper()
        return f"{exchange}:{_normalize_trade_symbol(str(row.get('symbol') or ''))}"

    def _explicit_scrip_code(self, row: dict[str, Any]) -> str:
        explicit = str(row.get("indstocks_scrip_code") or row.get("scrip_code") or "").strip().upper()
        if explicit:
            return explicit
        security_id = str(row.get("indstocks_security_id") or row.get("security_id") or "").strip()
        exchange = str(row.get("exchange") or "NSE").strip().upper()
        return f"{exchange}_{security_id}" if security_id and exchange else ""

    def _parse_candle(self, symbol: str, candle: Any) -> Candle | None:
        try:
            if isinstance(candle, dict):
                ts_raw = candle.get("ts") or candle.get("time") or candle.get("timestamp")
                timestamp = float(ts_raw)
                if timestamp > 10_000_000_000:
                    timestamp = timestamp / 1000.0
                return Candle(
                    symbol=symbol,
                    ts=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                    open=float(candle.get("o") if candle.get("o") is not None else candle.get("open")),
                    high=float(candle.get("h") if candle.get("h") is not None else candle.get("high")),
                    low=float(candle.get("l") if candle.get("l") is not None else candle.get("low")),
                    close=float(candle.get("c") if candle.get("c") is not None else candle.get("close")),
                    volume=float((candle.get("v") if candle.get("v") is not None else candle.get("volume")) or 0),
                    source=f"{self.source_name}:{self.interval}",
                )
            if not isinstance(candle, list) or len(candle) < 6:
                return None
            timestamp = float(candle[0])
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000.0
            return Candle(
                symbol=symbol,
                ts=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                open=float(candle[1]),
                high=float(candle[2]),
                low=float(candle[3]),
                close=float(candle[4]),
                volume=float(candle[5] or 0),
                source=f"{self.source_name}:{self.interval}",
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _effective_lookback_days(self) -> int:
        interval = str(self.interval or "").lower()
        if interval.endswith("second"):
            return 1
        if interval in {"1minute", "2minute", "3minute", "4minute", "5minute", "10minute", "15minute", "30minute"}:
            return min(self.lookback_days, 7)
        if interval in {"60minute", "120minute", "180minute", "240minute"}:
            return min(self.lookback_days, 14)
        return min(self.lookback_days, 365)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json,text/csv,*/*",
            "Authorization": self.access_token,
        }


class KiteMarketDataProvider(MarketDataProvider):
    source_name = "kite-live"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.kite_api_key
        self.access_token = settings.kite_access_token
        if not self.api_key or not self.access_token:
            raise MarketDataError("Kite provider needs KITE_API_KEY and KITE_ACCESS_TOKEN")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        instruments: list[tuple[str, dict[str, Any]]] = []
        for row in universe:
            instrument = row.get("kite_symbol") or f"{row.get('exchange', 'NSE')}:{row['symbol']}"
            instruments.append((instrument, row))

        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}",
        }
        quotes: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            for i in range(0, len(instruments), 200):
                chunk = instruments[i : i + 200]
                response = await client.get(
                    "https://api.kite.trade/quote",
                    params=[("i", instrument) for instrument, _ in chunk],
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                for instrument, row in chunk:
                    item = data.get(instrument)
                    if not item:
                        continue
                    price = item.get("last_price")
                    if price is None:
                        continue
                    ohlc = item.get("ohlc") or {}
                    quotes[row["symbol"]] = Quote(
                        symbol=row["symbol"],
                        price=float(price),
                        source=self.source_name,
                        asof=utc_now(),
                        open=ohlc.get("open"),
                        high=ohlc.get("high"),
                        low=ohlc.get("low"),
                        close=ohlc.get("close"),
                        volume=item.get("volume"),
                    )
        return quotes


class UpstoxMarketDataProvider(MarketDataProvider):
    source_name = "upstox-live"
    _instrument_key_cache: dict[str, str] = {}

    def __init__(self, settings: Settings) -> None:
        self.access_token = normalize_upstox_access_token(settings.upstox_access_token)
        self.base_url = settings.upstox_api_base_url
        self.interval = settings.upstox_candle_interval
        self.lookback_days = settings.upstox_candle_lookback_days
        self.multi_timeframe = settings.enable_upstox_multi_timeframe_candles
        self.daily_lookback_days = settings.upstox_daily_candle_lookback_days
        self.weekly_lookback_days = settings.upstox_weekly_candle_lookback_days
        self.candle_concurrency = max(1, int(settings.upstox_candle_concurrency or 10))
        self.candle_fetch_timeout_seconds = max(5, int(settings.upstox_candle_fetch_timeout_seconds or 35))
        self.last_quote_diagnostics: dict[str, Any] = {}
        self.last_candle_diagnostics: dict[str, Any] = {}
        if not self.access_token:
            raise MarketDataError("Upstox provider needs UPSTOX_ACCESS_TOKEN")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        requested = len(universe)
        quotes: dict[str, Quote] = {}
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=10, headers=self._headers()) as client:
            resolution = await self._ensure_instrument_keys(client, universe)
            missing_key_symbols = [row["symbol"] for row in universe if not row.get("upstox_instrument_key")]
            universe = [row for row in universe if row.get("upstox_instrument_key")]
            errors.extend(resolution["errors"])
            for i in range(0, len(universe), 100):
                chunk = universe[i : i + 100]
                try:
                    response = await client.get(
                        f"{self.base_url}/market-quote/quotes",
                        params={"instrument_key": ",".join(self._instrument_key(row) for row in chunk)},
                    )
                    response.raise_for_status()
                    data = response.json().get("data", {})
                except Exception as exc:
                    errors.append(_market_data_error_summary(exc))
                    continue
                for row in chunk:
                    item = self._find_quote_item(data, row)
                    if not item:
                        continue
                    price = item.get("last_price") or item.get("ltp")
                    if price is None:
                        continue
                    ohlc = item.get("ohlc") or {}
                    asof = _upstox_quote_asof(item)
                    quote_source = self.source_name if _is_nse_regular_session_now() and not _is_stale_quote(asof) else "upstox-last-traded"
                    quotes[row["symbol"]] = Quote(
                        symbol=row["symbol"],
                        price=float(price),
                        source=quote_source,
                        asof=asof,
                        open=ohlc.get("open"),
                        high=ohlc.get("high"),
                        low=ohlc.get("low"),
                        close=ohlc.get("close"),
                        volume=item.get("volume") or item.get("volume_traded"),
                    )
        self.last_quote_diagnostics = {
            "requested": requested,
            "resolved": len(universe),
            "returned": len(quotes),
            "missing_instrument_keys": missing_key_symbols[:20],
            "auto_resolved_instrument_keys": resolution["resolved"][:20],
            "missing_symbols": [row["symbol"] for row in universe if row["symbol"] not in quotes][:20],
            "errors": _unique_errors(errors)[:5],
            "source": "upstox_market_quote_quotes",
        }
        if requested and not quotes:
            raise MarketDataError(f"Upstox returned no quotes; diagnostics={self.last_quote_diagnostics}")
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        diagnostics: dict[str, Any] = {
            "requested_symbols": len(universe),
            "multi_timeframe": self.multi_timeframe,
            "intervals": [],
            "completed_requests": 0,
            "failed_requests": 0,
            "timed_out": False,
            "sample_errors": [],
        }
        specs = self._candle_specs()
        diagnostics["intervals"] = [spec["interval"] for spec in specs]
        semaphore = asyncio.Semaphore(self.candle_concurrency)
        async with httpx.AsyncClient(timeout=12, headers=self._headers()) as client:
            resolution = await self._ensure_instrument_keys(client, universe)
            diagnostics["auto_resolved_instrument_keys"] = resolution["resolved"][:20]
            diagnostics["missing_instrument_keys"] = [
                row["symbol"] for row in universe if not row.get("upstox_instrument_key")
            ][:20]
            for error in resolution["errors"][:5]:
                diagnostics["sample_errors"].append(error)
            universe = [row for row in universe if row.get("upstox_instrument_key")]
            tasks = [
                asyncio.create_task(self._fetch_candle_series(client, semaphore, row, spec))
                for row in universe
                for spec in specs
            ]
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.candle_fetch_timeout_seconds,
                )
            except asyncio.TimeoutError:
                diagnostics["timed_out"] = True
                for task in tasks:
                    task.cancel()
                results = []
                for task in tasks:
                    if task.done() and not task.cancelled():
                        try:
                            results.append(task.result())
                        except Exception as exc:
                            results.append(exc)
            for item in results:
                if isinstance(item, Exception):
                    diagnostics["failed_requests"] += 1
                    if len(diagnostics["sample_errors"]) < 5:
                        diagnostics["sample_errors"].append(_market_data_error_summary(item))
                    continue
                if not item:
                    diagnostics["failed_requests"] += 1
                    continue
                symbol, candles = item
                diagnostics["completed_requests"] += 1
                if candles:
                    output.setdefault(symbol, []).extend(candles)
        diagnostics["symbols_with_candles"] = len(output)
        diagnostics["total_candles"] = sum(len(items) for items in output.values())
        self.last_candle_diagnostics = diagnostics
        return output

    def _candle_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = [
            {
                "interval": self.interval,
                "lookback_days": self.lookback_days,
                "source": f"{self.source_name}:{self.interval}",
            }
        ]
        if not self.multi_timeframe:
            return specs
        seen = {self.interval}
        for interval, lookback_days in (
            ("day", self.daily_lookback_days),
            ("week", self.weekly_lookback_days),
        ):
            if interval in seen:
                continue
            specs.append(
                {
                    "interval": interval,
                    "lookback_days": lookback_days,
                    "source": f"{self.source_name}:{interval}",
                }
            )
            seen.add(interval)
        return specs

    async def _fetch_candle_series(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        row: dict[str, Any],
        spec: dict[str, Any],
    ) -> tuple[str, list[Candle]] | None:
        async with semaphore:
            to_date = date.today()
            from_date = to_date - timedelta(days=int(spec["lookback_days"]))
            instrument = quote(self._instrument_key(row), safe="")
            url = f"{self.base_url}/historical-candle/{instrument}/{spec['interval']}/{to_date.isoformat()}/{from_date.isoformat()}"
            try:
                response = await client.get(url)
                response.raise_for_status()
                raw_candles = response.json().get("data", {}).get("candles", [])
            except Exception:
                raw_candles = []
            return row["symbol"], [
                self._parse_candle(row["symbol"], candle, str(spec["source"]))
                for candle in reversed(raw_candles)
            ]

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

    async def _ensure_instrument_keys(
        self,
        client: httpx.AsyncClient,
        universe: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        resolved: list[str] = []
        errors: list[str] = []
        candidates: list[dict[str, Any]] = []
        for row in universe:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if row.get("upstox_instrument_key"):
                self._instrument_key_cache.setdefault(symbol, str(row["upstox_instrument_key"]))
                continue
            cached = self._instrument_key_cache.get(symbol)
            if cached:
                row["upstox_instrument_key"] = cached
                resolved.append(symbol)
                continue
            candidates.append(row)
        if not candidates:
            return {"resolved": resolved, "errors": errors}

        semaphore = asyncio.Semaphore(min(5, max(1, self.candle_concurrency)))

        async def resolve_one(row: dict[str, Any]) -> str | None:
            async with semaphore:
                return await self._resolve_instrument_key(client, row)

        results = await asyncio.gather(*(resolve_one(row) for row in candidates), return_exceptions=True)
        for row, result in zip(candidates, results):
            symbol = str(row.get("symbol") or "").strip().upper()
            if isinstance(result, Exception):
                errors.append(f"{symbol}: {_market_data_error_summary(result)}")
                continue
            if result:
                row["upstox_instrument_key"] = result
                self._instrument_key_cache[symbol] = result
                resolved.append(symbol)
        return {"resolved": resolved, "errors": errors}

    async def _resolve_instrument_key(self, client: httpx.AsyncClient, row: dict[str, Any]) -> str | None:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        queries = [symbol]
        name = str(row.get("name") or "").strip()
        if name and name.upper() != symbol:
            queries.append(name[:50])
        for query in queries:
            response = await client.get(
                f"{self.base_url}/instruments/search",
                params={
                    "query": query,
                    "exchanges": "NSE",
                    "segments": "EQ",
                    "page_number": 1,
                    "records": 30,
                },
            )
            response.raise_for_status()
            items = response.json().get("data", [])
            selected = self._select_instrument_search_result(row, items)
            if selected:
                key = str(selected.get("instrument_key") or "").strip()
                if key:
                    return key
        return None

    def _select_instrument_search_result(
        self,
        row: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or "").strip().upper()

        def score(item: dict[str, Any]) -> int:
            item_symbol = str(item.get("trading_symbol") or item.get("symbol") or "").strip().upper()
            item_name = str(item.get("name") or item.get("short_name") or "").strip().upper()
            item_segment = str(item.get("segment") or "").strip().upper()
            item_type = str(item.get("instrument_type") or "").strip().upper()
            item_key = str(item.get("instrument_key") or "").strip()
            if not item_key:
                return -1000
            points = 0
            if item_segment == "NSE_EQ":
                points += 100
            if item_type == "EQ":
                points += 25
            if item_symbol == symbol:
                points += 100
            elif item_symbol.startswith(symbol):
                points += 25
            if name and (item_name == name or name in item_name or item_name in name):
                points += 20
            return points

        ranked = sorted(items, key=score, reverse=True)
        if not ranked or score(ranked[0]) < 100:
            return None
        return ranked[0]

    def _instrument_key(self, row: dict[str, Any]) -> str:
        instrument = row.get("upstox_instrument_key")
        if not instrument:
            raise MarketDataError(f"{row['symbol']} is missing upstox_instrument_key")
        return instrument

    def _find_quote_item(self, data: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
        instrument = row.get("upstox_instrument_key", "")
        symbol = row["symbol"]
        for key, item in data.items():
            if key == instrument or key.endswith(f":{symbol}") or item.get("symbol") == symbol:
                return item
        if len(data) == 1:
            return next(iter(data.values()))
        return None

    def _parse_candle(self, symbol: str, candle: list[Any], source: str | None = None) -> Candle:
        return Candle(
            symbol=symbol,
            ts=str(candle[0]),
            open=float(candle[1]),
            high=float(candle[2]),
            low=float(candle[3]),
            close=float(candle[4]),
            volume=float(candle[5] or 0),
            source=source or self.source_name,
        )


def _upstox_quote_asof(item: dict[str, Any]) -> str:
    for key in (
        "last_trade_time",
        "last_traded_time",
        "ltt",
        "exchange_timestamp",
        "timestamp",
    ):
        value = item.get(key)
        if value is None:
            continue
        parsed = _parse_market_timestamp(value)
        if parsed:
            return parsed.isoformat()
    return utc_now()


def _parse_market_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return _parse_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_epoch(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_epoch(value: float) -> datetime | None:
    try:
        if value > 10_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_stale_quote(asof: str) -> bool:
    try:
        parsed = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() > 900


def _is_nse_regular_session_now() -> bool:
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if now_ist.weekday() >= 5:
        return False
    current_minutes = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= current_minutes <= (15 * 60 + 30)


class UpstoxSetupRequiredProvider(MarketDataProvider):
    source_name = "upstox-not-connected"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise MarketDataError(
            "Upstox access token is not configured. Connect Upstox from Settings before running market analytics."
        )

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        raise MarketDataError(
            "Upstox access token is not configured. Connect Upstox from Settings before running candle analytics."
        )


class KiteSetupRequiredProvider(MarketDataProvider):
    source_name = "kite-not-connected"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise MarketDataError(
            "Kite credentials are not configured. Save a Kite API key and access token before using Kite analytics."
        )

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        raise MarketDataError(
            "Kite credentials are not configured. Save a Kite API key and access token before using Kite analytics."
        )


class NubraMarketDataProvider(MarketDataProvider):
    source_name = "nubra"

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.nubra_api_base_url
        self.session_token = settings.nubra_session_token
        self.device_id = settings.nubra_device_id
        self.price_scale = settings.nubra_price_scale or 100
        self.interval = settings.nubra_candle_interval
        self.lookback_days = settings.nubra_candle_lookback_days
        self.candle_symbols_per_cycle = settings.nubra_candle_symbols_per_cycle
        self.source_name = "nubra-uat" if "uat" in self.base_url.lower() else "nubra-live"
        self.last_quote_diagnostics: dict[str, Any] = {}
        self._candle_cursor = 0
        if not self.session_token or not self.device_id:
            raise MarketDataError("Nubra provider needs NUBRA_SESSION_TOKEN and NUBRA_DEVICE_ID")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        failures: list[str] = []
        semaphore = asyncio.Semaphore(10)
        async with httpx.AsyncClient(timeout=10, headers=self._headers(), follow_redirects=True) as client:
            async def fetch(row: dict[str, Any]) -> Quote | None:
                async with semaphore:
                    return await self._current_price(client, row)

            results = await asyncio.gather(*(fetch(row) for row in universe), return_exceptions=True)
        for item in results:
            if isinstance(item, Quote):
                quotes[item.symbol] = item
            elif isinstance(item, Exception):
                failures.append(_market_data_error_summary(item))
        self.last_quote_diagnostics = {
            "requested": len(universe),
            "returned": len(quotes),
            "failures": len(failures),
            "sample_errors": _unique_errors(failures)[:5],
        }
        if universe and not quotes and failures:
            raise MarketDataError(
                f"Nubra quote failed for all {len(universe)} symbols; "
                f"sample_errors={self.last_quote_diagnostics['sample_errors']}"
            )
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        if self.candle_symbols_per_cycle <= 0:
            return {}
        selected = self._select_candle_universe(universe)
        output: dict[str, list[Candle]] = {}
        by_exchange: dict[str, list[dict[str, Any]]] = {}
        for row in selected:
            by_exchange.setdefault(str(row.get("exchange") or "NSE"), []).append(row)

        async with httpx.AsyncClient(timeout=20, headers=self._headers(), follow_redirects=True) as client:
            tasks = []
            for exchange, rows in by_exchange.items():
                # Nubra's chart endpoint accepts large symbol lists for some
                # accounts but may return an empty result for wider batches.
                # Small chunks keep the candle feed reliable and still allow
                # concurrent history refreshes across the enabled universe.
                for index in range(0, len(rows), 5):
                    tasks.append(self._candles_for_chunk(client, exchange, rows[index : index + 5]))
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, dict):
                output.update(item)
        return output

    def _select_candle_universe(self, universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = self.candle_symbols_per_cycle
        if limit <= 0 or limit >= len(universe):
            return universe
        if not universe:
            return []
        start = self._candle_cursor % len(universe)
        selected = [universe[(start + index) % len(universe)] for index in range(limit)]
        self._candle_cursor = (start + limit) % len(universe)
        return selected

    async def _current_price(self, client: httpx.AsyncClient, row: dict[str, Any]) -> Quote | None:
        symbol = self._nubra_symbol(row)
        params = {}
        exchange = str(row.get("exchange") or "NSE").upper()
        if exchange == "BSE":
            params["exchange"] = "BSE"
        try:
            response = await client.get(f"{self.base_url}/optionchains/{quote(symbol, safe='')}/price", params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise MarketDataError(
                f"Nubra quote HTTP {exc.response.status_code} for {row['symbol']} ({symbol}); "
                f"body={exc.response.text[:180]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MarketDataError(f"Nubra quote timeout for {row['symbol']} ({symbol})") from exc
        except Exception as exc:
            raise MarketDataError(f"Nubra quote error for {row['symbol']} ({symbol}): {exc.__class__.__name__}: {exc}") from exc
        price = self._scaled(data.get("price"))
        if price is None:
            raise MarketDataError(f"Nubra quote missing/invalid price for {row['symbol']} ({symbol}); keys={list(data)[:8]}")
        return Quote(
            symbol=row["symbol"],
            price=price,
            source=self.source_name,
            asof=utc_now(),
            open=None,
            high=None,
            low=None,
            close=self._scaled(data.get("prev_close")),
            volume=None,
        )

    async def _candles_for_chunk(
        self,
        client: httpx.AsyncClient,
        exchange: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, list[Candle]]:
        if not rows:
            return {}
        now = datetime.now(timezone.utc)
        payload = {
            "query": [
                {
                    "exchange": exchange,
                    "type": "STOCK",
                    "values": [self._nubra_symbol(row) for row in rows],
                    "fields": ["open", "high", "low", "close", "cumulative_volume"],
                    "startDate": (now - timedelta(days=self.lookback_days)).isoformat().replace("+00:00", "Z"),
                    "endDate": now.isoformat().replace("+00:00", "Z"),
                    "interval": self.interval,
                    "intraDay": False,
                    "realTime": False,
                }
            ]
        }
        try:
            response = await client.post(f"{self.base_url}/charts/timeseries", json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return {}
        by_nubra = {self._nubra_symbol(row): row["symbol"] for row in rows}
        output: dict[str, list[Candle]] = {}
        for result in data.get("result", []):
            for value_item in result.get("values", []):
                if not isinstance(value_item, dict):
                    continue
                for nubra_symbol, series in value_item.items():
                    symbol = by_nubra.get(nubra_symbol)
                    if symbol and isinstance(series, dict):
                        output[symbol] = self._parse_candle_series(symbol, series)
        return output

    def _parse_candle_series(self, symbol: str, series: dict[str, Any]) -> list[Candle]:
        by_ts: dict[int, dict[str, float]] = {}
        for field in ("open", "high", "low", "close", "cumulative_volume"):
            for point in series.get(field, []) or []:
                ts = self._point_ts(point)
                raw_value = self._point_value(point)
                if ts is None or raw_value is None:
                    continue
                value = raw_value if field == "cumulative_volume" else self._scaled(raw_value)
                if value is None:
                    continue
                by_ts.setdefault(ts, {})[field] = value
        candles: list[Candle] = []
        for ts, values in sorted(by_ts.items()):
            if not all(key in values for key in ("open", "high", "low", "close")):
                continue
            candles.append(
                Candle(
                    symbol=symbol,
                    ts=datetime.fromtimestamp(ts / 1_000_000_000, timezone.utc).isoformat(),
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values.get("cumulative_volume", 0)),
                    source=self.source_name,
                )
            )
        return candles[-96:]

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-device-id": self.device_id,
            "Authorization": f"Bearer {self.session_token}",
        }

    def _nubra_symbol(self, row: dict[str, Any]) -> str:
        return str(row.get("nubra_symbol") or row["symbol"]).strip()

    def _scaled(self, value: Any) -> float | None:
        try:
            return round(float(value) / float(self.price_scale), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def _point_ts(self, point: dict[str, Any]) -> int | None:
        value = point.get("ts", point.get("timestamp"))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _point_value(self, point: dict[str, Any]) -> float | None:
        value = point.get("v", point.get("value"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _market_data_error_summary(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:160]}"
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message[:220]}" if message else exc.__class__.__name__


def _provider_diagnostics(provider: MarketDataProvider) -> str:
    diagnostics = getattr(provider, "last_quote_diagnostics", None)
    if diagnostics:
        return str(diagnostics)[:700]
    return "no detailed diagnostics captured"


def _unique_errors(errors: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error in seen:
            continue
        seen.add(error)
        output.append(error)
    return output


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    safe_size = max(1, int(size or 1))
    return [items[index : index + safe_size] for index in range(0, len(items), safe_size)]


def _float_any(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _alpaca_bar_to_candle(symbol: str, item: dict[str, Any], source: str) -> Candle | None:
    try:
        return Candle(
            symbol=str(symbol).upper(),
            ts=str(item.get("t") or utc_now()),
            open=float(item.get("o")),
            high=float(item.get("h")),
            low=float(item.get("l")),
            close=float(item.get("c")),
            volume=float(item.get("v") or 0),
            source=source,
        )
    except (TypeError, ValueError):
        return None


def _polygon_bar_to_candle(symbol: str, item: dict[str, Any], source: str) -> Candle | None:
    try:
        return Candle(
            symbol=str(symbol).upper(),
            ts=_polygon_ts_to_iso(item.get("t")),
            open=float(item.get("o")),
            high=float(item.get("h")),
            low=float(item.get("l")),
            close=float(item.get("c")),
            volume=float(item.get("v") or 0),
            source=source,
        )
    except (TypeError, ValueError):
        return None


def _polygon_ts_to_iso(value: Any) -> str:
    if not value:
        return utc_now()
    try:
        numeric = float(value)
        if numeric > 10_000_000_000_000:
            numeric = numeric / 1_000_000_000
        elif numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
    except Exception:
        return utc_now()


def _normalize_trade_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    for suffix in ("-EQ", "_EQ", ".EQ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.replace(" ", "").replace("&AMP;", "&")


class IndStocksSetupRequiredProvider(MarketDataProvider):
    source_name = "indstocks-not-connected"

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise MarketDataError(
            "INDstocks access token is not configured. Paste the token in Broker settings before running market analytics."
        )

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        raise MarketDataError(
            "INDstocks access token is not configured. Paste the token in Broker settings before running candle analytics."
        )


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    region = normalize_market_region(settings.market_region)
    yahoo = YahooMarketDataProvider(settings)
    provider = normalize_market_data_provider_name(settings.market_data_provider)
    if provider == "simulated":
        return SimulatedMarketDataProvider()
    if provider == "yahoo":
        return yahoo

    india_provider = _build_india_market_data_provider(settings, yahoo)
    us_provider = _build_us_market_data_provider(settings, yahoo)
    if region == "BOTH":
        return MarketRegionRoutingProvider(india_provider=india_provider, us_provider=us_provider)
    return us_provider if region == "US" else india_provider


def _build_us_market_data_provider(settings: Settings, yahoo: YahooMarketDataProvider) -> MarketDataProvider:
    provider = normalize_us_market_data_provider_name(getattr(settings, "us_market_data_provider", "yahoo"))
    if provider == "yahoo":
        return yahoo
    if provider in {"alpaca", "alpaca_yahoo"}:
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            return yahoo if provider == "alpaca_yahoo" else AlpacaSetupRequiredProvider()
        primary = AlpacaMarketDataProvider(settings)
        return HistoricalCandleFallbackProvider(primary, yahoo, min_candles=55) if provider == "alpaca_yahoo" else primary
    if provider in {"polygon", "polygon_yahoo"}:
        if not settings.polygon_api_key:
            return yahoo if provider == "polygon_yahoo" else PolygonSetupRequiredProvider()
        primary = PolygonMarketDataProvider(settings)
        return HistoricalCandleFallbackProvider(primary, yahoo, min_candles=55) if provider == "polygon_yahoo" else primary
    return yahoo


def _build_india_market_data_provider(settings: Settings, yahoo: YahooMarketDataProvider) -> MarketDataProvider:
    provider = normalize_market_data_provider_name(settings.market_data_provider)
    if provider == "simulated":
        return SimulatedMarketDataProvider()
    if provider == "yahoo":
        return yahoo
    if provider in {"upstox", "upstox_yahoo"}:
        if not settings.upstox_access_token:
            return yahoo if provider == "upstox_yahoo" else UpstoxSetupRequiredProvider()
        primary = UpstoxMarketDataProvider(settings)
        return HistoricalCandleFallbackProvider(primary, yahoo) if provider == "upstox_yahoo" else primary
    if provider in {"kite", "kite_yahoo"}:
        if not settings.kite_api_key or not settings.kite_access_token:
            return yahoo if provider == "kite_yahoo" else KiteSetupRequiredProvider()
        primary = KiteMarketDataProvider(settings)
        return HistoricalCandleFallbackProvider(primary, yahoo) if provider == "kite_yahoo" else primary
    if provider == "nubra":
        return NubraMarketDataProvider(settings)
    return UpstoxSetupRequiredProvider()


def _build_indstocks_market_data_provider(settings: Settings, yahoo: YahooMarketDataProvider) -> MarketDataProvider:
    provider = normalize_market_data_provider_name(settings.market_data_provider)
    if provider == "upstox_yahoo" and not settings.upstox_access_token:
        return yahoo
    if provider in {"upstox", "upstox_yahoo"}:
        if not settings.upstox_access_token:
            return UpstoxSetupRequiredProvider()
        primary = UpstoxMarketDataProvider(settings)
        return HistoricalCandleFallbackProvider(primary, yahoo) if provider == "upstox_yahoo" else primary
    return UpstoxSetupRequiredProvider()
