from __future__ import annotations

import time
from typing import Any

from .config import Settings
from .db import Database
from .models import Candle, Quote, utc_now


class MarketBreadthService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def compute_breadth(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        candles_by_symbol: dict[str, list[Candle]],
    ) -> dict[str, Any]:
        if not self.settings.enable_market_breadth:
            return _neutral("disabled")
        if self._cache and time.monotonic() - self._cache[0] < self.settings.market_breadth_cache_seconds:
            return self._cache[1]
        try:
            snapshot = self._compute(universe, quotes, candles_by_symbol)
            history = self.db.get_state("market_breadth_history", [])
            if not isinstance(history, list):
                history = []
            history.append(snapshot)
            history = history[-20:]
            snapshot["history_count"] = len(history)
            snapshot["breadth_thrust"] = _breadth_thrust(history)
            snapshot["mcclellan_proxy"] = _mcclellan_proxy(history)
            self.db.set_state("market_breadth_history", history)
            self.db.set_state("market_breadth_context", snapshot)
            self._cache = (time.monotonic(), snapshot)
            self._log("INFO", "breadth_computed", "Market breadth computed", snapshot)
            return snapshot
        except Exception as exc:
            result = _neutral(f"{exc.__class__.__name__}: {str(exc)[:200]}")
            self._log("WARN", "breadth_error", "Market breadth failed safely", result)
            return result

    def _compute(
        self,
        universe: list[dict[str, Any]],
        quotes: dict[str, Quote],
        candles_by_symbol: dict[str, list[Candle]],
    ) -> dict[str, Any]:
        advance_count = 0
        decline_count = 0
        above_20 = 0
        above_50 = 0
        above_200 = 0
        ma20_count = 0
        ma50_count = 0
        ma200_count = 0
        new_highs = 0
        new_lows = 0
        checked = 0
        for row in universe:
            symbol = row["symbol"]
            quote = quotes.get(symbol)
            candles = candles_by_symbol.get(symbol) or []
            if not quote or len(candles) < 2:
                continue
            checked += 1
            closes = [float(candle.close) for candle in candles]
            price = float(quote.price or closes[-1])
            if closes[-1] > closes[-2]:
                advance_count += 1
            elif closes[-1] < closes[-2]:
                decline_count += 1
            sma20 = _sma(closes, 20)
            sma50 = _sma(closes, 50)
            sma200 = _sma(closes, 200)
            if sma20 is not None:
                ma20_count += 1
                above_20 += 1 if price > sma20 else 0
            if sma50 is not None:
                ma50_count += 1
                above_50 += 1 if price > sma50 else 0
            if sma200 is not None:
                ma200_count += 1
                above_200 += 1 if price > sma200 else 0
            lookback = closes[-52:] if len(closes) >= 52 else closes
            if lookback and price >= max(lookback):
                new_highs += 1
            if lookback and price <= min(lookback):
                new_lows += 1
        pct_20 = (above_20 / ma20_count) * 100 if ma20_count else 0.0
        pct_50 = (above_50 / ma50_count) * 100 if ma50_count else 0.0
        pct_200 = (above_200 / ma200_count) * 100 if ma200_count else 0.0
        highs_lows_ratio = new_highs / max(new_highs + new_lows, 1)
        regime = _breadth_regime(pct_50, highs_lows_ratio)
        return {
            "enabled": True,
            "updated_at": utc_now(),
            "symbols_checked": checked,
            "advance_count": advance_count,
            "decline_count": decline_count,
            "advance_decline_ratio": round(advance_count / max(decline_count, 1), 4),
            "pct_above_20dma": round(pct_20, 3),
            "pct_above_50dma": round(pct_50, 3),
            "pct_above_200dma": round(pct_200, 3),
            "new_highs_count": new_highs,
            "new_lows_count": new_lows,
            "new_highs_lows_ratio": round(highs_lows_ratio, 4),
            "breadth_thrust": False,
            "mcclellan_proxy": 0.0,
            "breadth_regime": regime,
            "breadth_score": _breadth_score(regime),
        }

    def _log(self, level: str, event: str, message: str, details: Any | None = None) -> None:
        try:
            self.db.insert_agent_log(level, "market_breadth", event, message, details)
        except Exception:
            pass


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _breadth_regime(pct_above_50: float, highs_lows_ratio: float) -> str:
    if pct_above_50 > 65 and highs_lows_ratio > 0.65:
        return "bull_confirmed"
    if 50 <= pct_above_50 <= 65:
        return "bull_weakening"
    if 40 <= pct_above_50 < 50:
        return "neutral"
    if 30 <= pct_above_50 < 40:
        return "bear_warning"
    return "bear_confirmed"


def _breadth_score(regime: str) -> float:
    return {
        "bull_confirmed": 0.5,
        "bull_weakening": 0.2,
        "neutral": 0.0,
        "bear_warning": -0.3,
        "bear_confirmed": -0.6,
    }.get(regime, 0.0)


def _breadth_thrust(history: list[dict[str, Any]]) -> bool:
    recent = history[-10:]
    if len(recent) < 2:
        return False
    values = [float(item.get("pct_above_50dma") or 0.0) for item in recent]
    return min(values[:-1]) < 40 and values[-1] > 60


def _mcclellan_proxy(history: list[dict[str, Any]]) -> float:
    if not history:
        return 0.0
    advances_minus_declines = [
        float(item.get("advance_count") or 0.0) - float(item.get("decline_count") or 0.0)
        for item in history
    ]
    ema19 = _ema(advances_minus_declines, 19)
    ema39 = _ema(advances_minus_declines, 39)
    return round(ema19 - ema39, 4)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema


def _neutral(reason: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "updated_at": utc_now(),
        "advance_decline_ratio": 1.0,
        "pct_above_20dma": 0.0,
        "pct_above_50dma": 0.0,
        "pct_above_200dma": 0.0,
        "new_highs_count": 0,
        "new_lows_count": 0,
        "new_highs_lows_ratio": 0.0,
        "breadth_thrust": False,
        "mcclellan_proxy": 0.0,
        "breadth_regime": "neutral",
        "breadth_score": 0.0,
        "data_gap": reason,
    }
