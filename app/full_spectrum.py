from __future__ import annotations

import re
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
    institutional_context: dict[str, Any],
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
    flow = _institutional_flow(row, institutional_context)
    liquidity = _liquidity_profile(candles, quote.price)
    fundamental = _fundamental_quality(row, flow)
    corporate_risk = _corporate_event_risk(flow)
    delivery = _delivery_accumulation(flow, candles)
    relative_strength = _relative_strength(closes, global_context)
    options_oi = _options_oi_layer(flow)
    backtest = _backtest_snapshot(candles)
    filters = _primary_filters(quote.price, indicators, key_levels, volumes, technical, liquidity, corporate_risk)
    confluence = _confluence_score(
        trend_context=trend_context,
        indicators=indicators,
        chart_patterns=chart_patterns,
        candlestick_v2=candlestick_v2,
        institutional=institutional,
        institutional_flow=flow,
        sentiment_score=sentiment_score,
        global_context=global_context,
        strategy_signals=strategy_signals,
        filters=filters,
        liquidity=liquidity,
        delivery=delivery,
        relative_strength=relative_strength,
        backtest=backtest,
    )
    conflicts = _signal_conflicts(
        technical=technical,
        sentiment_score=sentiment_score,
        global_context=global_context,
        confluence=confluence,
        liquidity=liquidity,
        corporate_risk=corporate_risk,
        options_oi=options_oi,
    )
    trade_plan = _trade_plan(quote.price, key_levels, indicators, confluence, risk_limits, liquidity, backtest)
    scorecard = _institutional_scorecard(
        price=quote.price,
        data_quality=data_quality,
        indicators=indicators,
        filters=filters,
        trend_context=trend_context,
        liquidity=liquidity,
        relative_strength=relative_strength,
        delivery=delivery,
        fundamental=fundamental,
        corporate_risk=corporate_risk,
        options_oi=options_oi,
        backtest=backtest,
        conflicts=conflicts,
        sentiment_score=sentiment_score,
        global_context=global_context,
        institutional_flow=flow,
        confluence=confluence,
        trade_plan=trade_plan,
    )
    risk_overrides = _risk_overrides(
        global_context,
        flow,
        indicators,
        confluence,
        data_quality,
        risk_limits,
        liquidity,
        corporate_risk,
        conflicts,
        scorecard,
    )
    signal_plan = _signal_plan(row, quote.price, trend_context, confluence, trade_plan, risk_overrides, scorecard)
    monitoring = _monitoring_checklist(quote.price, trade_plan, confluence, risk_overrides, scorecard)
    return {
        "version": "opentrade-full-spectrum-v2",
        "symbol": row.get("symbol"),
        "requirement_coverage": _requirement_coverage(data_quality, global_context, institutional_context),
        "data_quality": data_quality,
        "primary_filters": filters,
        "signal_plan": signal_plan,
        "trend_context": trend_context,
        "key_levels": key_levels,
        "fibonacci": fib,
        "indicator_suite": indicators,
        "liquidity_profile": liquidity,
        "relative_strength": relative_strength,
        "candlestick_v2": candlestick_v2,
        "chart_patterns": chart_patterns,
        "institutional_structure": institutional,
        "institutional_flow": flow,
        "fundamental_quality": fundamental,
        "corporate_event_risk": corporate_risk,
        "delivery_accumulation": delivery,
        "options_oi": options_oi,
        "backtest_snapshot": backtest,
        "signal_conflicts": conflicts,
        "institutional_scorecard": scorecard,
        "news_sentiment": _news_sentiment(sentiment_score),
        "confluence_score": confluence,
        "risk_overrides": risk_overrides,
        "trade_plan": trade_plan,
        "monitoring_checklist": monitoring,
        "data_gaps": _data_gaps(candles, row, institutional_context),
    }


def _data_quality(candles: list[Candle]) -> dict[str, Any]:
    count = len(candles)
    score = 0
    if count >= 30:
        score += 35
    if count >= 50:
        score += 20
    if count >= 96:
        score += 20
    if count >= 200:
        score += 15
    if any(candle.volume for candle in candles):
        score += 10
    return {
        "candle_count": count,
        "has_intraday_or_daily_history": count >= 30,
        "has_50_period_context": count >= 50,
        "has_200_period_context": count >= 200,
        "coverage": "strong" if count >= 200 else "usable" if count >= 50 else "limited" if count >= 30 else "thin",
        "score": min(score, 100),
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
    stochastic_k, stochastic_d = _stochastic(highs, lows, closes)
    cci_20 = _cci(candles, 20)
    ichimoku = _ichimoku(highs, lows, closes)
    volume_profile = _volume_profile_proxy(candles)
    divergence = _divergence_proxy(closes, highs, lows)
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
        "stochastic": {
            "k": _round(stochastic_k),
            "d": _round(stochastic_d),
            "bias": "overbought"
            if stochastic_k is not None and stochastic_k >= 80
            else "oversold"
            if stochastic_k is not None and stochastic_k <= 20
            else "neutral",
        },
        "cci_20": _round(cci_20),
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
        "ichimoku": ichimoku,
        "volume_profile_proxy": volume_profile,
        "divergence_proxy": divergence,
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
    liquidity: dict[str, Any],
    corporate_risk: dict[str, Any],
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
        "liquidity_tradeable": liquidity.get("tradeable"),
        "not_circuit_risk": not liquidity.get("circuit_risk_proxy"),
        "no_high_impact_event_risk": not corporate_risk.get("high_impact_risk"),
        "unavailable_filters": [
            "market_cap",
            "fo_ban",
            "promoter_pledge",
            "earnings_48h",
        ],
    }


def _confluence_score(
    trend_context: dict[str, Any],
    indicators: dict[str, Any],
    chart_patterns: dict[str, Any],
    candlestick_v2: dict[str, Any],
    institutional: dict[str, Any],
    institutional_flow: dict[str, Any],
    sentiment_score: float,
    global_context: dict[str, Any],
    strategy_signals: list[dict[str, Any]],
    filters: dict[str, Any],
    liquidity: dict[str, Any],
    delivery: dict[str, Any],
    relative_strength: dict[str, Any],
    backtest: dict[str, Any],
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
    flow_bias = institutional_flow.get("market_bias", {}).get("score")
    if flow_bias is not None and flow_bias > 0.1:
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
    if indicators.get("ichimoku", {}).get("bias") == "bullish_cloud":
        technical += 1
    if indicators.get("divergence_proxy", {}).get("signal") == "bullish_divergence":
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
    if liquidity.get("tradeable") and not liquidity.get("circuit_risk_proxy"):
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
    if delivery.get("bias") == "accumulation":
        news += 1
    if relative_strength.get("bias") == "outperforming":
        news += 1
    if backtest.get("expectancy") and backtest.get("expectancy") > 0:
        news += 1
    if (
        institutional_flow.get("symbol_flags", {}).get("official_announcements_count", 0) > 0
        and sentiment_score > 0.15
    ):
        news += 1

    macro = min(macro, 8)
    technical = min(technical, 10)
    candle = min(candle, 5)
    news = min(news, 3)
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
            "news_sentiment": {"score": news, "max": 3},
        },
    }


def _risk_overrides(
    global_context: dict[str, Any],
    institutional_flow: dict[str, Any],
    indicators: dict[str, Any],
    confluence: dict[str, Any],
    data_quality: dict[str, Any],
    risk_limits: dict[str, Any],
    liquidity: dict[str, Any],
    corporate_risk: dict[str, Any],
    conflicts: dict[str, Any],
    scorecard: dict[str, Any],
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
    if liquidity.get("liquidity_tier") == "illiquid":
        flags.append("illiquid_stock_no_new_longs")
    elif liquidity.get("liquidity_tier") == "thin":
        flags.append("thin_liquidity_reduce_size")
    if liquidity.get("circuit_risk_proxy"):
        flags.append("possible_circuit_stock_reduce_size")
    if corporate_risk.get("high_impact_risk"):
        flags.append("corporate_event_risk_no_new_longs")
    if conflicts.get("severity") == "high":
        flags.append("conflicted_signal_no_new_longs")
    for veto in (scorecard.get("hard_veto") or {}).get("failed", []):
        flags.append(f"scorecard_{veto}_no_new_longs")
    if not scorecard.get("buy_ready") and scorecard.get("total_score", 0) < scorecard.get("minimum_entry_score", 75):
        flags.append("institutional_scorecard_below_entry_threshold")
    symbol_flags = institutional_flow.get("symbol_flags", {})
    if symbol_flags.get("asm"):
        flags.append("asm_surveillance_no_new_longs")
    if symbol_flags.get("gsm"):
        flags.append("gsm_surveillance_no_new_longs")
    if symbol_flags.get("fno_ban"):
        flags.append("fo_ban_no_new_longs")
    no_new_longs = any(flag.endswith("_no_new_longs") for flag in flags)
    return {
        "flags": flags,
        "absolute_no_trade_conditions_checked": [
            "global risk-off proxy",
            "ASM/GSM/F&O-ban public feed flags",
            "ATR swing suitability",
            "minimum confluence",
            "data-quality coverage",
            "liquidity and circuit-risk proxy",
            "corporate event risk",
            "signal conflict severity",
            "institutional scorecard hard vetoes",
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
    liquidity: dict[str, Any],
    backtest: dict[str, Any],
) -> dict[str, Any]:
    atr = indicators.get("atr") or price * 0.02
    stop_pct = min(max(float(risk_limits.get("stop_loss_pct", 0.035) or 0.035), 0.005), 0.04)
    stop = min(price - atr * 1.2, price * (1 - stop_pct))
    risk = max(price - stop, price * 0.005)
    risk_per_trade_pct = min(float(risk_limits.get("max_order_value_pct", 0.04) or 0.04), 0.01)
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
            "method": "atr_stop_risk",
            "max_capital_at_risk_pct": risk_per_trade_pct,
            "risk_per_share": _round(risk),
            "conviction_tier": confluence.get("tier"),
            "liquidity_tier": liquidity.get("liquidity_tier"),
            "backtest_expectancy": backtest.get("expectancy"),
            "sizing_note": "paper broker still enforces max order, max position, cash, and daily loss limits",
        },
        "time_stop": "exit or re-score if not moving toward T1 within 5 trading sessions",
        "trailing_stop": "after T1, trail below previous swing low or 1.2 ATR, whichever is tighter",
    }


def _signal_plan(
    row: dict[str, Any],
    price: float,
    trend_context: dict[str, Any],
    confluence: dict[str, Any],
    trade_plan: dict[str, Any],
    risk_overrides: dict[str, Any],
    scorecard: dict[str, Any],
) -> dict[str, Any]:
    direction = trade_plan.get("direction", "NO_SIGNAL")
    return {
        "instrument": f"{row.get('exchange', 'NSE')}:{row.get('symbol')}",
        "sector": row.get("sector") or "unknown",
        "current_price": _round(price),
        "direction": direction,
        "decision_readiness": "actionable"
        if direction == "LONG" and scorecard.get("buy_ready") and not risk_overrides.get("no_new_longs")
        else "monitor_only",
        "institutional_grade": scorecard.get("grade"),
        "institutional_score": f"{scorecard.get('total_score', 0)}/{scorecard.get('max_score', 100)}",
        "failed_must_pass": scorecard.get("must_pass_failed", []),
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
    scorecard: dict[str, Any],
) -> list[str]:
    entry = trade_plan.get("entry_zone") or []
    stop = trade_plan.get("stop_loss")
    checklist = [
        f"Re-score confluence every cycle; current score is {confluence.get('total', 0)}/26.",
        f"Re-score institutional scorecard every cycle; current score is {scorecard.get('total_score', 0)}/100.",
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


def _institutional_flow(row: dict[str, Any], institutional_context: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip().upper()
    feeds = institutional_context.get("feeds") or {}
    flags = (institutional_context.get("symbol_flags") or {}).get(symbol, {})
    return {
        "available": bool(institutional_context.get("enabled")),
        "source_quality": institutional_context.get("source_quality", "unavailable"),
        "symbol": symbol,
        "symbol_flags": flags,
        "market_bias": institutional_context.get("market_bias", {"score": 0.0, "rationale": []}),
        "fii_dii_flow": feeds.get("fii_dii"),
        "pcr_oi": feeds.get("option_pcr"),
        "india_indices": feeds.get("indices"),
        "asm_gsm": {
            "asm": flags.get("asm"),
            "gsm": flags.get("gsm"),
        },
        "fno_ban": flags.get("fno_ban"),
        "official_announcements": flags.get("recent_announcements", []),
        "bulk_deals": flags.get("recent_bulk_deals", []),
        "delivery_percentage": flags.get("delivery_pct"),
        "nubra_placeholders": institutional_context.get("nubra_placeholders", {}),
        "note": "free feeds are best-effort/EOD/public unless a Nubra or licensed adapter is configured",
    }


def _liquidity_profile(candles: list[Candle], price: float) -> dict[str, Any]:
    if not candles:
        return {
            "available": False,
            "tradeable": False,
            "liquidity_tier": "unknown",
            "reason": "no candles/volume data",
        }
    volumes = [float(candle.volume or 0) for candle in candles if candle.volume is not None]
    recent = candles[-20:]
    avg_volume = mean([float(candle.volume or 0) for candle in recent]) if recent else 0.0
    avg_traded_value = avg_volume * price
    last = candles[-1]
    day_move_pct = ((last.close - last.open) / last.open) * 100 if last.open else 0.0
    volume_ratio = _volume_ratio(volumes, 20) if volumes else None
    if avg_traded_value >= 50_000_000:
        tier = "strong"
    elif avg_traded_value >= 10_000_000:
        tier = "tradeable"
    elif avg_traded_value >= 2_000_000:
        tier = "thin"
    else:
        tier = "illiquid"
    circuit_risk = abs(day_move_pct) >= 8.5 or bool(volume_ratio and volume_ratio > 8)
    return {
        "available": True,
        "avg_volume_20": _round(avg_volume),
        "avg_traded_value_20": _round(avg_traded_value),
        "volume_ratio_20": _round(volume_ratio),
        "last_move_pct": _round(day_move_pct),
        "liquidity_tier": tier,
        "tradeable": tier in {"strong", "tradeable", "thin"},
        "circuit_risk_proxy": circuit_risk,
        "spread_proxy": "unknown_without_depth",
        "price_impact_risk": "high" if tier in {"illiquid", "thin"} else "normal",
    }


def _fundamental_quality(row: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    announcements = flow.get("official_announcements") or []
    negative_event = _event_text_matches(announcements, r"loss|default|resign|fraud|forensic|penalty|downgrade|pledge")
    positive_event = _event_text_matches(announcements, r"profit|order|contract|approval|dividend|bonus|split|upgrade")
    score = 0.0
    reasons = []
    if positive_event:
        score += 0.15
        reasons.append("recent positive official announcement keyword")
    if negative_event:
        score -= 0.35
        reasons.append("recent negative official announcement keyword")
    return {
        "available": bool(announcements),
        "score": _round(max(min(score, 1.0), -1.0)),
        "quality_bucket": "event_positive" if score > 0 else "event_risk" if score < 0 else "unknown",
        "checked": [
            "official announcement keyword proxy",
            "promoter pledge placeholder",
            "debt/profitability ratios placeholder",
            "ROE/ROCE/revenue growth placeholder",
        ],
        "reasons": reasons or ["fundamental ratios not connected yet"],
        "data_gaps": [
            "revenue/profit growth",
            "debt/equity",
            "ROE/ROCE",
            "promoter holding and pledge",
            "cash-flow quality",
            "quarterly trend",
        ],
    }


def _corporate_event_risk(flow: dict[str, Any]) -> dict[str, Any]:
    announcements = flow.get("official_announcements") or []
    risk_keywords = r"board meeting|results|fund raising|rights|pledge|default|resign|auditor|forensic|fraud|penalty|sebi|insolvency"
    hits = [item for item in announcements if _event_text_matches([item], risk_keywords)]
    return {
        "available": bool(announcements),
        "high_impact_risk": bool(hits),
        "events": hits[:5],
        "risk_keywords_checked": [
            "results",
            "fund raising",
            "pledge",
            "auditor/governance",
            "SEBI/regulatory",
            "default/insolvency",
        ],
        "data_gap": None if announcements else "corporate action feed returned no symbol event this cycle",
    }


def _delivery_accumulation(flow: dict[str, Any], candles: list[Candle]) -> dict[str, Any]:
    delivery_pct = flow.get("delivery_percentage")
    volume_ratio = _volume_ratio([candle.volume for candle in candles if candle.volume is not None], 20) if candles else None
    price_change = ((candles[-1].close - candles[-5].close) / candles[-5].close) * 100 if len(candles) >= 5 and candles[-5].close else None
    if delivery_pct is not None:
        bias = "accumulation" if delivery_pct >= 50 and (price_change or 0) >= 0 else "distribution" if delivery_pct >= 50 and (price_change or 0) < 0 else "neutral"
    elif volume_ratio and volume_ratio >= 1.5 and price_change and price_change > 0:
        bias = "volume_accumulation_proxy"
    elif volume_ratio and volume_ratio >= 1.5 and price_change and price_change < 0:
        bias = "volume_distribution_proxy"
    else:
        bias = "unknown"
    return {
        "delivery_pct": delivery_pct,
        "volume_ratio_20": _round(volume_ratio),
        "price_change_5_candles_pct": _round(price_change),
        "bias": bias,
        "source": "delivery feed if available, otherwise price-volume proxy",
    }


def _relative_strength(closes: list[float], global_context: dict[str, Any]) -> dict[str, Any]:
    if len(closes) < 6:
        return {"available": False, "bias": "unknown"}
    stock_return = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if closes[-6] else 0.0
    nifty_change = None
    for item in global_context.get("markets", []) or []:
        if item.get("symbol") == "^NSEI" or item.get("label") == "Nifty 50":
            nifty_change = item.get("change_pct")
            break
    rs = stock_return - float(nifty_change or 0.0)
    return {
        "available": True,
        "stock_return_5_candles_pct": _round(stock_return),
        "nifty_change_pct": _round(float(nifty_change)) if nifty_change is not None else None,
        "relative_strength_pct": _round(rs),
        "bias": "outperforming" if rs >= 1.5 else "underperforming" if rs <= -1.5 else "neutral",
        "note": "uses Nifty 50 change when available, otherwise stock-only short-window proxy",
    }


def _options_oi_layer(flow: dict[str, Any]) -> dict[str, Any]:
    pcr_feed = flow.get("pcr_oi") or {}
    items = pcr_feed.get("items") if isinstance(pcr_feed, dict) else None
    pcr_values = []
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, dict) and item.get("pcr_oi") is not None:
                pcr_values.append(float(item["pcr_oi"]))
    avg_pcr = mean(pcr_values) if pcr_values else None
    if avg_pcr is None:
        bias = "unavailable"
    elif avg_pcr > 1.2:
        bias = "put_heavy_supportive"
    elif avg_pcr < 0.75:
        bias = "call_heavy_caution"
    else:
        bias = "balanced"
    return {
        "available": avg_pcr is not None,
        "market_pcr_proxy": _round(avg_pcr),
        "bias": bias,
        "fno_ban": flow.get("fno_ban"),
        "data_gap": None if avg_pcr is not None else "stock-level OI/PCR/IV/max-pain not connected",
    }


def _backtest_snapshot(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 40:
        return {"available": False, "reason": "need at least 40 candles"}
    closes = [candle.close for candle in candles]
    trades = []
    position: dict[str, Any] | None = None
    for index in range(20, len(candles)):
        price = closes[index]
        sma20 = mean(closes[index - 20 : index])
        volume_ratio = _volume_ratio([candle.volume for candle in candles[: index + 1]], 20)
        if position is None and price > sma20 and (volume_ratio or 0) >= 1.1:
            stop = price * 0.96
            target = price * 1.08
            position = {"entry": price, "stop": stop, "target": target, "entry_index": index}
            continue
        if position is None:
            continue
        exit_reason = None
        if price <= position["stop"]:
            exit_reason = "stop"
        elif price >= position["target"]:
            exit_reason = "target"
        elif index - position["entry_index"] >= 12:
            exit_reason = "time"
        if exit_reason:
            pnl_pct = ((price - position["entry"]) / position["entry"]) * 100 if position["entry"] else 0
            trades.append({"pnl_pct": pnl_pct, "exit": exit_reason})
            position = None
    if not trades:
        return {"available": True, "trades": 0, "expectancy": 0.0, "win_rate": 0.0, "max_drawdown_proxy": 0.0}
    wins = [trade for trade in trades if trade["pnl_pct"] > 0]
    expectancy = mean([trade["pnl_pct"] for trade in trades])
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade["pnl_pct"]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return {
        "available": True,
        "engine": "simple_sma20_volume_breakout_proxy",
        "trades": len(trades),
        "win_rate": _round(len(wins) / len(trades)),
        "expectancy": _round(expectancy),
        "max_drawdown_proxy": _round(max_drawdown),
        "last_5_trades": [{"pnl_pct": _round(trade["pnl_pct"]), "exit": trade["exit"]} for trade in trades[-5:]],
    }


def _signal_conflicts(
    technical: dict[str, Any],
    sentiment_score: float,
    global_context: dict[str, Any],
    confluence: dict[str, Any],
    liquidity: dict[str, Any],
    corporate_risk: dict[str, Any],
    options_oi: dict[str, Any],
) -> dict[str, Any]:
    conflicts = []
    if technical.get("score", 0) > 0.25 and sentiment_score < -0.25:
        conflicts.append("technical_bullish_vs_negative_news")
    if confluence.get("total", 0) >= 14 and global_context.get("regime") == "risk-off":
        conflicts.append("trade_signal_vs_global_risk_off")
    if confluence.get("total", 0) >= 14 and liquidity.get("liquidity_tier") in {"illiquid", "thin"}:
        conflicts.append("trade_signal_vs_liquidity_risk")
    if confluence.get("total", 0) >= 14 and corporate_risk.get("high_impact_risk"):
        conflicts.append("trade_signal_vs_corporate_event_risk")
    if options_oi.get("bias") == "call_heavy_caution" and confluence.get("total", 0) >= 14:
        conflicts.append("trade_signal_vs_options_caution")
    severity = "high" if len(conflicts) >= 2 or any("corporate_event" in item for item in conflicts) else "medium" if conflicts else "none"
    return {
        "severity": severity,
        "conflicts": conflicts,
        "decision_rule": "high severity conflict forces HOLD/no-new-longs; medium conflict reduces size",
    }


def _institutional_scorecard(
    price: float,
    data_quality: dict[str, Any],
    indicators: dict[str, Any],
    filters: dict[str, Any],
    trend_context: dict[str, Any],
    liquidity: dict[str, Any],
    relative_strength: dict[str, Any],
    delivery: dict[str, Any],
    fundamental: dict[str, Any],
    corporate_risk: dict[str, Any],
    options_oi: dict[str, Any],
    backtest: dict[str, Any],
    conflicts: dict[str, Any],
    sentiment_score: float,
    global_context: dict[str, Any],
    institutional_flow: dict[str, Any],
    confluence: dict[str, Any],
    trade_plan: dict[str, Any],
) -> dict[str, Any]:
    symbol_flags = institutional_flow.get("symbol_flags") or {}
    hard_veto = []
    warnings = []
    atr_pct = indicators.get("atr_pct")
    if data_quality.get("candle_count", 0) < 30:
        hard_veto.append("insufficient_candle_history")
    if global_context.get("regime") == "risk-off" and float(global_context.get("risk_score") or 0.0) <= -0.28:
        hard_veto.append("global_risk_off")
    if symbol_flags.get("asm"):
        hard_veto.append("asm_surveillance")
    if symbol_flags.get("gsm"):
        hard_veto.append("gsm_surveillance")
    if symbol_flags.get("fno_ban"):
        hard_veto.append("fno_ban")
    if liquidity.get("liquidity_tier") == "illiquid":
        hard_veto.append("illiquid_execution")
    if liquidity.get("circuit_risk_proxy"):
        hard_veto.append("possible_circuit_risk")
    if corporate_risk.get("high_impact_risk"):
        hard_veto.append("high_impact_corporate_event")
    if conflicts.get("severity") == "high":
        hard_veto.append("high_signal_conflict")
    if sentiment_score <= -0.45:
        hard_veto.append("severe_negative_news_sentiment")
    if atr_pct is not None and atr_pct > 8:
        hard_veto.append("extreme_atr_volatility")

    if data_quality.get("score", 0) < 75:
        warnings.append("limited_history_reduces_confidence")
    if liquidity.get("liquidity_tier") == "thin":
        warnings.append("thin_liquidity_reduce_position_size")
    if conflicts.get("severity") == "medium":
        warnings.append("medium_signal_conflict")

    sections = [
        _market_regime_section(global_context, institutional_flow, filters),
        _liquidity_section(liquidity),
        _trend_relative_strength_section(price, indicators, filters, trend_context, relative_strength),
        _momentum_section(indicators),
        _volume_accumulation_section(indicators, delivery),
        _news_event_section(sentiment_score, fundamental, corporate_risk, conflicts),
        _derivatives_section(options_oi, institutional_flow),
        _fundamental_section(fundamental, corporate_risk),
        _backtest_section(backtest),
        _risk_reward_section(price, indicators, trade_plan),
    ]
    total = sum(section["score"] for section in sections)
    max_score = sum(section["max"] for section in sections)
    min_entry_score = 75
    strict_confluence = 16
    section_map = {section["key"]: section for section in sections}
    must_pass_failed = []
    if hard_veto:
        must_pass_failed.append("hard_veto_clear")
    if total < min_entry_score:
        must_pass_failed.append("institutional_score_min_75")
    if int(confluence.get("total", 0) or 0) < strict_confluence:
        must_pass_failed.append("confluence_min_16")
    if data_quality.get("score", 0) < 55:
        must_pass_failed.append("data_quality_min_55")
    if section_map["liquidity_execution"]["score"] < 7:
        must_pass_failed.append("liquidity_execution_min_7")
    if section_map["trend_relative_strength"]["score"] < 9:
        must_pass_failed.append("trend_relative_strength_min_9")
    if section_map["risk_reward"]["score"] < 5:
        must_pass_failed.append("risk_reward_min_5")
    if sentiment_score < -0.2:
        must_pass_failed.append("sentiment_not_bearish")

    buy_ready = not must_pass_failed
    return {
        "version": "institutional-scorecard-v1",
        "total_score": _round(total),
        "max_score": max_score,
        "normalized_score": _round(total / max_score if max_score else 0),
        "minimum_entry_score": min_entry_score,
        "strict_confluence_required": strict_confluence,
        "grade": _scorecard_grade(total),
        "buy_ready": buy_ready,
        "hard_veto": {"passed": not hard_veto, "failed": _unique(hard_veto)},
        "must_pass_failed": _unique(must_pass_failed),
        "warnings": _unique(warnings),
        "sections": section_map,
        "entry_rule": "BUY only if hard veto clear, score >=75/100, confluence >=16/26, trend/liquidity/risk-reward must-pass gates clear, and sentiment is not bearish.",
        "exit_rule": "For open positions, exit on hard stop, target/invalidation, breakdown, severe negative news, high conflict, or global risk-off.",
        "accuracy_note": "No market system can guarantee 90% accuracy; this scorecard is designed to improve expectancy by rejecting low-quality trades.",
    }


def _market_regime_section(
    global_context: dict[str, Any],
    institutional_flow: dict[str, Any],
    filters: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    evidence = []
    risk_score = float(global_context.get("risk_score") or 0.0)
    if global_context.get("regime") == "risk-on":
        score += 4
        evidence.append("global regime risk-on")
    elif global_context.get("regime") == "risk-off":
        evidence.append("global regime risk-off")
    else:
        score += 2
        evidence.append("global regime neutral/unavailable")
    if risk_score > 0.15:
        score += 2
        evidence.append("positive global risk score")
    elif risk_score > -0.15:
        score += 1
        evidence.append("global risk score neutral")
    if filters.get("trend_not_down"):
        score += 2
        evidence.append("stock trend not down")
    flow_bias = float((institutional_flow.get("market_bias") or {}).get("score") or 0.0)
    if flow_bias > 0.1:
        score += 2
        evidence.append("positive institutional/free-feed market bias")
    elif flow_bias >= -0.1:
        score += 1
        evidence.append("institutional/free-feed bias neutral")
    return _score_section("market_regime", "Market Regime", score, 10, evidence)


def _liquidity_section(liquidity: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    tier = liquidity.get("liquidity_tier")
    if tier == "strong":
        score += 6
    elif tier == "tradeable":
        score += 5
    elif tier == "thin":
        score += 2
    evidence.append(f"liquidity tier {tier or 'unknown'}")
    if not liquidity.get("circuit_risk_proxy"):
        score += 3
        evidence.append("no circuit-risk proxy")
    if (liquidity.get("avg_traded_value_20") or 0) >= 10_000_000:
        score += 2
        evidence.append("average traded value >= 1 crore")
    if liquidity.get("price_impact_risk") == "normal":
        score += 1
        evidence.append("normal price-impact proxy")
    return _score_section("liquidity_execution", "Liquidity And Execution", score, 12, evidence)


def _trend_relative_strength_section(
    price: float,
    indicators: dict[str, Any],
    filters: dict[str, Any],
    trend_context: dict[str, Any],
    relative_strength: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    evidence = []
    daily = trend_context.get("daily")
    if daily == "STRONG_UPTREND":
        score += 4
    elif daily == "WEAK_UPTREND":
        score += 3
    elif daily == "SIDEWAYS":
        score += 1
    evidence.append(f"daily trend {daily}")
    ma = indicators.get("moving_averages") or {}
    if ma.get("sma_20") is not None and price > ma["sma_20"]:
        score += 2
        evidence.append("price above 20 SMA")
    if ma.get("sma_50") is not None and price > ma["sma_50"]:
        score += 2
        evidence.append("price above 50 SMA")
    if filters.get("above_200dma"):
        score += 2
        evidence.append("price above 200 DMA")
    elif filters.get("above_200dma") is None:
        score += 1
        evidence.append("200 DMA unavailable")
    if relative_strength.get("bias") == "outperforming":
        score += 4
        evidence.append("outperforming Nifty proxy")
    elif relative_strength.get("bias") == "neutral":
        score += 2
        evidence.append("relative strength neutral")
    if filters.get("within_25pct_period_high"):
        score += 2
        evidence.append("within 25% of period high")
    return _score_section("trend_relative_strength", "Trend And Relative Strength", score, 16, evidence)


def _momentum_section(indicators: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    rsi = indicators.get("rsi_14")
    if rsi is not None and 40 <= rsi <= 70:
        score += 3
        evidence.append("RSI in constructive 40-70 zone")
    elif rsi is not None and 70 < rsi <= 78:
        score += 1
        evidence.append("RSI extended but not extreme")
    adx = indicators.get("adx")
    if adx is not None and adx >= 20:
        score += 2
        evidence.append("ADX confirms trend strength")
    if (indicators.get("macd") or {}).get("bias") == "bullish":
        score += 2
        evidence.append("MACD bullish")
    if (indicators.get("ichimoku") or {}).get("bias") == "bullish_cloud":
        score += 2
        evidence.append("Ichimoku bullish cloud")
    cci = indicators.get("cci_20")
    if cci is not None and 0 <= cci <= 200:
        score += 1
        evidence.append("CCI positive without extreme extension")
    stochastic = indicators.get("stochastic") or {}
    if stochastic.get("bias") != "overbought":
        score += 2
        evidence.append("stochastic not overbought")
    return _score_section("momentum_quality", "Momentum Quality", score, 12, evidence)


def _volume_accumulation_section(indicators: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    volume_ratio = indicators.get("volume_ratio_20")
    if volume_ratio is not None and volume_ratio >= 1.5:
        score += 3
        evidence.append("volume expansion >= 1.5x")
    elif volume_ratio is not None and volume_ratio >= 1.1:
        score += 1
        evidence.append("volume modestly above average")
    if delivery.get("bias") in {"accumulation", "volume_accumulation_proxy"}:
        score += 3
        evidence.append(delivery.get("bias"))
    if (indicators.get("obv_slope") or 0) > 0:
        score += 2
        evidence.append("OBV slope positive")
    if (indicators.get("cmf_20") or 0) > 0:
        score += 2
        evidence.append("CMF positive")
    if (indicators.get("volume_profile_proxy") or {}).get("bias") == "above_poc":
        score += 2
        evidence.append("price above volume-profile proxy POC")
    return _score_section("volume_accumulation", "Volume And Accumulation", score, 12, evidence)


def _news_event_section(
    sentiment_score: float,
    fundamental: dict[str, Any],
    corporate_risk: dict[str, Any],
    conflicts: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    evidence = []
    if sentiment_score > 0.2:
        score += 4
        evidence.append("positive news sentiment")
    elif sentiment_score >= -0.1:
        score += 2
        evidence.append("news sentiment neutral")
    else:
        evidence.append("news sentiment negative")
    if fundamental.get("quality_bucket") == "event_positive":
        score += 2
        evidence.append("positive official event keyword")
    elif fundamental.get("quality_bucket") == "unknown":
        score += 1
        evidence.append("fundamental event data neutral/unknown")
    if not corporate_risk.get("high_impact_risk"):
        score += 2
        evidence.append("no high-impact corporate event risk")
    if conflicts.get("severity") == "none":
        score += 2
        evidence.append("no signal conflict")
    return _score_section("news_events", "News And Event Risk", score, 10, evidence)


def _derivatives_section(options_oi: dict[str, Any], institutional_flow: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    if institutional_flow.get("fno_ban"):
        evidence.append("F&O ban flag")
        return _score_section("derivatives_positioning", "Derivatives Positioning", 0, 6, evidence)
    bias = options_oi.get("bias")
    if bias == "put_heavy_supportive":
        score += 5
    elif bias == "balanced":
        score += 4
    elif bias == "call_heavy_caution":
        score += 1
    else:
        score += 3
    evidence.append(f"options/OI bias {bias or 'unavailable'}")
    if not institutional_flow.get("fno_ban"):
        score += 1
        evidence.append("not flagged in F&O ban feed")
    return _score_section("derivatives_positioning", "Derivatives Positioning", score, 6, evidence)


def _fundamental_section(fundamental: dict[str, Any], corporate_risk: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    bucket = fundamental.get("quality_bucket")
    if bucket == "event_positive":
        score += 4
    elif bucket == "unknown":
        score += 2
    evidence.append(f"fundamental bucket {bucket or 'unknown'}")
    if not corporate_risk.get("high_impact_risk"):
        score += 2
        evidence.append("no governance/event veto")
    return _score_section("fundamental_quality", "Fundamental Quality", score, 6, evidence)


def _backtest_section(backtest: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    if not backtest.get("available"):
        score = 3
        evidence.append(backtest.get("reason", "backtest unavailable"))
        return _score_section("backtest_expectancy", "Backtest Snapshot", score, 8, evidence)
    expectancy = float(backtest.get("expectancy") or 0.0)
    win_rate = float(backtest.get("win_rate") or 0.0)
    if expectancy > 0:
        score += 4
        evidence.append("positive expectancy proxy")
    elif expectancy == 0:
        score += 2
        evidence.append("flat expectancy proxy")
    if win_rate >= 0.5:
        score += 2
        evidence.append("win rate >= 50%")
    elif win_rate >= 0.35:
        score += 1
        evidence.append("win rate acceptable for swing proxy")
    if float(backtest.get("max_drawdown_proxy") or 0.0) >= -8:
        score += 2
        evidence.append("drawdown proxy within tolerance")
    return _score_section("backtest_expectancy", "Backtest Snapshot", score, 8, evidence)


def _risk_reward_section(price: float, indicators: dict[str, Any], trade_plan: dict[str, Any]) -> dict[str, Any]:
    score = 0
    evidence = []
    target_rr = _target_rr(trade_plan)
    if target_rr >= 2:
        score += 3
        evidence.append(f"target RR {target_rr}")
    stop = trade_plan.get("stop_loss")
    stop_pct = ((price - stop) / price) * 100 if stop and price else None
    if stop_pct is not None and 1.0 <= stop_pct <= 5.0:
        score += 2
        evidence.append("stop distance is swing-trade suitable")
    atr_pct = indicators.get("atr_pct")
    if atr_pct is not None and atr_pct <= 6:
        score += 2
        evidence.append("ATR volatility within limit")
    risk_pct = ((trade_plan.get("position_sizing") or {}).get("max_capital_at_risk_pct") or 0.0) * 100
    if risk_pct <= 1.0:
        score += 1
        evidence.append("risk per trade <= 1%")
    return _score_section("risk_reward", "Risk Reward", score, 8, evidence)


def _score_section(key: str, label: str, score: float, max_score: int, evidence: list[str]) -> dict[str, Any]:
    score = max(min(float(score), float(max_score)), 0.0)
    ratio = score / max_score if max_score else 0.0
    status = "pass" if ratio >= 0.7 else "watch" if ratio >= 0.45 else "fail"
    return {
        "key": key,
        "label": label,
        "score": _round(score),
        "max": max_score,
        "status": status,
        "evidence": _unique(evidence)[:6],
    }


def _scorecard_grade(score: float) -> str:
    if score >= 88:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 72:
        return "B"
    if score >= 62:
        return "C"
    return "Reject"


def _target_rr(trade_plan: dict[str, Any]) -> float:
    values = []
    for target in trade_plan.get("targets") or []:
        if not isinstance(target, dict):
            continue
        try:
            values.append(float(target.get("rr")))
        except (TypeError, ValueError):
            continue
    return max(values) if values else 0.0


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


def _data_gaps(candles: list[Candle], row: dict[str, Any], institutional_context: dict[str, Any]) -> list[str]:
    gaps = []
    if len(candles) < 200:
        gaps.append("200-period trend and true 52-week context unavailable")
    feeds = institutional_context.get("feeds") or {}
    if not institutional_context.get("enabled"):
        gaps.append("free institutional feeds disabled")
    if feeds.get("fo_ban", {}).get("status") != "ok":
        gaps.append("F&O ban adapter not connected or returned no usable feed")
    if feeds.get("delivery_pct", {}).get("status") != "ok":
        gaps.append("delivery percentage requires NSE/BSE bhavcopy integration")
    if feeds.get("option_pcr", {}).get("status") not in {"ok", "partial_or_empty"}:
        gaps.append("PCR/OI requires option-chain data")
    if feeds.get("fii_dii", {}).get("status") != "ok":
        gaps.append("FII/DII market flow feed unavailable this cycle")
    if feeds.get("asm", {}).get("status") != "ok" or feeds.get("gsm", {}).get("status") != "ok":
        gaps.append("ASM/GSM surveillance feed unavailable this cycle")
    gaps.extend(
        [
            "FII/DII stock-level flows require licensed/exchange datasets",
            "GIFT Nifty, FedWatch, DXY detail, yield curve, and macro calendar require dedicated feeds",
            "Paid Reuters/Bloomberg/Dow Jones/broker research feeds not connected",
            "Social sentiment, analyst consensus, consensus targets, and promoter pledge feeds not connected",
            "Volume profile HVN/LVN/POC requires tick or volume-at-price data",
            "Full NSE/BSE coverage depends on the enabled symbols in universe.csv",
        ]
    )
    if not row.get("sector"):
        gaps.append("sector metadata missing")
    return gaps


def _event_text_matches(items: list[dict[str, Any]], pattern: str) -> bool:
    for item in items:
        text = " ".join(str(value) for value in item.values()).lower()
        if re.search(pattern, text):
            return True
    return False


def _requirement_coverage(
    data_quality: dict[str, Any],
    global_context: dict[str, Any],
    institutional_context: dict[str, Any],
) -> dict[str, Any]:
    history = data_quality.get("coverage", "thin")
    macro_enabled = bool(global_context.get("enabled"))
    feeds = institutional_context.get("feeds") or {}
    institutional_enabled = bool(institutional_context.get("enabled"))
    return {
        "phase_1_global_macro": {
            "status": "partial" if macro_enabled else "not_enabled",
            "implemented": "global index, crude, gold, USD/INR, and global-news risk score",
            "gap": "GIFT Nifty, India VIX, FedWatch, yield curve, and macro-calendar feeds still need dedicated adapters",
        },
        "phase_2_news_sentiment": {
            "status": "partial_with_official_free_feeds" if institutional_enabled else "partial",
            "implemented": "rotating Google News RSS, NSE corporate announcements, source weighting, recency decay, event labels, optional LLM refinement",
            "gap": "paid wires, BSE filing expansion, social sentiment, analyst consensus, and corporate actions depth need adapters",
        },
        "phase_3_universe_scan": {
            "status": "implemented_for_enabled_universe",
            "implemented": "every enabled symbol is scanned before LLM shortlisting",
            "gap": "full NSE/BSE breadth requires a populated universe.csv and valid provider symbols",
        },
        "phase_4_historical_trend": {
            "status": "implemented" if history in {"usable", "strong"} else "limited_by_history",
            "implemented": "multi-timeframe proxy trend, swing structure, moving averages, levels, gaps, Fibonacci",
            "gap": "true weekly/52-week/200-DMA context needs enough historical candles from the provider",
        },
        "phase_5_candlesticks": {
            "status": "proxy_engine",
            "implemented": "major single and multi-candle pattern proxies with reliability/confirmation",
            "gap": "the prompt's exhaustive pattern library is approximated, not a full TA-Lib-style recognizer",
        },
        "phase_6_chart_patterns": {
            "status": "proxy_engine",
            "implemented": "double-top/bottom, breakout/breakdown, and volatility-contraction proxies",
            "gap": "full H&S, cup-and-handle, wedge, flag, pennant, IPO-base, and volume-profile engines need deeper history",
        },
        "phase_7_smc_wyckoff": {
            "status": "proxy_engine",
            "implemented": "liquidity sweep, BOS/range, FVG, order-block proxy, premium/discount, Wyckoff proxy",
            "gap": "true ICT/SMC/Wyckoff labeling requires richer multi-timeframe structure and volume/liquidity feeds",
        },
        "phase_8_indicators": {
            "status": "expanded",
            "implemented": "EMA/SMA, ADX, RSI, MACD, Bollinger, ATR, OBV slope, CMF, stochastic, CCI, Ichimoku, divergence proxy, candle volume-profile proxy, volume ratio",
            "gap": "true tick-level volume profile and full multi-timeframe divergence engine need deeper historical/depth data",
        },
        "phase_9_confluence": {
            "status": "implemented_with_neutral_gaps",
            "implemented": "26-point confluence score, 100-point institutional scorecard, hard vetoes, and tier thresholds; free FII/DII, PCR, ASM/GSM, announcements can contribute when available",
            "gap": "missing institutional/feed-only factors are recorded as gaps instead of fabricated",
        },
        "phase_10_signal_output": {
            "status": "implemented_as_json_audit",
            "implemented": "signal plan, trade plan, invalidation, monitoring checklist, confluence breakdown, data gaps",
            "gap": "UI renders structured cards rather than the prompt's long textual report format",
        },
        "phase_11_risk_management": {
            "status": "expanded_for_paper_long_only",
            "implemented": "max positions, ATR-aware sizing metadata, max position/order size, hard stop, take profit, daily loss, conflict vetoes, LLM policy gates, free ASM/GSM/F&O-ban hooks",
            "gap": "sector concentration, 3-stop lockout, true event-calendar lockout, and live kill-switch workflow need adapters/UI",
        },
        "phase_13_backtest_and_learning": {
            "status": "proxy_engine",
            "implemented": "per-symbol candle backtest snapshot, strategy metrics, closed-trade realized P&L, expectancy proxy",
            "gap": "full walk-forward testing, slippage, brokerage, taxes, and multi-year survivorship-safe data need a historical database",
        },
        "phase_14_smallcap_liquidity": {
            "status": "proxy_engine",
            "implemented": "average traded value, liquidity tier, volume spike, circuit-risk proxy, price-impact flag",
            "gap": "bid/ask spread and true market depth require broker/Nubra depth endpoint",
        },
        "phase_12_intelligence_loop": {
            "status": "single_cycle_loop",
            "implemented": "continuous configurable agent cycle with snapshots and dashboard monitoring",
            "gap": "separate pre-market/opening/intraday/post-market/weekly schedules are not split yet",
        },
        "free_feed_status": {
            "source_quality": institutional_context.get("source_quality", "unavailable"),
            "fii_dii": feeds.get("fii_dii", {}).get("status"),
            "asm": feeds.get("asm", {}).get("status"),
            "gsm": feeds.get("gsm", {}).get("status"),
            "option_pcr": feeds.get("option_pcr", {}).get("status"),
            "corporate_announcements": feeds.get("corporate_announcements", {}).get("status"),
        },
    }


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


def _stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    window: int = 14,
) -> tuple[float | None, float | None]:
    if len(closes) < window or len(highs) < window or len(lows) < window:
        return None, None
    k_values: list[float] = []
    for index in range(window - 1, len(closes)):
        high = max(highs[index - window + 1 : index + 1])
        low = min(lows[index - window + 1 : index + 1])
        k_values.append(50.0 if high == low else ((closes[index] - low) / (high - low)) * 100)
    return k_values[-1], mean(k_values[-3:]) if len(k_values) >= 3 else None


def _cci(candles: list[Candle], window: int = 20) -> float | None:
    if len(candles) < window:
        return None
    typical_prices = [(candle.high + candle.low + candle.close) / 3 for candle in candles[-window:]]
    typical_mean = mean(typical_prices)
    mean_deviation = mean(abs(price - typical_mean) for price in typical_prices)
    if mean_deviation == 0:
        return 0.0
    return (typical_prices[-1] - typical_mean) / (0.015 * mean_deviation)


def _ichimoku(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    if len(closes) < 26 or len(highs) < 26 or len(lows) < 26:
        return {"available": False, "reason": "need at least 26 candles"}

    def midpoint(window: int) -> float | None:
        if len(highs) < window or len(lows) < window:
            return None
        return (max(highs[-window:]) + min(lows[-window:])) / 2

    conversion = midpoint(9)
    base = midpoint(26)
    span_b = midpoint(52)
    span_a = (conversion + base) / 2 if conversion is not None and base is not None else None
    price = closes[-1]
    cloud_top = max(value for value in (span_a, span_b) if value is not None) if span_a is not None or span_b is not None else None
    cloud_bottom = min(value for value in (span_a, span_b) if value is not None) if span_a is not None or span_b is not None else None
    if cloud_top is not None and price > cloud_top and conversion is not None and base is not None and conversion >= base:
        bias = "bullish_cloud"
    elif cloud_bottom is not None and price < cloud_bottom and conversion is not None and base is not None and conversion <= base:
        bias = "bearish_cloud"
    else:
        bias = "neutral"
    return {
        "available": True,
        "conversion": _round(conversion),
        "base": _round(base),
        "span_a": _round(span_a),
        "span_b": _round(span_b),
        "bias": bias,
    }


def _volume_profile_proxy(candles: list[Candle], buckets: int = 12) -> dict[str, Any]:
    if len(candles) < 20:
        return {"available": False, "reason": "need at least 20 candles"}
    lows = [candle.low for candle in candles]
    highs = [candle.high for candle in candles]
    low = min(lows)
    high = max(highs)
    if high <= low:
        return {"available": False, "reason": "flat price range"}
    bucket_size = (high - low) / buckets
    profile = [0.0 for _ in range(buckets)]
    for candle in candles:
        typical = (candle.high + candle.low + candle.close) / 3
        index = min(int((typical - low) / bucket_size), buckets - 1)
        profile[index] += float(candle.volume or 0.0)
    poc_index = max(range(buckets), key=lambda index: profile[index])
    poc = low + bucket_size * (poc_index + 0.5)
    current = candles[-1].close
    sorted_buckets = sorted(range(buckets), key=lambda index: profile[index], reverse=True)
    high_volume_nodes = [low + bucket_size * (index + 0.5) for index in sorted_buckets[:3]]
    low_volume_nodes = [low + bucket_size * (index + 0.5) for index in sorted_buckets[-3:]]
    return {
        "available": True,
        "method": "candle_typical_price_volume_bucket_proxy",
        "poc": _round(poc),
        "price_vs_poc_pct": _round(((current - poc) / poc) * 100 if poc else None),
        "bias": "above_poc" if current >= poc else "below_poc",
        "high_volume_nodes": [_round(value) for value in high_volume_nodes],
        "low_volume_nodes": [_round(value) for value in low_volume_nodes],
        "limitation": "not tick-level volume-at-price",
    }


def _divergence_proxy(closes: list[float], highs: list[float], lows: list[float]) -> dict[str, Any]:
    if len(closes) < 24 or len(highs) < 24 or len(lows) < 24:
        return {"available": False, "signal": "insufficient_history"}
    previous_low = min(lows[-16:-8])
    recent_low = min(lows[-8:])
    previous_high = max(highs[-16:-8])
    recent_high = max(highs[-8:])
    previous_rsi = _rsi(closes[:-5], 14)
    current_rsi = _rsi(closes, 14)
    signal = "none"
    if current_rsi is not None and previous_rsi is not None:
        if recent_low < previous_low and current_rsi > previous_rsi:
            signal = "bullish_divergence"
        elif recent_high > previous_high and current_rsi < previous_rsi:
            signal = "bearish_divergence"
    return {
        "available": True,
        "signal": signal,
        "previous_rsi": _round(previous_rsi),
        "current_rsi": _round(current_rsi),
        "recent_low": _round(recent_low),
        "previous_low": _round(previous_low),
        "recent_high": _round(recent_high),
        "previous_high": _round(previous_high),
    }


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
