"""Decide CE, PE or no trade on an index — the call an options lane needs.

Buying a call (CE) profits when the index rises, a put (PE) when it falls. An
option also loses value every day it is held, so unlike a stock there is no
"wait and see": a flat market loses money. That makes NO TRADE the correct
answer most of the time, and this module is written so that neutral is what it
returns unless several independent readings agree.

Five readings, each voting bullish or bearish, deliberately drawn from
different kinds of evidence so they can genuinely disagree:

  trend      EMA20 against EMA50, and price against EMA20
  pattern    candlestick structure, via the existing detector in indicators.py
  volume     whether today's move carried above-average participation
  location   where the close sits in the day's range
  positioning put/call open interest, when F&O data is supplied

WHY A VOTE AND NOT A SCORE: the equity engine's conviction score blends
factors into one number, and a blend hides disagreement — a strong trend can
carry a weak, contradicted signal over the line. For a leveraged instrument the
useful question is not "how bullish overall" but "does anything disagree", so
the readings are kept separate and agreement is required.

NOTHING HERE IS VALIDATED YET. It produces a call for display; auto-trading is
off by default and is the operator's explicit choice.
"""
from __future__ import annotations

from . import indicators as ta

BULLISH_PATTERNS = frozenset({
    "hammer", "inverted_hammer", "bullish_marubozu", "bullish_engulfing", "morning_star",
})
BEARISH_PATTERNS = frozenset({
    "hanging_man", "shooting_star", "bearish_marubozu", "bearish_engulfing", "evening_star",
})

# A call needs this many readings agreeing AND a net majority.
#
# Was 3-of-5 with ZERO contradiction, which fired on 12% of sessions — the lane
# spent almost all its time declining. That is not risk management, it is
# absence. Risk on an option position is controlled by SIZE and the STOP, both
# of which are enforced at entry; refusing to trade only guarantees no return.
#
# Now 2 agreeing and strictly more than the other side. A single dissenting
# reading no longer vetoes — five independent readings rarely agree unanimously,
# so demanding it made the strong-signal case unreachable.
MIN_AGREEING = 2
# Five readings, and `confidence` is the share of them that agreed. So the
# HIGHEST confidence a MIN_AGREEING call can ever report is 2/5 = 0.40.
#
# This matters because the options lane ALSO gates on min_confidence, in the
# same units. When MIN_AGREEING was 3 the stored setting 0.60 matched it
# exactly; loosening to 2 left that 0.60 in place, so the lane went on
# demanding 3-of-5 through a number in a JSON file while this constant said 2.
# Every call on 2026-07-31 read confidence 0.40 and was refused. Anything that
# compares against confidence must be checked against this ceiling.
N_READINGS = 5
MAX_CONFIDENCE_AT_MIN_AGREEING = MIN_AGREEING / N_READINGS


def max_confidence(n_readings=N_READINGS):
    """The ceiling `confidence` can reach for a MIN_AGREEING call, given how
    many readings actually voted.

    Must be computed rather than hardcoded: live market internals add readings
    to the five daily ones, so the denominator is no longer always 5. A fixed
    2/5 would let a stored threshold silently out-run what the vote can produce
    the moment the reading count changes — the same failure that had this lane
    refusing every call it generated.
    """
    return MIN_AGREEING / max(1, int(n_readings or N_READINGS))


def _ema(values, span):
    if not values:
        return None
    k = 2.0 / (span + 1.0)
    out = float(values[0])
    for v in values[1:]:
        out = float(v) * k + out * (1 - k)
    return out


def trend_vote(closes):
    """EMA20 vs EMA50, plus price against EMA20."""
    if len(closes) < 50:
        return 0, "insufficient history"
    fast, slow, last = _ema(closes[-20:], 20), _ema(closes[-50:], 50), float(closes[-1])
    if fast is None or slow is None:
        return 0, "no ema"
    if fast > slow and last > fast:
        return 1, f"uptrend (ema20 {fast:.0f} > ema50 {slow:.0f}, price above)"
    if fast < slow and last < fast:
        return -1, f"downtrend (ema20 {fast:.0f} < ema50 {slow:.0f}, price below)"
    return 0, "trend mixed"


def pattern_vote(opens, highs, lows, closes):
    """Candlestick structure on the latest bar, from the shared detector."""
    try:
        found = set(ta.candlestick_patterns(opens, highs, lows, closes) or ())
    except Exception:
        return 0, "pattern detection failed"
    bull, bear = found & BULLISH_PATTERNS, found & BEARISH_PATTERNS
    if bull and not bear:
        return 1, "bullish pattern: " + ", ".join(sorted(bull))
    if bear and not bull:
        return -1, "bearish pattern: " + ", ".join(sorted(bear))
    if bull and bear:
        return 0, "patterns conflict"
    return 0, "no pattern"


def volume_vote(volumes, closes):
    """Participation. Volume confirms direction; it never sets it on its own —
    heavy selling and heavy buying look identical in the volume column."""
    if len(volumes) < 20 or len(closes) < 2:
        return 0, "insufficient volume history"
    recent = [float(v) for v in volumes[-20:] if v]
    if not recent:
        return 0, "no volume"
    average = sum(recent) / len(recent)
    if average <= 0:
        return 0, "no volume"
    ratio = float(volumes[-1]) / average
    if ratio < 1.2:
        return 0, f"volume unremarkable ({ratio:.1f}x)"
    rose = float(closes[-1]) > float(closes[-2])
    return (1 if rose else -1), f"volume {ratio:.1f}x on a {'up' if rose else 'down'} bar"


def location_vote(open_, high, low, close):
    """Where the bar closed within its range: strength shows up as closing near
    the extreme, which is the same idea as the equity lanes' knife guard."""
    if high is None or low is None or high <= low:
        return 0, "no range"
    pos = (float(close) - float(low)) / (float(high) - float(low))
    if pos >= 0.70:
        return 1, f"closed strong ({pos * 100:.0f}% of range)"
    if pos <= 0.30:
        return -1, f"closed weak ({pos * 100:.0f}% of range)"
    return 0, f"closed mid-range ({pos * 100:.0f}%)"


def positioning_vote(put_oi, call_oi):
    """Put/call open interest. Read as positioning, not as a crowd to follow:
    heavy put OI marks a level the market is defending, which is supportive.
    Extremes only — a PCR near 1 says nothing."""
    try:
        put_oi, call_oi = float(put_oi or 0), float(call_oi or 0)
    except (TypeError, ValueError):
        return 0, "no oi"
    if put_oi <= 0 or call_oi <= 0:
        return 0, "no oi"
    pcr = put_oi / call_oi
    if pcr >= 1.3:
        return 1, f"put-heavy positioning (pcr {pcr:.2f})"
    if pcr <= 0.7:
        return -1, f"call-heavy positioning (pcr {pcr:.2f})"
    return 0, f"positioning balanced (pcr {pcr:.2f})"


def decide(opens, highs, lows, closes, volumes, put_oi=None, call_oi=None,
           min_agreeing=MIN_AGREEING, extra_votes=None):
    """Return {call, confidence, votes, reasons, n_readings}.

    `call` is "CE", "PE" or None. None means do not trade, and is the expected
    answer on most days — an option held through a flat market loses to time
    decay, so indecision is a losing position rather than a free one.

    `extra_votes` is {name: (vote, reason)} — live market internals (breadth,
    FII positioning, heavyweight contribution, VIX). The five readings below are
    all computed from DAILY bars, so on any intraday decision they describe
    yesterday. Every one of the extra readings was already in the database and
    unused, which is how a CE was bought on a tape that was 32% advancing with
    FIIs 10:1 short.

    They are added as VOTES, not as a veto. A veto would have blocked both the
    2026-07-30 CE (closed +21.7%) and the 2026-07-31 CE (+33.9% intraday) —
    on 30 Jul the internals read bearish and were simply wrong. Risk on an
    option is held by size and the stop, which are enforced at entry.

    UNVALIDATED: the internals have no history to backtest against, so this is
    shipped on reasoning and tagged. The reading is recorded with every entry so
    it can be read back later.
    """
    votes = {}
    reasons = []
    for name, (vote, why) in {
        "trend": trend_vote(closes),
        "pattern": pattern_vote(opens, highs, lows, closes),
        "volume": volume_vote(volumes, closes),
        "location": location_vote(opens[-1] if opens else None,
                                  highs[-1] if highs else None,
                                  lows[-1] if lows else None,
                                  closes[-1] if closes else None),
        "positioning": positioning_vote(put_oi, call_oi),
    }.items():
        votes[name] = vote
        reasons.append(f"{name}: {why}")
    for name, pair in (extra_votes or {}).items():
        try:
            vote, why = pair
        except (TypeError, ValueError):
            continue
        votes[name] = int(vote)
        reasons.append(f"{name}: {why}")

    bullish = sum(1 for v in votes.values() if v > 0)
    bearish = sum(1 for v in votes.values() if v < 0)
    call = None
    if bullish >= min_agreeing and bullish > bearish:
        call = "CE"
    elif bearish >= min_agreeing and bearish > bullish:
        call = "PE"
    # Confidence is the share of readings that agreed, so it can never exceed 1
    # and never implies more certainty than the number of inputs supports.
    agreed = max(bullish, bearish)
    confidence = round(agreed / len(votes), 2) if call else 0.0
    # n_readings so callers can clamp a stored min_confidence against what this
    # particular vote could actually produce — the denominator is no longer
    # always 5 once live internals are supplied.
    return dict(call=call, confidence=confidence, votes=votes, reasons=reasons,
                bullish=bullish, bearish=bearish, n_readings=len(votes))
