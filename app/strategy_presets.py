from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean, pstdev
from typing import Any

from .models import Candle


@dataclass(frozen=True)
class StrategySignal:
    name: str
    score: float
    direction: str
    confidence: float
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_strategy_presets(candles: list[Candle], quote_price: float) -> list[StrategySignal]:
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
        _minervini_trend_template(closes, quote_price),
        _vcp_breakout(candles, quote_price),
        _darvas_box_breakout(highs, lows, closes, volumes, quote_price),
        _ema_pullback_continuation(closes, quote_price),
        _bollinger_squeeze_breakout(closes, quote_price),
        _rsi_mean_reversion(closes, quote_price),
        _donchian_momentum_breakout(highs, lows, closes, volumes, quote_price),
        _volume_price_accumulation(candles, quote_price),
        _failed_breakdown_reversal(candles, quote_price),
    ]
    return signals


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


def _vcp_breakout(candles: list[Candle], quote_price: float) -> StrategySignal:
    ranges = [((candle.high - candle.low) / candle.close) for candle in candles[-30:] if candle.close]
    volumes = [candle.volume for candle in candles[-30:]]
    early_range = mean(ranges[:10])
    late_range = mean(ranges[-10:])
    volume_dryup = mean(volumes[-5:]) < mean(volumes[:15]) * 0.75 if len(volumes) >= 20 else False
    pivot = max(candle.high for candle in candles[-15:-1])
    breakout = quote_price > pivot
    score = 0.0
    notes: list[str] = []
    if late_range < early_range * 0.65:
        score += 0.35
        notes.append("range contraction")
    if volume_dryup:
        score += 0.25
        notes.append("volume dry-up")
    if breakout:
        score += 0.35
        notes.append("pivot breakout")
    direction = "BUY" if score >= 0.65 else "HOLD"
    return StrategySignal("vcp_breakout", round(score, 3), direction, round(score, 3), notes)


def _darvas_box_breakout(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    quote_price: float,
) -> StrategySignal:
    box_high = max(highs[-20:-1])
    box_low = min(lows[-20:-1])
    box_width_pct = ((box_high - box_low) / box_low) * 100 if box_low else 100
    volume_ratio = volumes[-1] / mean(volumes[-20:-1]) if mean(volumes[-20:-1]) else 1
    score = 0.0
    notes: list[str] = []
    if box_width_pct <= 12:
        score += 0.25
        notes.append("compact box")
    if quote_price > box_high:
        score += 0.45
        notes.append("box breakout")
    if volume_ratio > 1.3 and closes[-1] > closes[-2]:
        score += 0.2
        notes.append("breakout volume confirmation")
    direction = "BUY" if score >= 0.6 else "HOLD"
    return StrategySignal("darvas_box_breakout", round(score, 3), direction, round(score, 3), notes)


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


def _bollinger_squeeze_breakout(closes: list[float], quote_price: float) -> StrategySignal:
    recent = closes[-20:]
    basis = mean(recent)
    sigma = pstdev(recent) if len(recent) > 1 else 0
    upper = basis + (2 * sigma)
    width_pct = ((upper - (basis - (2 * sigma))) / basis) * 100 if basis else 100
    prior_widths = []
    for i in range(max(20, len(closes) - 80), len(closes) - 20):
        sample = closes[i : i + 20]
        if len(sample) == 20 and mean(sample):
            prior_widths.append(((4 * pstdev(sample)) / mean(sample)) * 100)
    squeeze = bool(prior_widths) and width_pct <= sorted(prior_widths)[max(0, int(len(prior_widths) * 0.2) - 1)]
    score = 0.0
    notes: list[str] = []
    if squeeze:
        score += 0.35
        notes.append("low volatility squeeze")
    if quote_price > upper:
        score += 0.4
        notes.append("upper band breakout")
    direction = "BUY" if score >= 0.6 else "HOLD"
    return StrategySignal("bollinger_squeeze_breakout", round(score, 3), direction, round(score, 3), notes)


def _rsi_mean_reversion(closes: list[float], quote_price: float) -> StrategySignal:
    rsi = _rsi(closes, 14)
    score = 0.0
    notes: list[str] = []
    if rsi is not None and rsi < 32:
        score += 0.35
        notes.append("RSI oversold")
    if len(closes) >= 3 and closes[-1] > closes[-2] and closes[-2] < closes[-3]:
        score += 0.25
        notes.append("first rebound candle")
    if _sma(closes, 50) and quote_price > _sma(closes, 50):
        score += 0.12
        notes.append("above 50 SMA")
    direction = "BUY" if score >= 0.52 else "HOLD"
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
    average_volume = mean(volumes[-20:-1]) if len(volumes) >= 21 else mean(volumes[:-1])
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
    if len(recent) >= 2 and recent[-1].close > recent[-1].open and recent[-1].volume > mean([c.volume for c in recent[:-1]]) * 1.15:
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
    if last.volume > mean([candle.volume for candle in recent[:-1]]) * 1.2:
        score += 0.18
        notes.append("reversal volume")
    if _rsi([candle.close for candle in candles], 14) and _rsi([candle.close for candle in candles], 14) < 45:
        score += 0.1
        notes.append("reversal from lower RSI zone")
    direction = "BUY" if score >= 0.56 else "HOLD"
    return StrategySignal("failed_breakdown_reversal", round(score, 3), direction, round(score, 3), notes)


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return mean(values[-window:])


def _ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    alpha = 2 / (window + 1)
    ema = mean(values[:window])
    for value in values[window:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema


def _rsi(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    gains = []
    losses = []
    for previous, current in zip(values[-(window + 1) :], values[-window:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    average_gain = mean(gains)
    average_loss = mean(losses)
    if average_loss == 0:
        return 100.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))
