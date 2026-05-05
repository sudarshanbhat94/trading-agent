from __future__ import annotations

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

    async def get_quotes(self, universe: list[dict[str, Any]]) -> dict[str, Quote]:
        symbols = [row.get("yahoo_symbol") or f"{row['symbol']}.NS" for row in universe]
        by_yahoo = dict(zip(symbols, universe))
        quotes: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=8) as client:
            for i in range(0, len(symbols), 40):
                chunk = symbols[i : i + 40]
                response = await client.get(
                    "https://query1.finance.yahoo.com/v7/finance/quote",
                    params={"symbols": ",".join(chunk)},
                )
                response.raise_for_status()
                data = response.json()
                for item in data.get("quoteResponse", {}).get("result", []):
                    yahoo_symbol = item.get("symbol")
                    row = by_yahoo.get(yahoo_symbol)
                    price = item.get("regularMarketPrice") or item.get("postMarketPrice")
                    if not row or not price:
                        continue
                    symbol = row["symbol"]
                    quotes[symbol] = Quote(
                        symbol=symbol,
                        price=float(price),
                        source=self.source_name,
                        asof=utc_now(),
                        open=item.get("regularMarketOpen"),
                        high=item.get("regularMarketDayHigh"),
                        low=item.get("regularMarketDayLow"),
                        close=item.get("regularMarketPreviousClose"),
                        volume=item.get("regularMarketVolume"),
                    )
        return quotes


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


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    provider = settings.market_data_provider
    if provider == "simulated":
        return SimulatedMarketDataProvider()
    if provider == "yahoo":
        return YahooMarketDataProvider()
    if provider == "kite":
        return KiteMarketDataProvider(settings)
    if provider == "upstox":
        return UpstoxMarketDataProvider(settings)
    raise MarketDataError(f"Unsupported MARKET_DATA_PROVIDER={provider!r}")
