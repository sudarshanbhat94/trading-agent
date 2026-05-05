from __future__ import annotations

from .models import TechnicalSnapshot


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) <= window:
        return None
    gains = []
    losses = []
    recent = values[-(window + 1) :]
    for previous, current in zip(recent, recent[1:]):
        change = current - previous
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    average_gain = sum(gains) / window
    average_loss = sum(losses) / window
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def technical_snapshot(prices: list[float]) -> TechnicalSnapshot:
    fast = _sma(prices, 5)
    slow = _sma(prices, 20)
    rsi = _rsi(prices)
    momentum = None
    score = 0.0
    trend = "warming-up"

    if len(prices) >= 6:
        previous = prices[-6]
        momentum = ((prices[-1] - previous) / previous) * 100 if previous else 0
        score += max(min(momentum / 4, 0.35), -0.35)

    if fast is not None and slow is not None:
        spread = (fast - slow) / slow if slow else 0
        score += max(min(spread * 18, 0.45), -0.45)
        if spread > 0.002:
            trend = "uptrend"
        elif spread < -0.002:
            trend = "downtrend"
        else:
            trend = "flat"

    if rsi is not None:
        if rsi < 30:
            score += 0.2
        elif rsi > 72:
            score -= 0.25

    score = max(min(score, 1.0), -1.0)
    return TechnicalSnapshot(
        score=score,
        trend=trend,
        rsi=rsi,
        sma_fast=fast,
        sma_slow=slow,
        momentum_pct=momentum,
    )
