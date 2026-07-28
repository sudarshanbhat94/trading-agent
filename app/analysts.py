"""Independent analysts and a CIO that reconciles them.

`recommendation.py` blends every signal into one score. That is useful, but it
cannot tell you *who disagrees*: a bullish chart against a risk-off tape and a
bad filing collapses into a mid-range number that reads like mild conviction
rather than a genuine fight.

So each analyst here owns one domain, reaches its own conclusion with its own
confidence, and cites its own evidence. The CIO then reconciles them and — the
point of the arrangement — reports the dissent explicitly instead of averaging
it away.

Deterministic, like the rest of this stack. No model is called: `LLMBrain` is
disabled on this deployment anyway, and an analyst that invents its rationale
is worse than no analyst.

Conventions:

- `stance` runs -1.0 (maximally bearish) to +1.0 (maximally bullish).
- An analyst with no usable facts **abstains**: confidence 0.0, excluded from
  the consensus. Abstaining and voting neutral are different things, and
  collapsing them would let missing data masquerade as balance.
- Nothing here influences trading. The engine's lanes and gates are untouched.
"""

from __future__ import annotations

import logging

_LOG = logging.getLogger("openstocks.analysts")


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _dict(value):
    return value if isinstance(value, dict) else {}


def _clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def _opinion(agent, stance, confidence, rationale, evidence):
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    return {
        "agent": agent,
        "stance": round(_clamp(stance), 3),
        "confidence": confidence,
        "rationale": rationale,
        "evidence": evidence,
        "abstained": confidence <= 0.0,
    }


def _abstain(agent, reason):
    return _opinion(agent, 0.0, 0.0, reason, [])


def technical_analyst(facts):
    """Trend, structure and location, from price and indicators."""
    technicals = _dict(facts.get("technicals"))
    price = _num(facts.get("price"))
    close = _num(facts.get("close"))
    sma20, sma50 = _num(facts.get("sma20")), _num(facts.get("sma50"))
    supertrend = _dict(technicals.get("supertrend"))
    kijun = _num(_dict(technicals.get("ichimoku")).get("kijun"))

    votes, evidence = [], []
    if close is not None and sma20 is not None and sma50 is not None:
        votes.append((0.5 * (close > sma20) + 0.3 * (close > sma50)
                      + 0.2 * (sma20 > sma50)) * 2 - 1)
        evidence.append({"metric": "close/sma20/sma50",
                         "value": [round(close, 2), round(sma20, 2), round(sma50, 2)],
                         "source": "daily candles"})
    direction = supertrend.get("direction")
    if direction in ("up", "down"):
        votes.append(1.0 if direction == "up" else -1.0)
        evidence.append({"metric": "supertrend", "value": direction,
                         "source": "indicators.supertrend"})
    if kijun is not None and price is not None and kijun > 0:
        votes.append(_clamp((price / kijun - 1) * 12))
        evidence.append({"metric": "ichimoku.kijun", "value": round(kijun, 2),
                         "source": "indicators.ichimoku"})

    if not votes:
        return _abstain("technical", "No price history or indicators available")
    stance = sum(votes) / len(votes)
    spread = sum(abs(v - stance) for v in votes) / len(votes)
    confidence = (len(votes) / 3.0) * max(0.0, 1.0 - spread / 2.0)
    if technicals.get("stale"):
        confidence *= 0.6
        evidence.append({"metric": "as_of", "value": technicals.get("as_of"),
                         "source": "candle freshness"})
    trend = "bullish" if stance > 0.15 else "bearish" if stance < -0.15 else "mixed"
    return _opinion("technical", stance, confidence,
                    f"Price structure is {trend} on {len(votes)} of 3 checks", evidence)


def catalyst_analyst(facts):
    """Fresh corporate filings and news tone."""
    sentiment = _num(facts.get("news_score"))
    items = facts.get("catalysts")
    if not isinstance(items, (list, tuple)):
        items = facts.get("news")
    if not isinstance(items, (list, tuple)):
        items = []
    material = [c for c in items if isinstance(c, dict)
                and str(c.get("label") or c.get("type") or "").lower()
                in ("results", "order", "corp_action")]

    if sentiment is None and not material:
        return _abstain("catalyst", "No fresh filings or scored news")

    evidence, stance = [], 0.0
    if sentiment is not None:
        stance = _clamp(sentiment)
        evidence.append({"metric": "news_score", "value": round(sentiment, 2),
                         "source": "sentiment_events"})
    for item in material[:3]:
        evidence.append({"metric": "filing",
                         "value": str(item.get("headline") or item.get("title") or "")[:120],
                         "source": "nse_announcements"})
    # A material filing sharpens whatever the tone already says. On its own it
    # is a reason to look, not a direction — so it adds confidence, not stance.
    confidence = 0.35 + 0.2 * min(len(material), 2) + (0.25 if sentiment is not None else 0.0)
    tone = "positive" if stance > 0.1 else "negative" if stance < -0.1 else "neutral"
    rationale = (f"Fresh material filing with {tone} news tone" if material
                 else f"News tone {tone}, no material filing")
    return _opinion("catalyst", stance, confidence, rationale, evidence)


def risk_analyst(facts):
    """Volatility, regime, liquidity and data quality.

    Structurally one-sided: risk never argues to buy. Its stance is zero or
    negative, so it can veto enthusiasm but never manufacture it.
    """
    technicals = _dict(facts.get("technicals"))
    atr_pct = _num(facts.get("atr_pct"))
    rvol = _num(facts.get("rvol"))
    regime_on = facts.get("regime_on")

    penalties, evidence, notes = [], [], []
    if atr_pct is not None:
        if atr_pct >= 0.04:
            penalties.append(min(1.0, (atr_pct - 0.04) / 0.04 + 0.4))
            notes.append(f"daily range {atr_pct * 100:.1f}% of price")
        evidence.append({"metric": "atr_pct", "value": round(atr_pct, 4),
                         "source": "v2_engine features"})
    if regime_on is False:
        penalties.append(0.5)
        notes.append("market regime risk-off")
        evidence.append({"metric": "regime_on", "value": False, "source": "v2_engine regime"})
    if rvol is not None:
        if rvol < 0.7:
            penalties.append(0.3)
            notes.append(f"thin volume at {rvol:.1f}x average")
        evidence.append({"metric": "rvol", "value": round(rvol, 2),
                         "source": "v2_engine features"})
    if technicals.get("stale"):
        penalties.append(0.4)
        notes.append(f"indicators as of {technicals.get('as_of')}")
        evidence.append({"metric": "stale", "value": True, "source": "candle freshness"})

    if not evidence:
        return _abstain("risk", "No risk inputs available")
    stance = -_clamp(sum(penalties), 0.0, 1.0)
    confidence = min(1.0, 0.4 + 0.2 * len(evidence))
    rationale = ("Elevated risk: " + "; ".join(notes)) if notes else "No elevated risk detected"
    return _opinion("risk", stance, confidence, rationale, evidence)


def position_analyst(facts):
    """Whether an existing holding changes the picture."""
    held = facts.get("held")
    if not held:
        return _abstain("position", "No open position in this name")
    held = _dict(held)
    pnl = _num(held.get("pnl"))
    evidence = [{"metric": "position",
                 "value": {"strategy": held.get("strategy"), "pnl_pct": pnl},
                 "source": "v2_positions"}]
    if pnl is None:
        return _opinion("position", 0.0, 0.3, "Position held; P&L unavailable", evidence)
    # Neither branch is a directional forecast, so the stance stays small: an
    # extended winner is a concentration question, a loser is what the stop is
    # for. Both argue against adding, not about where the price goes.
    stance = -0.3 if pnl > 15 else (-0.2 if pnl < -5 else 0.0)
    rationale = (f"Already held and up {pnl:.1f}% — adding increases concentration"
                 if pnl > 15 else
                 f"Already held and down {pnl:.1f}% — the stop, not a top-up, is the plan"
                 if pnl < -5 else f"Already held, {pnl:+.1f}%")
    return _opinion("position", stance, 0.45, rationale, evidence)


ANALYSTS = (technical_analyst, catalyst_analyst, risk_analyst, position_analyst)


def chief_investment_officer(opinions):
    """Reconcile the analysts and report where they disagree.

    Consensus is confidence-weighted over analysts that did NOT abstain. The
    valuable output is `dissent`: when a bull and a bear genuinely conflict the
    CIO names both and lowers its own confidence, rather than letting them
    cancel into false calm.
    """
    opinions = [o for o in (opinions or []) if isinstance(o, dict)]
    active = [o for o in opinions if not o.get("abstained")]
    abstained = [o.get("agent") for o in opinions if o.get("abstained")]

    if not active:
        return {"stance": 0.0, "confidence": 0.0, "consensus": "no view", "dissent": [],
                "participating": 0, "abstained": abstained,
                "rationale": "Every analyst abstained for lack of data"}

    weight = sum(o.get("confidence", 0.0) for o in active) or 1.0
    stance = sum(o.get("stance", 0.0) * o.get("confidence", 0.0) for o in active) / weight

    bulls = [o for o in active if o.get("stance", 0.0) > 0.15]
    bears = [o for o in active if o.get("stance", 0.0) < -0.15]
    dissent = []
    if bulls and bears:
        dissent = [f"{o['agent']} ({'bullish' if o['stance'] > 0 else 'bearish'}): {o['rationale']}"
                   for o in bulls + bears]

    spread = sum(abs(o.get("stance", 0.0) - stance) * o.get("confidence", 0.0)
                 for o in active) / weight
    confidence = (weight / len(ANALYSTS)) * max(0.0, 1.0 - spread / 2.0)

    consensus = ("bullish" if stance > 0.3 else "leaning bullish" if stance > 0.1
                 else "bearish" if stance < -0.3 else "leaning bearish" if stance < -0.1
                 else "neutral")
    rationale = f"{len(active)} of {len(ANALYSTS)} analysts reporting; consensus {consensus}"
    if dissent:
        rationale += f", with {len(bulls)} bullish against {len(bears)} bearish"
    return {
        "stance": round(_clamp(stance), 3),
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "consensus": consensus,
        "dissent": dissent,
        "participating": len(active),
        "abstained": abstained,
        "rationale": rationale,
    }


def analyse(facts):
    """Run every analyst and reconcile. Never raises."""
    facts = _dict(facts)
    opinions = []
    for analyst in ANALYSTS:
        name = getattr(analyst, "__name__", "unknown").replace("_analyst", "")
        try:
            opinions.append(analyst(facts))
        except Exception:
            # One broken analyst must not silence the panel.
            _LOG.exception("analyst %s failed", name)
            opinions.append(_abstain(name, "Analyst failed"))
    try:
        cio = chief_investment_officer(opinions)
    except Exception:
        _LOG.exception("CIO reconciliation failed")
        cio = {"stance": 0.0, "confidence": 0.0, "consensus": "no view", "dissent": [],
               "participating": 0, "abstained": [], "rationale": "Reconciliation failed"}
    return {"opinions": opinions, "cio": cio}
