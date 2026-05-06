from __future__ import annotations

import asyncio
import math
import random
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .models import Candle, Quote, utc_now


class MarketDataError(RuntimeError):
    pass


class MarketDataProvider(ABC):
    source_name: str

    @abstractmethod
    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        raise NotImplementedError

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        return {}


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
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "OpenTrade/1.0 (+paper-trading-dashboard)",
        }

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        symbols = [row.get("yahoo_symbol") or f"{row['symbol']}.NS" for row in universe]
        by_yahoo = dict(zip(symbols, universe))
        quotes: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=10, headers=self.headers, follow_redirects=True) as client:
            for i in range(0, len(symbols), 40):
                chunk = symbols[i : i + 40]
                try:
                    response = await client.get(
                        "https://query1.finance.yahoo.com/v7/finance/quote",
                        params={"symbols": ",".join(chunk)},
                    )
                    response.raise_for_status()
                    data = response.json()
                except Exception:
                    continue
                for item in data.get("quoteResponse", {}).get("result", []):
                    yahoo_symbol = item.get("symbol")
                    row = by_yahoo.get(yahoo_symbol)
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
        return quotes

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
        try:
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
        except Exception:
            return None

    async def _candles_from_chart(self, client: httpx.AsyncClient, row: dict[str, Any]) -> list[Candle]:
        try:
            response = await client.get(
                self._chart_url(row),
                params={"range": self.range, "interval": self.interval, "includePrePost": "false"},
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
            return candles[-96:]
        except Exception:
            return []

    def _chart_url(self, row: dict[str, Any]) -> str:
        yahoo_symbol = row.get("yahoo_symbol") or f"{row['symbol']}.NS"
        return f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(yahoo_symbol, safe='')}"

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

    def __init__(self, settings: Settings) -> None:
        self.access_token = settings.upstox_access_token
        self.base_url = settings.upstox_api_base_url
        self.interval = settings.upstox_candle_interval
        self.lookback_days = settings.upstox_candle_lookback_days
        if not self.access_token:
            raise MarketDataError("Upstox provider needs UPSTOX_ACCESS_TOKEN")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=10, headers=self._headers()) as client:
            for i in range(0, len(universe), 100):
                chunk = universe[i : i + 100]
                response = await client.get(
                    f"{self.base_url}/market-quote/quotes",
                    params={"instrument_key": ",".join(self._instrument_key(row) for row in chunk)},
                )
                response.raise_for_status()
                data = response.json().get("data", {})
                for row in chunk:
                    item = self._find_quote_item(data, row)
                    if not item:
                        continue
                    price = item.get("last_price") or item.get("ltp")
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
                        volume=item.get("volume") or item.get("volume_traded"),
                    )
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        to_date = date.today()
        from_date = to_date - timedelta(days=self.lookback_days)
        async with httpx.AsyncClient(timeout=12, headers=self._headers()) as client:
            for row in universe:
                instrument = quote(self._instrument_key(row), safe="")
                url = f"{self.base_url}/historical-candle/{instrument}/{self.interval}/{to_date.isoformat()}/{from_date.isoformat()}"
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    raw_candles = response.json().get("data", {}).get("candles", [])
                except Exception:
                    raw_candles = []
                output[row["symbol"]] = [self._parse_candle(row["symbol"], candle) for candle in reversed(raw_candles)]
        return output

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

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

    def _parse_candle(self, symbol: str, candle: list[Any]) -> Candle:
        return Candle(
            symbol=symbol,
            ts=str(candle[0]),
            open=float(candle[1]),
            high=float(candle[2]),
            low=float(candle[3]),
            close=float(candle[4]),
            volume=float(candle[5] or 0),
            source=self.source_name,
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
        if not self.session_token or not self.device_id:
            raise MarketDataError("Nubra provider needs NUBRA_SESSION_TOKEN and NUBRA_DEVICE_ID")

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        semaphore = asyncio.Semaphore(10)
        async with httpx.AsyncClient(timeout=10, headers=self._headers(), follow_redirects=True) as client:
            async def fetch(row: dict[str, Any]) -> Quote | None:
                async with semaphore:
                    return await self._current_price(client, row)

            results = await asyncio.gather(*(fetch(row) for row in universe), return_exceptions=True)
        for item in results:
            if isinstance(item, Quote):
                quotes[item.symbol] = item
        return quotes

    async def get_candles(self, universe: list[dict[str, Any]]) -> dict[str, list[Candle]]:
        if self.candle_symbols_per_cycle <= 0:
            return {}
        selected = universe[: self.candle_symbols_per_cycle]
        output: dict[str, list[Candle]] = {}
        by_exchange: dict[str, list[dict[str, Any]]] = {}
        for row in selected:
            by_exchange.setdefault(str(row.get("exchange") or "NSE"), []).append(row)

        async with httpx.AsyncClient(timeout=20, headers=self._headers(), follow_redirects=True) as client:
            tasks = []
            for exchange, rows in by_exchange.items():
                for index in range(0, len(rows), 40):
                    tasks.append(self._candles_for_chunk(client, exchange, rows[index : index + 40]))
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, dict):
                output.update(item)
        return output

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
        except Exception:
            return None
        price = self._scaled(data.get("price"))
        if price is None:
            return None
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


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    provider = settings.market_data_provider
    if provider == "simulated":
        return SimulatedMarketDataProvider()
    if provider == "yahoo":
        return YahooMarketDataProvider(settings)
    if provider == "kite":
        return KiteMarketDataProvider(settings)
    if provider == "upstox":
        return UpstoxMarketDataProvider(settings)
    if provider == "nubra":
        return NubraMarketDataProvider(settings)
    raise MarketDataError(f"Unsupported MARKET_DATA_PROVIDER={provider!r}")
