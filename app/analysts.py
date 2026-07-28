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


def macro_analyst(facts):
    """Scheduled calendar events: derivatives expiry, policy weeks, earnings.

    Distinct from `risk_analyst`, which reads the *current* market state
    (volatility, regime, liquidity). This reads the *diary* — events that are
    known in advance and change the character of a session regardless of what
    the tape is doing today.

    Non-positive by construction, like risk: a quiet calendar is the absence of
    a headwind, not a reason to buy. Expects `facts["macro"]`, a dict of flags
    from `macro_calendar`; abstains without it.
    """
    macro = _dict(facts.get("macro"))
    if not macro:
        return _abstain("macro", "No macro calendar context supplied")

    penalties, evidence, notes = [], [], []
    if macro.get("is_expiry_day"):
        penalties.append(0.45)
        notes.append("derivatives expiry today — intraday whipsaw risk")
        evidence.append({"metric": "is_expiry_day", "value": True, "source": "macro_calendar"})
    elif macro.get("is_expiry_week"):
        penalties.append(0.2)
        notes.append("expiry week")
        evidence.append({"metric": "is_expiry_week", "value": True, "source": "macro_calendar"})
    if macro.get("is_rbi_week"):
        penalties.append(0.35)
        notes.append("RBI policy week")
        evidence.append({"metric": "is_rbi_week", "value": True, "source": "macro_calendar"})
    if macro.get("is_budget_week"):
        penalties.append(0.4)
        notes.append("union budget week")
        evidence.append({"metric": "is_budget_week", "value": True, "source": "macro_calendar"})

    days_away = _num(macro.get("earnings_days_away"))
    if days_away is not None and 0 <= days_away <= 3:
        # Mirrors EARNINGS_BLOCK_DAYS in v2_live: the engine already refuses new
        # entries this close to a result, so the analyst should say why.
        penalties.append(0.5)
        notes.append(f"earnings in {int(days_away)} trading day(s)")
        evidence.append({"metric": "earnings_days_away", "value": int(days_away),
                         "source": "macro_calendar"})

    if not evidence:
        evidence.append({"metric": "calendar", "value": "clear", "source": "macro_calendar"})
        return _opinion("macro", 0.0, 0.5, "No scheduled event risk in the window", evidence)

    stance = -_clamp(sum(penalties), 0.0, 1.0)
    confidence = min(1.0, 0.5 + 0.15 * len(evidence))
    return _opinion("macro", stance, confidence, "Event risk: " + "; ".join(notes), evidence)


def fundamental_analyst(facts):
    """Ownership: who holds the company, and which way that is moving.

    The only fundamental this deployment can actually source. Revenue, EPS, PE
    and ROE are blocked — NSE publishes results figures behind ~3,800
    per-record links and Yahoo's quoteSummary now returns 401 — so this
    analyst reads shareholding, which NSE serves inline, and abstains rather
    than pretending to a fuller view.

    Direction: a promoter stake that is RISING across quarters means insiders
    are buying their own company, historically the more informative side of
    the trade. A falling stake is the warning. Magnitudes stay modest: a stake
    can also move through dilution, pledging or an offer for sale, none of
    which is a directional forecast, so this argues at the margin rather than
    carrying a call on its own.
    """
    data = _dict(facts.get("shareholding"))
    if not data:
        return _abstain("fundamental", "No shareholding data for this symbol")
    latest = _dict(data.get("latest"))
    promoter = _num(latest.get("promoter_pct"))
    if promoter is None:
        return _abstain("fundamental", "No promoter holding figure")

    evidence = [{"metric": "promoter_pct", "value": promoter,
                 "source": f"NSE shareholding, {latest.get('as_of')}"}]
    quarters = int(_num(data.get("quarters")) or 1)
    change = _num(data.get("promoter_change_pp"))

    if change is None:
        # One quarter tells you the level but nothing about direction. A high
        # promoter stake is mild reassurance, not a signal.
        stance = 0.15 if promoter >= 50 else 0.0
        return _opinion("fundamental", stance, 0.3,
                        f"Promoter holding {promoter:.2f}%, single quarter — no trend yet",
                        evidence)

    evidence.append({"metric": "promoter_change_pp", "value": change,
                     "source": f"{quarters} quarters of NSE shareholding"})
    # 1pp of promoter stake in a quarter is a meaningful move; 5pp is a large
    # one. Scale between, and cap so this never dominates the panel.
    stance = _clamp(change / 5.0, -0.6, 0.6)
    confidence = min(0.75, 0.3 + 0.15 * quarters)
    if abs(change) < 0.25:
        direction = "steady"
    elif change > 0:
        direction = f"up {change:+.2f}pp"
    else:
        direction = f"down {change:+.2f}pp"
    return _opinion("fundamental", stance, confidence,
                    f"Promoter holding {promoter:.2f}%, {direction} over {quarters} quarters",
                    evidence)


ANALYSTS = (technical_analyst, catalyst_analyst, fundamental_analyst,
             risk_analyst, macro_analyst, position_analyst)


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
