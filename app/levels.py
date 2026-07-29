"""Price levels the Indian market actually reacts to.

Candle patterns are close to worthless without a level: a rejection wick in the
middle of a range is noise, the same wick at yesterday's high is a signal. This
supplies the levels so a pattern reading can be conditioned on location.

Covers list items 17, 18, 21, 22, 23. Two are deliberately absent because they
need intraday bars that are not recorded for the indices yet:
  19 opening range  — needs index 5-min bars (Phase 1)
  20 VWAP           — same
Building either off daily data would produce a number that looks right and
means nothing, which is worse than not having it.

Everything here is a pure function over daily OHLC. `bars` is a list of
(date, open, high, low, close) ordered oldest-first.
"""
from __future__ import annotations

# Strike spacing is what makes round numbers real here: options exist at these
# levels, so price genuinely reacts to them rather than to psychology alone.
ROUND_STEPS = {
    "NIFTY": (50, 100, 500),
    "BANKNIFTY": (100, 500, 1000),
    "FINNIFTY": (50, 100, 500),
    "MIDCPNIFTY": (25, 50, 100),
}
DEFAULT_STEPS = (50, 100, 500)


def _f(value, default=0.0):
    try:
        out = float(value)
        return out if out == out else default
    except (TypeError, ValueError):
        return default


def previous_day(bars):
    """(PDH, PDL, PDC) — the most-watched intraday levels in India."""
    if len(bars) < 2:
        return None
    _d, _o, high, low, close = bars[-2]
    return dict(pdh=_f(high), pdl=_f(low), pdc=_f(close))


def round_levels(spot, symbol="NIFTY", steps=None):
    """Nearest round number above and below, per step size.

    Returned per step rather than collapsed, because a 50-point level and a
    500-point level carry very different weight — the 500 has far more open
    interest sitting on it.
    """
    spot = _f(spot)
    if spot <= 0:
        return {}
    out = {}
    for step in (steps or ROUND_STEPS.get(symbol.upper(), DEFAULT_STEPS)):
        below = (int(spot) // step) * step
        above = below + step
        out[step] = dict(below=float(below), above=float(above),
                         nearest=float(below if spot - below <= above - spot else above))
    return out


def period_extremes(bars, window):
    """(high, low) over the last `window` sessions — weekly ~5, monthly ~21."""
    if not bars or window <= 0:
        return None
    tail = bars[-window:]
    return dict(high=max(_f(b[2]) for b in tail), low=min(_f(b[3]) for b in tail))


def unfilled_gaps(bars, lookback=60, min_pct=0.15):
    """Gaps that price has not yet traded back through.

    A gap is unfilled while no later bar has covered its span. Indian indices
    gap often — seventeen hours of global news arrive at once — and unfilled
    gaps act as magnets. `min_pct` drops the tick-sized gaps that are not
    levels, only rounding.
    """
    if len(bars) < 3:
        return []
    window = bars[-lookback:]
    out = []
    for i in range(1, len(window)):
        prev_high, prev_low = _f(window[i - 1][2]), _f(window[i - 1][3])
        open_, high, low = _f(window[i][1]), _f(window[i][2]), _f(window[i][3])
        if prev_high <= 0:
            continue
        if low > prev_high:                                   # gap UP
            lo, hi, side = prev_high, low, "up"
        elif high < prev_low:                                 # gap DOWN
            lo, hi, side = high, prev_low, "down"
        else:
            continue
        if (hi - lo) / prev_high * 100 < min_pct:
            continue
        # Filling is DIRECTIONAL, and getting this wrong makes every gap look
        # closed the moment the next bar touches its edge. A gap UP is filled
        # only when price trades back DOWN to the prior high; a gap DOWN only
        # when price trades back UP to the prior low. Merely entering the span
        # is not a fill.
        later = window[i + 1:]
        if side == "up":
            filled = any(_f(b[3]) <= lo for b in later)
        else:
            filled = any(_f(b[2]) >= hi for b in later)
        if not filled:
            out.append(dict(date=window[i][0], side=side, low=lo, high=hi))
    return out


def swing_levels(bars, span=2, lookback=60, keep=5):
    """Recent confirmed swing highs and lows.

    A swing needs `span` bars either side, so the newest `span` bars can never
    qualify. That lag is the definition, not a defect: a high is only a high
    once price has failed to exceed it.
    """
    window = bars[-lookback:]
    if len(window) < 2 * span + 1:
        return dict(highs=[], lows=[])
    highs, lows = [], []
    for i in range(span, len(window) - span):
        h = [_f(b[2]) for b in window[i - span:i + span + 1]]
        l = [_f(b[3]) for b in window[i - span:i + span + 1]]
        centre_h, centre_l = _f(window[i][2]), _f(window[i][3])
        if centre_h == max(h) and h.count(centre_h) == 1:
            highs.append(centre_h)
        if centre_l == min(l) and l.count(centre_l) == 1:
            lows.append(centre_l)
    return dict(highs=highs[-keep:], lows=lows[-keep:])


def nearest_level(spot, levels, within_pct=1.0):
    """Closest level to spot, if inside `within_pct`.

    Distance is what makes a level tradeable: a wick at a level two percent away
    is not a reaction to it. Returns None when nothing is close, which is the
    common and correct answer.
    """
    spot = _f(spot)
    if spot <= 0 or not levels:
        return None
    candidates = [(abs(spot - _f(v)) / spot * 100, _f(v), name)
                  for name, v in levels if _f(v) > 0]
    if not candidates:
        return None
    distance, value, name = min(candidates)
    if distance > within_pct:
        return None
    return dict(name=name, level=value, distance_pct=distance,
                side="above" if value > spot else "below")


def summarise(bars, spot, symbol="NIFTY", within_pct=1.0):
    """Every level for one session, plus which one price is currently at."""
    prev = previous_day(bars)
    weekly = period_extremes(bars, 5)
    monthly = period_extremes(bars, 21)
    swings = swing_levels(bars)
    rounds = round_levels(spot, symbol)
    flat = []
    if prev:
        flat += [("PDH", prev["pdh"]), ("PDL", prev["pdl"]), ("PDC", prev["pdc"])]
    if weekly:
        flat += [("weekly_high", weekly["high"]), ("weekly_low", weekly["low"])]
    if monthly:
        flat += [("monthly_high", monthly["high"]), ("monthly_low", monthly["low"])]
    for value in swings["highs"]:
        flat.append(("swing_high", value))
    for value in swings["lows"]:
        flat.append(("swing_low", value))
    # only the widest round step — the 50-pointers are too dense to be levels
    if rounds:
        widest = max(rounds)
        flat.append((f"round_{widest}", rounds[widest]["nearest"]))
    return dict(previous_day=prev, weekly=weekly, monthly=monthly,
                swings=swings, rounds=rounds,
                gaps=unfilled_gaps(bars),
                at_level=nearest_level(spot, flat, within_pct))
