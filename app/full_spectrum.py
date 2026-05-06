from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

from .models import Candle, Quote


def full_spectrum_analysis(
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    technical: dict[str, Any],
    candle_tools: dict[str, Any],
    strategy_signals: list[dict[str, Any]],
    sentiment_score: float,
    global_context: dict[str, Any],
    risk_limits: dict[str, Any],
) -> dict[str, Any]:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    data_quality = _data_quality(candles)
    indicators = _indicator_suite(candles)
    key_levels = _key_levels(candles, quote.price)
    trend_context = _trend_context(closes, highs, lows, indicators)
    fib = _fib_levels(candles)
    candlestick_v2 = _candlestick_v2(candles, candle_tools)
    chart_patterns = _chart_patterns(candles)
    institutional = _institutional_structure(candles, quote.price, key_levels)
    filters = _primary_filters(quote.price, indicators, key_levels, volumes, technical)
    confluence = _confluence_score(
        trend_context=trend_context,
        indicators=indicators,
        chart_patterns=chart_patterns,
        candlestick_v2=candlestick_v2,
        institutional=institutional,
        sentiment_score=sentiment_score,
        global_context=global_context,
        strategy_signals=strategy_signals,
        filters=filters,
    )
    risk_overrides = _risk_overrides(global_context, indicators, confluence, data_quality, risk_limits)
    trade_plan = _trade_plan(quote.price, key_levels, indicators, confluence, risk_limits)
    signal_plan = _signal_plan(row, quote.price, trend_context, confluence, trade_plan, risk_overrides)
    monitoring = _monitoring_checklist(quote.price, trade_plan, confluence, risk_overrides)
    return {
        "version": "opentrade-full-spectrum-v2",
        "symbol": row.get("symbol"),
        "data_quality": data_quality,
        "primary_filters": filters,
        "signal_plan": signal_plan,
        "trend_context": trend_context,
        "key_levels": key_levels,
        "fibonacci": fib,
        "indicator_suite": indicators,
        "candlestick_v2": candlestick_v2,
        "chart_patterns": chart_patterns,
        "institutional_structure": institutional,
        "institutional_flow": _institutional_flow_placeholder(),
        "news_sentiment": _news_sentiment(sentiment_score),
        "confluence_score": confluence,
        "risk_overrides": risk_overrides,
        "trade_plan": trade_plan,
        "monitoring_checklist": monitoring,
        "data_gaps": _data_gaps(candles, row),
    }


def _data_quality(candles: list[Candle]) -> dict[str, Any]:
    count = len(candles)
    return {
        "candle_count": count,
        "has_intraday_or_daily_history": count >= 30,
        "has_50_period_context": count >= 50,
        "has_200_period_context": count >= 200,
        "coverage": "strong" if count >= 200 else "usable" if count >= 50 else "limited" if count >= 30 else "thin",
    }


def _indicator_suite(candles: list[Candle]) -> dict[str, Any]:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    atr = _atr(candles, 14)
    atr_pct = (atr / closes[-1]) * 100 if atr and closes else None
    macd_line, signal_line, histogram = _macd(closes)
    bb = _bollinger(closes)
    return {
        "moving_averages": {
            "ema_9": _round(_ema(closes, 9)),
            "ema_21": _round(_ema(closes, 21)),
            "sma_20": _round(_sma(closes, 20)),
            "sma_50": _round(_sma(closes, 50)),
            "sma_100": _round(_sma(closes, 100)),
            "sma_200": _round(_sma(closes, 200)),
        },
        "adx": _round(_adx(candles, 14)),
        "rsi_14": _round(_rsi(closes, 14)),
        "macd": {
            "line": _round(macd_line),
            "signal": _round(signal_line),
            "histogram": _round(histogram),
            "bias": "bullish" if histogram and histogram > 0 else "bearish" if histogram and histogram < 0 else "neutral",
        },
        "bollinger": bb,
        "atr": _round(atr),
        "atr_pct": _round(atr_pct),
        "obv_slope": _round(_obv_slope(closes, volumes)),
        "cmf_20": _round(_cmf(highs, lows, closes, volumes, 20)),
        "volume_ratio_20": _round(_volume_ratio(volumes, 20)),
    }


def _key_levels(candles: list[Candle], price: float) -> dict[str, Any]:
    if not candles:
        return {}
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    return {
        "period_high": _round(max(highs)),
        "period_low": _round(min(lows)),
        "period_high_distance_pct": _round(((max(highs) - price) / price) * 100 if price else None),
        "period_low_distance_pct": _round(((price - min(lows)) / price) * 100 if price else None),
        "prev_swing_high": _round(max(highs[-20:-1]) if len(highs) >= 21 else max(highs[:-1] or highs)),
        "prev_swing_low": _round(min(lows[-20:-1]) if len(lows) >= 21 else min(lows[:-1] or lows)),
        "open_gaps": _open_gaps(candles),
        "vwap_period": _round(_vwap(candles)),
        "period_return_pct": _round(((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else None),
    }


def _trend_context(closes: list[float], highs: list[float], lows: list[float], indicators: dict[str, Any]) -> dict[str, Any]:
    ma = indicators["moving_averages"]
    adx = indicators.get("adx")
    state = _trend_state(closes, highs, lows, ma, adx)
    return {
        "daily": state,
        "weekly": _coarser_trend(closes, 5),
        "four_hour": _coarser_trend(closes, 16),
        "one_hour": _coarser_trend(closes, 4),
        "trend_age_candles": _trend_age(closes),
        "structure": _swing_structure(highs, lows),
    }


def _fib_levels(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 5:
        return {"available": False}
    high = max(candles, key=lambda candle: candle.high)
    low = min(candles, key=lambda candle: candle.low)
    swing_high = high.high
    swing_low = low.low
    span = swing_high - swing_low
    if span <= 0:
        return {"available": False}
    return {
        "available": True,
        "swing_high": _round(swing_high),
        "swing_low": _round(swing_low),
        "levels": {
            "23.6": _round(swing_high - span * 0.236),
            "38.2": _round(swing_high - span * 0.382),
            "50.0": _round(swing_high - span * 0.5),
            "61.8": _round(swing_high - span * 0.618),
            "78.6": _round(swing_high - span * 0.786),
        },
        "extensions": {
            "127.2": _round(swing_high + span * 0.272),
            "161.8": _round(swing_high + span * 0.618),
            "261.8": _round(swing_high + span * 1.618),
        },
    }


def _candlestick_v2(candles: list[Candle], candle_tools: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 3:
        return {"patterns": candle_tools.get("patterns", ["insufficient-candles"]), "score": candle_tools.get("score", 0.0)}
    last = candles[-1]
    previous = candles[-2]
    third = candles[-3]
    patterns = list(candle_tools.get("patterns", []))
    score = float(candle_tools.get("score", 0.0) or 0.0)
    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 0.01)
    upper = last.high - max(last.open, last.close)
    lower = min(last.open, last.close) - last.low
    if body / candle_range > 0.78 and last.close > last.open:
        patterns.append("bullish-marubozu-like")
        score += 0.16
    if body / candle_range > 0.78 and last.close < last.open:
        patterns.append("bearish-marubozu-like")
        score -= 0.16
    if body / candle_range < 0.18 and lower > candle_range * 0.55 and upper < candle_range * 0.15:
        patterns.append("dragonfly-doji-like")
        score += 0.12
    if body / candle_range < 0.18 and upper > candle_range * 0.55 and lower < candle_range * 0.15:
        patterns.append("gravestone-doji-like")
        score -= 0.12
    if previous.high <= third.high and previous.low >= third.low:
        patterns.append("inside-day-compression")
    if third.close < third.open and abs(previous.close - previous.open) < abs(third.close - third.open) * 0.45 and last.close > last.open and last.close > (third.open + third.close) / 2:
        patterns.append("morning-star-like")
        score += 0.24
    if third.close > third.open and abs(previous.close - previous.open) < abs(third.close - third.open) * 0.45 and last.close < last.open and last.close < (third.open + third.close) / 2:
        patterns.append("evening-star-like")
        score -= 0.24
    if len(candles) >= 4:
        recent = candles[-3:]
        if all(c.close > c.open for c in recent) and recent[-1].close > recent[-2].close > recent[-3].close:
            patterns.append("three-white-soldiers-like")
            score += 0.2
        if all(c.close < c.open for c in recent) and recent[-1].close < recent[-2].close < recent[-3].close:
            patterns.append("three-black-crows-like")
            score -= 0.2
    if abs(last.low - previous.low) / max(last.low, 0.01) < 0.003:
        patterns.append("tweezer-bottom-like")
        score += 0.08
    if abs(last.high - previous.high) / max(last.high, 0.01) < 0.003:
        patterns.append("tweezer-top-like")
        score -= 0.08
    return {
        "patterns": _unique(patterns) or ["no-clear-pattern"],
        "score": _round(max(min(score, 1.0), -1.0)),
        "reliability": _reliability(score),
        "confirmation": "confirmed" if abs(score) >= 0.35 else "pending",
    }


def _chart_patterns(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 30:
        return {"patterns": ["insufficient-history"], "score": 0.0}
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    patterns: list[dict[str, Any]] = []
    score = 0.0
    recent_high = max(highs[-20:])
    prior_high = max(highs[-40:-20]) if len(highs) >= 40 else max(highs[:-20])
    recent_low = min(lows[-20:])
    prior_low = min(lows[-40:-20]) if len(lows) >= 40 else min(lows[:-20])
    volume_ratio = _volume_ratio(volumes, 20) or 1.0
    if abs(recent_high - prior_high) / max(recent_high, 0.01) < 0.025 and closes[-1] < min(lows[-20:]) * 1.02:
        patterns.append({"name": "double-top-risk", "direction": "bearish", "status": "forming"})
        score -= 0.18
    if abs(recent_low - prior_low) / max(recent_low, 0.01) < 0.025 and closes[-1] > max(highs[-20:]) * 0.98:
        patterns.append({"name": "double-bottom-base", "direction": "bullish", "status": "forming"})
        score += 0.18
    if closes[-1] > max(highs[-15:-1]) and volume_ratio >= 1.3:
        patterns.append({"name": "range-breakout", "direction": "bullish", "status": "confirmed"})
        score += 0.22
    if closes[-1] < min(lows[-15:-1]) and volume_ratio >= 1.3:
        patterns.append({"name": "range-breakdown", "direction": "bearish", "status": "confirmed"})
        score -= 0.22
    if _range_contraction(candles):
        patterns.append({"name": "volatility-contraction-base", "direction": "neutral", "status": "forming"})
        score += 0.08
    return {"patterns": patterns or [{"name": "no-major-pattern", "direction": "neutral", "status": "none"}], "score": _round(score)}


def _institutional_structure(candles: list[Candle], price: float, key_levels: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 20:
        return {"available": False, "reason": "insufficient candles for SMC/Wyckoff approximation"}
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    closes = [candle.close for candle in candles]
    recent_range_high = max(highs[-20:])
    recent_range_low = min(lows[-20:])
    mid = (recent_range_high + recent_range_low) / 2
    sweep_high = highs[-1] > max(highs[-10:-1]) and closes[-1] < max(highs[-10:-1])
    sweep_low = lows[-1] < min(lows[-10:-1]) and closes[-1] > min(lows[-10:-1])
    bos = "bullish_bos" if closes[-1] > max(highs[-10:-1]) else "bearish_bos" if closes[-1] < min(lows[-10:-1]) else "range"
    fvg = _fair_value_gap(candles)
    wyckoff = "phase_c_spring_candidate" if sweep_low else "utad_distribution_risk" if sweep_high else "markup" if bos == "bullish_bos" else "markdown" if bos == "bearish_bos" else "range_accumulation_or_distribution"
    return {
        "available": True,
        "wyckoff_phase": wyckoff,
        "market_structure": bos,
        "liquidity_sweep": {"high_sweep": sweep_high, "low_sweep": sweep_low},
        "premium_discount": "discount" if price < mid else "premium" if price > mid else "equilibrium",
        "range": {"high": _round(recent_range_high), "low": _round(recent_range_low), "mid": _round(mid)},
        "order_block_proxy": _order_block_proxy(candles),
        "fair_value_gap": fvg,
        "liquidity_targets": {
            "upside": key_levels.get("prev_swing_high"),
            "downside": key_levels.get("prev_swing_low"),
        },
    }


def _primary_filters(
    price: float,
    indicators: dict[str, Any],
    key_levels: dict[str, Any],
    volumes: list[float],
    technical: dict[str, Any],
) -> dict[str, Any]:
    ma = indicators["moving_averages"]
    rsi = indicators.get("rsi_14")
    atr_pct = indicators.get("atr_pct")
    period_high = key_levels.get("period_high")
    return {
        "above_200dma": None if ma.get("sma_200") is None else price > ma["sma_200"],
        "adx_min_20": None if indicators.get("adx") is None else indicators["adx"] >= 20,
        "volume_ratio_min_1_5": None if indicators.get("volume_ratio_20") is None else indicators["volume_ratio_20"] >= 1.5,
        "rsi_40_70": None if rsi is None else 40 <= rsi <= 70,
        "within_25pct_period_high": None if not period_high else ((period_high - price) / price) * 100 <= 25,
        "atr_pct_1_5_to_6": None if atr_pct is None else 1.5 <= atr_pct <= 6.0,
        "trend_not_down": technical.get("trend") != "downtrend",
        "unavailable_filters": [
            "delivery_pct",
            "market_cap",
            "avg_daily_value",
            "fo_ban",
            "promoter_pledge",
            "asm_gsm_surveillance",
            "earnings_48h",
        ],
    }


def _confluence_score(
    trend_context: dict[str, Any],
    indicators: dict[str, Any],
    chart_patterns: dict[str, Any],
    candlestick_v2: dict[str, Any],
    institutional: dict[str, Any],
    sentiment_score: float,
    global_context: dict[str, Any],
    strategy_signals: list[dict[str, Any]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    macro = 0
    if trend_context["daily"] in {"STRONG_UPTREND", "WEAK_UPTREND"}:
        macro += 2
    if global_context.get("risk_score", 0) > 0.15:
        macro += 1
    if filters.get("trend_not_down"):
        macro += 1
    if global_context.get("regime") == "risk-on":
        macro += 1
    if filters.get("within_25pct_period_high"):
        macro += 1

    technical = 0
    if institutional.get("wyckoff_phase") in {"phase_c_spring_candidate", "markup"}:
        technical += 2
    if institutional.get("fair_value_gap", {}).get("present"):
        technical += 1
    if institutional.get("liquidity_sweep", {}).get("low_sweep"):
        technical += 2
    technical += min(2, round(abs(chart_patterns.get("score", 0)) * 6))
    if institutional.get("premium_discount") == "discount":
        technical += 1
    if filters.get("above_200dma") or filters.get("above_200dma") is None and indicators["moving_averages"].get("sma_50"):
        technical += 1

    candle = 0
    if abs(candlestick_v2.get("score", 0)) >= 0.35:
        candle += 2
    if indicators.get("macd", {}).get("bias") == "bullish" and (indicators.get("obv_slope") or 0) >= 0:
        candle += 1
    if filters.get("volume_ratio_min_1_5"):
        candle += 1
    if filters.get("rsi_40_70"):
        candle += 1

    news = 0
    if sentiment_score > 0.15:
        news += 1
    if sentiment_score > -0.25:
        news += 1
    if any(signal.get("direction") == "BUY" for signal in strategy_signals):
        news += 1
    if filters.get("volume_ratio_min_1_5"):
        news += 1

    macro = min(macro, 8)
    technical = min(technical, 10)
    candle = min(candle, 5)
    news = min(news, 4)
    total = macro + technical + candle + news
    return {
        "total": total,
        "max": 26,
        "normalized": _round(total / 26),
        "tier": _signal_tier(total),
        "breakdown": {
            "macro_flow": {"score": macro, "max": 8},
            "technical_structure": {"score": technical, "max": 10},
            "candle_timing": {"score": candle, "max": 5},
            "news_sentiment": {"score": news, "max": 4},
        },
    }


def _risk_overrides(
    global_context: dict[str, Any],
    indicators: dict[str, Any],
    confluence: dict[str, Any],
    data_quality: dict[str, Any],
    risk_limits: dict[str, Any],
) -> dict[str, Any]:
    atr_pct = indicators.get("atr_pct")
    flags = []
    if global_context.get("regime") == "risk-off" and global_context.get("risk_score", 0) <= -0.28:
        flags.append("global_risk_off_no_new_longs")
    if atr_pct is not None and atr_pct > 6:
        flags.append("atr_above_swing_suitability_reduce_size")
    if confluence.get("total", 0) < 10:
        flags.append("confluence_below_watch_threshold")
    if data_quality.get("coverage") in {"thin", "limited"}:
        flags.append("limited_history_use_smaller_size")
    no_new_longs = "global_risk_off_no_new_longs" in flags
    return {
        "flags": flags,
        "absolute_no_trade_conditions_checked": [
            "global risk-off proxy",
            "ATR swing suitability",
            "minimum confluence",
            "data-quality coverage",
        ],
        "no_new_longs": no_new_longs,
        "size_multiplier": _conviction_size_multiplier(confluence.get("total", 0), flags),
        "risk_per_trade_pct": min(float(risk_limits.get("max_order_value_pct", 0.04) or 0.04), 0.02),
    }


def _trade_plan(
    price: float,
    key_levels: dict[str, Any],
    indicators: dict[str, Any],
    confluence: dict[str, Any],
    risk_limits: dict[str, Any],
) -> dict[str, Any]:
    atr = indicators.get("atr") or price * 0.02
    stop_pct = min(max(float(risk_limits.get("stop_loss_pct", 0.035) or 0.035), 0.005), 0.04)
    stop = min(price - atr * 1.2, price * (1 - stop_pct))
    risk = max(price - stop, price * 0.005)
    return {
        "direction": "LONG" if confluence.get("total", 0) >= 14 else "WATCH" if confluence.get("total", 0) >= 10 else "NO_SIGNAL",
        "horizon": "swing_3_to_7_days",
        "entry_zone": [_round(price * 0.995), _round(price * 1.005)],
        "stop_loss": _round(stop),
        "targets": [
            {"label": "T1", "price": _round(price + risk * 1.5), "rr": 1.5},
            {"label": "T2", "price": _round(price + risk * 2.5), "rr": 2.5},
            {"label": "T3", "price": _round(key_levels.get("prev_swing_high") or price + risk * 3.5), "rr": "structure"},
        ],
        "invalidation": {
            "chart": _round(stop),
            "macro": "risk-off regime or high-impact event within 48h",
            "news": "strong negative regulatory/credit/promoter pledge event",
        },
        "position_sizing": {
            "max_capital_at_risk_pct": min(float(risk_limits.get("max_order_value_pct", 0.04) or 0.04), 0.02),
            "conviction_tier": confluence.get("tier"),
            "sizing_note": "paper broker still enforces max order, max position, cash, and daily loss limits",
        },
    }


def _signal_plan(
    row: dict[str, Any],
    price: float,
    trend_context: dict[str, Any],
    confluence: dict[str, Any],
    trade_plan: dict[str, Any],
    risk_overrides: dict[str, Any],
) -> dict[str, Any]:
    direction = trade_plan.get("direction", "NO_SIGNAL")
    return {
        "instrument": f"{row.get('exchange', 'NSE')}:{row.get('symbol')}",
        "sector": row.get("sector") or "unknown",
        "current_price": _round(price),
        "direction": direction,
        "decision_readiness": "actionable" if direction == "LONG" and not risk_overrides.get("no_new_longs") else "monitor_only",
        "confluence": f"{confluence.get('total', 0)}/{confluence.get('max', 26)} {confluence.get('tier', 'NO_SIGNAL')}",
        "trend_alignment": {
            "weekly": trend_context.get("weekly"),
            "daily": trend_context.get("daily"),
            "four_hour": trend_context.get("four_hour"),
            "one_hour": trend_context.get("one_hour"),
        },
        "next_review": "next agent cycle",
    }


def _monitoring_checklist(
    price: float,
    trade_plan: dict[str, Any],
    confluence: dict[str, Any],
    risk_overrides: dict[str, Any],
) -> list[str]:
    entry = trade_plan.get("entry_zone") or []
    stop = trade_plan.get("stop_loss")
    checklist = [
        f"Re-score confluence every cycle; current score is {confluence.get('total', 0)}/26.",
        "Refresh quote, candles, global regime, and latest symbol news before acting.",
        "Block new long entries if global risk-off or no-new-longs override appears.",
    ]
    if entry:
        checklist.append(f"Monitor entry zone around {entry[0]} to {entry[-1]} versus current price {round(price, 3)}.")
    if stop:
        checklist.append(f"Hard invalidation if price trades through stop {stop}.")
    if risk_overrides.get("flags"):
        checklist.append(f"Risk flags active: {', '.join(risk_overrides['flags'])}.")
    checklist.append("Trail or re-evaluate after T1; paper broker risk exits remain active.")
    return checklist


def _institutional_flow_placeholder() -> dict[str, Any]:
    return {
        "available": False,
        "fii_dii_flow": None,
        "pcr_oi": None,
        "delivery_percentage": None,
        "block_bulk_deals": None,
        "note": "requires licensed/exchange datasets; not fabricated from price candles",
    }


def _news_sentiment(sentiment_score: float) -> dict[str, Any]:
    if sentiment_score > 0.2:
        bias = "bullish"
    elif sentiment_score < -0.2:
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "aggregate_score": _round(sentiment_score),
        "bias": bias,
        "source": "OpenTrade rotating news sentiment service",
    }


def _conviction_size_multiplier(confluence_total: int, flags: list[str]) -> float:
    if flags:
        return 0.5
    if confluence_total >= 22:
        return 2.0
    if confluence_total >= 18:
        return 1.5
    if confluence_total >= 14:
        return 1.0
    return 0.0


def _data_gaps(candles: list[Candle], row: dict[str, Any]) -> list[str]:
    gaps = []
    if len(candles) < 200:
        gaps.append("200-period trend and true 52-week context unavailable")
    gaps.extend(
        [
            "FII/DII stock-level flows require licensed/exchange datasets",
            "PCR/OI requires derivatives chain data",
            "delivery percentage requires NSE/BSE bhavcopy integration",
            "macro calendar requires a dedicated events feed",
        ]
    )
    if not row.get("sector"):
        gaps.append("sector metadata missing")
    return gaps


def _trend_state(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    ma: dict[str, float | None],
    adx: float | None,
) -> str:
    if len(closes) < 20:
        return "SIDEWAYS"
    structure = _swing_structure(highs, lows)
    above_fast = ma.get("sma_20") is not None and closes[-1] > ma["sma_20"]
    above_slow = ma.get("sma_50") is not None and closes[-1] > ma["sma_50"]
    if structure == "HH_HL" and above_fast and (above_slow or ma.get("sma_50") is None):
        return "STRONG_UPTREND" if adx and adx >= 30 else "WEAK_UPTREND"
    if structure == "LH_LL" and not above_fast:
        return "STRONG_DOWNTREND" if adx and adx >= 30 else "WEAK_DOWNTREND"
    return "SIDEWAYS"


def _coarser_trend(closes: list[float], step: int) -> str:
    sampled = closes[::step] if step > 1 else closes
    if len(sampled) < 5:
        return "insufficient"
    return "up" if sampled[-1] > sampled[-5] else "down" if sampled[-1] < sampled[-5] else "sideways"


def _swing_structure(highs: list[float], lows: list[float]) -> str:
    if len(highs) < 10:
        return "UNKNOWN"
    first_high = max(highs[-10:-5])
    second_high = max(highs[-5:])
    first_low = min(lows[-10:-5])
    second_low = min(lows[-5:])
    if second_high > first_high and second_low > first_low:
        return "HH_HL"
    if second_high < first_high and second_low < first_low:
        return "LH_LL"
    return "RANGE"


def _trend_age(closes: list[float]) -> int | None:
    if len(closes) < 10:
        return None
    direction = 1 if closes[-1] >= closes[-5] else -1
    age = 0
    for previous, current in zip(reversed(closes[:-1]), reversed(closes[1:])):
        if direction == 1 and current >= previous:
            age += 1
        elif direction == -1 and current <= previous:
            age += 1
        else:
            break
    return age


def _open_gaps(candles: list[Candle]) -> list[dict[str, Any]]:
    gaps = []
    for previous, current in zip(candles[-20:-1], candles[-19:]):
        if current.low > previous.high:
            gaps.append({"type": "UP_GAP", "range": [_round(previous.high), _round(current.low)], "filled": False})
        elif current.high < previous.low:
            gaps.append({"type": "DOWN_GAP", "range": [_round(current.high), _round(previous.low)], "filled": False})
    return gaps[-5:]


def _fair_value_gap(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 3:
        return {"present": False}
    a, _, c = candles[-3], candles[-2], candles[-1]
    if c.low > a.high:
        return {"present": True, "type": "bullish_fvg", "range": [_round(a.high), _round(c.low)]}
    if c.high < a.low:
        return {"present": True, "type": "bearish_fvg", "range": [_round(c.high), _round(a.low)]}
    return {"present": False}


def _order_block_proxy(candles: list[Candle]) -> dict[str, Any] | None:
    for candle in reversed(candles[-12:]):
        body_pct = abs(candle.close - candle.open) / max(candle.high - candle.low, 0.01)
        if body_pct > 0.55:
            direction = "bullish" if candle.close < candle.open else "bearish"
            return {"type": f"{direction}_order_block_proxy", "range": [_round(candle.low), _round(candle.high)]}
    return None


def _range_contraction(candles: list[Candle]) -> bool:
    if len(candles) < 30:
        return False
    ranges = [(candle.high - candle.low) / candle.close for candle in candles[-30:] if candle.close]
    return bool(ranges) and mean(ranges[-10:]) < mean(ranges[:10]) * 0.7


def _macd(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(values) < 35:
        return None, None, None
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)
    macd = [f - s for f, s in zip(fast[-len(slow) :], slow)]
    signal = _ema_series(macd, 9)
    if not signal:
        return None, None, None
    return macd[-1], signal[-1], macd[-1] - signal[-1]


def _bollinger(values: list[float]) -> dict[str, Any]:
    if len(values) < 20:
        return {"available": False}
    recent = values[-20:]
    basis = mean(recent)
    sigma = pstdev(recent) if len(recent) > 1 else 0
    upper = basis + (2 * sigma)
    lower = basis - (2 * sigma)
    width_pct = ((upper - lower) / basis) * 100 if basis else None
    return {
        "available": True,
        "basis": _round(basis),
        "upper": _round(upper),
        "lower": _round(lower),
        "width_pct": _round(width_pct),
        "squeeze": bool(width_pct is not None and width_pct < 6),
    }


def _adx(candles: list[Candle], window: int) -> float | None:
    if len(candles) <= window + 1:
        return None
    plus_dm = []
    minus_dm = []
    true_ranges = []
    for previous, current in zip(candles[-(window + 1) :], candles[-window:]):
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    tr = sum(true_ranges) or 1
    plus_di = 100 * (sum(plus_dm) / tr)
    minus_di = 100 * (sum(minus_dm) / tr)
    return 100 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1)


def _atr(candles: list[Candle], window: int) -> float | None:
    if len(candles) <= window:
        return None
    ranges = [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in zip(candles[-(window + 1) :], candles[-window:])
    ]
    return mean(ranges) if ranges else None


def _cmf(highs: list[float], lows: list[float], closes: list[float], volumes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    mfv = []
    for high, low, close, volume in zip(highs[-window:], lows[-window:], closes[-window:], volumes[-window:]):
        spread = high - low
        multiplier = ((close - low) - (high - close)) / spread if spread else 0
        mfv.append(multiplier * volume)
    total_volume = sum(volumes[-window:]) or 1
    return sum(mfv) / total_volume


def _obv_slope(closes: list[float], volumes: list[float]) -> float | None:
    if len(closes) < 10:
        return None
    obv = 0.0
    series = []
    for previous, current, volume in zip(closes[:-1], closes[1:], volumes[1:]):
        if current > previous:
            obv += volume
        elif current < previous:
            obv -= volume
        series.append(obv)
    if len(series) < 5:
        return None
    base = abs(series[-5]) or 1
    return (series[-1] - series[-5]) / base


def _volume_ratio(volumes: list[float], window: int) -> float | None:
    if len(volumes) < window + 1:
        return None
    average = mean(volumes[-(window + 1) : -1])
    return volumes[-1] / average if average else None


def _vwap(candles: list[Candle]) -> float | None:
    total_volume = sum(candle.volume for candle in candles)
    if not total_volume:
        return None
    return sum(((candle.high + candle.low + candle.close) / 3) * candle.volume for candle in candles) / total_volume


def _rsi(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    gains = []
    losses = []
    for previous, current in zip(values[-(window + 1) :], values[-window:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _sma(values: list[float], window: int) -> float | None:
    return mean(values[-window:]) if len(values) >= window else None


def _ema(values: list[float], window: int) -> float | None:
    series = _ema_series(values, window)
    return series[-1] if series else None


def _ema_series(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return []
    alpha = 2 / (window + 1)
    ema = mean(values[:window])
    series = [ema]
    for value in values[window:]:
        ema = (value * alpha) + (ema * (1 - alpha))
        series.append(ema)
    return series


def _signal_tier(total: int) -> str:
    if total >= 22:
        return "MAXIMUM_CONVICTION"
    if total >= 18:
        return "HIGH_CONVICTION"
    if total >= 14:
        return "TRADE_SIGNAL"
    if total >= 10:
        return "WATCHLIST"
    return "NO_SIGNAL"


def _reliability(score: float) -> str:
    absolute = abs(score)
    if absolute >= 0.6:
        return "very_high"
    if absolute >= 0.4:
        return "high"
    if absolute >= 0.22:
        return "medium"
    return "low"


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None
