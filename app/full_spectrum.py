from __future__ import annotations

import re
from typing import Any

from .market_regions import INDIA_EXCHANGES
from .models import Candle, Quote
from .strategy_backtest import strategy_backtest_snapshot


def _mean(values: list[float] | tuple[float, ...] | Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _pstdev(values: list[float] | tuple[float, ...] | Any) -> float:
    items = list(values)
    if not items:
        return 0.0
    avg = _mean(items)
    return (sum((value - avg) ** 2 for value in items) / len(items)) ** 0.5


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
    delivery_data: dict[str, Any] | None = None,
    options_data: dict[str, Any] | None = None,
    sector_context: dict[str, Any] | None = None,
    market_breadth: dict[str, Any] | None = None,
    macro_event_context: dict[str, Any] | None = None,
    timeframe_candles: dict[str, list[Candle]] | None = None,
    performance_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeframe_candles = timeframe_candles or {}
    intraday_candles = timeframe_candles.get("intraday") or []
    daily_candles = timeframe_candles.get("daily") or candles
    weekly_candles = timeframe_candles.get("weekly") or []
    analysis_candles = timeframe_candles.get("analysis") or daily_candles or candles
    candles = analysis_candles
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    data_quality = _data_quality(candles)
    indicators = _indicator_suite(candles)
    key_levels = _key_levels(candles, quote.price)
    trend_context = _trend_context(closes, highs, lows, indicators)
    timeframe_alignment = _multi_timeframe_alignment(
        weekly_candles=weekly_candles,
        daily_candles=daily_candles,
        intraday_candles=intraday_candles,
        fallback_candles=candles,
    )
    trend_context["timeframe_alignment"] = timeframe_alignment
    stage_analysis = _stage_analysis(
        candles=candles,
        quote_price=quote.price,
        technical=technical,
        weekly_candles=weekly_candles,
        daily_candles=daily_candles,
    )
    session_momentum = _session_momentum(quote)
    price_volume_divergence = _price_volume_divergence(candles, technical)
    entry_quality = _entry_grade(candles, quote.price, indicators)
    entry_quality = _strategy_confirmed_entry_quality(entry_quality, strategy_signals)
    breakout_quality = _false_breakout_filter(candles, quote.price)
    fib = _fib_levels(candles)
    candlestick_v2 = _candlestick_v2(candles, candle_tools)
    chart_patterns = _chart_patterns(candles)
    institutional = _institutional_structure(candles, quote.price, key_levels)
    flow = _institutional_flow(row, institutional_context)
    liquidity = _liquidity_profile(candles, quote.price)
    fundamental = _fundamental_quality(row, flow)
    corporate_risk = _corporate_event_risk(flow)
    delivery = _delivery_accumulation(flow, candles, delivery_data)
    sector_rotation = _sector_rotation_layer(sector_context)
    relative_strength = _relative_strength(closes, global_context)
    options_oi = _options_oi_layer(flow, options_data, quote.price)
    backtest = _backtest_snapshot(candles, strategy_signals, risk_limits)
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
        stage_analysis=stage_analysis,
        timeframe_alignment=timeframe_alignment,
        price_volume_divergence=price_volume_divergence,
        entry_quality=entry_quality,
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
    trade_plan = _trade_plan(quote.price, key_levels, indicators, confluence, risk_limits, liquidity, backtest, options_oi)
    strategy_logic = _phase3_strategy_logic_filters(
        entry_quality=entry_quality,
        breakout_quality=breakout_quality,
        indicators=indicators,
        delivery=delivery,
        institutional_flow=flow,
        options_oi=options_oi,
        macro_event_context=macro_event_context or {},
        fundamental=fundamental,
    )
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
        strategy_logic=strategy_logic,
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
        stage_analysis,
        timeframe_alignment,
        price_volume_divergence,
        entry_quality,
        breakout_quality,
        delivery,
        macro_event_context or {},
        options_oi,
        strategy_logic,
    )
    signal_plan = _signal_plan(row, quote.price, trend_context, confluence, trade_plan, risk_overrides, scorecard)
    monitoring = _monitoring_checklist(quote.price, trade_plan, confluence, risk_overrides, scorecard)
    return {
        "version": "openstocks-full-spectrum-v2",
        "symbol": row.get("symbol"),
        "timeframe_data": {
            "analysis_candle_count": len(candles),
            "intraday_candle_count": len(intraday_candles),
            "daily_candle_count": len(daily_candles),
            "weekly_candle_count": len(weekly_candles),
            "analysis_source": candles[-1].source if candles else None,
            "intraday_source": intraday_candles[-1].source if intraday_candles else None,
            "daily_source": daily_candles[-1].source if daily_candles else None,
            "weekly_source": weekly_candles[-1].source if weekly_candles else None,
        },
        "requirement_coverage": _requirement_coverage(data_quality, global_context, institutional_context),
        "data_quality": data_quality,
        "primary_filters": filters,
        "signal_plan": signal_plan,
        "trend_context": trend_context,
        "stage_analysis": stage_analysis,
        "session_momentum": session_momentum,
        "price_volume_divergence": price_volume_divergence,
        "entry_quality": entry_quality,
        "breakout_quality": breakout_quality,
        "strategy_logic_filters": strategy_logic,
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
        "sector_rotation": sector_rotation,
        "market_breadth": market_breadth or {},
        "macro_event_context": macro_event_context or {},
        "performance_feedback": performance_feedback or {},
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


def _session_momentum(quote: Quote) -> dict[str, Any]:
    price = _float_or_none(quote.price)
    open_price = _float_or_none(quote.open)
    high = _float_or_none(quote.high)
    low = _float_or_none(quote.low)
    volume = _float_or_none(quote.volume)
    day_gain_pct = ((price - open_price) / open_price) * 100 if price and open_price else None
    day_range_pct = ((high - low) / open_price) * 100 if high and low and open_price else None
    range_position = (price - low) / (high - low) if price and high and low and high > low else None
    day_high_distance_pct = max(((high - price) / price) * 100, 0.0) if price and high else None
    source = str(quote.source or "")
    live_source = any(token in source.lower() for token in ("upstox", "kite", "nubra", "alpaca", "polygon", "live"))
    fast_mover = (
        day_gain_pct is not None
        and day_gain_pct >= 4.0
        and (range_position or 0.0) >= 0.70
        and (day_high_distance_pct is None or day_high_distance_pct <= 2.0)
    )
    confirmed = fast_mover or (
        day_gain_pct is not None
        and day_gain_pct >= 1.5
        and (range_position or 0.0) >= 0.55
        and (day_high_distance_pct is None or day_high_distance_pct <= 3.0)
    )
    failed_drive = (
        day_gain_pct is not None
        and (
            day_gain_pct < 1.0
            or (
                range_position is not None
                and range_position < 0.45
                and day_high_distance_pct is not None
                and day_high_distance_pct > 2.5
            )
        )
    )
    return {
        "available": bool(price and open_price and high and low),
        "source": source,
        "live_source": live_source,
        "price": _round(price),
        "open": _round(open_price),
        "high": _round(high),
        "low": _round(low),
        "volume": _round(volume),
        "day_gain_pct": _round(day_gain_pct),
        "day_range_pct": _round(day_range_pct),
        "day_range_position": _round(range_position),
        "day_high_distance_pct": _round(day_high_distance_pct),
        "confirmed": confirmed,
        "fast_mover": fast_mover,
        "failed_drive": failed_drive,
        "policy": "Broad momentum entries need current-session confirmation; fast movers are reviewed from live quote/open/high/low evidence.",
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
    support, support_tests = _tested_level(lows[-20:], mode="support")
    resistance, resistance_tests = _tested_level(highs[-20:], mode="resistance")
    distance_to_support = ((price - support) / price) * 100 if support and price else None
    distance_to_resistance = ((resistance - price) / price) * 100 if resistance and price else None
    risk_reward = (
        distance_to_resistance / max(distance_to_support, 0.01)
        if distance_to_resistance is not None and distance_to_support is not None
        else None
    )
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
        "nearest_support": _round(support),
        "nearest_resistance": _round(resistance),
        "support_strength": support_tests,
        "resistance_strength": resistance_tests,
        "distance_to_support_pct": _round(distance_to_support),
        "distance_to_resistance_pct": _round(distance_to_resistance),
        "risk_reward_from_current": _round(risk_reward),
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


def _stage_analysis(
    candles: list[Candle],
    quote_price: float,
    technical: dict[str, Any],
    weekly_candles: list[Candle] | None = None,
    daily_candles: list[Candle] | None = None,
) -> dict[str, Any]:
    weekly_candles = weekly_candles or []
    daily_candles = daily_candles or candles
    source_timeframe = "weekly" if len(weekly_candles) >= 30 else "daily" if len(daily_candles) >= 30 else "fallback"
    stage_candles = weekly_candles if source_timeframe == "weekly" else daily_candles if source_timeframe == "daily" else candles
    if len(stage_candles) < 30:
        return {
            "stage": "Stage1_Base",
            "stage_confidence": "low",
            "sma_30w_proxy": None,
            "sma_slope_pct": None,
            "volume_pattern": "insufficient_history",
            "buy_permitted": False,
            "source_timeframe": source_timeframe,
            "stage_note": "Need at least 30 candles for Weinstein stage proxy.",
        }
    if source_timeframe == "weekly":
        proxy = stage_candles
    else:
        proxy = stage_candles[::5] if len(stage_candles) >= 150 else stage_candles[-30:]
    closes = [c.close for c in proxy]
    sma = _mean(closes[-30:]) if len(closes) >= 30 else _mean(closes)
    prior_slice = closes[-35:-5] if len(closes) >= 35 else closes[: max(len(closes) - 5, 1)]
    prior_sma = _mean(prior_slice) if prior_slice else sma
    slope_pct = ((sma - prior_sma) / prior_sma) * 100 / 5 if prior_sma else 0.0
    recent_high = max(c.high for c in stage_candles[-30:])
    volume_pattern = _stage_volume_pattern(stage_candles)
    down_volume_days = sum(1 for candle in stage_candles[-10:] if candle.close < candle.open and candle.volume)
    up_volume_days = sum(1 for candle in stage_candles[-10:] if candle.close > candle.open and candle.volume)
    near_sma = abs(quote_price - sma) / sma <= 0.05 if sma else False
    near_high = quote_price >= recent_high * 0.92 if recent_high else False
    if quote_price < sma and slope_pct < 0:
        stage = "Stage4_Decline"
    elif near_high and (volume_pattern == "rising_volume_downtrend" or down_volume_days > up_volume_days):
        stage = "Stage3_Distribution"
    elif quote_price > sma and slope_pct > 0 and volume_pattern == "rising_volume_uptrend":
        stage = "Stage2_Markup"
    elif near_sma and abs(slope_pct) < 0.2 and volume_pattern == "neutral":
        stage = "Stage1_Base"
    else:
        stage = "Stage2_Markup" if quote_price > sma and slope_pct >= 0 else "Stage1_Base"
    clarity = abs(slope_pct) + (abs(quote_price - sma) / sma * 100 if sma else 0)
    confidence = "high" if clarity >= 5 or volume_pattern != "neutral" else "medium" if clarity >= 2 else "low"
    return {
        "stage": stage,
        "stage_confidence": confidence,
        "sma_30w_proxy": _round(sma),
        "sma_slope_pct": _round(slope_pct),
        "volume_pattern": volume_pattern,
        "buy_permitted": stage == "Stage2_Markup",
        "source_timeframe": source_timeframe,
        "stage_note": f"{stage} from price vs 30-week proxy, slope, and {volume_pattern}.",
    }


def _multi_timeframe_alignment(
    weekly_candles: list[Candle] | None,
    daily_candles: list[Candle] | None,
    intraday_candles: list[Candle] | None,
    fallback_candles: list[Candle],
) -> dict[str, Any]:
    weekly_source = weekly_candles or _coarsen_candles(daily_candles or fallback_candles, 5)
    daily_source = daily_candles or fallback_candles
    intraday_source = intraday_candles or fallback_candles[-16:]
    weekly = _timeframe_view([c.close for c in weekly_source if c.close], weekly_source)
    daily = _timeframe_view([c.close for c in daily_source if c.close], daily_source)
    intraday = _timeframe_view([c.close for c in intraday_source[-16:] if c.close], intraday_source[-16:])
    views = {"weekly_proxy": weekly, "daily": daily, "intraday_proxy": intraday}
    usable_views = {
        key: value
        for key, value in views.items()
        if value.get("direction") not in {None, "unavailable"}
    }
    up_count = sum(1 for item in usable_views.values() if item.get("direction") == "up")
    sideways_count = sum(1 for item in usable_views.values() if item.get("direction") == "sideways")
    down_count = sum(1 for item in usable_views.values() if item.get("direction") == "down")
    usable_count = len(usable_views)
    if up_count == 3:
        grade = "A"
    elif usable_count >= 2 and up_count >= 2 and down_count == 0:
        grade = "B"
    elif usable_count >= 2 and up_count >= 1 and down_count <= 1:
        grade = "C"
    else:
        grade = "D"
    return {
        "timeframes": views,
        "alignment_score": up_count,
        "alignment_grade": grade,
        "usable_timeframes": usable_count,
        "source_counts": {
            "weekly": len(weekly_source),
            "daily": len(daily_source),
            "intraday": len(intraday_source),
        },
    }


def _price_volume_divergence(candles: list[Candle], technical: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 20:
        return {"available": False, "divergence_score": 0.0, "reason": "insufficient candles"}
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]
    obv = _obv_series(closes, volumes)
    ad_line = _ad_line(candles)
    price_new_high = max(highs[-5:]) >= max(highs[-20:])
    obv_price_divergence = price_new_high and max(obv[-5:]) < max(obv[-20:])
    ad_price_divergence = len(ad_line) >= 11 and closes[-1] > closes[-11] and ad_line[-1] < ad_line[-11]
    avg_volume_20 = _mean([v for v in volumes[-20:] if v]) if any(volumes[-20:]) else 0.0
    climax_volume_top = any(c.volume > avg_volume_20 * 4 and c.high >= max(highs[-52:] or highs) for c in candles[-3:]) if avg_volume_20 else False
    rsi = technical.get("rsi")
    panic_volume_bottom = any(
        c.volume > avg_volume_20 * 4
        and (c.close - c.low) / max(c.high - c.low, 0.01) < 0.25
        and rsi is not None
        and rsi < 35
        for c in candles[-3:]
    ) if avg_volume_20 else False
    score = 0.0
    if obv_price_divergence:
        score -= 0.4
    if ad_price_divergence:
        score -= 0.3
    if climax_volume_top:
        score -= 0.5
    if panic_volume_bottom:
        score += 0.3
    return {
        "available": True,
        "obv_price_divergence": obv_price_divergence,
        "ad_price_divergence": ad_price_divergence,
        "climax_volume_top": climax_volume_top,
        "panic_volume_bottom": panic_volume_bottom,
        "divergence_score": _round(max(min(score, 1.0), -1.0)),
    }


def _entry_grade(candles: list[Candle], quote_price: float, indicators: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 21:
        return {"entry_grade": "WATCH", "quality_score": 0.0, "entry_note": "insufficient candles for pivot"}
    last = candles[-1]
    pivot = max(c.high for c in candles[-21:-1])
    distance = ((quote_price - pivot) / pivot) * 100 if pivot else None
    ma = indicators.get("moving_averages") or {}
    position = (last.close - last.low) / max(last.high - last.low, 0.01)
    volumes = [c.volume for c in candles[-21:-1] if c.volume]
    avg_volume = _mean(volumes) if volumes else 0.0
    volume_ratio = _float_or_none(indicators.get("volume_ratio_20"))
    volume_confirmation = bool(avg_volume and last.volume > avg_volume * 1.5)
    if distance is not None and distance < 0:
        pullback = _pullback_entry_quality(
            quote_price=quote_price,
            pivot=pivot,
            distance_from_pivot=distance,
            ma=ma,
            rsi=_float_or_none(indicators.get("rsi_14")),
            volume_ratio=volume_ratio,
            obv_slope=_float_or_none(indicators.get("obv_slope")),
            cmf_20=_float_or_none(indicators.get("cmf_20")),
        )
        if pullback:
            grade = pullback["entry_grade"]
            quality = {"A": 1.0, "B": 0.75, "C": 0.45}.get(grade, 0.0)
            return {
                "entry_grade": grade,
                "setup_type": "pullback_buy_zone",
                "pivot": _round(pivot),
                "distance_from_pivot_pct": _round(distance),
                "distance_to_sma20_pct": pullback.get("distance_to_sma20_pct"),
                "distance_to_sma50_pct": pullback.get("distance_to_sma50_pct"),
                "last_close_position_in_range": _round(position),
                "volume_confirmation": bool(
                    volume_confirmation
                    or (volume_ratio is not None and volume_ratio >= 1.1)
                    or (pullback.get("money_flow_support") is True)
                ),
                "quality_score": quality,
                "entry_note": pullback["entry_note"],
            }
    if distance is None or distance < 0:
        grade = "WATCH"
    elif distance <= 2:
        grade = "A"
    elif distance <= 5:
        grade = "B"
    elif distance <= 8:
        grade = "C"
    else:
        grade = "D"
    quality = {"A": 1.0, "B": 0.7, "C": 0.4, "D": 0.0, "WATCH": 0.0}.get(grade, 0.0)
    return {
        "entry_grade": grade,
        "setup_type": "pivot_breakout",
        "pivot": _round(pivot),
        "distance_from_pivot_pct": _round(distance),
        "last_close_position_in_range": _round(position),
        "volume_confirmation": volume_confirmation,
        "quality_score": quality,
        "entry_note": f"{grade} entry from pivot distance and volume confirmation.",
    }


def _pullback_entry_quality(
    *,
    quote_price: float,
    pivot: float,
    distance_from_pivot: float,
    ma: dict[str, Any],
    rsi: float | None,
    volume_ratio: float | None,
    obv_slope: float | None,
    cmf_20: float | None,
) -> dict[str, Any]:
    sma20 = _float_or_none(ma.get("sma_20"))
    sma50 = _float_or_none(ma.get("sma_50"))
    sma200 = _float_or_none(ma.get("sma_200"))
    if quote_price <= 0 or pivot <= 0 or not (sma20 or sma50):
        return {}

    distance_to_sma20 = ((quote_price - sma20) / sma20) * 100 if sma20 else None
    distance_to_sma50 = ((quote_price - sma50) / sma50) * 100 if sma50 else None
    near_sma20 = distance_to_sma20 is not None and -2.0 <= distance_to_sma20 <= 3.5
    near_sma50 = distance_to_sma50 is not None and -1.5 <= distance_to_sma50 <= 4.0
    above_sma20 = sma20 is not None and quote_price >= sma20 * 0.985
    above_sma50 = sma50 is not None and quote_price >= sma50 * 0.985
    trend_stack = bool(
        sma20
        and sma50
        and sma20 >= sma50 * 0.995
        and (sma200 is None or sma50 >= sma200 * 0.98)
    )
    constructive_rsi = rsi is None or 38 <= rsi <= 72
    money_flow_support = bool(
        (volume_ratio is not None and volume_ratio >= 1.1)
        or (obv_slope is not None and obv_slope > 0)
        or (cmf_20 is not None and cmf_20 > 0)
    )
    weak_participation = volume_ratio is not None and volume_ratio < 0.65
    close_enough_to_high = distance_from_pivot >= -10.0
    supported_pullback = (near_sma20 or near_sma50) and above_sma50
    if not (trend_stack and constructive_rsi and close_enough_to_high and supported_pullback) or weak_participation:
        return {}

    if distance_from_pivot >= -3.0 and above_sma20 and (volume_ratio is None or volume_ratio >= 0.9 or money_flow_support):
        grade = "A"
    elif distance_from_pivot >= -6.5 and (above_sma20 or near_sma20 or near_sma50):
        grade = "B"
    else:
        grade = "C"
    return {
        "entry_grade": grade,
        "distance_to_sma20_pct": _round(distance_to_sma20),
        "distance_to_sma50_pct": _round(distance_to_sma50),
        "money_flow_support": money_flow_support,
        "entry_note": (
            f"{grade} pullback entry: price is below the breakout pivot but holding the rising 20/50 SMA zone "
            "with constructive RSI and acceptable participation."
        ),
    }


def _strategy_confirmed_entry_quality(entry: dict[str, Any], strategy_signals: list[dict[str, Any]]) -> dict[str, Any]:
    if str(entry.get("entry_grade") or "").upper() != "WATCH":
        return entry
    distance = _float_or_none(entry.get("distance_from_pivot_pct"))
    if distance is None or distance < -4.0 or distance > 5.0:
        return entry
    candidates = [
        signal
        for signal in strategy_signals
        if str(signal.get("direction") or "").upper() == "BUY"
        and float(signal.get("score") or 0.0) >= 0.7
        and isinstance(signal.get("metadata"), dict)
        and signal["metadata"].get("fresh_entry_confirmed") is True
    ]
    if not candidates:
        return entry
    best = max(candidates, key=lambda item: float(item.get("score") or 0.0))
    metadata = best.get("metadata") if isinstance(best.get("metadata"), dict) else {}
    volume_ratio = _float_or_none(metadata.get("volume_ratio_20"))
    volume_confirmed = bool(entry.get("volume_confirmation") or (volume_ratio is not None and volume_ratio >= 1.1))
    grade = "A" if distance >= -1.5 and volume_confirmed else "B"
    return {
        **entry,
        "entry_grade": grade,
        "setup_type": "strategy_confirmed_entry",
        "strategy_entry_confirmed_by": best.get("name"),
        "strategy_entry_score": _round(float(best.get("score") or 0.0)),
        "volume_confirmation": volume_confirmed,
        "quality_score": max(float(entry.get("quality_score") or 0.0), 1.0 if grade == "A" else 0.75),
        "entry_note": (
            f"{grade} entry confirmed by {best.get('name')}: strategy preset marked a fresh BUY while price remains within the allowable pivot zone."
        ),
    }


def _false_breakout_filter(candles: list[Candle], quote_price: float) -> dict[str, Any]:
    if len(candles) < 25:
        return {"is_breakout_attempt": False, "breakout_quality": "insufficient_history", "false_breakout_risk_score": 0.0}
    prior_resistance = max(c.high for c in candles[-25:-2])
    last = candles[-1]
    candle_pos = (last.close - last.low) / max(last.high - last.low, 0.01)
    is_attempt = 0 <= ((quote_price - prior_resistance) / prior_resistance) * 100 <= 3 if prior_resistance else False
    volumes = [c.volume for c in candles[-21:-1] if c.volume]
    avg_volume = _mean(volumes) if volumes else 0.0
    close_in_upper_range = candle_pos >= 0.75
    volume_expansion = bool(avg_volume and last.volume > avg_volume * 1.5)
    breakout_quality = "not_breakout"
    if is_attempt:
        if close_in_upper_range and volume_expansion:
            breakout_quality = "confirmed"
        elif close_in_upper_range or volume_expansion:
            breakout_quality = "suspect"
        else:
            breakout_quality = "false_breakout_risk"
    crossed_indices = [idx for idx, c in enumerate(candles[-4:-1], start=len(candles) - 4) if c.close > prior_resistance]
    two_day_failed = bool(crossed_indices and any(c.close < prior_resistance for c in candles[crossed_indices[0] + 1 :]))
    risk_score = 0.0
    if breakout_quality == "suspect":
        risk_score = 0.4
    elif breakout_quality == "false_breakout_risk":
        risk_score = 0.8
    if two_day_failed:
        risk_score = 1.0
    repeated = _repeated_failed_breakouts(candles)
    if repeated.get("repeated_failed_breakouts"):
        risk_score = max(risk_score, 0.85)
    return {
        "is_breakout_attempt": is_attempt,
        "breakout_quality": breakout_quality,
        "two_day_rule_failed": two_day_failed,
        "prior_resistance": _round(prior_resistance),
        "close_in_upper_range": close_in_upper_range,
        "volume_expansion": volume_expansion,
        "failed_breakout_count": repeated.get("failed_breakout_count", 0),
        "repeated_failed_breakouts": repeated.get("repeated_failed_breakouts", False),
        "failed_breakout_events": repeated.get("failed_breakout_events", []),
        "false_breakout_risk_score": risk_score,
    }


def _repeated_failed_breakouts(candles: list[Candle], lookback: int = 80) -> dict[str, Any]:
    if len(candles) < 35:
        return {"failed_breakout_count": 0, "repeated_failed_breakouts": False, "failed_breakout_events": []}
    events = []
    start = max(21, len(candles) - lookback)
    end = max(start, len(candles) - 1)
    for idx in range(start, end):
        prior = candles[max(0, idx - 20) : idx]
        if len(prior) < 10:
            continue
        resistance = max(candle.high for candle in prior)
        candle = candles[idx]
        if not resistance or candle.close <= resistance * 1.003:
            continue
        follow = candles[idx + 1 : min(len(candles), idx + 6)]
        failed = any(item.close < resistance * 0.995 for item in follow)
        if failed:
            events.append(
                {
                    "ts": candle.ts,
                    "breakout_close": _round(candle.close),
                    "prior_resistance": _round(resistance),
                }
            )
    return {
        "failed_breakout_count": len(events),
        "repeated_failed_breakouts": len(events) >= 2,
        "failed_breakout_events": events[-3:],
    }


def _phase3_strategy_logic_filters(
    entry_quality: dict[str, Any],
    breakout_quality: dict[str, Any],
    indicators: dict[str, Any],
    delivery: dict[str, Any],
    institutional_flow: dict[str, Any],
    options_oi: dict[str, Any],
    macro_event_context: dict[str, Any],
    fundamental: dict[str, Any],
) -> dict[str, Any]:
    hard_blocks: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []

    def hard(flag: str, reason: str, value: Any = None) -> None:
        hard_blocks.append({"flag": flag, "reason": reason, "value": value})

    def penalty(flag: str, reason: str, value: Any = None, score_penalty: float = 0.0, size_multiplier: float | None = None) -> None:
        payload = {"flag": flag, "reason": reason, "value": value, "score_penalty": score_penalty}
        if size_multiplier is not None:
            payload["size_multiplier"] = size_multiplier
        penalties.append(payload)

    distance = _float_or_none(entry_quality.get("distance_from_pivot_pct"))
    if distance is not None and distance > 5.0:
        hard(
            "PRICE_EXTENDED_FROM_PIVOT",
            "fresh long is more than 5% above pivot; wait for reset or tighter base",
            {"distance_from_pivot_pct": _round(distance), "pivot": entry_quality.get("pivot")},
        )
    elif distance is not None and distance > 2.0:
        penalty(
            "ENTRY_NOT_FRESH_FROM_PIVOT",
            "entry is no longer fresh from pivot; position size must be reduced",
            {"distance_from_pivot_pct": _round(distance), "pivot": entry_quality.get("pivot")},
            score_penalty=4.0,
            size_multiplier=0.85,
        )

    volume_ratio = _float_or_none(indicators.get("volume_ratio_20"))
    volume_confirmed = bool(
        breakout_quality.get("volume_expansion")
        or entry_quality.get("volume_confirmation")
        or (volume_ratio is not None and volume_ratio >= 1.5)
    )
    breakout_state = str(breakout_quality.get("breakout_quality") or "").lower()
    if breakout_state == "suspect" and not volume_confirmed:
        hard(
            "SUSPECT_BREAKOUT_WITHOUT_VOLUME",
            "suspect breakout has no volume expansion; do not buy until volume confirms",
            {"breakout_quality": breakout_quality, "volume_ratio_20": _round(volume_ratio)},
        )
    elif breakout_state == "suspect":
        penalty(
            "SUSPECT_BREAKOUT_REDUCED_SIZE",
            "breakout is still suspect even with some volume evidence",
            {"breakout_quality": breakout_quality, "volume_ratio_20": _round(volume_ratio)},
            score_penalty=8.0,
            size_multiplier=0.5,
        )
    if breakout_quality.get("two_day_rule_failed"):
        hard("FAILED_BREAKOUT_TWO_DAY_RULE", "two-day breakout rule failed", breakout_quality)
    if volume_ratio is None:
        penalty(
            "VOLUME_RATIO_MISSING",
            "20-period volume ratio is unavailable; reduce confidence",
            None,
            score_penalty=4.0,
            size_multiplier=0.75,
        )
    elif volume_ratio < 0.8:
        penalty(
            "LOW_VOLUME_RATIO",
            "volume ratio below 0.8x shows weak participation",
            {"volume_ratio_20": _round(volume_ratio)},
            score_penalty=12.0,
            size_multiplier=0.5,
        )
    elif volume_ratio < 1.1:
        penalty(
            "WEAK_VOLUME_RATIO",
            "volume ratio below 1.1x is not enough for a momentum entry",
            {"volume_ratio_20": _round(volume_ratio)},
            score_penalty=6.0,
            size_multiplier=0.75,
        )

    failed_count = int(_float_or_none(breakout_quality.get("failed_breakout_count")) or 0)
    if failed_count >= 2:
        penalty(
            "REPEATED_FAILED_BREAKOUTS",
            "symbol has multiple recent failed breakout attempts",
            {
                "failed_breakout_count": failed_count,
                "events": breakout_quality.get("failed_breakout_events", []),
            },
            score_penalty=min(18.0, 10.0 + max(failed_count - 2, 0) * 3.0),
            size_multiplier=0.5,
        )

    event_thesis = _event_driven_thesis(macro_event_context, fundamental, institutional_flow)
    earnings_window = _earnings_window(macro_event_context)
    if earnings_window.get("active") and not event_thesis.get("supported"):
        hard(
            "EARNINGS_LOCKOUT_NOT_EVENT_DRIVEN",
            "known earnings window blocks fresh BUY unless an explicit event-driven thesis is present",
            earnings_window,
        )
    elif earnings_window.get("active"):
        penalty(
            "EARNINGS_EVENT_DRIVEN_TINY_SIZE",
            "event-driven earnings setup must use tiny size until event risk clears",
            {"earnings": earnings_window, "event_thesis": event_thesis},
            score_penalty=6.0,
            size_multiplier=0.25,
        )

    sponsorship = _institutional_sponsorship(delivery, institutional_flow, options_oi)
    if not sponsorship.get("supported"):
        if _us_reference_data_mode(delivery):
            penalty(
                "US_REFERENCE_PRICE_VOLUME_ONLY",
                "US Yahoo/reference setup has no true institutional-flow feed; use only as price-volume momentum evidence and reduce size",
                sponsorship,
                score_penalty=0.0,
                size_multiplier=0.5,
            )
        else:
            penalty(
                "INSTITUTIONAL_SPONSORSHIP_MISSING",
                "flow/accumulation support is missing; do not describe the setup as institutional without proof",
                sponsorship,
                score_penalty=10.0,
                size_multiplier=0.75,
            )

    size_cap = 1.0
    for item in penalties:
        multiplier = _float_or_none(item.get("size_multiplier"))
        if multiplier is not None:
            size_cap = min(size_cap, multiplier)
    return {
        "version": "phase3-strategy-logic-v1",
        "passed": not hard_blocks,
        "hard_blocks": hard_blocks,
        "penalties": penalties,
        "score_penalty": _round(sum(float(item.get("score_penalty") or 0.0) for item in penalties)),
        "sizing": {
            "max_multiplier": _round(size_cap),
            "policy": "hard blocks prevent fresh BUY; penalties reduce score and position size",
        },
        "pivot_extension": {
            "distance_from_pivot_pct": _round(distance),
            "max_buy_distance_pct": 5.0,
            "too_extended": bool(distance is not None and distance > 5.0),
        },
        "breakout_volume": {
            "breakout_quality": breakout_state or None,
            "volume_ratio_20": _round(volume_ratio),
            "volume_confirmed": volume_confirmed,
            "suspect_without_volume": breakout_state == "suspect" and not volume_confirmed,
        },
        "event_driven_thesis": event_thesis,
        "earnings_window": earnings_window,
        "institutional_sponsorship": sponsorship,
    }


def _earnings_window(macro_event_context: dict[str, Any]) -> dict[str, Any]:
    trading_days = macro_event_context.get("earnings_trading_days_away")
    days = macro_event_context.get("earnings_days_away")
    active = False
    trading_value = _float_or_none(trading_days)
    days_value = _float_or_none(days)
    if trading_value is not None:
        active = 0 <= trading_value <= 10
    elif days_value is not None:
        active = 0 <= days_value <= 14
    return {
        "active": active,
        "earnings_days_away": days,
        "earnings_trading_days_away": trading_days,
        "source": macro_event_context.get("source"),
    }


def _event_driven_thesis(
    macro_event_context: dict[str, Any],
    fundamental: dict[str, Any],
    institutional_flow: dict[str, Any],
) -> dict[str, Any]:
    evidence = []
    explicit_keys = (
        "event_driven",
        "explicit_event_driven",
        "earnings_event_driven",
        "allow_earnings_trade",
        "catalyst_trade",
    )
    for key in explicit_keys:
        if macro_event_context.get(key) is True:
            evidence.append(f"macro_context.{key}=true")
    text_values = [
        macro_event_context.get("strategy"),
        macro_event_context.get("strategy_type"),
        macro_event_context.get("thesis"),
        macro_event_context.get("event_thesis"),
        macro_event_context.get("catalyst"),
    ]
    joined = " ".join(str(value or "").lower() for value in text_values)
    if "event-driven" in joined or "event driven" in joined or "catalyst" in joined or "earnings trade" in joined:
        evidence.append("macro_context contains explicit event/catalyst thesis")
    if str(fundamental.get("quality_bucket") or "").lower() in {"event_positive", "event_positive_with_ratios"}:
        evidence.append("positive official event in fundamental quality")
    announcements = institutional_flow.get("official_announcements") or []
    if _event_text_matches(announcements, r"order|contract|approval|dividend|bonus|split|upgrade"):
        evidence.append("positive official announcement catalyst")
    return {
        "supported": bool(evidence),
        "evidence": _unique(evidence),
        "policy": "earnings-window BUY requires explicit event-driven/catalyst evidence",
    }


def _institutional_sponsorship(
    delivery: dict[str, Any],
    institutional_flow: dict[str, Any],
    options_oi: dict[str, Any],
) -> dict[str, Any]:
    evidence = []
    delivery_bias = str(delivery.get("net_bias") or delivery.get("trend_direction") or delivery.get("bias") or "").lower()
    delivery_score = _float_or_none(delivery.get("delivery_score"))
    delivery_pct = _float_or_none(delivery.get("delivery_pct") or delivery.get("delivery_percentage"))
    if delivery.get("institutional_fingerprint") or delivery.get("fingerprint"):
        evidence.append("delivery institutional fingerprint")
    if delivery_bias == "accumulation" and ((delivery_score is not None and delivery_score > 0) or (delivery_pct is not None and delivery_pct >= 50)):
        evidence.append("delivery accumulation")
    if delivery_bias == "volume_accumulation_proxy" and delivery_score is not None and delivery_score > 0:
        evidence.append("price-volume accumulation proxy")
    if institutional_flow.get("bulk_deals"):
        evidence.append("recent bulk/block deal evidence")
    if _positive_fund_flow(institutional_flow.get("fii_dii_flow")):
        evidence.append("positive FII/DII or fund-flow feed")
    market_bias_score = _float_or_none((institutional_flow.get("market_bias") or {}).get("score"))
    if market_bias_score is not None and market_bias_score >= 0.15:
        evidence.append("positive institutional market-bias score")
    option_bias = str(options_oi.get("bias") or "").lower()
    pcr = _float_or_none(options_oi.get("pcr_oi") or options_oi.get("market_pcr_proxy"))
    max_pain_distance = _float_or_none(options_oi.get("max_pain_distance_pct"))
    if option_bias in {"put_heavy_supportive", "max_pain_above_supportive"}:
        evidence.append("supportive options accumulation/OI bias")
    if pcr is not None and pcr >= 1.2:
        evidence.append("put-heavy PCR support")
    if max_pain_distance is not None and max_pain_distance > 3:
        evidence.append("max pain above price/supportive")
    return {
        "supported": bool(evidence),
        "evidence": _unique(evidence),
        "missing_if_false": [
            "delivery accumulation/fingerprint",
            "price-volume accumulation proxy",
            "bulk or block deal evidence",
            "positive fund-flow/FII-DII feed",
            "supportive options accumulation/OI",
        ] if not evidence else [],
    }


def _positive_fund_flow(feed: Any) -> bool:
    if not isinstance(feed, dict):
        return False
    for key in ("score", "net", "net_flow", "net_buy", "net_purchase", "fii_net", "dii_net"):
        value = _float_or_none(feed.get(key))
        if value is not None and value > 0:
            return True
    items = feed.get("items")
    if isinstance(items, dict):
        return any(_positive_fund_flow(item) for item in items.values() if isinstance(item, dict))
    if isinstance(items, list):
        return any(_positive_fund_flow(item) for item in items if isinstance(item, dict))
    return False


def _us_reference_data_mode(delivery: dict[str, Any]) -> bool:
    source = str(delivery.get("source") or "").lower()
    gap = str(delivery.get("data_gap") or "").lower()
    market = str(delivery.get("market_region") or "").upper()
    return market == "US" or source.startswith("us_") or "not_applicable_us" in gap or "us_equities" in gap


def _tested_level(values: list[float], mode: str) -> tuple[float | None, int]:
    if not values:
        return None, 0
    candidates = sorted(values)[:5] if mode == "support" else sorted(values, reverse=True)[:5]
    best_level: float | None = None
    best_tests = 0
    for level in candidates:
        tests = sum(1 for value in values if abs(value - level) / max(level, 0.01) <= 0.01)
        if tests > best_tests:
            best_level = level
            best_tests = tests
    if best_tests < 2:
        best_level = min(values) if mode == "support" else max(values)
    return best_level, best_tests


def _stage_volume_pattern(candles: list[Candle]) -> str:
    recent = candles[-20:]
    if len(recent) < 5:
        return "neutral"
    up_volumes = [c.volume for c in recent if c.close > c.open and c.volume]
    down_volumes = [c.volume for c in recent if c.close < c.open and c.volume]
    first_half = recent[: len(recent) // 2]
    second_half = recent[len(recent) // 2 :]
    first_avg = _mean([c.volume for c in first_half if c.volume] or [0])
    second_avg = _mean([c.volume for c in second_half if c.volume] or [0])
    if second_avg <= first_avg * 1.1:
        return "neutral"
    if sum(up_volumes) > sum(down_volumes) * 1.2:
        return "rising_volume_uptrend"
    if sum(down_volumes) > sum(up_volumes) * 1.2:
        return "rising_volume_downtrend"
    return "neutral"


def _timeframe_view(closes: list[float], candles: list[Candle]) -> dict[str, Any]:
    if len(closes) < 5:
        return {"direction": "unavailable", "strength": "weak", "price_vs_20sma": "unknown", "candle_count": len(closes)}
    sma5 = _mean(closes[-5:])
    sma20 = _mean(closes[-20:]) if len(closes) >= 20 else _mean(closes)
    distance = ((sma5 - sma20) / sma20) * 100 if sma20 else 0.0
    direction = "up" if distance > 0.4 else "down" if distance < -0.4 else "sideways"
    adx_proxy = _adx(candles, 14) if len(candles) >= 15 else None
    return {
        "direction": direction,
        "strength": "strong" if adx_proxy is not None and adx_proxy > 25 else "weak",
        "price_vs_20sma": "above" if closes[-1] >= sma20 else "below",
        "sma5": _round(sma5),
        "sma20": _round(sma20),
        "adx_proxy": _round(adx_proxy),
        "candle_count": len(closes),
    }


def _coarsen_candles(candles: list[Candle], step: int) -> list[Candle]:
    if step <= 1 or len(candles) < step:
        return candles
    output: list[Candle] = []
    for index in range(0, len(candles), step):
        bucket = candles[index : index + step]
        if not bucket:
            continue
        first = bucket[0]
        last = bucket[-1]
        output.append(
            Candle(
                symbol=first.symbol,
                ts=last.ts,
                open=first.open,
                high=max(item.high for item in bucket),
                low=min(item.low for item in bucket),
                close=last.close,
                volume=sum(float(item.volume or 0) for item in bucket),
                source=f"{first.source}:coarsened_{step}",
            )
        )
    return output


def _obv_series(closes: list[float], volumes: list[float]) -> list[float]:
    output = [0.0]
    for previous_close, close, volume in zip(closes, closes[1:], volumes[1:]):
        current = output[-1]
        if close > previous_close:
            current += volume
        elif close < previous_close:
            current -= volume
        output.append(current)
    return output


def _ad_line(candles: list[Candle]) -> list[float]:
    output: list[float] = []
    running = 0.0
    for candle in candles:
        span = candle.high - candle.low
        multiplier = (((candle.close - candle.low) - (candle.high - candle.close)) / span) if span else 0.0
        running += multiplier * candle.volume
        output.append(running)
    return output


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
    stage_analysis: dict[str, Any],
    timeframe_alignment: dict[str, Any],
    price_volume_divergence: dict[str, Any],
    entry_quality: dict[str, Any],
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
    delivery_score = float(delivery.get("delivery_score") or 0.0)
    if delivery_score > 0.6:
        news += 2
    elif delivery_score < -0.6:
        news -= 2
    if stage_analysis.get("stage") == "Stage2_Markup":
        technical += 2 if stage_analysis.get("stage_confidence") == "high" else 1
    elif stage_analysis.get("stage") in {"Stage3_Distribution", "Stage4_Decline"}:
        technical -= 3
    alignment_grade = timeframe_alignment.get("alignment_grade")
    if alignment_grade == "A":
        technical += 2
    elif alignment_grade == "B":
        technical += 1
    elif alignment_grade == "D":
        technical -= 2
    technical += float(price_volume_divergence.get("divergence_score") or 0.0)
    if entry_quality.get("volume_confirmation"):
        candle += 1
    if float(entry_quality.get("last_close_position_in_range") or 0.0) > 0.75:
        candle += 1

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
    stage_analysis: dict[str, Any],
    timeframe_alignment: dict[str, Any],
    price_volume_divergence: dict[str, Any],
    entry_quality: dict[str, Any],
    breakout_quality: dict[str, Any],
    delivery: dict[str, Any],
    macro_event_context: dict[str, Any],
    options_oi: dict[str, Any],
    strategy_logic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy_logic = strategy_logic or {}
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
    if stage_analysis.get("stage") in {"Stage3_Distribution", "Stage4_Decline"}:
        flags.append("stage_no_new_longs")
    if timeframe_alignment.get("alignment_grade") == "D":
        flags.append("timeframe_conflict_no_new_longs")
    if price_volume_divergence.get("climax_volume_top"):
        flags.append("climax_top_detected_no_new_longs")
    if entry_quality.get("entry_grade") == "WATCH":
        flags.append("watch_entry_needs_confirmation_reduce_size")
    if entry_quality.get("entry_grade") == "D":
        flags.append("extended_entry_no_new_longs")
    if breakout_quality.get("two_day_rule_failed"):
        flags.append("false_breakout_two_day_rule_failed_no_new_longs")
    if breakout_quality.get("breakout_quality") == "suspect":
        flags.append("suspect_breakout_reduce_size")
    if breakout_quality.get("breakout_quality") == "false_breakout_risk":
        flags.append("false_breakout_risk_no_new_longs")
    if float(macro_event_context.get("event_risk_score") or 0.0) > 0.6:
        flags.append("high_macro_event_risk")
    delivery_bias = str(delivery.get("net_bias") or delivery.get("trend_direction") or delivery.get("bias") or "").lower()
    if delivery_bias in {"distribution", "volume_distribution_proxy"}:
        flags.append("delivery_distribution_no_new_longs")
    if options_oi.get("buy_suppressed"):
        flags.append("options_max_pain_8pct_below_no_new_longs")
    for block in strategy_logic.get("hard_blocks") or []:
        flag = str(block.get("flag") or "phase3_strategy_block").lower()
        flags.append(f"phase3_{flag}_no_new_longs")
    for item in strategy_logic.get("penalties") or []:
        flag = str(item.get("flag") or "phase3_strategy_penalty").lower()
        flags.append(f"phase3_{flag}_reduce_size")
    symbol_flags = institutional_flow.get("symbol_flags", {})
    if symbol_flags.get("asm"):
        flags.append("asm_surveillance_no_new_longs")
    if symbol_flags.get("gsm"):
        flags.append("gsm_surveillance_no_new_longs")
    if symbol_flags.get("fno_ban"):
        flags.append("fo_ban_no_new_longs")
    no_new_longs = any(flag.endswith("_no_new_longs") for flag in flags)
    size_multiplier = _conviction_size_multiplier(confluence.get("total", 0), flags)
    phase3_size_cap = _float_or_none((strategy_logic.get("sizing") or {}).get("max_multiplier"))
    if phase3_size_cap is not None:
        size_multiplier = min(size_multiplier, phase3_size_cap)
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
            "accumulation proxy scorecard hard vetoes",
            "Phase 3 pivot/breakout/earnings strategy logic",
        ],
        "no_new_longs": no_new_longs,
        "size_multiplier": _round(size_multiplier),
        "phase3_strategy_logic": {
            "passed": strategy_logic.get("passed"),
            "hard_blocks": strategy_logic.get("hard_blocks", []),
            "penalties": strategy_logic.get("penalties", []),
            "size_cap": phase3_size_cap,
        },
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
    options_oi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options_oi = options_oi or {}
    atr = indicators.get("atr") or price * 0.02
    atr_pct = indicators.get("atr_pct")
    if atr_pct is None and price > 0:
        atr_pct = (atr / price) * 100
    stop_pct = min(max(float(risk_limits.get("stop_loss_pct", 0.035) or 0.035), 0.005), 0.04)
    support = _float_or_none(key_levels.get("nearest_support") or key_levels.get("prev_swing_low"))
    stop, stop_basis = _planned_stop(price, atr, stop_pct, support)
    risk = max(price - stop, price * 0.005)
    risk_per_trade_pct = min(float(risk_limits.get("max_order_value_pct", 0.04) or 0.04), 0.01)
    targets, target_policy = _planned_targets(
        price=price,
        risk=risk,
        atr=atr,
        atr_pct=atr_pct,
        key_levels=key_levels,
        confluence=confluence,
        liquidity=liquidity,
    )
    option_zones = options_oi.get("oi_concentration_zones") or {}
    option_resistance = (option_zones.get("resistance") or [])[:3]
    option_support = (option_zones.get("support") or [])[:3]
    return {
        "direction": "LONG" if confluence.get("total", 0) >= 14 else "WATCH" if confluence.get("total", 0) >= 10 else "NO_SIGNAL",
        "horizon": "swing_3_to_7_days",
        "entry_zone": [_round(price * 0.995), _round(price * 1.005)],
        "stop_loss": _round(stop),
        "stop_basis": stop_basis,
        "targets": targets,
        "target_policy": target_policy,
        "invalidation": {
            "chart": _round(stop),
            "macro": "risk-off regime or high-impact event within 48h",
            "news": "strong negative regulatory/credit/promoter pledge event",
            "options": "BUY suppressed if stock-level max pain sits 8% or more below current price",
        },
        "options_intelligence": {
            "source": options_oi.get("audit_label") or options_oi.get("source"),
            "max_pain": options_oi.get("max_pain"),
            "max_pain_distance_pct": options_oi.get("max_pain_distance_pct"),
            "support_zones_from_put_oi": option_support,
            "resistance_zones_from_call_oi": option_resistance,
            "note": "OI concentration zones are used as derivatives support/resistance when stock option-chain data is available",
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
        "target_execution": _target_execution_plan(targets),
        "time_stop": "exit or re-score if not moving toward T1 within 5 trading sessions",
        "trailing_stop": "after T1, trail below previous swing low or 1.2 ATR, whichever is tighter",
    }


def _planned_stop(price: float, atr: float, stop_pct: float, support: float | None) -> tuple[float, str]:
    if price <= 0:
        return 0.0, "invalid_price"
    risk_floor = price * 0.006
    risk_ceiling = price * 0.08
    atr_value = max(float(atr or 0.0), 0.0)
    target_risk = max(atr_value * 1.5 if atr_value else price * stop_pct, risk_floor)
    if support and 0 < support < price:
        support_stop = support - max(float(atr or 0.0) * 0.25, price * 0.002)
        support_risk = price - support_stop
        max_support_risk = max(target_risk * 1.25, target_risk + price * 0.005)
        if risk_floor <= support_risk <= min(risk_ceiling, max_support_risk):
            return max(support_stop, price - risk_ceiling), "nearest_support_minus_atr_buffer"
    risk = min(max(target_risk, risk_floor), risk_ceiling)
    return price - risk, "atr_1_5x" if atr_value else "configured_pct_fallback"


def _planned_targets(
    price: float,
    risk: float,
    atr: float,
    atr_pct: float | None,
    key_levels: dict[str, Any],
    confluence: dict[str, Any],
    liquidity: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rr_profile = _target_rr_profile(confluence, atr_pct, liquidity)
    structure_levels = _overhead_structure_levels(price, key_levels)
    targets: list[dict[str, Any]] = []
    used_structure: set[float] = set()
    previous_price = price
    for index, rr in enumerate(rr_profile, start=1):
        ladder_price = price + risk * rr
        basis = "dynamic_volatility_rr"
        note = "volatility and conviction target"
        structure_price = _structure_target_for_step(
            price=price,
            risk=risk,
            desired=ladder_price,
            structure_levels=structure_levels,
            used_structure=used_structure,
            step=index,
        )
        if structure_price is not None:
            target_price = structure_price
            used_structure.add(structure_price)
            basis = "overhead_structure"
            note = "uses nearby resistance/swing level"
        else:
            target_price = ladder_price
        min_gap = max(risk * 0.55, price * 0.004)
        if target_price <= previous_price + min_gap:
            target_price = previous_price + min_gap
            basis = "spacing_adjusted"
            note = "lifted to keep target ladder sequential"
        actual_rr = (target_price - price) / risk if risk > 0 else 0.0
        probability_label = _target_probability_label(actual_rr, index, confluence, liquidity)
        targets.append(
            {
                "label": f"T{index}",
                "price": _round(target_price),
                "rr": _round(actual_rr),
                "basis": basis,
                "distance_pct": _round(((target_price - price) / price) * 100 if price else None),
                "probability_label": probability_label,
                "suggested_exit_pct": [70, 20, 10][index - 1],
                "note": note,
            }
        )
        previous_price = target_price
    return targets, {
        "method": "structure_first_dynamic_rr",
        "risk_per_share": _round(risk),
        "atr": _round(atr),
        "atr_pct": _round(atr_pct),
        "rr_profile": [_round(value) for value in rr_profile],
        "structure_levels_above_price": [_round(level) for level in structure_levels[:5]],
        "note": "Targets prefer usable overhead structure; otherwise RR adapts to volatility, conviction, and liquidity.",
    }


def _target_rr_profile(
    confluence: dict[str, Any],
    atr_pct: float | None,
    liquidity: dict[str, Any],
) -> list[float]:
    tier = str(confluence.get("tier") or "").upper()
    if tier == "MAXIMUM_CONVICTION":
        profile = [1.10, 1.85, 2.65]
    elif tier == "HIGH_CONVICTION":
        profile = [1.00, 1.65, 2.35]
    elif tier == "TRADE_SIGNAL":
        profile = [0.90, 1.45, 2.05]
    else:
        profile = [0.82, 1.25, 1.75]
    volatility = float(atr_pct or 0.0)
    if volatility >= 6:
        profile = [value * 0.82 for value in profile]
    elif 0 < volatility <= 1.2:
        profile = [value * 1.12 for value in profile]
    liquidity_tier = str(liquidity.get("liquidity_tier") or "").lower()
    if liquidity_tier in {"thin", "illiquid"}:
        profile = [value * 0.86 for value in profile]
    return [round(max(value, 0.70), 3) for value in profile]


def _target_probability_label(
    rr: float,
    step: int,
    confluence: dict[str, Any],
    liquidity: dict[str, Any],
) -> str:
    tier = str(confluence.get("tier") or "").upper()
    liquidity_tier = str(liquidity.get("liquidity_tier") or "").lower()
    if liquidity_tier in {"thin", "illiquid"}:
        return "stretch" if step >= 2 else "moderate"
    if step == 1 and rr <= 1.6:
        return "higher" if tier in {"HIGH_CONVICTION", "MAXIMUM_CONVICTION"} else "moderate"
    if step == 2 and rr <= 2.4 and tier in {"HIGH_CONVICTION", "MAXIMUM_CONVICTION"}:
        return "moderate"
    return "stretch"


def _target_execution_plan(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = {
        "T1": "book partial profit and move stop to breakeven/entry zone if liquidity allows",
        "T2": "book another partial and trail below latest swing low or 1.2 ATR",
        "T3": "exit remaining size or keep only a runner with a hard trailing stop",
    }
    plan = []
    for target in targets[:3]:
        label = str(target.get("label") or "").upper()
        plan.append(
            {
                "label": label,
                "price": target.get("price"),
                "suggested_exit_pct": target.get("suggested_exit_pct"),
                "probability_label": target.get("probability_label"),
                "action": actions.get(label, "reduce risk and reassess"),
            }
        )
    return plan


def _overhead_structure_levels(price: float, key_levels: dict[str, Any]) -> list[float]:
    levels: list[float] = []
    for key in ("nearest_resistance", "prev_swing_high", "period_high"):
        value = _float_or_none(key_levels.get(key))
        if value and value > price * 1.003:
            levels.append(value)
    return sorted({round(level, 4) for level in levels})


def _structure_target_for_step(
    price: float,
    risk: float,
    desired: float,
    structure_levels: list[float],
    used_structure: set[float],
    step: int,
) -> float | None:
    if not structure_levels or risk <= 0:
        return None
    lower_rr = 0.65 if step == 1 else 0.85
    upper_rr = 1.65 if step == 1 else 1.8 if step == 2 else 2.4
    lower = price + risk * lower_rr
    upper = desired + risk * upper_rr
    for level in structure_levels:
        if level in used_structure:
            continue
        if lower <= level <= upper:
            return level
    return None


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
        "accumulation_proxy_score": f"{scorecard.get('total_score', 0)}/{scorecard.get('max_score', 100)}",
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
        f"Re-score accumulation proxy scorecard every cycle; current score is {scorecard.get('total_score', 0)}/100.",
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
    avg_volume = _mean([float(candle.volume or 0) for candle in recent]) if recent else 0.0
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
    pe = _positive_float_or_none(row.get("pe") or row.get("trailing_pe"))
    forward_pe = _positive_float_or_none(row.get("forward_pe"))
    pb = _positive_float_or_none(row.get("pb") or row.get("price_to_book"))
    market_cap = _positive_float_or_none(row.get("market_cap"))
    beta = _float_or_none(row.get("beta"))
    eps_ttm = _float_or_none(row.get("eps_ttm"))
    ratio_available = any(value is not None for value in (pe, forward_pe, pb, market_cap, beta, eps_ttm))
    quote_type = str(row.get("yahoo_quote_type") or row.get("security_type") or "").strip().upper()
    exchange = str(row.get("exchange") or "").strip().upper()
    sector = str(row.get("sector") or "").strip().upper()
    industry = str(row.get("industry") or "").strip().upper()
    is_etf = quote_type == "ETF" or exchange in {"ARCA", "NYSEARCA"} or "ETF" in {sector, industry}
    score = 0.0
    reasons = []
    if ratio_available:
        score += 0.08
        reasons.append("Yahoo/reference market ratios available")
    if pe is not None and 0 < pe <= 65:
        score += 0.05
        reasons.append("PE is within a tradable range")
    elif pe is not None and pe > 100:
        score -= 0.06
        reasons.append("PE is stretched")
    if pb is not None and 0 < pb <= 12:
        score += 0.03
        reasons.append("PB is available and not extreme")
    elif pb is not None and pb > 20:
        score -= 0.05
        reasons.append("PB is stretched")
    if positive_event:
        score += 0.15
        reasons.append("recent positive official announcement keyword")
    if negative_event:
        score -= 0.35
        reasons.append("recent negative official announcement keyword")
    if negative_event:
        quality_bucket = "event_risk"
    elif positive_event and ratio_available:
        quality_bucket = "event_positive_with_ratios"
    elif positive_event:
        quality_bucket = "event_positive"
    elif is_etf and ratio_available:
        quality_bucket = "etf_reference_available"
    elif is_etf:
        quality_bucket = "etf_reference_pending"
    elif ratio_available:
        quality_bucket = "reference_ratios_available"
    else:
        quality_bucket = "unknown"
    unavailable_fields = []
    if not ratio_available:
        unavailable_fields.extend(["PE/PB/market-cap reference"])
    if is_etf:
        unavailable_fields.extend(["ETF holdings, expense ratio, and fund-flow depth"])
    else:
        unavailable_fields.extend(
            [
                "revenue/profit growth",
                "debt/equity",
                "ROE/ROCE",
                "promoter holding and pledge",
                "cash-flow quality",
                "quarterly trend",
            ]
        )
    return {
        "available": bool(announcements) or ratio_available,
        "score": _round(max(min(score, 1.0), -1.0)),
        "quality_bucket": quality_bucket,
        "source": row.get("fundamental_source") or ("yahoo_quote" if ratio_available and exchange not in INDIA_EXCHANGES else "reference"),
        "asof": row.get("fundamental_asof"),
        "security_type": "ETF" if is_etf else quote_type or None,
        "pe": _round(pe, 2),
        "forward_pe": _round(forward_pe, 2),
        "pb": _round(pb, 2),
        "market_cap": _round(market_cap, 2),
        "beta": _round(beta, 3),
        "eps_ttm": _round(eps_ttm, 3),
        "metrics": {
            "pe": _round(pe, 2),
            "trailing_pe": _round(pe, 2),
            "forward_pe": _round(forward_pe, 2),
            "pb": _round(pb, 2),
            "price_to_book": _round(pb, 2),
            "market_cap": _round(market_cap, 2),
            "beta": _round(beta, 3),
            "eps_ttm": _round(eps_ttm, 3),
            "security_type": "ETF" if is_etf else quote_type or None,
            "reference_data_available": ratio_available,
        },
        "checked": [
            "Yahoo/reference quote ratios when available",
            "official announcement keyword proxy",
            "promoter pledge placeholder",
            "debt/profitability ratios placeholder",
            "ROE/ROCE/revenue growth placeholder",
        ],
        "reasons": reasons or ["fundamental ratios not connected yet"],
        "data_gaps": [],
        "unavailable_fields": _unique(unavailable_fields),
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


def _delivery_accumulation(flow: dict[str, Any], candles: list[Candle], delivery_data: dict[str, Any] | None = None) -> dict[str, Any]:
    if delivery_data and delivery_data.get("source") == "not_applicable_to_us_market":
        volume_ratio = _volume_ratio([candle.volume for candle in candles if candle.volume is not None], 20) if candles else None
        price_change = ((candles[-1].close - candles[-5].close) / candles[-5].close) * 100 if len(candles) >= 5 and candles[-5].close else None
        if volume_ratio and volume_ratio >= 1.5 and price_change and price_change > 0:
            bias = "volume_accumulation_proxy"
            proxy_score = 0.35
        elif volume_ratio and volume_ratio >= 1.5 and price_change and price_change < 0:
            bias = "volume_distribution_proxy"
            proxy_score = -0.35
        else:
            bias = "neutral"
            proxy_score = 0.0
        return {
            **delivery_data,
            "bias": bias,
            "net_bias": bias,
            "volume_ratio_20": _round(volume_ratio),
            "price_change_5_candles_pct": _round(price_change),
            "delivery_score": proxy_score,
            "institutional_fingerprint": False,
            "source": "us_price_volume_proxy_no_delivery_data",
            "data_gap": "delivery_not_applicable_us_equities",
            "note": "US equities do not have NSE delivery bhavcopy; this is price-volume accumulation only, not true institutional flow.",
        }
    if delivery_data and delivery_data.get("available"):
        score_payload = delivery_data.get("score_payload") or {}
        return {
            **delivery_data,
            "bias": delivery_data.get("net_bias") or delivery_data.get("trend_direction") or "neutral",
            "delivery_score": float(score_payload.get("score") if isinstance(score_payload, dict) else delivery_data.get("delivery_score") or 0.0),
            "institutional_fingerprint": bool(score_payload.get("fingerprint") if isinstance(score_payload, dict) else delivery_data.get("institutional_fingerprint")),
            "source": "nse_delivery_bhavcopy",
        }
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
        "delivery_score": 0.0,
        "institutional_fingerprint": False,
        "source": "delivery feed if available, otherwise volume_proxy_no_delivery_data",
        "data_gap": "volume_proxy_no_delivery_data" if delivery_pct is None else None,
    }


def _sector_rotation_layer(sector_context: dict[str, Any] | None) -> dict[str, Any]:
    if not sector_context:
        return {
            "available": False,
            "sector_tailwind": False,
            "sector_headwind": False,
            "sector_rotation_score": 0.0,
            "data_gap": "sector_rotation_unavailable",
        }
    return sector_context


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


def _options_oi_layer(
    flow: dict[str, Any],
    options_data: dict[str, Any] | None = None,
    current_price: float | None = None,
) -> dict[str, Any]:
    options_data = options_data or {}
    if options_data.get("status") == "not_fno_no_stock_options":
        return {
            "available": False,
            "source": options_data.get("source", "nse_equity_non_fno_no_stock_options"),
            "audit_label": options_data.get("audit_label", "nse_equity_non_fno_no_stock_options"),
            "status": "not_fno_no_stock_options",
            "stock_option_status": "not_fno_no_stock_options",
            "market_pcr_proxy": None,
            "pcr_oi": None,
            "max_pain": None,
            "max_pain_distance_pct": None,
            "buy_suppressed": False,
            "oi_concentration_zones": {"support": [], "resistance": []},
            "bias": "unavailable",
            "fno_ban": flow.get("fno_ban"),
            "data_gap": None,
            "note": options_data.get("note") or "No stock-level options chain exists for this non-F&O equity.",
        }
    if options_data.get("status") == "ok":
        max_pain_distance = options_data.get("max_pain_distance_pct")
        if options_data.get("buy_suppressed"):
            bias = "max_pain_bearish_suppression"
        elif max_pain_distance is not None and float(max_pain_distance) > 3:
            bias = "max_pain_above_supportive"
        elif max_pain_distance is not None and float(max_pain_distance) < -3:
            bias = "max_pain_below_caution"
        else:
            bias = "balanced"
        return {
            "available": True,
            "source": options_data.get("source", "nse_option_chain_stock_level"),
            "audit_label": options_data.get("audit_label", "nse_option_chain_stock_level"),
            "status": "ok",
            "underlying_price": options_data.get("underlying_price") or _round(current_price),
            "pcr_oi": options_data.get("pcr_oi"),
            "market_pcr_proxy": options_data.get("pcr_oi"),
            "max_pain": options_data.get("max_pain"),
            "max_pain_distance_pct": max_pain_distance,
            "buy_suppressed": bool(options_data.get("buy_suppressed")),
            "buy_suppression_reason": options_data.get("buy_suppression_reason"),
            "oi_concentration_zones": options_data.get("oi_concentration_zones") or {},
            "strike_pcr": options_data.get("strike_pcr") or [],
            "top_oi_change": options_data.get("top_oi_change") or {},
            "bias": bias,
            "fno_ban": flow.get("fno_ban"),
            "data_gap": None,
        }
    pcr_feed = flow.get("pcr_oi") or {}
    items = pcr_feed.get("items") if isinstance(pcr_feed, dict) else None
    pcr_values = []
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, dict) and item.get("pcr_oi") is not None:
                pcr_values.append(float(item["pcr_oi"]))
    avg_pcr = _mean(pcr_values) if pcr_values else None
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
        "source": options_data.get("source") or "market_index_pcr_proxy",
        "audit_label": options_data.get("source") or "market_index_pcr_proxy",
        "market_pcr_proxy": _round(avg_pcr),
        "bias": bias,
        "fno_ban": flow.get("fno_ban"),
        "stock_option_status": options_data.get("status"),
        "stock_option_error": options_data.get("error"),
        "data_gap": None if avg_pcr is not None else "stock-level OI/PCR/IV/max-pain unavailable",
    }


def _backtest_snapshot(
    candles: list[Candle],
    strategy_signals: list[dict[str, Any]] | None = None,
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bool((risk_limits or {}).get("skip_symbol_backtest")):
        return {
            "available": False,
            "engine": "deferred_for_broad_cycle",
            "reason": (risk_limits or {}).get("backtest_skip_reason") or "deferred_to_preserve_cycle_budget",
            "deferred": True,
            "decision_symbols": int((risk_limits or {}).get("decision_symbols") or 0),
            "backtest_note": "Per-symbol walk-forward validation is deferred during broad open-market scans so all selected symbols receive live strategy decisions within the cycle budget.",
        }
    if len(candles) < 60:
        return {"available": False, "reason": "need at least 60 candles for setup-wise validation"}
    cost_bps = float((risk_limits or {}).get("execution_cost_bps") or 0.0)
    strategy_snapshot = strategy_backtest_snapshot(candles, execution_cost_bps=cost_bps)
    best = strategy_snapshot.get("best_strategy_backtest") or {}
    return {
        "available": True,
        "engine": strategy_snapshot.get("backtest_engine"),
        "trades": best.get("trades", 0),
        "win_rate": best.get("win_rate", 0.0),
        "expectancy": best.get("expectancy_pct", 0.0),
        "max_drawdown_proxy": best.get("max_drawdown_pct", 0.0),
        "last_5_trades": best.get("last_5", []),
        "execution_cost_bps": cost_bps,
        "validation_scope": "setup_wise_walk_forward",
        "legacy_proxy_removed": True,
        **strategy_snapshot,
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
    strategy_logic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy_logic = strategy_logic or {}
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
    if fundamental.get("quality_bucket") == "event_risk":
        hard_veto.append("fundamental_event_risk")
    if conflicts.get("severity") == "high":
        hard_veto.append("high_signal_conflict")
    if sentiment_score <= -0.45:
        hard_veto.append("severe_negative_news_sentiment")
    if atr_pct is not None and atr_pct > 8:
        hard_veto.append("extreme_atr_volatility")
    for block in strategy_logic.get("hard_blocks") or []:
        hard_veto.append(f"phase3_{str(block.get('flag') or 'strategy_logic').lower()}")

    if data_quality.get("score", 0) < 75:
        warnings.append("limited_history_reduces_confidence")
    if liquidity.get("liquidity_tier") == "thin":
        warnings.append("thin_liquidity_reduce_position_size")
    if conflicts.get("severity") == "medium":
        warnings.append("medium_signal_conflict")
    for item in strategy_logic.get("penalties") or []:
        warnings.append(f"phase3_{str(item.get('flag') or 'strategy_penalty').lower()}")

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
    phase3_penalty = float(strategy_logic.get("score_penalty") or 0.0)
    total = max(sum(section["score"] for section in sections) - phase3_penalty, 0.0)
    max_score = sum(section["max"] for section in sections)
    min_entry_score = 75
    strict_confluence = 16
    section_map = {section["key"]: section for section in sections}
    confluence_total = int(confluence.get("total", 0) or 0)
    us_reference_momentum_ready = (
        _us_reference_data_mode(delivery)
        and total >= 55
        and confluence_total >= 18
        and not hard_veto
        and section_map["liquidity_execution"]["score"] >= 7
        and section_map["trend_relative_strength"]["score"] >= 9
        and section_map["risk_reward"]["score"] >= 4
        and sentiment_score >= -0.2
        and not strategy_logic.get("hard_blocks")
    )
    if us_reference_momentum_ready:
        warnings.append("us_yahoo_reference_momentum_small_size_only")
    must_pass_failed = []
    if hard_veto:
        must_pass_failed.append("hard_veto_clear")
    effective_min_entry_score = 55 if us_reference_momentum_ready else min_entry_score
    if total < effective_min_entry_score:
        must_pass_failed.append("us_reference_momentum_score_min_55" if _us_reference_data_mode(delivery) else "accumulation_proxy_score_min_75")
    if confluence_total < strict_confluence:
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
    sponsorship = strategy_logic.get("institutional_sponsorship") or {}
    if not sponsorship.get("supported") and not _us_reference_data_mode(delivery):
        must_pass_failed.append("flow_or_accumulation_support_required")
    if strategy_logic.get("hard_blocks"):
        must_pass_failed.append("phase3_strategy_logic_clear")

    buy_ready = not must_pass_failed
    return {
        "version": "accumulation-proxy-scorecard-v1",
        "total_score": _round(total),
        "max_score": max_score,
        "normalized_score": _round(total / max_score if max_score else 0),
        "minimum_entry_score": min_entry_score,
        "effective_minimum_entry_score": effective_min_entry_score,
        "strict_confluence_required": strict_confluence,
        "grade": _scorecard_grade(total),
        "buy_ready": buy_ready,
        "hard_veto": {"passed": not hard_veto, "failed": _unique(hard_veto)},
        "must_pass_failed": _unique(must_pass_failed),
        "warnings": _unique(warnings),
        "us_reference_momentum_ready": us_reference_momentum_ready,
        "phase3_penalty": _round(phase3_penalty),
        "institutional_sponsorship": sponsorship,
        "sections": section_map,
        "entry_rule": "BUY only if hard veto clear, score >=75/100, confluence >=16/26, trend/liquidity/risk-reward must-pass gates clear, sentiment is not bearish, Phase 3 strategy logic is clean, and verified flow or accumulation evidence supports the setup. US Yahoo reference-mode swing signals may use score >=55 with confluence >=18, fresh quote data, and strict technical confirmation, but only as small-size price-volume momentum signals.",
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
    if fundamental.get("quality_bucket") in {"event_positive", "event_positive_with_ratios"}:
        score += 2
        evidence.append("positive official event keyword")
    elif fundamental.get("quality_bucket") in {"unknown", "etf_reference_pending"}:
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
    if options_oi.get("status") == "not_fno_no_stock_options":
        evidence.append("stock is not in NSE F&O; stock-level PCR/Max Pain unavailable")
        score += 2
        if not institutional_flow.get("fno_ban"):
            score += 1
            evidence.append("not flagged in F&O ban feed")
        return _score_section("derivatives_positioning", "Derivatives Positioning", score, 6, evidence)
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
    if bucket == "event_positive_with_ratios":
        score += 5
        evidence.append("positive event plus reference ratios available")
    elif bucket == "event_positive":
        score += 4
    elif bucket in {"reference_ratios_available", "etf_reference_available"}:
        score += 3
        evidence.append("Yahoo/reference ratios available")
    elif bucket == "unknown":
        score += 1
        evidence.append("real fundamental ratios unavailable; neutral placeholder capped")
    elif bucket == "etf_reference_pending":
        score += 1
        evidence.append("ETF company fundamentals not applicable; ETF depth still pending")
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
    best_strategy = backtest.get("best_strategy_backtest") or {}
    if best_strategy.get("trades"):
        expectancy = max(expectancy, float(best_strategy.get("expectancy_pct") or 0.0))
        win_rate = max(win_rate, float(best_strategy.get("win_rate") or 0.0))
    if expectancy > 0:
        score += 4
        evidence.append("positive expectancy after cost/slippage")
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
    if best_strategy.get("strategy"):
        evidence.append(f"best tested strategy {best_strategy.get('strategy')}")
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
    if abs(float(sentiment_score or 0.0)) < 1e-12:
        bias = "DATA_MISSING"
    elif sentiment_score > 0.2:
        bias = "bullish"
    elif sentiment_score < -0.2:
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "aggregate_score": _round(sentiment_score),
        "bias": bias,
        "source": "OpenStocks rotating news sentiment service",
        "note": "0.0 means DATA_MISSING, not neutral" if bias == "DATA_MISSING" else None,
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
    if len(candles) < 60:
        gaps.append("insufficient candle history for swing analysis")
    if not row.get("sector"):
        gaps.append("sector metadata missing")
    return _unique(gaps)


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
            "implemented": "26-point confluence score, 100-point accumulation proxy scorecard, hard vetoes, and tier thresholds; free FII/DII, PCR, ASM/GSM, announcements can contribute when available",
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
    return bool(ranges) and _mean(ranges[-10:]) < _mean(ranges[:10]) * 0.7


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
    return k_values[-1], _mean(k_values[-3:]) if len(k_values) >= 3 else None


def _cci(candles: list[Candle], window: int = 20) -> float | None:
    if len(candles) < window:
        return None
    typical_prices = [(candle.high + candle.low + candle.close) / 3 for candle in candles[-window:]]
    typical_mean = _mean(typical_prices)
    mean_deviation = _mean(abs(price - typical_mean) for price in typical_prices)
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
    basis = _mean(recent)
    sigma = _pstdev(recent) if len(recent) > 1 else 0
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
    return _mean(ranges) if ranges else None


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
    average = _mean(volumes[-(window + 1) : -1])
    return volumes[-1] / average if average else None


def _vwap(candles: list[Candle]) -> float | None:
    total_volume = sum(candle.volume for candle in candles)
    if not total_volume:
        return None
    return sum(((candle.high + candle.low + candle.close) / 3) * candle.volume for candle in candles) / total_volume


def _rsi(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = _mean(gains[:window])
    avg_loss = _mean(losses[:window])
    for gain, loss in zip(gains[window:], losses[window:]):
        avg_gain = ((avg_gain * (window - 1)) + gain) / window
        avg_loss = ((avg_loss * (window - 1)) + loss) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _sma(values: list[float], window: int) -> float | None:
    return _mean(values[-window:]) if len(values) >= window else None


def _ema(values: list[float], window: int) -> float | None:
    series = _ema_series(values, window)
    return series[-1] if series else None


def _ema_series(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return []
    alpha = 2 / (window + 1)
    ema = _mean(values[:window])
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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None
