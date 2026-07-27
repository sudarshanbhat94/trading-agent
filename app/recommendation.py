"""Structured, evidence-grounded stock recommendations.

The brief asks for a seven-level call with confidence, reasoning, a bull and
bear case, risks, catalysts, levels, targets, a time horizon — and that it
never hallucinates.

That last requirement drives the design: this module is **deterministic**. It
does not ask a language model to write an opinion. Every statement it emits is
generated from a stored numeric fact and carries an `evidence` entry naming the
metric, its value and where it came from. If a fact is missing the signal is
dropped and confidence falls, rather than the gap being filled with prose.

Nothing here influences what the engine trades. `api_stock`'s existing
`verdict` (BUY / WATCH / AVOID) and the lane logic in `v2_live` are untouched;
this is an additional, richer read of the same facts for a human.
"""

from __future__ import annotations

import logging

_LOG = logging.getLogger("openstocks.recommendation")

# Ordered worst -> best. Index doubles as the rating score.
RATINGS = ("Strong Sell", "Sell", "Reduce", "Hold", "Accumulate", "Buy", "Strong Buy")
HOLD_INDEX = RATINGS.index("Hold")

# Composite score cut-points. A composite of 0 is neutral; the ladder is
# symmetric so a bearish setup is graded as strictly as a bullish one.
_BANDS = ((-0.60, 0), (-0.35, 1), (-0.12, 2), (0.12, 3), (0.35, 4), (0.60, 5))


def _band(composite: float) -> int:
    for threshold, index in _BANDS:
        if composite < threshold:
            return index
    return len(RATINGS) - 1


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class _Signal:
    """One weighted opinion, with the fact that produced it."""

    __slots__ = ("name", "score", "weight", "claim", "metric", "value", "source")

    def __init__(self, name, score, weight, claim, metric, value, source):
        self.name = name
        self.score = _clamp(score)
        self.weight = weight
        self.claim = claim
        self.metric = metric
        self.value = value
        self.source = source

    def as_evidence(self) -> dict:
        return {"claim": self.claim, "metric": self.metric,
                "value": self.value, "source": self.source}


def _dict(value):
    """Coerce to a mapping. A malformed field should cost its own signal, not
    the whole recommendation — losing a good conviction score because
    `technicals` arrived as a string would be the wrong failure."""
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def _num(value):
    """Return a float, or None when the fact is absent or unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number      # reject NaN


def _collect_signals(facts: dict) -> list[_Signal]:
    """Build the weighted signal set from whatever facts are present."""
    signals: list[_Signal] = []
    price = _num(facts.get("price"))
    technicals = _dict(facts.get("technicals"))

    # 1. The engine's own conviction. Highest weight: it is the score the
    #    deterministic multi-factor model already produced for this name.
    conviction = _num(facts.get("conviction"))
    if conviction is not None:
        signals.append(_Signal(
            "conviction", (conviction - 0.5) * 2.0, 0.34,
            f"Engine conviction {conviction:.2f} on a 0-1 scale",
            "conviction", round(conviction, 3), "v2_engine.conviction",
        ))

    # 2. Moving-average structure.
    close, sma20, sma50 = _num(facts.get("close")), _num(facts.get("sma20")), _num(facts.get("sma50"))
    if close is not None and sma20 is not None and sma50 is not None:
        above20, above50, stacked = close > sma20, close > sma50, sma20 > sma50
        score = (0.5 * above20 + 0.3 * above50 + 0.2 * stacked) * 2 - 1
        state = ("above its 20- and 50-day averages" if above20 and above50
                 else "above its 20-day average" if above20
                 else "below its key moving averages")
        signals.append(_Signal(
            "trend", score, 0.16, f"Price is {state}",
            "close/sma20/sma50", [round(close, 2), round(sma20, 2), round(sma50, 2)],
            "daily candles",
        ))

    # 3. SuperTrend direction.
    supertrend = _dict(technicals.get("supertrend"))
    direction = supertrend.get("direction")
    line = _num(supertrend.get("value"))
    if direction in ("up", "down") and line is not None:
        signals.append(_Signal(
            "supertrend", 1.0 if direction == "up" else -1.0, 0.14,
            f"SuperTrend is {direction}, line at {line:.2f}",
            "supertrend", {"direction": direction, "line": round(line, 2)},
            "indicators.supertrend",
        ))

    # 4. Ichimoku baseline.
    kijun = _num(_dict(technicals.get("ichimoku")).get("kijun"))
    if kijun is not None and price is not None and kijun > 0:
        gap = (price / kijun - 1)
        signals.append(_Signal(
            "ichimoku", _clamp(gap * 12), 0.10,
            f"Price is {'above' if price >= kijun else 'below'} the Ichimoku baseline {kijun:.2f}",
            "ichimoku.kijun", round(kijun, 2), "indicators.ichimoku",
        ))

    # 5. Relative strength against the market.
    rs20 = _num(facts.get("rs20"))
    if rs20 is not None:
        signals.append(_Signal(
            "relative_strength", _clamp(rs20 * 12), 0.12,
            ("Outperforming the market over 20 days" if rs20 > 0.01
             else "Lagging the market over 20 days" if rs20 < -0.01
             else "Moving in line with the market"),
            "rs20", round(rs20, 4), "v2_engine features",
        ))

    # 6. News sentiment, when there is any.
    sentiment = _num(facts.get("news_score"))
    if sentiment is not None:
        signals.append(_Signal(
            "news", _clamp(sentiment), 0.14,
            ("Recent news skews positive" if sentiment > 0.1
             else "Recent news skews negative" if sentiment < -0.1
             else "Recent news is mixed or neutral"),
            "news_score", round(sentiment, 2), "sentiment_events",
        ))

    return signals


def _confidence(signals: list[_Signal], stale: bool) -> float:
    """Confidence = how much of the model was available x how much it agrees.

    Deliberately not a probability. It answers "how much should you trust this
    read", which is low when facts are missing or the signals contradict.
    """
    if not signals:
        return 0.0
    total_weight = sum(s.weight for s in signals)
    coverage = min(1.0, total_weight / 1.0)
    mean = sum(s.score * s.weight for s in signals) / total_weight
    spread = sum(abs(s.score - mean) * s.weight for s in signals) / total_weight
    agreement = max(0.0, 1.0 - spread / 2.0)
    confidence = coverage * agreement
    if stale:
        # Indicators computed from a candle set that lags the live price are a
        # weaker basis for a call; say so numerically, not just in prose.
        confidence *= 0.7
    return round(max(0.0, min(1.0, confidence)), 2)


def _levels(facts: dict, price):
    """Support and resistance, each tagged with where it came from."""
    technicals = _dict(facts.get("technicals"))
    pivots = _dict(technicals.get("pivot_points"))
    supertrend = _dict(technicals.get("supertrend"))
    support, resistance = [], []

    def _add(bucket, label, value):
        number = _num(value)
        if number is not None and number > 0:
            bucket.append({"label": label, "price": round(number, 2)})

    _add(support, "pivot S1", pivots.get("s1"))
    _add(support, "pivot S2", pivots.get("s2"))
    _add(resistance, "pivot R1", pivots.get("r1"))
    _add(resistance, "pivot R2", pivots.get("r2"))
    if supertrend.get("direction") == "up":
        _add(support, "SuperTrend", supertrend.get("value"))
    elif supertrend.get("direction") == "down":
        _add(resistance, "SuperTrend", supertrend.get("value"))

    if price is not None:
        # A "support" above spot or "resistance" below it is neither.
        support = [level for level in support if level["price"] < price]
        resistance = [level for level in resistance if level["price"] > price]
    support.sort(key=lambda x: -x["price"])
    resistance.sort(key=lambda x: x["price"])
    return support, resistance


def _risks(facts: dict, held: bool) -> list[str]:
    risks: list[str] = []
    technicals = _dict(facts.get("technicals"))
    atr_pct = _num(facts.get("atr_pct"))
    if atr_pct is not None and atr_pct >= 0.04:
        risks.append(f"High volatility: average daily range is {atr_pct * 100:.1f}% of price")
    if technicals.get("stale"):
        risks.append(f"Indicators are computed from candles up to {technicals.get('as_of')}, "
                     "which lag the live price")
    if not facts.get("regime_on"):
        risks.append("Broad market regime is risk-off, which lowers the odds on long setups")
    rvol = _num(facts.get("rvol"))
    if rvol is not None and rvol < 0.7:
        risks.append(f"Thin participation: volume is {rvol:.1f}x its own average")
    if held:
        risks.append("Position is already held — a poll-based stop fills at the gapped "
                     "open and cannot defend an overnight gap")
    return risks


def _catalysts(facts: dict) -> list[dict]:
    out = []
    for item in _list(facts.get("news"))[:4]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if title:
            out.append({"headline": title[:160], "type": item.get("label") or "news",
                        "when": item.get("when"), "score": item.get("score")})
    return out


def _horizon(rating_index: int, facts: dict) -> str:
    if rating_index == HOLD_INDEX:
        return "No action — reassess on the next catalyst or a break of the levels above"
    hold_days = facts.get("hold_days")
    if isinstance(hold_days, int) and hold_days > 0:
        return f"About {hold_days} trading sessions, matching the swing lane's validated hold"
    return "1-2 weeks (swing horizon)"


def build_recommendation(facts: dict) -> dict:
    """Produce the structured recommendation from stored facts.

    `facts` accepts whatever is available; each missing key simply removes its
    signal and lowers confidence. Never raises — a recommendation failing must
    not take down the stock page.
    """
    try:
        price = _num(facts.get("price"))
        signals = _collect_signals(facts)
        technicals = _dict(facts.get("technicals"))
        stale = bool(technicals.get("stale"))

        if not signals:
            return {
                "rating": "Hold", "rating_score": HOLD_INDEX, "confidence": 0.0,
                "reasoning": [], "bull_case": [], "bear_case": [], "risks": [],
                "catalysts": [], "support": [], "resistance": [], "targets": [],
                "time_horizon": "Insufficient data for a call",
                "evidence": [], "insufficient_data": True,
            }

        total_weight = sum(s.weight for s in signals)
        composite = sum(s.score * s.weight for s in signals) / total_weight
        index = _band(composite)
        confidence = _confidence(signals, stale)

        # A thin or contradictory evidence base must not produce a conviction
        # call. Pull extreme ratings toward Hold rather than overstating.
        if confidence < 0.35 and index in (0, len(RATINGS) - 1):
            index = 1 if index == 0 else len(RATINGS) - 2

        bull = [s.claim for s in sorted(signals, key=lambda s: -s.score * s.weight) if s.score > 0.05]
        bear = [s.claim for s in sorted(signals, key=lambda s: s.score * s.weight) if s.score < -0.05]
        support, resistance = _levels(facts, price)

        targets = []
        primary = _num(facts.get("target"))
        if primary is not None and price is not None and primary > price:
            targets.append({"label": "engine target", "price": round(primary, 2),
                            "upside_pct": round((primary / price - 1) * 100, 2)})
        for level in resistance[:2]:
            targets.append({"label": level["label"], "price": level["price"],
                            "upside_pct": round((level["price"] / price - 1) * 100, 2)
                            if price else None})

        return {
            "rating": RATINGS[index],
            "rating_score": index,
            "confidence": confidence,
            "composite": round(composite, 3),
            "reasoning": [s.claim for s in sorted(signals, key=lambda s: -s.weight)],
            "bull_case": bull,
            "bear_case": bear,
            "risks": _risks(facts, bool(facts.get("held"))),
            "catalysts": _catalysts(facts),
            "support": support,
            "resistance": resistance,
            "entry": _num(facts.get("entry")),
            "stoploss": _num(facts.get("stop")),
            "targets": targets,
            "time_horizon": _horizon(index, facts),
            "evidence": [s.as_evidence() for s in signals],
            "insufficient_data": False,
        }
    except Exception:
        _LOG.exception("recommendation build failed for %s", facts.get("symbol"))
        return {
            "rating": "Hold", "rating_score": HOLD_INDEX, "confidence": 0.0,
            "reasoning": [], "bull_case": [], "bear_case": [], "risks": [],
            "catalysts": [], "support": [], "resistance": [], "targets": [],
            "time_horizon": "Unavailable", "evidence": [], "insufficient_data": True,
        }
