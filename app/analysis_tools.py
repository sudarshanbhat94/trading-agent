from __future__ import annotations

import json
from typing import Any

from .full_spectrum import full_spectrum_analysis
from .indicators import technical_snapshot
from .models import Candle, Quote
from .strategy_presets import choose_best_strategy, evaluate_strategy_presets


def _mean(values: list[float] | tuple[float, ...] | Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def build_symbol_tool_context(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    position: dict[str, Any] | None,
    sentiment_score: float,
    risk_limits: dict[str, Any],
    global_context: dict[str, Any] | None = None,
    institutional_context: dict[str, Any] | None = None,
    sentiment_detail: dict[str, Any] | None = None,
    delivery_data: dict[str, Any] | None = None,
    options_data: dict[str, Any] | None = None,
    sector_context: dict[str, Any] | None = None,
    market_breadth: dict[str, Any] | None = None,
    macro_event_context: dict[str, Any] | None = None,
    timeframe_candles: dict[str, list[Candle]] | None = None,
) -> dict[str, Any]:
    timeframe_candles = timeframe_candles or {}
    analysis_candles = timeframe_candles.get("analysis") or timeframe_candles.get("daily") or candles
    intraday_candles = timeframe_candles.get("intraday") or []
    daily_candles = timeframe_candles.get("daily") or analysis_candles
    weekly_candles = timeframe_candles.get("weekly") or []
    closes = [candle.close for candle in analysis_candles] or [quote.price]
    highs = [candle.high for candle in analysis_candles]
    lows = [candle.low for candle in analysis_candles]
    volumes = [candle.volume for candle in analysis_candles]
    technical = technical_snapshot(closes, highs, lows, volumes)
    candle_tools = _candle_tools(analysis_candles)
    strategy_signals = evaluate_strategy_presets(analysis_candles, quote.price)
    best_strategy = choose_best_strategy(strategy_signals)
    normalized_global_context = global_context or {
        "enabled": False,
        "risk_score": 0.0,
        "confidence": 0.0,
        "regime": "unavailable",
    }
    normalized_institutional_context = institutional_context or {
        "enabled": False,
        "source_quality": "unavailable",
        "feeds": {},
        "symbol_flags": {},
        "market_bias": {"score": 0.0, "rationale": []},
    }
    strategy_signal_dicts = [signal.to_dict() for signal in strategy_signals]
    technical_dict = technical.to_dict()
    full_spectrum = full_spectrum_analysis(
        row=row,
        quote=quote,
        candles=analysis_candles,
        technical=technical_dict,
        candle_tools=candle_tools,
        strategy_signals=strategy_signal_dicts,
        sentiment_score=sentiment_score,
        global_context=normalized_global_context,
        institutional_context=normalized_institutional_context,
        risk_limits=risk_limits,
        delivery_data=delivery_data,
        options_data=options_data,
        sector_context=sector_context,
        market_breadth=market_breadth,
        macro_event_context=macro_event_context,
        timeframe_candles={
            "intraday": intraday_candles,
            "daily": daily_candles,
            "weekly": weekly_candles,
            "analysis": analysis_candles,
        },
    )
    return {
        "tool_protocol": "mcp-style-json-context",
        "symbol": row["symbol"],
        "company": row.get("name"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "exchange": row.get("exchange", "NSE"),
        "quote": quote.to_dict(),
        "position": _position_context(position, quote),
        "technical_math": technical_dict,
        "candlestick_analysis": candle_tools,
        "strategy_signals": strategy_signal_dicts,
        "best_strategy": best_strategy.to_dict(),
        "sentiment": _sentiment_context(sentiment_score, sentiment_detail),
        "global_market_context": normalized_global_context,
        "institutional_context": normalized_institutional_context,
        "delivery_data": delivery_data or {},
        "options_intelligence": options_data or {},
        "sector_rotation": sector_context or {},
        "market_breadth_context": market_breadth or {},
        "macro_event_context": macro_event_context or {},
        "timeframe_data": {
            "analysis_candle_count": len(analysis_candles),
            "intraday_candle_count": len(intraday_candles),
            "daily_candle_count": len(daily_candles),
            "weekly_candle_count": len(weekly_candles),
            "analysis_source": analysis_candles[-1].source if analysis_candles else None,
            "intraday_source": intraday_candles[-1].source if intraday_candles else None,
            "daily_source": daily_candles[-1].source if daily_candles else None,
            "weekly_source": weekly_candles[-1].source if weekly_candles else None,
        },
        "full_spectrum_analysis": full_spectrum,
        "risk_limits": risk_limits,
        "recent_candles": [candle.to_dict() for candle in analysis_candles[-24:]],
    }


def deterministic_score(context: dict[str, Any]) -> float:
    return deterministic_score_breakdown(context)["combined"]


def deterministic_score_breakdown(context: dict[str, Any]) -> dict[str, Any]:
    technical = float(context["technical_math"]["score"])
    sentiment = float(context["sentiment"]["score"])
    candle_score = float(context["candlestick_analysis"]["score"])
    preset_score = float(context["best_strategy"]["score"])
    full_spectrum_score = _full_spectrum_score(context)
    delivery_score = _delivery_score(context)
    sector_rotation_score = _sector_rotation_score(context)
    stage_score = _stage_score(context)
    divergence_score = _divergence_score(context)
    entry_quality_score = _entry_quality_score(context)
    global_risk = float(context.get("global_market_context", {}).get("risk_score", 0.0) or 0.0)
    institutional_score = _institutional_score(context)
    global_weight = float(context.get("risk_limits", {}).get("global_risk_weight", 0.1) or 0.0)
    institutional_weight = float(context.get("risk_limits", {}).get("institutional_risk_weight", 0.12) or 0.0)
    global_weight = max(min(global_weight, 0.3), 0.0)
    institutional_weight = max(min(institutional_weight, 0.3), 0.0)
    if global_weight + institutional_weight > 0.45:
        scale = 0.45 / (global_weight + institutional_weight)
        global_weight *= scale
        institutional_weight *= scale
    remaining = 1.0 - global_weight - institutional_weight
    components = [
        {"name": "technical_math", "score": technical, "weight": round(0.20 * remaining, 4)},
        {"name": "candlestick_analysis", "score": candle_score, "weight": round(0.10 * remaining, 4)},
        {"name": "best_strategy", "score": preset_score, "weight": round(0.15 * remaining, 4)},
        {"name": "sentiment", "score": sentiment, "weight": round(0.09 * remaining, 4)},
        {"name": "full_spectrum_layers", "score": full_spectrum_score, "weight": round(0.10 * remaining, 4)},
        {"name": "delivery_score", "score": delivery_score, "weight": round(0.08 * remaining, 4)},
        {"name": "sector_rotation_score", "score": sector_rotation_score, "weight": round(0.07 * remaining, 4)},
        {"name": "stage_score", "score": stage_score, "weight": round(0.10 * remaining, 4)},
        {"name": "divergence_score", "score": divergence_score, "weight": round(0.06 * remaining, 4)},
        {"name": "entry_quality_score", "score": entry_quality_score, "weight": round(0.05 * remaining, 4)},
        {"name": "global_market_context", "score": global_risk, "weight": round(global_weight, 4)},
        {"name": "free_institutional_context", "score": institutional_score, "weight": round(institutional_weight, 4)},
    ]
    raw = sum(component["score"] * component["weight"] for component in components)
    combined = max(min(raw, 1.0), -1.0)
    score_percent = round(((combined + 1.0) / 2.0) * 100.0, 1)
    return {
        "formula": "technical_math*scaled_0.20 + candlestick_analysis*scaled_0.10 + best_strategy*scaled_0.15 + sentiment*scaled_0.09 + full_spectrum_layers*scaled_0.10 + delivery_score*scaled_0.08 + sector_rotation_score*scaled_0.07 + stage_score*scaled_0.10 + divergence_score*scaled_0.06 + entry_quality_score*scaled_0.05 + global_market_context*global_risk_weight + free_institutional_context*institutional_risk_weight",
        "components": [
            {
                **component,
                "contribution": round(component["score"] * component["weight"], 4),
            }
            for component in components
        ],
        "raw": round(raw, 4),
        "combined": combined,
        "score_percent": score_percent,
        "score_percent_note": "0% is strongly avoid, 50% is neutral, 100% is strongest deterministic setup before hard gates.",
        "clamped": combined != raw,
    }


def _sentiment_context(score: float, detail: dict[str, Any] | None) -> dict[str, Any]:
    detail = detail or {}
    confidence = float(detail.get("confidence", 0.0) or 0.0)
    headlines = detail.get("headlines") or []
    status = "DATA_MISSING" if abs(float(score or 0.0)) < 1e-12 or (confidence <= 0.0 and not headlines) else "AVAILABLE"
    return {
        "score": score,
        "status": status,
        "confidence": confidence,
        "headline_count": len(headlines),
        "headlines": headlines[:8],
        "events": (detail.get("events") or [])[:8],
        "asof": detail.get("asof"),
    }


def _position_context(position: dict[str, Any] | None, quote: Quote) -> dict[str, Any]:
    if not position:
        return {"qty": 0, "avg_price": 0, "market_price": quote.price}

    output = {
        "symbol": position.get("symbol", quote.symbol),
        "qty": position.get("qty", 0),
        "avg_price": position.get("avg_price", 0),
        "market_price": position.get("market_price", quote.price),
        "realized_pnl": position.get("realized_pnl", 0.0),
        "updated_at": position.get("updated_at"),
        "strategy": position.get("strategy", "unknown"),
    }
    details = _json_object(position.get("details_json"))
    opened = details.get("opened_from_decision") or details.get("decision") or {}
    if isinstance(opened, dict):
        output["opened_action"] = opened.get("action")
        output["opened_confidence"] = opened.get("confidence")
        output["opened_reason"] = _short_text(opened.get("reason"), 320)
    for key in ("exit_plan", "trade_plan", "stop_loss", "take_profit", "trailing_stop"):
        if key in details:
            output[key] = details[key]
    return output


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _short_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _institutional_score(context: dict[str, Any]) -> float:
    institutional = context.get("institutional_context") or {}
    score = float((institutional.get("market_bias") or {}).get("score", 0.0) or 0.0)
    symbol = str(context.get("symbol") or "").upper()
    flags = (institutional.get("symbol_flags") or {}).get(symbol, {})
    if flags.get("asm"):
        score -= 0.35
    if flags.get("gsm"):
        score -= 0.45
    if flags.get("fno_ban"):
        score -= 0.25
    return round(max(min(score, 1.0), -1.0), 4)


def _delivery_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    delivery = full.get("delivery_accumulation") or {}
    return round(max(min(float(delivery.get("delivery_score") or 0.0), 1.0), -1.0), 4)


def _sector_rotation_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    sector = full.get("sector_rotation") or {}
    return round(max(min(float(sector.get("sector_rotation_score") or 0.0), 1.0), -1.0), 4)


def _stage_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    stage = full.get("stage_analysis") or {}
    name = stage.get("stage")
    confidence = stage.get("stage_confidence")
    if name == "Stage2_Markup" and confidence == "high":
        return 0.8
    if name == "Stage2_Markup" and confidence == "medium":
        return 0.5
    if name == "Stage1_Base":
        return 0.1
    if name == "Stage3_Distribution":
        return -0.7
    if name == "Stage4_Decline":
        return -0.9
    return 0.0


def _divergence_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    divergence = full.get("price_volume_divergence") or {}
    return round(max(min(float(divergence.get("divergence_score") or 0.0), 1.0), -1.0), 4)


def _entry_quality_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    entry = full.get("entry_quality") or {}
    quality = float(entry.get("quality_score") or 0.0)
    return round(max(min((quality * 1.5) - 0.5, 1.0), -1.0), 4)


def _full_spectrum_score(context: dict[str, Any]) -> float:
    full = context.get("full_spectrum_analysis") or {}
    confluence = full.get("confluence_score") or {}
    risk = full.get("risk_overrides") or {}
    liquidity = full.get("liquidity_profile") or {}
    delivery = full.get("delivery_accumulation") or {}
    relative_strength = full.get("relative_strength") or {}
    corporate_risk = full.get("corporate_event_risk") or {}
    options_oi = full.get("options_oi") or {}
    backtest = full.get("backtest_snapshot") or {}
    conflicts = full.get("signal_conflicts") or {}
    scorecard = full.get("institutional_scorecard") or {}
    sector = full.get("sector_rotation") or {}
    confluence_total = int(confluence.get("total") or 0)

    total = float(confluence.get("total") or 0.0)
    score = max(min((total - 10.0) / 12.0, 0.75), -0.45)
    if scorecard.get("total_score") is not None:
        score = max(min((float(scorecard.get("total_score") or 0.0) - 60.0) / 25.0, 0.9), -0.6)

    if risk.get("no_new_longs"):
        score -= 0.35
    if scorecard.get("hard_veto", {}).get("failed"):
        score -= 0.35
    if scorecard.get("buy_ready"):
        score += 0.15
    if liquidity.get("liquidity_tier") == "illiquid":
        score -= 0.3
    elif liquidity.get("liquidity_tier") == "thin":
        score -= 0.12
    elif liquidity.get("liquidity_tier") in {"tradeable", "strong"}:
        score += 0.08
    if liquidity.get("circuit_risk_proxy"):
        score -= 0.2
    if delivery.get("bias") in {"accumulation", "volume_accumulation_proxy"}:
        score += 0.12
    elif delivery.get("bias") in {"distribution", "volume_distribution_proxy"}:
        score -= 0.18
    if relative_strength.get("bias") == "outperforming":
        score += 0.12
    elif relative_strength.get("bias") == "underperforming":
        score -= 0.12
    if corporate_risk.get("high_impact_risk"):
        score -= 0.3
    if options_oi.get("bias") == "put_heavy_supportive":
        score += 0.06
    elif options_oi.get("bias") == "call_heavy_caution":
        score -= 0.1
    elif options_oi.get("buy_suppressed"):
        score -= 0.35
    expectancy = backtest.get("expectancy")
    if expectancy is not None:
        score += max(min(float(expectancy) / 10.0, 0.12), -0.12)
    if conflicts.get("severity") == "high":
        score -= 0.3
    elif conflicts.get("severity") == "medium":
        score -= 0.12
    if sector.get("sector_tailwind"):
        score += 0.15
    if sector.get("sector_headwind"):
        score -= 0.25
    if sector.get("sector_tier") == "bottom_quartile" and confluence_total < 18:
        score -= 0.15
    score += float((full.get("price_volume_divergence") or {}).get("divergence_score") or 0.0)
    return round(max(min(score, 1.0), -1.0), 4)


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
    atr = _mean(true_ranges) if true_ranges else 0.0
    atr_pct = (atr / last.close) * 100 if last.close else 0.0
    volumes = [candle.volume for candle in recent[:-1] if candle.volume]
    volume_ratio = last.volume / _mean(volumes) if volumes else None
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
