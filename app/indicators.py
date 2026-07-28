from __future__ import annotations

from .models import TechnicalSnapshot


def _mean(values: list[float] | tuple[float, ...]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pstdev(values: list[float] | tuple[float, ...]) -> float:
    if not values:
        return 0.0
    avg = _mean(values)
    return (sum((value - avg) ** 2 for value in values) / len(values)) ** 0.5


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
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    average_gain = sum(gains[:window]) / window
    average_loss = sum(losses[:window]) / window
    for gain, loss in zip(gains[window:], losses[window:]):
        average_gain = ((average_gain * (window - 1)) + gain) / window
        average_loss = ((average_loss * (window - 1)) + loss) / window
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
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
    middle = _mean(recent)
    deviation = _pstdev(recent)
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
    atr = _mean(true_ranges[-window:]) if len(true_ranges) >= window else None
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
    return _mean(dx_values[-window:]) if len(dx_values) >= window else None


def _stochastic(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> tuple[float | None, float | None]:
    if len(closes) < window or not highs or not lows:
        return None, None
    k_values: list[float] = []
    for index in range(window - 1, len(closes)):
        high = max(highs[index - window + 1 : index + 1])
        low = min(lows[index - window + 1 : index + 1])
        k_values.append(50.0 if high == low else ((closes[index] - low) / (high - low)) * 100)
    k = k_values[-1] if k_values else None
    d = _mean(k_values[-3:]) if len(k_values) >= 3 else None
    return k, d


def _volume_ratio(volumes: list[float], window: int = 20) -> float | None:
    if len(volumes) <= window:
        return None
    baseline = _mean(volumes[-(window + 1) : -1])
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
    avg_volume = _mean(volumes[-window:]) or 1
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


# ---------------------------------------------------------------------------
# Additional indicators.
#
# These are public and additive: technical_snapshot() and the TechnicalSnapshot
# model are deliberately untouched, so nothing the engine currently scores on
# changes. Everything is computed locally from OHLCV — no indicator APIs.
#
# House conventions kept from the functions above: return None (or an empty
# result) when there is not enough data rather than raising or guessing, and
# use a simple mean for ATR rather than Wilder smoothing, matching _atr_pct.
# ---------------------------------------------------------------------------


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True range series, one shorter than closes (needs a previous close)."""
    return [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]


def atr(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> float | None:
    """Average true range in price terms (_atr_pct returns it as a percentage)."""
    if len(closes) <= window or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    ranges = _true_ranges(highs, lows, closes)
    return _mean(ranges[-window:]) if len(ranges) >= window else None


def vwap(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    window: int | None = None,
) -> float | None:
    """Volume-weighted average price over `window` bars (all bars if None).

    Uses the typical price (H+L+C)/3. Intraday VWAP is normally anchored to the
    session open, so pass the session's bars.
    """
    if not closes or len(highs) != len(closes) or len(lows) != len(closes) or len(volumes) != len(closes):
        return None
    if window is not None:
        if window <= 0 or len(closes) < window:
            return None
        highs, lows, closes, volumes = highs[-window:], lows[-window:], closes[-window:], volumes[-window:]
    total_volume = sum(volumes)
    if total_volume <= 0:
        return None
    weighted = sum(((h + l + c) / 3) * v for h, l, c, v in zip(highs, lows, closes, volumes))
    return weighted / total_volume


def supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, float | str | None]:
    """SuperTrend line and its direction.

    Bands are built from a rolling ATR around the median price, then carried
    forward: a band only moves in the direction that tightens it, unless price
    has closed through it. Direction flips when close crosses the active band.
    """
    empty: dict[str, float | str | None] = {"value": None, "direction": None, "upper": None, "lower": None}
    if len(closes) <= period or len(highs) != len(closes) or len(lows) != len(closes) or period <= 0:
        return empty

    ranges = _true_ranges(highs, lows, closes)
    if len(ranges) < period:
        return empty

    # ranges[i] corresponds to bar i+1, so bar index = offset + period.
    final_upper: list[float] = []
    final_lower: list[float] = []
    trend_up: list[bool] = []
    line: list[float] = []

    for offset in range(period - 1, len(ranges)):
        bar = offset + 1
        band_atr = _mean(ranges[offset - period + 1 : offset + 1])
        median = (highs[bar] + lows[bar]) / 2
        basic_upper = median + multiplier * band_atr
        basic_lower = median - multiplier * band_atr

        if not final_upper:
            upper, lower = basic_upper, basic_lower
            up = closes[bar] > upper
        else:
            prev_upper, prev_lower = final_upper[-1], final_lower[-1]
            upper = basic_upper if (basic_upper < prev_upper or closes[bar - 1] > prev_upper) else prev_upper
            lower = basic_lower if (basic_lower > prev_lower or closes[bar - 1] < prev_lower) else prev_lower
            if trend_up[-1]:
                up = closes[bar] >= lower
            else:
                up = closes[bar] > upper

        final_upper.append(upper)
        final_lower.append(lower)
        trend_up.append(up)
        line.append(lower if up else upper)

    if not line:
        return empty
    return {
        "value": line[-1],
        "direction": "up" if trend_up[-1] else "down",
        "upper": final_upper[-1],
        "lower": final_lower[-1],
    }


def ichimoku(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
) -> dict[str, float | None]:
    """Ichimoku Kinko Hyo components at the latest bar.

    senkou_a/senkou_b are returned unshifted — they are the values that would be
    plotted `base` bars ahead. chikou is the latest close, plotted `base` bars
    back. Shifting is a charting concern, so it is left to the caller.
    """
    result: dict[str, float | None] = {
        "tenkan": None, "kijun": None, "senkou_a": None, "senkou_b": None, "chikou": None,
    }
    if not closes or len(highs) != len(closes) or len(lows) != len(closes):
        return result

    def _midpoint(window: int) -> float | None:
        if window <= 0 or len(closes) < window:
            return None
        return (max(highs[-window:]) + min(lows[-window:])) / 2

    tenkan = _midpoint(conversion)
    kijun = _midpoint(base)
    result["tenkan"] = tenkan
    result["kijun"] = kijun
    result["senkou_a"] = (tenkan + kijun) / 2 if tenkan is not None and kijun is not None else None
    result["senkou_b"] = _midpoint(span_b)
    result["chikou"] = closes[-1] if len(closes) >= base else None
    return result


def pivot_points(high: float, low: float, close: float, method: str = "classic") -> dict[str, float | None]:
    """Classic or Fibonacci pivot levels from one completed period's H/L/C.

    Feed the previous session's bar to get today's levels.
    """
    if high < low:
        return {}
    span = high - low
    pivot = (high + low + close) / 3

    if method == "fibonacci":
        return {
            "pivot": pivot,
            "r1": pivot + 0.382 * span, "r2": pivot + 0.618 * span, "r3": pivot + span,
            "s1": pivot - 0.382 * span, "s2": pivot - 0.618 * span, "s3": pivot - span,
        }
    if method != "classic":
        raise ValueError(f"unknown pivot method: {method!r}")
    return {
        "pivot": pivot,
        "r1": 2 * pivot - low,
        "r2": pivot + span,
        "r3": high + 2 * (pivot - low),
        "s1": 2 * pivot - high,
        "s2": pivot - span,
        "s3": low - 2 * (high - pivot),
    }


FIBONACCI_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fibonacci_levels(high: float, low: float, uptrend: bool = True) -> dict[str, float]:
    """Retracement levels between a swing low and swing high.

    uptrend=True measures the pullback down from the high (0% at the high);
    uptrend=False measures the bounce up from the low.
    """
    if high < low:
        return {}
    span = high - low
    levels: dict[str, float] = {}
    for ratio in FIBONACCI_RATIOS:
        price = high - span * ratio if uptrend else low + span * ratio
        levels[f"{ratio * 100:.1f}%"] = price
    return levels


# Every name candlestick_patterns() can emit. Callers that let a user choose
# patterns validate against this, so the vocabulary cannot drift from the
# detector that produces it.
CANDLESTICK_PATTERNS = (
    "doji", "hammer", "hanging_man", "inverted_hammer", "shooting_star",
    "bullish_marubozu", "bearish_marubozu", "bullish_engulfing",
    "bearish_engulfing", "morning_star", "evening_star",
)


def _body(open_: float, close: float) -> float:
    return abs(close - open_)


def candlestick_patterns(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    doji_body_ratio: float = 0.1,
    shadow_ratio: float = 2.0,
) -> list[str]:
    """Patterns present on the most recent bar.

    Returns every pattern that matches, since single- and multi-bar patterns can
    legitimately coincide. Empty list when nothing matches or data is short.
    """
    n = len(closes)
    if n == 0 or len(opens) != n or len(highs) != n or len(lows) != n:
        return []

    found: list[str] = []
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    span = h - l
    if span <= 0:
        return []
    body = _body(o, c)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    bullish = c > o

    # --- single bar ---
    if body <= span * doji_body_ratio:
        found.append("doji")
    if body > 0 and lower_shadow >= body * shadow_ratio and upper_shadow <= body:
        found.append("hammer" if bullish else "hanging_man")
    if body > 0 and upper_shadow >= body * shadow_ratio and lower_shadow <= body:
        found.append("inverted_hammer" if bullish else "shooting_star")
    if body >= span * 0.95:
        found.append("bullish_marubozu" if bullish else "bearish_marubozu")

    # --- two bar ---
    if n >= 2:
        po, pc = opens[-2], closes[-2]
        prev_body = _body(po, pc)
        prev_bullish = pc > po
        if prev_body > 0 and body > prev_body:
            if bullish and not prev_bullish and c >= po and o <= pc:
                found.append("bullish_engulfing")
            elif not bullish and prev_bullish and c <= po and o >= pc:
                found.append("bearish_engulfing")

    # --- three bar ---
    if n >= 3:
        first_o, first_c = opens[-3], closes[-3]
        mid_o, mid_c = opens[-2], closes[-2]
        first_body = _body(first_o, first_c)
        mid_body = _body(mid_o, mid_c)
        small_middle = first_body > 0 and mid_body <= first_body * 0.5
        if small_middle and body > 0:
            midpoint = (first_o + first_c) / 2
            if first_c < first_o and bullish and c > midpoint and max(mid_o, mid_c) < first_c:
                found.append("morning_star")
            elif first_c > first_o and not bullish and c < midpoint and min(mid_o, mid_c) > first_c:
                found.append("evening_star")

    return found


def advanced_snapshot(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> dict[str, object]:
    """Every additional indicator in one call, for scanners and AI context.

    Each entry is independently None/empty when its own data requirement is not
    met, so a short history degrades field by field instead of all at once.
    """
    result: dict[str, object] = {
        "atr": atr(highs, lows, closes),
        "vwap": vwap(highs, lows, closes, volumes),
        "supertrend": supertrend(highs, lows, closes),
        "ichimoku": ichimoku(highs, lows, closes),
        "candlestick_patterns": candlestick_patterns(opens, highs, lows, closes),
        "pivot_points": {},
        "fibonacci": {},
    }
    if len(closes) >= 2:
        result["pivot_points"] = pivot_points(highs[-2], lows[-2], closes[-2])
    if len(closes) >= 20:
        window_high, window_low = max(highs[-20:]), min(lows[-20:])
        result["fibonacci"] = fibonacci_levels(window_high, window_low)
    return result


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
