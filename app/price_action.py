"""Price-action readings for index direction, each measurable on its own.

The first direction engine combined five weak readings and scored 36% — worse
than chance. The mistake was judging the blend: a composite hides which parts
carry information and which are dragging, exactly as a portfolio return hides
which factors work. These are written as separate functions so each can be
scored against forward returns independently.

The concepts here are the ones a discretionary price-action trader actually
uses, and which the first version had none of:

  market structure   higher highs and higher lows, and the BREAK of that
                     sequence. Trend is a structure, not a moving average.
  liquidity sweep    price pushes through an obvious prior high, fails, and
                     closes back inside. Stops sit above old highs, so the push
                     is often the market taking them rather than a real move.
  compression        a narrow range after a wide one. Volatility cycles, so
                     quiet ranges precede expansion — direction unknown, but
                     size knowable.
  pullback in trend  entering after a retracement rather than at the extreme,
                     which is where trend-followers are supposed to buy and
                     where the first engine explicitly did not.
  level proximity    distance to the nearest prior swing level, since moves
                     stall at levels the market has already reacted to.
  effort vs result   a big range on ordinary volume, or a small range on heavy
                     volume, both say the move is not what it appears.

Every function returns (vote, reason) with vote in {-1, 0, +1} and abstains on
insufficient data rather than guessing. Abstention is a real answer here: an
option held on a weak read loses to decay.
"""
from __future__ import annotations


def _swings(highs, lows, span=2):
    """Indices of confirmed swing highs and lows.

    A swing needs `span` bars either side, so the most recent `span` bars can
    never form one — that lag is inherent, not a bug: a high is only a high once
    price has failed to exceed it.
    """
    swing_highs, swing_lows = [], []
    for i in range(span, len(highs) - span):
        window_h = highs[i - span:i + span + 1]
        window_l = lows[i - span:i + span + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swing_highs.append(i)
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swing_lows.append(i)
    return swing_highs, swing_lows


def market_structure(highs, lows, closes, span=2):
    """Higher highs + higher lows = uptrend; the break of it is the signal."""
    if len(highs) < 4 * span + 6:
        return 0, "insufficient history for structure"
    sh, sl = _swings(highs, lows, span)
    if len(sh) < 2 or len(sl) < 2:
        return 0, "no confirmed swings"
    hh = highs[sh[-1]] > highs[sh[-2]]
    hl = lows[sl[-1]] > lows[sl[-2]]
    lh = highs[sh[-1]] < highs[sh[-2]]
    ll = lows[sl[-1]] < lows[sl[-2]]
    last = closes[-1]
    if hh and hl:
        return 1, f"uptrend structure (HH {highs[sh[-1]]:.0f}, HL {lows[sl[-1]]:.0f})"
    if lh and ll:
        return -1, f"downtrend structure (LH {highs[sh[-1]]:.0f}, LL {lows[sl[-1]]:.0f})"
    # A break of the last swing high out of a downtrend is a change of character.
    if ll and last > highs[sh[-1]]:
        return 1, "break above the last swing high"
    if hh and last < lows[sl[-1]]:
        return -1, "break below the last swing low"
    return 0, "structure unclear"


def liquidity_sweep(opens, highs, lows, closes, span=2, lookback=20):
    """Push through a prior extreme that fails and closes back inside.

    Stops cluster above old highs and below old lows, so a brief push through is
    frequently the market reaching them rather than a genuine breakout. The
    reversal is the tradeable part, which is why this votes AGAINST the sweep.
    """
    if len(highs) < lookback + 2 * span + 2:
        return 0, "insufficient history for sweep"
    sh, sl = _swings(highs[:-1], lows[:-1], span)
    if not sh and not sl:
        return 0, "no prior extremes"
    recent_high = max((highs[i] for i in sh[-lookback:]), default=None)
    recent_low = min((lows[i] for i in sl[-lookback:]), default=None)
    high, low, close, open_ = highs[-1], lows[-1], closes[-1], opens[-1]
    if recent_high and high > recent_high and close < recent_high:
        return -1, f"swept the {recent_high:.0f} high and closed back below"
    if recent_low and low < recent_low and close > recent_low:
        return 1, f"swept the {recent_low:.0f} low and closed back above"
    return 0, "no sweep"


def compression(highs, lows, window=10):
    """Range narrowing against its own recent average.

    Says nothing about direction — only that a move is more likely than usual.
    Returned as a magnitude flag so a scorer can weight other readings by it
    rather than treat it as a side.
    """
    if len(highs) < window + 1:
        return 0, "insufficient history for range"
    ranges = [h - l for h, l in zip(highs[-window:], lows[-window:])]
    average = sum(ranges) / len(ranges)
    if average <= 0:
        return 0, "no range"
    ratio = (highs[-1] - lows[-1]) / average
    if ratio <= 0.6:
        return 1, f"compressed ({ratio:.2f}x average range)"
    if ratio >= 1.8:
        return -1, f"expanded ({ratio:.2f}x average range)"
    return 0, f"range normal ({ratio:.2f}x)"


def trend_pullback(highs, lows, closes, window=20):
    """In an uptrend, has price pulled back rather than extended?

    Trend-followers are supposed to buy retracements, not highs. The first
    engine's `location` vote did the opposite — it rewarded closing at the
    extreme, which is where the move is most likely to pause.
    """
    if len(closes) < window + 5:
        return 0, "insufficient history for pullback"
    hi = max(highs[-window:])
    lo = min(lows[-window:])
    if hi <= lo:
        return 0, "no range"
    pos = (closes[-1] - lo) / (hi - lo)
    rising = closes[-1] > sum(closes[-window:]) / window
    if rising and 0.30 <= pos <= 0.70:
        return 1, f"pullback inside an uptrend ({pos * 100:.0f}% of range)"
    if not rising and 0.30 <= pos <= 0.70:
        return -1, f"pullback inside a downtrend ({pos * 100:.0f}% of range)"
    return 0, f"extended, not a pullback ({pos * 100:.0f}%)"


def effort_vs_result(highs, lows, closes, volumes, window=20):
    """Heavy volume that produces little range is absorption, not conviction."""
    if len(volumes) < window + 1:
        return 0, "insufficient volume history"
    vols = [v for v in volumes[-window:] if v]
    ranges = [h - l for h, l in zip(highs[-window:], lows[-window:])]
    if not vols or not ranges:
        return 0, "no data"
    avg_v = sum(vols) / len(vols)
    avg_r = sum(ranges) / len(ranges)
    if avg_v <= 0 or avg_r <= 0:
        return 0, "no data"
    rel_v = volumes[-1] / avg_v
    rel_r = (highs[-1] - lows[-1]) / avg_r
    if rel_v >= 1.5 and rel_r <= 0.8:
        # Lots of trading, little movement: someone is absorbing the flow.
        rose = closes[-1] > (highs[-1] + lows[-1]) / 2
        return (1 if rose else -1), f"absorption (vol {rel_v:.1f}x, range {rel_r:.1f}x)"
    if rel_v <= 0.7 and rel_r >= 1.5:
        # A big move nobody participated in tends not to hold.
        rose = closes[-1] > (highs[-1] + lows[-1]) / 2
        return (-1 if rose else 1), f"unsupported move (vol {rel_v:.1f}x, range {rel_r:.1f}x)"
    return 0, f"effort matches result (vol {rel_v:.1f}x, range {rel_r:.1f}x)"


READINGS = {
    "structure": lambda o, h, l, c, v: market_structure(h, l, c),
    "sweep": lambda o, h, l, c, v: liquidity_sweep(o, h, l, c),
    "compression": lambda o, h, l, c, v: compression(h, l),
    "pullback": lambda o, h, l, c, v: trend_pullback(h, l, c),
    "effort": lambda o, h, l, c, v: effort_vs_result(h, l, c, v),
}
