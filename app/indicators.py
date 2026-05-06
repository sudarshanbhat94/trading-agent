from __future__ import annotations

from statistics import mean, pstdev

from .models import TechnicalSnapshot


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    alpha = 2 / (window + 1)
    ema = sum(values[:window]) / window
    for value in values[window:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema


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


def _macd(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(values) < 35:
        return None, None, None
    macd_values: list[float] = []
    for index in range(26, len(values) + 1):
        subset = values[:index]
        fast = _ema(subset, 12)
        slow = _ema(subset, 26)
        if fast is not None and slow is not None:
            macd_values.append(fast - slow)
    signal = _ema(macd_values, 9)
    line = macd_values[-1] if macd_values else None
    histogram = line - signal if line is not None and signal is not None else None
    return line, signal, histogram


def _bollinger(values: list[float], window: int = 20) -> tuple[float | None, float | None]:
    if len(values) < window:
        return None, None
    recent = values[-window:]
    middle = mean(recent)
    deviation = pstdev(recent)
    if deviation == 0:
        return 0.5, 0.0
    upper = middle + (2 * deviation)
    lower = middle - (2 * deviation)
    close = values[-1]
    pct_b = (close - lower) / (upper - lower) if upper != lower else 0.5
    bandwidth = ((upper - lower) / middle) * 100 if middle else None
    return pct_b, bandwidth


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> float | None:
    if len(closes) <= window or not highs or not lows:
        return None
    true_ranges = []
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    atr = mean(true_ranges[-window:]) if len(true_ranges) >= window else None
    return (atr / closes[-1]) * 100 if atr and closes[-1] else None


def _adx(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> float | None:
    if len(closes) <= window * 2 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for index in range(1, len(closes)):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    dx_values: list[float] = []
    for index in range(window, len(true_ranges) + 1):
        tr = sum(true_ranges[index - window : index])
        if tr == 0:
            continue
        plus_di = 100 * sum(plus_dm[index - window : index]) / tr
        minus_di = 100 * sum(minus_dm[index - window : index]) / tr
        denominator = plus_di + minus_di
        if denominator:
            dx_values.append(100 * abs(plus_di - minus_di) / denominator)
    return mean(dx_values[-window:]) if len(dx_values) >= window else None


def _stochastic(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> tuple[float | None, float | None]:
    if len(closes) < window or not highs or not lows:
        return None, None
    k_values: list[float] = []
    for index in range(window - 1, len(closes)):
        high = max(highs[index - window + 1 : index + 1])
        low = min(lows[index - window + 1 : index + 1])
        k_values.append(50.0 if high == low else ((closes[index] - low) / (high - low)) * 100)
    k = k_values[-1] if k_values else None
    d = mean(k_values[-3:]) if len(k_values) >= 3 else None
    return k, d


def _volume_ratio(volumes: list[float], window: int = 20) -> float | None:
    if len(volumes) <= window:
        return None
    baseline = mean(volumes[-(window + 1) : -1])
    return volumes[-1] / baseline if baseline else None


def _obv_slope(closes: list[float], volumes: list[float], window: int = 10) -> float | None:
    if len(closes) <= window or len(volumes) != len(closes):
        return None
    obv = [0.0]
    for previous, current, volume in zip(closes, closes[1:], volumes[1:]):
        if current > previous:
            obv.append(obv[-1] + volume)
        elif current < previous:
            obv.append(obv[-1] - volume)
        else:
            obv.append(obv[-1])
    avg_volume = mean(volumes[-window:]) or 1
    return (obv[-1] - obv[-window]) / (avg_volume * window)


def _cmf(highs: list[float], lows: list[float], closes: list[float], volumes: list[float], window: int = 20) -> float | None:
    if len(closes) < window or len(volumes) != len(closes):
        return None
    mfv = []
    for high, low, close, volume in zip(highs[-window:], lows[-window:], closes[-window:], volumes[-window:]):
        spread = high - low
        multiplier = ((close - low) - (high - close)) / spread if spread else 0
        mfv.append(multiplier * volume)
    volume_sum = sum(volumes[-window:])
    return sum(mfv) / volume_sum if volume_sum else None


def technical_snapshot(
    prices: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> TechnicalSnapshot:
    fast = _sma(prices, 5)
    slow = _sma(prices, 20)
    sma_50 = _sma(prices, 50)
    sma_200 = _sma(prices, 200)
    ema_9 = _ema(prices, 9)
    ema_21 = _ema(prices, 21)
    rsi = _rsi(prices)
    macd_line, macd_signal, macd_histogram = _macd(prices)
    bollinger_pct_b, bollinger_bandwidth_pct = _bollinger(prices)
    highs = highs or []
    lows = lows or []
    volumes = volumes or []
    atr_pct = _atr_pct(highs, lows, prices)
    adx = _adx(highs, lows, prices)
    stochastic_k, stochastic_d = _stochastic(highs, lows, prices)
    volume_ratio = _volume_ratio(volumes)
    obv_slope = _obv_slope(prices, volumes)
    cmf_20 = _cmf(highs, lows, prices, volumes)
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

    if macd_histogram is not None:
        score += max(min(macd_histogram / max(prices[-1], 1), 0.12), -0.12)
    if adx is not None and adx >= 25 and trend == "uptrend":
        score += 0.08
    if adx is not None and adx >= 25 and trend == "downtrend":
        score -= 0.08
    if volume_ratio is not None and volume_ratio >= 1.5 and momentum is not None:
        score += 0.05 if momentum > 0 else -0.05

    if rsi is not None:
        if rsi < 30:
            score += 0.2
        elif rsi > 72:
            score -= 0.25

    score = max(min(score, 1.0), -1.0)
    last = prices[-1] if prices else 0
    return TechnicalSnapshot(
        score=_round(score) or 0.0,
        trend=trend,
        rsi=_round(rsi),
        sma_fast=_round(fast),
        sma_slow=_round(slow),
        momentum_pct=_round(momentum),
        ema_9=_round(ema_9),
        ema_21=_round(ema_21),
        sma_50=_round(sma_50),
        sma_200=_round(sma_200),
        macd_line=_round(macd_line),
        macd_signal=_round(macd_signal),
        macd_histogram=_round(macd_histogram),
        bollinger_pct_b=_round(bollinger_pct_b),
        bollinger_bandwidth_pct=_round(bollinger_bandwidth_pct),
        atr_pct=_round(atr_pct),
        adx=_round(adx),
        stochastic_k=_round(stochastic_k),
        stochastic_d=_round(stochastic_d),
        volume_ratio_20=_round(volume_ratio),
        obv_slope=_round(obv_slope),
        cmf_20=_round(cmf_20),
        distance_from_sma_20_pct=_round(((last - slow) / slow) * 100 if slow and last else None),
        distance_from_sma_50_pct=_round(((last - sma_50) / sma_50) * 100 if sma_50 and last else None),
    )
