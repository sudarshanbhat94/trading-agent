from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Candle


def _mean(values: list[float] | tuple[float, ...] | Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _pstdev(values: list[float] | tuple[float, ...] | Any) -> float:
    items = list(values)
    if not items:
        return 0.0
    avg = _mean(items)
    return (sum((value - avg) ** 2 for value in items) / len(items)) ** 0.5


@dataclass(frozen=True)
class StrategySignal:
    name: str
    score: float
    direction: str
    confidence: float
    notes: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_strategy_presets(
    candles: list[Candle],
    quote_price: float,
    intraday_candles: list[Candle] | None = None,
    market_breadth: dict[str, Any] | None = None,
    sector_context: dict[str, Any] | None = None,
    pattern_state: dict[str, Any] | None = None,
) -> list[StrategySignal]:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    if len(candles) < 30:
        return [
            StrategySignal(
                name="insufficient_history",
                score=0.0,
                direction="HOLD",
                confidence=0.0,
                notes=["need at least 30 candles for preset analysis"],
            )
        ]

    signals = [
        _normalized_momentum_factor(candles, quote_price),
        _time_series_momentum_trend(candles, quote_price),
        _aggressive_relative_strength_breakout(candles, quote_price),
        _fifty_two_week_high_momentum(candles, quote_price),
        _minervini_trend_template(closes, quote_price),
        _vcp_breakout(candles, quote_price),
        _darvas_box_breakout(candles, quote_price, (pattern_state or {}).get("darvas_box") or {}),
        _ema_pullback_continuation(closes, quote_price),
        _bollinger_squeeze_breakout(closes, volumes, quote_price),
        _rsi_mean_reversion(closes, quote_price),
        _donchian_momentum_breakout(highs, lows, closes, volumes, quote_price),
        _volume_price_accumulation(candles, quote_price),
        _failed_breakdown_reversal(candles, quote_price),
        _anchored_vwap_reclaim(candles, quote_price, intraday_candles or []),
        _volume_profile_value_area_breakout(candles, quote_price),
        _liquidity_sweep_reclaim(candles, quote_price),
        _breadth_aligned_leadership(candles, quote_price, market_breadth or {}, sector_context or {}),
    ]
    return [_normalized_signal(signal) for signal in signals]


def _normalized_momentum_factor(candles: list[Candle], quote_price: float) -> StrategySignal:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    score = 0.0
    notes: list[str] = []
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    ret_252 = _return_pct(closes, 252)
    vol_63 = _return_volatility_pct(closes, 63)
    vol_126 = _return_volatility_pct(closes, 126)
    risk_adjusted_3m = ret_63 / vol_63 if vol_63 else 0.0
    risk_adjusted_6m = ret_126 / vol_126 if vol_126 else 0.0
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    high_252 = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    distance_from_high = ((high_252 - quote_price) / high_252) * 100 if high_252 else 100.0
    atr_pct = _atr_pct(candles, 14)
    volume_ratio = _volume_ratio(volumes, 20)
    if ret_126 >= 18:
        score += 0.18
        notes.append("6-month absolute momentum above 18%")
    if ret_252 >= 25:
        score += 0.12
        notes.append("12-month absolute momentum above 25%")
    if risk_adjusted_3m >= 2.0:
        score += 0.14
        notes.append("3-month momentum is strong after volatility adjustment")
    if risk_adjusted_6m >= 2.0:
        score += 0.14
        notes.append("6-month momentum is strong after volatility adjustment")
    if sma_50 and quote_price > sma_50:
        score += 0.10
        notes.append("price above 50 SMA")
    if sma_50 and sma_200 and quote_price > sma_50 > sma_200:
        score += 0.14
        notes.append("trend stack is bullish")
    if distance_from_high <= 8:
        score += 0.10
        notes.append("near 52-week high")
    if 0 < atr_pct <= 6.0:
        score += 0.08
        notes.append("volatility is tradable")
    elif atr_pct > 9.0:
        score -= 0.12
        notes.append("volatility is too wide")
    if ret_63 < -3:
        score -= 0.12
        notes.append("recent 3-month momentum is negative")
    if volume_ratio >= 1.1:
        score += 0.05
        notes.append("volume confirms interest")
    direction = "BUY" if score >= 0.68 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("normalized_momentum_factor", round(bounded, 3), direction, round(bounded, 3), notes)


def _time_series_momentum_trend(candles: list[Candle], quote_price: float) -> StrategySignal:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    score = 0.0
    notes: list[str] = []
    ret_21 = _return_pct(closes, 21)
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    atr_pct = _atr_pct(candles, 14)
    channel_high = max(highs[-55:-1]) if len(highs) >= 56 else max(highs[:-1])
    channel_low = min(lows[-55:-1]) if len(lows) >= 56 else min(lows[:-1])
    channel_width = ((channel_high - channel_low) / channel_low) * 100 if channel_low else 100.0
    volume_ratio = _volume_ratio(volumes, 20)
    if ret_63 > 8 and ret_126 > 12:
        score += 0.24
        notes.append("medium-term time-series momentum is positive")
    if ret_21 > 0:
        score += 0.08
        notes.append("1-month trend is not fighting the setup")
    if sma_20 and sma_50 and quote_price > sma_20 > sma_50:
        score += 0.18
        notes.append("price above rising short trend stack")
    if sma_200 and quote_price > sma_200:
        score += 0.12
        notes.append("above long trend filter")
    if quote_price >= channel_high:
        score += 0.16
        notes.append("55-period breakout")
    elif quote_price >= channel_high * 0.97:
        score += 0.08
        notes.append("within 3% of breakout level")
    if channel_width <= 22:
        score += 0.08
        notes.append("breakout base is not too wide")
    if 0 < atr_pct <= 5.5:
        score += 0.08
        notes.append("ATR risk is controlled")
    elif atr_pct > 8:
        score -= 0.14
        notes.append("ATR risk too high")
    if volume_ratio >= 1.2:
        score += 0.06
        notes.append("volume confirms breakout interest")
    direction = "BUY" if score >= 0.66 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("time_series_momentum_trend", round(bounded, 3), direction, round(bounded, 3), notes)


def _aggressive_relative_strength_breakout(candles: list[Candle], quote_price: float) -> StrategySignal:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    volumes = [candle.volume for candle in candles]
    score = 0.0
    notes: list[str] = []
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    high_126 = max(highs[-126:]) if len(highs) >= 126 else max(highs)
    pivot = max(highs[-21:-1]) if len(highs) >= 22 else max(highs[:-1])
    avg_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else _mean(volumes[:-1])
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 1.0
    atr_pct = _atr_pct(candles, 14)
    if ret_63 >= 12:
        score += 0.22
        notes.append("3-month relative strength above 12%")
    if ret_126 >= 20:
        score += 0.18
        notes.append("6-month leadership above 20%")
    if sma_20 and sma_50 and quote_price > sma_20 > sma_50:
        score += 0.16
        notes.append("price above stacked 20/50 SMA")
    if sma_200 and quote_price > sma_200:
        score += 0.10
        notes.append("above 200 SMA")
    if high_126 and quote_price >= high_126 * 0.92:
        score += 0.12
        notes.append("within 8% of 6-month high")
    if pivot and 0 <= ((quote_price - pivot) / pivot) <= 0.035:
        score += 0.16
        notes.append("fresh pivot breakout within 3.5%")
    if volume_ratio >= 1.4:
        score += 0.12
        notes.append("volume expansion above 1.4x")
    if 0 < atr_pct <= 6.5:
        score += 0.08
        notes.append("tradable volatility")
    elif atr_pct > 9:
        score -= 0.12
        notes.append("volatility too wide for aggressive entry")
    direction = "BUY" if score >= 0.68 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("aggressive_relative_strength_breakout", round(bounded, 3), direction, round(bounded, 3), notes)


def _fifty_two_week_high_momentum(candles: list[Candle], quote_price: float) -> StrategySignal:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    volumes = [candle.volume for candle in candles]
    score = 0.0
    notes: list[str] = []
    high_252 = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    low_252 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    avg_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else _mean(volumes[:-1])
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 1.0
    distance_from_high = ((high_252 - quote_price) / high_252) * 100 if high_252 else 100.0
    distance_from_50 = ((quote_price - sma_50) / sma_50) * 100 if sma_50 else 0.0
    if distance_from_high <= 5:
        score += 0.24
        notes.append("within 5% of 52-week high")
    elif distance_from_high <= 10:
        score += 0.14
        notes.append("within 10% of 52-week high")
    if ret_126 >= 15:
        score += 0.18
        notes.append("6-month momentum above 15%")
    if ret_63 >= 8:
        score += 0.12
        notes.append("3-month momentum above 8%")
    if sma_50 and quote_price > sma_50:
        score += 0.12
        notes.append("above 50 SMA")
    if sma_50 and sma_200 and sma_50 > sma_200 and quote_price > sma_200:
        score += 0.16
        notes.append("50 SMA above 200 SMA")
    if low_252 and quote_price >= low_252 * 1.25:
        score += 0.08
        notes.append("well above yearly low")
    if volume_ratio >= 1.15:
        score += 0.08
        notes.append("volume above recent average")
    if distance_from_50 > 18:
        score -= 0.12
        notes.append("extended above 50 SMA")
    direction = "BUY" if score >= 0.64 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("fifty_two_week_high_momentum", round(bounded, 3), direction, round(bounded, 3), notes)


def choose_best_strategy(signals: list[StrategySignal]) -> StrategySignal:
    actionable = [signal for signal in signals if signal.direction != "HOLD"]
    if not actionable:
        return max(signals, key=lambda signal: signal.confidence, default=signals[0])
    return max(actionable, key=lambda signal: abs(signal.score) * signal.confidence)


def _minervini_trend_template(closes: list[float], quote_price: float) -> StrategySignal:
    sma_50 = _sma(closes, 50)
    sma_150 = _sma(closes, 150)
    sma_200 = _sma(closes, 200)
    low_52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    high_52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    notes: list[str] = []
    score = 0.0

    if sma_50 and quote_price > sma_50:
        score += 0.18
        notes.append("price above 50 SMA")
    if sma_150 and quote_price > sma_150:
        score += 0.16
        notes.append("price above 150 SMA")
    if sma_200 and quote_price > sma_200:
        score += 0.16
        notes.append("price above 200 SMA")
    if sma_50 and sma_150 and sma_50 > sma_150:
        score += 0.14
        notes.append("50 SMA above 150 SMA")
    if sma_150 and sma_200 and sma_150 > sma_200:
        score += 0.14
        notes.append("150 SMA above 200 SMA")
    if len(closes) >= 220 and sma_200 and sma_200 > _sma(closes[:-20], 200):
        score += 0.12
        notes.append("200 SMA rising")
    if quote_price >= low_52 * 1.3:
        score += 0.1
        notes.append("price at least 30% above period low")
    if high_52 and quote_price >= high_52 * 0.75:
        score += 0.1
        notes.append("price within 25% of period high")

    direction = "BUY" if score >= 0.68 else "HOLD"
    return StrategySignal("minervini_trend_template", round(score, 3), direction, round(score, 3), notes)


def _split_evenly(values: list[Candle], parts: int) -> list[list[Candle]]:
    if parts <= 0:
        return []
    size = len(values) // parts
    output: list[list[Candle]] = []
    for index in range(parts):
        start = index * size
        end = (index + 1) * size if index < parts - 1 else len(values)
        output.append(values[start:end])
    return output


def _base_range_pct(candles: list[Candle]) -> float | None:
    lows = [candle.low for candle in candles if candle.low]
    highs = [candle.high for candle in candles if candle.high]
    if not lows or not highs:
        return None
    low = min(lows)
    return ((max(highs) - low) / low) * 100 if low else None


def _vcp_breakout(candles: list[Candle], quote_price: float) -> StrategySignal:
    if len(candles) < 45:
        return StrategySignal("vcp_breakout", 0.0, "HOLD", 0.0, ["need at least 45 candles for VCP base"])

    base = candles[-65:] if len(candles) >= 65 else candles[-45:]
    setup = base[:-1]
    current = base[-1]
    if len(setup) < 44:
        return StrategySignal("vcp_breakout", 0.0, "HOLD", 0.0, ["need completed base before breakout candle"])

    thirds = _split_evenly(setup, 3)
    contraction_ranges = [_base_range_pct(segment) for segment in thirds]
    progressive_contraction = (
        all(value is not None for value in contraction_ranges)
        and contraction_ranges[1] <= contraction_ranges[0] * 0.85
        and contraction_ranges[2] <= contraction_ranges[1] * 0.85
    )
    base_high = max(candle.high for candle in setup)
    base_low = min(candle.low for candle in setup)
    base_width_pct = ((base_high - base_low) / base_low) * 100 if base_low else 100.0
    early_volume = _mean(candle.volume for candle in setup[: max(12, len(setup) // 3)])
    late_volume = _mean(candle.volume for candle in setup[-10:])
    baseline_volume = _mean(candle.volume for candle in setup[-21:-1])
    volume_dryup = bool(early_volume) and late_volume <= early_volume * 0.75
    volume_ratio = (current.volume / baseline_volume) if baseline_volume else None
    breakout = quote_price > base_high and current.close >= base_high * 0.995
    breakout_volume = volume_ratio is not None and volume_ratio >= 1.25
    score = 0.0
    notes: list[str] = []
    if progressive_contraction:
        score += 0.35
        notes.append("three-leg volatility contraction")
    else:
        notes.append("contraction cycles not progressive")
    if 4 <= len(setup) // 5 <= 13 and base_width_pct <= 35:
        score += 0.15
        notes.append("base duration/width acceptable")
    elif base_width_pct > 35:
        notes.append("base too wide for clean VCP")
    if volume_dryup:
        score += 0.20
        notes.append("volume dry-up")
    if breakout:
        score += 0.20
        notes.append("breakout through full-base pivot")
    if breakout_volume:
        score += 0.15
        notes.append("breakout volume confirmation")
    elif breakout:
        notes.append("breakout needs volume confirmation")
    direction = "BUY" if score >= 0.75 and breakout and breakout_volume else "HOLD"
    return StrategySignal("vcp_breakout", round(score, 3), direction, round(score, 3), notes)


def _darvas_box_breakout(
    candles: list[Candle],
    quote_price: float,
    state: dict[str, Any] | None = None,
) -> StrategySignal:
    if len(candles) < 25:
        return StrategySignal("darvas_box_breakout", 0.0, "HOLD", 0.0, ["need at least 25 candles for confirmed Darvas box"])

    state = state if isinstance(state, dict) else {}
    current = candles[-1]
    box = _active_darvas_box_from_state(state)
    source = "persisted_state"
    if box is None:
        box = _find_confirmed_darvas_box(candles[:-1])
        source = "reconstructed_from_history"
    if box is None:
        return StrategySignal(
            "darvas_box_breakout",
            0.0,
            "HOLD",
            0.0,
            ["no static 3-session Darvas pivot confirmed"],
            {"state_update": _darvas_state_update(None, current, "NO_BOX", source)},
        )

    box_high = float(box["box_high"])
    box_low = float(box["box_low"])
    box_width_pct = ((box_high - box_low) / box_low) * 100 if box_low else 100
    box_duration = _darvas_box_duration(candles, box)
    baseline_volume = _mean(candle.volume for candle in candles[-21:-1])
    volume_ratio = current.volume / baseline_volume if baseline_volume else 1.0
    confirmed_breakout = quote_price > box_high and current.close >= box_high
    downside_break = quote_price < box_low or current.close < box_low
    volume_confirmed = volume_ratio >= 1.3 and len(candles) >= 2 and current.close > candles[-2].close
    score = 0.0
    notes: list[str] = []
    status = "ACTIVE"
    if 5 <= box_duration <= 45:
        score += 0.2
        notes.append("static Darvas box held after pivot")
    if box_width_pct <= 15:
        score += 0.25
        notes.append("compact box")
    elif box_width_pct > 22:
        notes.append("box too wide")
    if confirmed_breakout:
        score += 0.25
        notes.append("box breakout")
    if volume_confirmed:
        score += 0.25
        notes.append("breakout volume confirmation")
    elif confirmed_breakout:
        notes.append("breakout needs volume confirmation")
    if downside_break:
        score = min(score, 0.2)
        status = "INVALIDATED"
        notes.append("box invalidated by downside break")
    elif confirmed_breakout and volume_confirmed:
        status = "BROKEN_UP"
    direction = "BUY" if score >= 0.75 and status == "BROKEN_UP" else "HOLD"
    state_update = _darvas_state_update(box, current, status, source)
    state_update["box_duration_candles"] = box_duration
    metadata = {
        "box_high": round(box_high, 4),
        "box_low": round(box_low, 4),
        "box_width_pct": round(box_width_pct, 4),
        "box_duration_candles": box_duration,
        "volume_ratio": round(volume_ratio, 4),
        "state_source": source,
        "state_update": state_update,
    }
    return StrategySignal("darvas_box_breakout", round(score, 3), direction, round(score, 3), notes, metadata)


def _active_darvas_box_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    if str(state.get("status") or "").upper() != "ACTIVE":
        return None
    box_high = _float_or_none(state.get("box_high"))
    box_low = _float_or_none(state.get("box_low"))
    if box_high is None or box_low is None or box_high <= box_low or box_low <= 0:
        return None
    return {
        "box_high": box_high,
        "box_low": box_low,
        "pivot_index": int(state.get("pivot_index") or 0),
        "pivot_ts": state.get("pivot_ts"),
        "confirmed_at": state.get("confirmed_at"),
        "box_duration_candles": int(state.get("box_duration_candles") or 0),
    }


def _darvas_box_duration(candles: list[Candle], box: dict[str, Any]) -> int:
    pivot_ts = box.get("pivot_ts")
    if pivot_ts:
        for index, candle in enumerate(candles):
            if candle.ts == pivot_ts:
                return max(0, len(candles) - index)
    return max(0, int(box.get("box_duration_candles") or 0))


def _find_confirmed_darvas_box(candles: list[Candle]) -> dict[str, Any] | None:
    if len(candles) < 24:
        return None
    start = max(0, len(candles) - 60)
    for index in range(len(candles) - 4, start - 1, -1):
        pivot_high = candles[index].high
        if pivot_high <= 0:
            continue
        held_three_sessions = all(candles[index + offset].high <= pivot_high for offset in range(1, 4))
        not_broken_after_confirmation = all(candle.high <= pivot_high for candle in candles[index + 4 :])
        if not held_three_sessions or not not_broken_after_confirmation:
            continue
        confirmation_slice = candles[index + 1 : index + 4]
        if not confirmation_slice:
            continue
        box_low = min(candle.low for candle in confirmation_slice)
        if box_low <= 0 or ((pivot_high - box_low) / box_low) * 100 > 22:
            continue
        return {
            "box_high": pivot_high,
            "box_low": box_low,
            "pivot_index": index,
            "pivot_ts": candles[index].ts,
            "confirmed_at": candles[index + 3].ts,
        }
    return None


def _darvas_state_update(box: dict[str, Any] | None, candle: Candle, status: str, source: str) -> dict[str, Any]:
    payload = {
        "status": status,
        "source": source,
        "last_candle_ts": candle.ts,
        "last_close": round(float(candle.close), 4),
    }
    if box:
        payload.update(
            {
                "box_high": round(float(box["box_high"]), 4),
                "box_low": round(float(box["box_low"]), 4),
                "pivot_index": int(box.get("pivot_index") or 0),
                "pivot_ts": box.get("pivot_ts"),
                "confirmed_at": box.get("confirmed_at"),
            }
        )
        if status in {"ACTIVE", "BROKEN_UP"}:
            payload["status"] = "ACTIVE" if status == "ACTIVE" else "BROKEN_UP"
    return payload


def _ema_pullback_continuation(closes: list[float], quote_price: float) -> StrategySignal:
    ema_21 = _ema(closes, 21)
    ema_50 = _ema(closes, 50)
    notes: list[str] = []
    score = 0.0
    if ema_21 and ema_50 and ema_21 > ema_50:
        score += 0.35
        notes.append("21 EMA above 50 EMA")
    if ema_21 and abs(quote_price - ema_21) / ema_21 <= 0.015:
        score += 0.28
        notes.append("price pulling into 21 EMA")
    if len(closes) >= 3 and closes[-1] > closes[-2] > closes[-3]:
        score += 0.17
        notes.append("short-term bounce")
    direction = "BUY" if score >= 0.58 else "HOLD"
    return StrategySignal("ema_pullback_continuation", round(score, 3), direction, round(score, 3), notes)


def _bollinger_squeeze_breakout(closes: list[float], volumes: list[float], quote_price: float) -> StrategySignal:
    recent = closes[-20:]
    basis = _mean(recent)
    sigma = _pstdev(recent) if len(recent) > 1 else 0
    upper = basis + (2 * sigma)
    width_pct = ((upper - (basis - (2 * sigma))) / basis) * 100 if basis else 100
    prior_widths = []
    for i in range(max(20, len(closes) - 80), len(closes) - 20):
        sample = closes[i : i + 20]
        sample_mean = _mean(sample) if len(sample) == 20 else 0.0
        if sample_mean:
            prior_widths.append(((4 * _pstdev(sample)) / sample_mean) * 100)
    squeeze = bool(prior_widths) and width_pct <= sorted(prior_widths)[max(0, int(len(prior_widths) * 0.2) - 1)]
    baseline_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else 0.0
    volume_ratio = volumes[-1] / baseline_volume if baseline_volume else None
    volume_confirmed = volume_ratio is not None and volume_ratio >= 1.25
    score = 0.0
    notes: list[str] = []
    if squeeze:
        score += 0.3
        notes.append("low volatility squeeze")
    if quote_price > upper:
        score += 0.25
        notes.append("upper band breakout")
    if volume_confirmed:
        score += 0.25
        notes.append("volume confirms squeeze break")
    elif quote_price > upper:
        notes.append("upper band break needs volume confirmation")
    if len(closes) >= 2 and closes[-1] > closes[-2]:
        score += 0.08
        notes.append("close momentum positive")
    direction = "BUY" if score >= 0.7 and squeeze and quote_price > upper and volume_confirmed else "HOLD"
    return StrategySignal("bollinger_squeeze_breakout", round(score, 3), direction, round(score, 3), notes)


def _rsi_mean_reversion(closes: list[float], quote_price: float) -> StrategySignal:
    rsi = _rsi(closes, 14)
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    score = 0.0
    notes: list[str] = []
    if rsi is not None and rsi < 32:
        score += 0.35
        notes.append("RSI oversold")
    if len(closes) >= 3 and closes[-1] > closes[-2] and closes[-2] < closes[-3]:
        score += 0.25
        notes.append("first rebound candle")
    if sma_50 and quote_price > sma_50:
        score += 0.12
        notes.append("above 50 SMA")
    if sma_200 and quote_price > sma_200:
        score += 0.08
        notes.append("above 200 SMA")
    if sma_50 and sma_200 and sma_50 < sma_200:
        score -= 0.18
        notes.append("major trend still weak")
    direction = "BUY" if score >= 0.64 else "HOLD"
    return StrategySignal("rsi_mean_reversion", round(score, 3), direction, round(score, 3), notes)


def _donchian_momentum_breakout(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    quote_price: float,
) -> StrategySignal:
    channel_high = max(highs[-55:-1]) if len(highs) >= 56 else max(highs[:-1])
    channel_low = min(lows[-55:-1]) if len(lows) >= 56 else min(lows[:-1])
    average_volume = _mean(volumes[-20:-1]) if len(volumes) >= 21 else _mean(volumes[:-1])
    volume_ratio = volumes[-1] / average_volume if average_volume else 1.0
    trend = _sma(closes, 20) and _sma(closes, 50) and _sma(closes, 20) > _sma(closes, 50)
    score = 0.0
    notes: list[str] = []
    if quote_price > channel_high:
        score += 0.38
        notes.append("55-period channel breakout")
    if trend:
        score += 0.24
        notes.append("20 SMA above 50 SMA")
    if volume_ratio >= 1.25:
        score += 0.18
        notes.append("volume confirmation")
    if channel_low and ((channel_high - channel_low) / channel_low) <= 0.18:
        score += 0.12
        notes.append("controlled channel width")
    direction = "BUY" if score >= 0.62 else "HOLD"
    return StrategySignal("donchian_momentum_breakout", round(score, 3), direction, round(score, 3), notes)


def _volume_price_accumulation(candles: list[Candle], quote_price: float) -> StrategySignal:
    recent = candles[-20:]
    up_volume = sum(candle.volume for candle in recent if candle.close >= candle.open)
    down_volume = sum(candle.volume for candle in recent if candle.close < candle.open)
    closes = [candle.close for candle in candles]
    score = 0.0
    notes: list[str] = []
    if down_volume and up_volume / down_volume >= 1.35:
        score += 0.28
        notes.append("up-volume accumulation")
    if len(closes) >= 10 and closes[-1] >= max(closes[-10:-1]):
        score += 0.22
        notes.append("near 10-period closing high")
    if _ema(closes, 10) and _ema(closes, 21) and _ema(closes, 10) > _ema(closes, 21):
        score += 0.2
        notes.append("10 EMA above 21 EMA")
    if len(recent) >= 2 and recent[-1].close > recent[-1].open and recent[-1].volume > _mean([c.volume for c in recent[:-1]]) * 1.15:
        score += 0.16
        notes.append("fresh demand candle")
    direction = "BUY" if score >= 0.58 else "HOLD"
    return StrategySignal("volume_price_accumulation", round(score, 3), direction, round(score, 3), notes)


def _failed_breakdown_reversal(candles: list[Candle], quote_price: float) -> StrategySignal:
    recent = candles[-20:]
    prior_lows = [candle.low for candle in recent[:-1]]
    last = recent[-1]
    previous = recent[-2]
    score = 0.0
    notes: list[str] = []
    if prior_lows and previous.close < min(prior_lows[:-1] or prior_lows) and quote_price > previous.high:
        score += 0.34
        notes.append("failed breakdown reclaimed prior high")
    if last.close > last.open:
        score += 0.14
        notes.append("bullish reclaim candle")
    if last.volume > _mean([candle.volume for candle in recent[:-1]]) * 1.2:
        score += 0.18
        notes.append("reversal volume")
    if _rsi([candle.close for candle in candles], 14) and _rsi([candle.close for candle in candles], 14) < 45:
        score += 0.1
        notes.append("reversal from lower RSI zone")
    direction = "BUY" if score >= 0.56 else "HOLD"
    return StrategySignal("failed_breakdown_reversal", round(score, 3), direction, round(score, 3), notes)


def _anchored_vwap_reclaim(candles: list[Candle], quote_price: float, intraday_candles: list[Candle]) -> StrategySignal:
    source = intraday_candles[-80:] if len(intraday_candles) >= 20 else candles[-60:]
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in source]
    if len(source) < 20:
        return StrategySignal("vwap_reclaim_order_flow", 0.0, "HOLD", 0.0, ["needs at least 20 candles for VWAP/order-flow analysis"])
    vwap = _vwap(source)
    last = source[-1]
    previous = source[-2]
    volume_ratio = _volume_ratio(volumes, 20)
    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    score = 0.0
    notes: list[str] = ["intraday VWAP/order-flow proxy" if len(intraday_candles) >= 20 else "daily candle VWAP proxy; connect intraday/L2 feed for higher precision"]
    if vwap and quote_price > vwap:
        score += 0.24
        notes.append(f"price above anchored VWAP {vwap:.2f}")
    if vwap and previous.close <= vwap <= max(last.close, quote_price):
        score += 0.22
        notes.append("fresh VWAP reclaim")
    elif vwap and last.low <= vwap * 1.01 and quote_price > vwap:
        score += 0.14
        notes.append("pullback held near VWAP")
    if sma_20 and sma_50 and sma_20 > sma_50 and quote_price > sma_20:
        score += 0.16
        notes.append("VWAP signal aligns with rising 20/50 trend")
    if last.close > last.open and volume_ratio >= 1.2:
        score += 0.18
        notes.append("demand candle with above-average volume")
    if _close_location(last) >= 0.7:
        score += 0.08
        notes.append("latest candle closed in upper range")
    if vwap and quote_price > vwap * 1.08:
        score -= 0.18
        notes.append("price is extended more than 8% above VWAP")
    direction = "BUY" if score >= 0.66 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("vwap_reclaim_order_flow", round(bounded, 3), direction, round(bounded, 3), notes)


def _volume_profile_value_area_breakout(candles: list[Candle], quote_price: float) -> StrategySignal:
    profile = _volume_profile(candles[-80:])
    if not profile:
        return StrategySignal("volume_profile_value_area_breakout", 0.0, "HOLD", 0.0, ["needs candle volume for volume-profile analysis"])
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    last = candles[-1]
    volume_ratio = _volume_ratio(volumes, 20)
    score = 0.0
    notes: list[str] = [
        f"POC {profile['poc']:.2f}",
        f"value area {profile['val']:.2f}-{profile['vah']:.2f}",
    ]
    if quote_price > profile["vah"]:
        score += 0.24
        notes.append("price is above value-area high")
    if len(closes) >= 2 and closes[-2] <= profile["vah"] < quote_price:
        score += 0.18
        notes.append("fresh value-area breakout")
    if volume_ratio >= 1.25:
        score += 0.18
        notes.append("breakout has volume confirmation")
    if _close_location(last) >= 0.72:
        score += 0.10
        notes.append("close held near candle high")
    if quote_price > profile["poc"]:
        score += 0.10
        notes.append("trading above point of control")
    distance_from_poc = ((quote_price - profile["poc"]) / profile["poc"]) * 100 if profile["poc"] else 0.0
    if distance_from_poc > 10:
        score -= 0.14
        notes.append("too extended above point of control")
    direction = "BUY" if score >= 0.66 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("volume_profile_value_area_breakout", round(bounded, 3), direction, round(bounded, 3), notes)


def _liquidity_sweep_reclaim(candles: list[Candle], quote_price: float) -> StrategySignal:
    recent = candles[-30:]
    if len(recent) < 22:
        return StrategySignal("liquidity_sweep_reclaim", 0.0, "HOLD", 0.0, ["needs at least 22 candles for liquidity-sweep analysis"])
    last = recent[-1]
    previous_window = recent[-21:-1]
    prior_low = min(candle.low for candle in previous_window)
    prior_high = max(candle.high for candle in previous_window)
    volumes = [candle.volume for candle in candles]
    volume_ratio = _volume_ratio(volumes, 20)
    score = 0.0
    notes: list[str] = []
    swept_low = last.low < prior_low and last.close > prior_low
    swept_high_failed = last.high > prior_high and last.close < prior_high
    if swept_low:
        score += 0.36
        notes.append("sell-side liquidity sweep reclaimed prior support")
    if last.close > last.open:
        score += 0.12
        notes.append("reclaim candle closed green")
    if _close_location(last) >= 0.65:
        score += 0.12
        notes.append("reclaim closed in upper candle range")
    if volume_ratio >= 1.25:
        score += 0.16
        notes.append("sweep occurred on elevated volume")
    if quote_price > prior_high:
        score += 0.14
        notes.append("reclaim followed through above recent resistance")
    if swept_high_failed:
        score -= 0.28
        notes.append("buy-side sweep failed back below resistance")
    direction = "BUY" if score >= 0.62 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("liquidity_sweep_reclaim", round(bounded, 3), direction, round(bounded, 3), notes)


def _breadth_aligned_leadership(
    candles: list[Candle],
    quote_price: float,
    market_breadth: dict[str, Any],
    sector_context: dict[str, Any],
) -> StrategySignal:
    closes = [candle.close for candle in candles]
    highs = [candle.high for candle in candles]
    volumes = [candle.volume for candle in candles]
    regime = str(market_breadth.get("breadth_regime") or "neutral")
    pct_above_50 = float(market_breadth.get("pct_above_50dma") or 0.0)
    ret_63 = _return_pct(closes, 63)
    ret_126 = _return_pct(closes, 126)
    high_126 = max(highs[-126:]) if len(highs) >= 126 else max(highs)
    volume_ratio = _volume_ratio(volumes, 20)
    score = 0.0
    notes: list[str] = []
    if regime in {"bull_confirmed", "bull_weakening"} and pct_above_50 >= 50:
        score += 0.20
        notes.append("market breadth allows selective long exposure")
    elif regime in {"bear_warning", "bear_confirmed"}:
        score -= 0.25
        notes.append("market breadth is defensive")
    if ret_63 >= 10 and ret_126 >= 18:
        score += 0.22
        notes.append("3- and 6-month leadership confirmed")
    if high_126 and quote_price >= high_126 * 0.95:
        score += 0.14
        notes.append("price is within 5% of 6-month high")
    if sector_context.get("sector_tailwind"):
        score += 0.14
        notes.append("sector rotation is a tailwind")
    if sector_context.get("sector_headwind"):
        score -= 0.18
        notes.append("sector rotation is a headwind")
    if volume_ratio >= 1.15:
        score += 0.10
        notes.append("leadership has volume participation")
    direction = "BUY" if score >= 0.64 else "HOLD"
    bounded = _clamp01(score)
    return StrategySignal("breadth_aligned_leadership", round(bounded, 3), direction, round(bounded, 3), notes)


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return _mean(values[-window:])


def _ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    alpha = 2 / (window + 1)
    ema = _mean(values[:window])
    for value in values[window:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema


def _rsi(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    average_gain = _mean(gains[:window])
    average_loss = _mean(losses[:window])
    for gain, loss in zip(gains[window:], losses[window:]):
        average_gain = ((average_gain * (window - 1)) + gain) / window
        average_loss = ((average_loss * (window - 1)) + loss) / window
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def _return_pct(values: list[float], window: int) -> float:
    if len(values) <= window:
        return 0.0
    base = values[-window - 1]
    if not base:
        return 0.0
    return ((values[-1] - base) / base) * 100


def _atr_pct(candles: list[Candle], window: int) -> float:
    if len(candles) <= window:
        return 0.0
    true_ranges: list[float] = []
    for previous, current in zip(candles[-(window + 1) : -1], candles[-window:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr = _mean(true_ranges)
    close = candles[-1].close
    return (atr / close) * 100 if close else 0.0


def _return_volatility_pct(values: list[float], window: int) -> float:
    if len(values) <= window:
        return 0.0
    returns: list[float] = []
    for previous, current in zip(values[-(window + 1) : -1], values[-window:]):
        if previous:
            returns.append(((current - previous) / previous) * 100)
    return _pstdev(returns)


def _volume_ratio(values: list[float], window: int) -> float:
    if len(values) <= window:
        return 1.0
    base = _mean([float(value or 0.0) for value in values[-(window + 1) : -1]])
    return float(values[-1] or 0.0) / base if base else 1.0


def _vwap(candles: list[Candle]) -> float | None:
    total_volume = sum(float(candle.volume or 0.0) for candle in candles)
    if total_volume <= 0:
        return None
    total_value = sum(((candle.high + candle.low + candle.close) / 3.0) * float(candle.volume or 0.0) for candle in candles)
    return total_value / total_volume


def _close_location(candle: Candle) -> float:
    spread = candle.high - candle.low
    if spread <= 0:
        return 0.5
    return max(0.0, min(1.0, (candle.close - candle.low) / spread))


def _volume_profile(candles: list[Candle], buckets: int = 24) -> dict[str, float] | None:
    usable = [candle for candle in candles if candle.volume and candle.high >= candle.low]
    if len(usable) < 20:
        return None
    low = min(candle.low for candle in usable)
    high = max(candle.high for candle in usable)
    if high <= low:
        return None
    bucket_count = max(8, min(buckets, len(usable)))
    step = (high - low) / bucket_count
    volumes = [0.0 for _ in range(bucket_count)]
    for candle in usable:
        typical = (candle.high + candle.low + candle.close) / 3.0
        index = min(bucket_count - 1, max(0, int((typical - low) / step)))
        volumes[index] += float(candle.volume or 0.0)
    total = sum(volumes)
    if total <= 0:
        return None
    poc_index = max(range(bucket_count), key=lambda index: volumes[index])
    ordered = sorted(range(bucket_count), key=lambda index: volumes[index], reverse=True)
    selected: list[int] = []
    running = 0.0
    for index in ordered:
        selected.append(index)
        running += volumes[index]
        if running >= total * 0.70:
            break
    val_index = min(selected)
    vah_index = max(selected)
    return {
        "poc": low + (poc_index + 0.5) * step,
        "val": low + val_index * step,
        "vah": low + (vah_index + 1.0) * step,
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(float(value or 0.0), 1.0))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_signal(signal: StrategySignal) -> StrategySignal:
    score = _clamp01(signal.score)
    confidence = _clamp01(signal.confidence)
    return StrategySignal(
        name=signal.name,
        score=round(score, 3),
        direction=signal.direction,
        confidence=round(confidence, 3),
        notes=signal.notes,
        metadata=signal.metadata,
    )
