from __future__ import annotations

from statistics import mean
from typing import Any

from .indicators import technical_snapshot
from .models import Candle, Quote
from .strategy_presets import choose_best_strategy, evaluate_strategy_presets


def build_symbol_tool_context(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    position: dict[str, Any] | None,
    sentiment_score: float,
    risk_limits: dict[str, Any],
    global_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    closes = [candle.close for candle in candles] or [quote.price]
    technical = technical_snapshot(closes)
    candle_tools = _candle_tools(candles)
    strategy_signals = evaluate_strategy_presets(candles, quote.price)
    best_strategy = choose_best_strategy(strategy_signals)
    return {
        "tool_protocol": "mcp-style-json-context",
        "symbol": row["symbol"],
        "company": row.get("name"),
        "sector": row.get("sector"),
        "exchange": row.get("exchange", "NSE"),
        "quote": quote.to_dict(),
        "position": position or {"qty": 0, "avg_price": 0, "market_price": quote.price},
        "technical_math": technical.to_dict(),
        "candlestick_analysis": candle_tools,
        "strategy_signals": [signal.to_dict() for signal in strategy_signals],
        "best_strategy": best_strategy.to_dict(),
        "sentiment": {"score": sentiment_score},
        "global_market_context": global_context
        or {
            "enabled": False,
            "risk_score": 0.0,
            "confidence": 0.0,
            "regime": "unavailable",
        },
        "risk_limits": risk_limits,
        "recent_candles": [candle.to_dict() for candle in candles[-24:]],
    }


def deterministic_score(context: dict[str, Any]) -> float:
    return deterministic_score_breakdown(context)["combined"]


def deterministic_score_breakdown(context: dict[str, Any]) -> dict[str, Any]:
    technical = float(context["technical_math"]["score"])
    sentiment = float(context["sentiment"]["score"])
    candle_score = float(context["candlestick_analysis"]["score"])
    preset_score = float(context["best_strategy"]["score"])
    global_risk = float(context.get("global_market_context", {}).get("risk_score", 0.0) or 0.0)
    global_weight = float(context.get("risk_limits", {}).get("global_risk_weight", 0.1) or 0.0)
    global_weight = max(min(global_weight, 0.3), 0.0)
    remaining = 1.0 - global_weight
    components = [
        {"name": "technical_math", "score": technical, "weight": round(0.40 * remaining, 4)},
        {"name": "candlestick_analysis", "score": candle_score, "weight": round(0.20 * remaining, 4)},
        {"name": "best_strategy", "score": preset_score, "weight": round(0.25 * remaining, 4)},
        {"name": "sentiment", "score": sentiment, "weight": round(0.15 * remaining, 4)},
        {"name": "global_market_context", "score": global_risk, "weight": round(global_weight, 4)},
    ]
    raw = sum(component["score"] * component["weight"] for component in components)
    combined = max(min(raw, 1.0), -1.0)
    return {
        "formula": "technical_math*scaled_0.40 + candlestick_analysis*scaled_0.20 + best_strategy*scaled_0.25 + sentiment*scaled_0.15 + global_market_context*global_risk_weight",
        "components": [
            {
                **component,
                "contribution": round(component["score"] * component["weight"], 4),
            }
            for component in components
        ],
        "raw": round(raw, 4),
        "combined": combined,
        "clamped": combined != raw,
    }


def _candle_tools(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 3:
        return {"score": 0.0, "patterns": ["insufficient-candles"], "atr_pct": None, "volume_ratio": None}

    recent = candles[-20:]
    last = candles[-1]
    previous = candles[-2]
    patterns: list[str] = []
    score = 0.0

    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 0.01)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    if body / candle_range < 0.12:
        patterns.append("doji")
    if last.close > last.open and previous.close < previous.open and last.close > previous.open and last.open < previous.close:
        patterns.append("bullish-engulfing")
        score += 0.25
    if last.close < last.open and previous.close > previous.open and last.open > previous.close and last.close < previous.open:
        patterns.append("bearish-engulfing")
        score -= 0.25
    if lower_wick > body * 2 and upper_wick < body:
        patterns.append("hammer-like")
        score += 0.12
    if upper_wick > body * 2 and lower_wick < body:
        patterns.append("shooting-star-like")
        score -= 0.12

    highs = [candle.high for candle in recent[:-1]]
    lows = [candle.low for candle in recent[:-1]]
    if highs and last.close > max(highs):
        patterns.append("range-breakout")
        score += 0.22
    if lows and last.close < min(lows):
        patterns.append("range-breakdown")
        score -= 0.22

    true_ranges = [
        max(candle.high - candle.low, abs(candle.high - prev.close), abs(candle.low - prev.close))
        for prev, candle in zip(recent, recent[1:])
    ]
    atr = mean(true_ranges) if true_ranges else 0.0
    atr_pct = (atr / last.close) * 100 if last.close else 0.0
    volumes = [candle.volume for candle in recent[:-1] if candle.volume]
    volume_ratio = last.volume / mean(volumes) if volumes else None
    if volume_ratio and volume_ratio > 1.8 and last.close > last.open:
        patterns.append("bullish-volume-expansion")
        score += 0.16
    if volume_ratio and volume_ratio > 1.8 and last.close < last.open:
        patterns.append("bearish-volume-expansion")
        score -= 0.16

    if atr_pct > 4:
        patterns.append("high-volatility")
        score *= 0.7

    return {
        "score": round(max(min(score, 1.0), -1.0), 3),
        "patterns": patterns or ["no-clear-pattern"],
        "atr_pct": round(atr_pct, 3),
        "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
        "last_body_pct_of_range": round((body / candle_range) * 100, 2),
    }
