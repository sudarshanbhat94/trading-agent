from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Candle, Quote, utc_now


POSITIVE_KEYWORDS = {
    "results",
    "profit",
    "revenue",
    "earnings",
    "pat",
    "ebitda",
    "approval",
    "usfda",
    "sebi",
    "clearance",
    "settlement",
    "dropped",
    "dismissed",
    "acquisition",
    "merger",
    "order win",
    "contract",
    "capacity",
    "expansion",
    "guidance raised",
    "dividend",
    "buyback",
    "bonus",
    "fundraise closed",
}

SUSPECT_KEYWORDS = {
    "circuit",
    "operator",
    "surveillance",
    "asm",
    "gsm",
    "unusual volume",
    "no news",
    "promoter pledge",
}

EARNINGS_TERMS = {"results", "profit", "revenue", "earnings", "pat", "ebitda"}
OVERHANG_TERMS = {"dropped", "dismissed", "settlement", "clearance", "approval", "sebi"}
NEWS_TERMS = {"order win", "contract", "capacity", "expansion", "acquisition", "merger", "usfda", "dividend", "buyback", "bonus"}


@dataclass(frozen=True)
class GainerLevels:
    pivot: float | None
    entry: float | None
    max_entry: float | None
    stop: float | None
    target1: float | None
    target2: float | None
    target3: float | None


def evaluate_indian_top_gainer_playbook(
    *,
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    market_action: dict[str, Any],
    sentiment: dict[str, Any] | None = None,
    rs_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic NSE top-gainers playbook.

    This is intentionally numeric/rule based. It produces an audit object that
    the UI and strategy engine can consume. It does not call an LLM and it does
    not let narrative text alter computed prices or indicators.
    """

    event_types = {str(item or "").upper() for item in market_action.get("event_types") or []}
    if "TOP_GAINER" not in event_types:
        return {"available": False, "reason": "not_moneycontrol_top_gainer"}

    symbol = str(row.get("symbol") or quote.symbol or market_action.get("symbol") or "").upper()
    price = _first_float(quote.price, market_action.get("price"), market_action.get("currentPrice"))
    gain_pct = _first_float(market_action.get("pct_change"), market_action.get("currPerChange"), market_action.get("perChange"))
    volume = _first_float(quote.volume, market_action.get("volume"))
    avg_volume = _first_float(market_action.get("avg_volume"), market_action.get("avgVol"))
    volume_ratio = _first_float(market_action.get("volume_multiplier"))
    if volume_ratio is None and volume and avg_volume:
        volume_ratio = volume / avg_volume if avg_volume else None
    market_cap_cr = _first_float(market_action.get("market_cap_cr"), market_action.get("mcap"), row.get("market_cap_cr"))
    value_cr = _first_float(market_action.get("value_crore"), market_action.get("value"))
    avg_turnover_cr = None
    if avg_volume and price:
        avg_turnover_cr = (avg_volume * price) / 10_000_000.0
    if avg_turnover_cr is None and value_cr is not None:
        avg_turnover_cr = value_cr / max(volume_ratio or 1.0, 1.0)

    sentiment = sentiment or {}
    rs_context = rs_context or {}
    highs = [float(c.high) for c in candles if c.high is not None and float(c.high) > 0]
    lows = [float(c.low) for c in candles if c.low is not None and float(c.low) > 0]
    closes = [float(c.close) for c in candles if c.close is not None and float(c.close) > 0]
    volumes = [float(c.volume or 0.0) for c in candles]

    labels = [str(item or "") for item in market_action.get("stock_labels") or []]
    headline_text = _headline_text(sentiment, labels, market_action)
    keyword_gate = _keyword_gate(headline_text)
    stage = _weinstein_stage(price, closes, highs, lows, volumes)
    vcp = _vcp_score(candles)
    rs_rank = _first_float(rs_context.get("rs_rank"), rs_context.get("percentile_252"), rs_context.get("percentile_126"))
    rs_improving = bool(rs_context.get("improving"))
    high_52w = max(highs[-252:]) if len(highs) >= 60 else (max(highs) if highs else None)
    low_52w = min(lows[-252:]) if len(lows) >= 60 else (min(lows) if lows else None)
    pct_from_52w_high = ((high_52w - price) / high_52w) * 100 if high_52w and price else None
    pct_from_52w_low = ((price - low_52w) / low_52w) * 100 if low_52w and price else None
    delivery_pct = _first_float(row.get("delivery_pct"), market_action.get("delivery_pct"))

    data_gaps: list[str] = []
    for key, value in (
        ("market_cap", market_cap_cr),
        ("avg_turnover_30d", avg_turnover_cr),
        ("delivery_pct", delivery_pct),
        ("free_float", row.get("free_float_pct")),
        ("listing_age", row.get("listing_date")),
        ("nifty500_membership", row.get("nifty500_member")),
        ("asm_gsm_stage", row.get("surveillance_stage")),
    ):
        if value in (None, ""):
            data_gaps.append(key)

    hard_excludes: list[str] = []
    if market_cap_cr is not None and market_cap_cr < 200:
        hard_excludes.append("market_cap_below_200cr")
    if price is not None and price < 10:
        hard_excludes.append("price_below_10")
    if avg_turnover_cr is not None and avg_turnover_cr < 0.5:
        hard_excludes.append("avg_turnover_below_50_lakh")
    surveillance = str(row.get("surveillance_stage") or row.get("_surveillance_stage") or "").upper()
    row_asm = bool(row.get("asm") or row.get("_asm_surveillance") or surveillance == "ASM")
    row_gsm = bool(row.get("gsm") or row.get("_gsm_surveillance") or surveillance == "GSM")
    if "ASM" in event_types or "GSM" in event_types or row_asm or row_gsm:
        hard_excludes.append("asm_or_gsm_surveillance")

    above_200dma = bool(price and stage.get("ma_200") and price > float(stage["ma_200"]))
    if not stage.get("ma_200") and stage.get("ma_150"):
        above_200dma = bool(price and price > float(stage["ma_150"]))
    if not stage.get("ma_200") and not stage.get("ma_150") and stage.get("ma_50"):
        above_200dma = bool(price and price > float(stage["ma_50"]))

    tier = "TIER 3 - WATCH ONLY"
    tier_reasons: list[str] = []
    is_tier1 = bool(row.get("nifty500_member") or row.get("index_membership") == "NIFTY500")
    tier2_core = (
        (market_cap_cr is None or market_cap_cr > 500)
        and (avg_turnover_cr is None or avg_turnover_cr > 2.0)
    )
    tier2_precheck = (
        above_200dma
        and (volume_ratio or 0.0) > 1.5
        and (rs_improving or (rs_rank is not None and rs_rank >= 70))
    )
    if hard_excludes:
        tier = "HARD EXCLUDE"
        tier_reasons.extend(hard_excludes)
    elif is_tier1:
        tier = "TIER 1"
        tier_reasons.append("known_nifty500_constituent")
    elif tier2_core and tier2_precheck:
        tier = "TIER 2"
        tier_reasons.append("extension_layer_passed_available_liquidity_and_technical_precheck")
        missing_soft = [item for item in data_gaps if item in {"delivery_pct", "free_float", "listing_age", "asm_gsm_stage"}]
        if missing_soft:
            tier_reasons.append("tier2_supporting_data_unavailable_size_down")
    else:
        if not tier2_core:
            tier_reasons.append("tier2_liquidity_or_market_cap_not_confirmed")
        if not tier2_precheck:
            tier_reasons.append("tier2_technical_precheck_failed")

    quant_score = 0
    if stage.get("stage") == "Stage 2":
        quant_score += 25
    elif stage.get("stage") == "Stage 1":
        quant_score += 10
    if vcp["score"] > 7:
        quant_score += 20
    elif vcp["score"] >= 4:
        quant_score += 12
    if rs_rank is not None and rs_rank > 80:
        quant_score += 20
    elif rs_rank is not None and rs_rank >= 60:
        quant_score += 10
    if volume_ratio is not None and volume_ratio > 2:
        quant_score += 15
    elif volume_ratio is not None and volume_ratio >= 1.5:
        quant_score += 8
    if pct_from_52w_high is not None and pct_from_52w_high <= 15:
        quant_score += 10
    elif pct_from_52w_high is not None and pct_from_52w_high <= 30:
        quant_score += 5
    if delivery_pct is not None and delivery_pct > 40:
        quant_score += 10
    elif delivery_pct is not None and delivery_pct >= 30:
        quant_score += 5

    darvas = _darvas_box(candles)
    levels = _levels(price, darvas.get("box_top"), darvas.get("box_bottom"), high_52w, stage.get("stage") == "Stage 2", volume_ratio)
    catalyst = _catalyst_review(keyword_gate, quant_score, headline_text, event_types, gain_pct, pct_from_52w_high, volume_ratio)
    anti_patterns = _anti_patterns(
        price=price,
        gain_pct=gain_pct,
        pivot=levels.pivot,
        volume_ratio=volume_ratio,
        pct_from_52w_high=pct_from_52w_high,
        stage=stage.get("stage"),
        keyword_gate=keyword_gate,
        event_types=event_types,
        market_cap_cr=market_cap_cr,
        avg_turnover_cr=avg_turnover_cr,
    )
    final_signal = _final_signal(
        quant_score=quant_score,
        stage=str(stage.get("stage") or ""),
        volume_ratio=volume_ratio,
        catalyst=catalyst,
        tier=tier,
        hard_excludes=hard_excludes,
        anti_patterns=anti_patterns,
        keyword_gate=keyword_gate,
    )
    strategy_match = _strategy_match(final_signal, catalyst, stage, vcp, event_types)
    audit = _audit_trail(
        symbol=symbol,
        name=str(row.get("name") or market_action.get("name") or symbol),
        gain_pct=gain_pct,
        quant_score=quant_score,
        stage=str(stage.get("stage") or "Unknown"),
        vcp_score=float(vcp.get("score") or 0.0),
        rs_rank=rs_rank,
        volume_ratio=volume_ratio,
        levels=levels,
        catalyst=catalyst,
    )
    return {
        "available": True,
        "source": "moneycontrol_top_gainers_nse_playbook",
        "generated_at": utc_now(),
        "symbol": symbol,
        "name": row.get("name") or market_action.get("name") or symbol,
        "gain_pct": _round(gain_pct),
        "cmp": _round(price),
        "volume": _round(volume),
        "avg_volume": _round(avg_volume),
        "volume_ratio": _round(volume_ratio),
        "sector": row.get("sector") or market_action.get("sector") or "",
        "market_cap_cr": _round(market_cap_cr),
        "avg_turnover_cr": _round(avg_turnover_cr),
        "value_cr": _round(value_cr),
        "tier": tier,
        "tier_reasons": tier_reasons,
        "hard_excluded": bool(hard_excludes),
        "hard_excludes": hard_excludes,
        "data_gaps": data_gaps,
        "quant_score": int(quant_score),
        "setup_confidence": _confidence_bucket(final_signal, quant_score),
        "weinstein": stage,
        "vcp": vcp,
        "relative_strength": {
            "rs_rank": _round(rs_rank),
            "improving": rs_improving,
            "source": rs_context.get("source") or "universe_return_percentile",
        },
        "positioning_52w": {
            "high": _round(high_52w),
            "low": _round(low_52w),
            "pct_below_high": _round(pct_from_52w_high),
            "pct_above_low": _round(pct_from_52w_low),
        },
        "delivery": {
            "delivery_pct": _round(delivery_pct),
            "trend": row.get("delivery_trend") or "unavailable",
        },
        "darvas": darvas,
        "levels": levels.__dict__,
        "keyword_gate": keyword_gate,
        "catalyst_review": catalyst,
        "final_signal": final_signal,
        "strategy_match": strategy_match,
        "anti_patterns": anti_patterns,
        "audit_trail": audit,
        "headlines": _headlines(sentiment),
        "rules_note": "Price levels, indicators, scores, and final signal are deterministic. Text is used only for catalyst tagging and user explanation.",
    }


def build_playbook_dashboard(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = [item for item in records if isinstance(item, dict) and item.get("available")]
    records.sort(
        key=lambda item: (
            _signal_rank(item.get("final_signal")),
            float(item.get("quant_score") or 0),
            float(item.get("gain_pct") or 0),
        ),
        reverse=True,
    )
    excluded = [item for item in records if item.get("hard_excluded") or item.get("tier") == "HARD EXCLUDE"]
    tier3 = [item for item in records if item.get("tier") == "TIER 3 - WATCH ONLY"]
    buys = [item for item in records if item.get("final_signal") in {"STRONG BUY", "MODERATE BUY"}]
    watch = [item for item in records if item.get("final_signal") == "WATCH" or "WATCH" in str(item.get("tier") or "")]
    avoid = [item for item in records if item.get("final_signal") == "AVOID"]
    return {
        "enabled": True,
        "source": "moneycontrol_top_gainers_nse_playbook",
        "generated_at": utc_now(),
        "total_gainers_evaluated": len(records),
        "tier_summary": {
            "tier1": sum(1 for item in records if item.get("tier") == "TIER 1"),
            "tier2": sum(1 for item in records if item.get("tier") == "TIER 2"),
            "tier3_watch": len(tier3),
            "excluded": len(excluded),
        },
        "signal_summary": {
            "strong_buy": sum(1 for item in records if item.get("final_signal") == "STRONG BUY"),
            "moderate_buy": sum(1 for item in records if item.get("final_signal") == "MODERATE BUY"),
            "watch": len(watch),
            "avoid": len(avoid),
        },
        "catalyst_distribution": _counts((item.get("catalyst_review") or {}).get("catalyst_type") for item in records),
        "top_gainer_pct": max([float(item.get("gain_pct") or 0.0) for item in records], default=0.0),
        "buy_signals": buys[:12],
        "tomorrow_watchlist": [
            item
            for item in records
            if item.get("final_signal") in {"WATCH", "QUANT HOLD"} and not item.get("hard_excluded")
        ][:20],
        "do_not_chase": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "reason": _avoid_reason(item),
                "anti_patterns": item.get("anti_patterns") or [],
            }
            for item in records
            if item.get("final_signal") == "AVOID" or item.get("hard_excluded")
        ][:30],
        "records": records[:80],
        "disclaimer": (
            "This analysis is generated by a hybrid quantitative and AI system for educational and informational purposes only. "
            "It does not constitute investment advice or a solicitation to buy or sell securities. All trading involves substantial risk of loss. "
            "Past performance does not guarantee future results. Always conduct your own research and consult a SEBI-registered investment advisor before making decisions."
        ),
    }


def _keyword_gate(text: str) -> dict[str, Any]:
    lowered = text.lower()
    positive = sorted(term for term in POSITIVE_KEYWORDS if term in lowered)
    suspect = sorted(term for term in SUSPECT_KEYWORDS if term in lowered)
    return {
        "positive_keywords": positive,
        "suspect_keywords": suspect,
        "has_positive_keyword": bool(positive),
        "has_suspect_keyword": bool(suspect),
    }


def _weinstein_stage(
    price: float | None,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
) -> dict[str, Any]:
    if not price or len(closes) < 50:
        return {"stage": "Unknown", "buy_permitted": False, "reason": "need_at_least_50_daily_candles"}
    ma_50 = _mean(closes[-50:])
    ma_150 = _mean(closes[-150:]) if len(closes) >= 150 else None
    ma_200 = _mean(closes[-200:]) if len(closes) >= 200 else None
    anchor = ma_150 or ma_50
    prior_anchor = _mean(closes[-180:-30]) if len(closes) >= 180 else _mean(closes[-80:-30]) if len(closes) >= 80 else anchor
    rising = bool(anchor and prior_anchor and anchor > prior_anchor * 1.01)
    falling = bool(anchor and prior_anchor and anchor < prior_anchor * 0.99)
    above_ma = bool(anchor and price > anchor)
    extended_pct = ((price - anchor) / anchor) * 100 if anchor else None
    recent_high = max(highs[-40:]) if len(highs) >= 40 else max(highs)
    prior_high = max(highs[-80:-40]) if len(highs) >= 80 else recent_high
    recent_low = min(lows[-40:]) if len(lows) >= 40 else min(lows)
    prior_low = min(lows[-80:-40]) if len(lows) >= 80 else recent_low
    higher_structure = recent_high >= prior_high and recent_low >= prior_low
    volume_expanding = _mean([v for v in volumes[-20:] if v > 0]) >= _mean([v for v in volumes[-60:-20] if v > 0]) if len(volumes) >= 60 else False
    if above_ma and rising and higher_structure:
        stage = "Stage 2"
        reason = "price above rising long moving average with higher high/low structure"
    elif not above_ma and falling:
        stage = "Stage 4"
        reason = "price below falling long moving average"
    elif extended_pct is not None and extended_pct > 35 and not rising:
        stage = "Stage 3"
        reason = "price extended while long moving average is not rising"
    else:
        stage = "Stage 1"
        reason = "base or transition structure"
    return {
        "stage": stage,
        "buy_permitted": stage == "Stage 2",
        "reason": reason,
        "ma_50": _round(ma_50),
        "ma_150": _round(ma_150),
        "ma_200": _round(ma_200),
        "ma_rising": rising,
        "higher_highs_lows": higher_structure,
        "up_week_volume_expanding_proxy": volume_expanding,
        "extension_from_anchor_ma_pct": _round(extended_pct),
    }


def _vcp_score(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 24:
        return {"score": 0, "contractions": [], "reason": "need_at_least_24_daily_candles"}
    lookback = min(len(candles), 60)
    segment_count = 4 if lookback >= 48 else 3
    width = lookback // segment_count
    base = candles[-lookback:]
    ranges: list[float] = []
    avg_volumes: list[float] = []
    for index in range(segment_count):
        segment = base[index * width : (index + 1) * width]
        highs = [float(item.high) for item in segment if item.high]
        lows = [float(item.low) for item in segment if item.low]
        vols = [float(item.volume or 0) for item in segment if float(item.volume or 0) > 0]
        if not highs or not lows:
            continue
        high = max(highs)
        low = min(lows)
        ranges.append(((high - low) / high) * 100 if high else 0.0)
        avg_volumes.append(_mean(vols))
    smaller = sum(1 for i in range(1, len(ranges)) if ranges[i] < ranges[i - 1] * 0.85)
    volume_dry = sum(1 for i in range(1, len(avg_volumes)) if avg_volumes[i] < avg_volumes[i - 1] * 0.90)
    final_tight = ranges[-1] <= 6.0 if ranges else False
    score = 0
    score += min(smaller, 3) * 2
    score += min(volume_dry, 3)
    if final_tight:
        score += 2
    if lookback >= 45:
        score += 1
    return {
        "score": int(max(0, min(10, score))),
        "contractions": [_round(item) for item in ranges],
        "volume_dryup_steps": volume_dry,
        "base_duration_sessions": lookback,
        "final_tight_zone": final_tight,
        "reason": "successive range and volume contractions" if score >= 4 else "no clean VCP structure",
    }


def _darvas_box(candles: list[Candle]) -> dict[str, Any]:
    if len(candles) < 15:
        return {"available": False, "reason": "need_at_least_15_candles"}
    window = min(40, max(15, len(candles) - 1))
    base = candles[-(window + 1) : -1] if len(candles) > window else candles[-window:]
    highs = [float(item.high) for item in base if item.high]
    lows = [float(item.low) for item in base if item.low]
    if not highs or not lows:
        return {"available": False, "reason": "missing_high_low"}
    return {
        "available": True,
        "window_sessions": len(base),
        "box_top": _round(max(highs)),
        "box_bottom": _round(min(lows)),
        "rule": "highest high and lowest low of the latest 3-8 week consolidation window",
    }


def _levels(
    price: float | None,
    pivot: float | None,
    box_bottom: float | None,
    high_52w: float | None,
    stage2: bool,
    volume_ratio: float | None,
) -> GainerLevels:
    if not price or not pivot:
        return GainerLevels(None, None, None, None, None, None, None)
    entry = max(pivot, price) if price <= pivot * 1.05 else pivot
    stop = entry * 0.93
    if box_bottom and box_bottom < entry:
        stop = min(stop, box_bottom * 0.995)
    target2 = high_52w if high_52w and high_52w > entry else entry * 1.30
    target3 = entry * 1.30 if stage2 and (volume_ratio or 0.0) > 2 else None
    return GainerLevels(
        pivot=_round(pivot),
        entry=_round(entry),
        max_entry=_round(pivot * 1.05),
        stop=_round(stop),
        target1=_round(entry * 1.20),
        target2=_round(target2),
        target3=_round(target3),
    )


def _catalyst_review(
    keyword_gate: dict[str, Any],
    quant_score: int,
    text: str,
    event_types: set[str],
    gain_pct: float | None,
    pct_from_52w_high: float | None,
    volume_ratio: float | None,
) -> dict[str, Any]:
    lowered = text.lower()
    if pct_from_52w_high is not None and pct_from_52w_high > 40 and (gain_pct or 0.0) >= 5 and not keyword_gate["has_positive_keyword"]:
        catalyst_type = "SHORT_COVER"
    elif any(term in lowered for term in EARNINGS_TERMS):
        catalyst_type = "EARNINGS_BEAT"
    elif any(term in lowered for term in OVERHANG_TERMS):
        catalyst_type = "OVERHANG_REMOVAL"
    elif any(term in lowered for term in NEWS_TERMS):
        catalyst_type = "NEWS_CATALYST"
    elif (gain_pct or 0.0) >= 25 and pct_from_52w_high is not None and pct_from_52w_high <= 20:
        catalyst_type = "HIGH_TIGHT_FLAG"
    elif "52_WEEK_HIGH" in event_types or (pct_from_52w_high is not None and pct_from_52w_high <= 5):
        catalyst_type = "TECHNICAL_BREAKOUT"
    else:
        catalyst_type = "SECTOR_ROTATION" if keyword_gate["has_positive_keyword"] else "TECHNICAL_BREAKOUT"
    strength = "WEAK"
    if quant_score >= 70 and (volume_ratio or 0.0) > 2 and catalyst_type != "SHORT_COVER":
        strength = "STRONG"
    elif quant_score >= 50 and (volume_ratio or 0.0) >= 1.5 and catalyst_type != "SHORT_COVER":
        strength = "MODERATE"
    return {
        "catalyst_type": catalyst_type,
        "catalyst_strength": strength,
        "sentiment": "NEGATIVE" if catalyst_type == "SHORT_COVER" else "POSITIVE" if keyword_gate["has_positive_keyword"] or catalyst_type == "TECHNICAL_BREAKOUT" else "NEUTRAL",
        "catalyst_confirmed": bool(keyword_gate["has_positive_keyword"] or catalyst_type in {"TECHNICAL_BREAKOUT", "HIGH_TIGHT_FLAG"}),
        "review_source": "deterministic_keyword_prefilter",
        "llm_product_term": "Catalyst Review",
        "headline_used": text[:180],
    }


def _final_signal(
    *,
    quant_score: int,
    stage: str,
    volume_ratio: float | None,
    catalyst: dict[str, Any],
    tier: str,
    hard_excludes: list[str],
    anti_patterns: list[dict[str, Any]],
    keyword_gate: dict[str, Any],
) -> str:
    anti_codes = {str(item.get("code") or "") for item in anti_patterns}
    if hard_excludes or tier == "TIER 3 - WATCH ONLY" or catalyst.get("catalyst_type") == "SHORT_COVER":
        return "AVOID" if hard_excludes or catalyst.get("catalyst_type") == "SHORT_COVER" else "WATCH"
    if stage in {"Stage 3", "Stage 4"} or "OPERATOR_RISK" in anti_codes:
        return "AVOID"
    if quant_score < 50:
        return "QUANT HOLD"
    strength = catalyst.get("catalyst_strength")
    confirmed = catalyst.get("catalyst_confirmed") is True
    if (
        quant_score >= 70
        and stage == "Stage 2"
        and strength == "STRONG"
        and confirmed
        and (volume_ratio or 0.0) > 2
        and "CHASING" not in anti_codes
    ):
        return "STRONG BUY"
    if quant_score >= 55 and stage in {"Stage 1", "Stage 2"} and strength in {"MODERATE", "STRONG"} and confirmed and (volume_ratio or 0.0) > 1.5:
        return "MODERATE BUY" if "CHASING" not in anti_codes else "WATCH"
    if quant_score >= 50:
        return "WATCH"
    return "QUANT HOLD"


def _anti_patterns(
    *,
    price: float | None,
    gain_pct: float | None,
    pivot: float | None,
    volume_ratio: float | None,
    pct_from_52w_high: float | None,
    stage: str | None,
    keyword_gate: dict[str, Any],
    event_types: set[str],
    market_cap_cr: float | None,
    avg_turnover_cr: float | None,
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if price and pivot and price > pivot * 1.05:
        flags.append({"code": "CHASING", "label": "CHASING - AVOID", "reason": "price is more than 5% above pivot"})
    if price and pivot and price > pivot and (volume_ratio or 0.0) < 1.5:
        flags.append({"code": "FAILED_BREAKOUT_RISK", "label": "FAILED BREAKOUT RISK", "reason": "breakout lacks 1.5x volume confirmation"})
    if pct_from_52w_high is not None and pct_from_52w_high > 40 and (gain_pct or 0.0) >= 5 and not keyword_gate["has_positive_keyword"]:
        flags.append({"code": "SHORT_COVER", "label": "SHORT COVER - AVOID", "reason": "large move from weak 52-week position without company catalyst"})
    if keyword_gate["has_suspect_keyword"] or ((volume_ratio or 0.0) > 5 and not keyword_gate["has_positive_keyword"]):
        flags.append({"code": "OPERATOR_RISK", "label": "OPERATOR RISK - AVOID", "reason": "surveillance/suspect keywords or extreme volume with no catalyst"})
    if stage in {"Stage 3", "Stage 4"}:
        flags.append({"code": "STAGE_TRAP", "label": "STAGE 3/4 TRAP - AVOID", "reason": "Weinstein stage does not permit fresh longs"})
    if "ONLY_BUYERS" in event_types or (avg_turnover_cr is not None and avg_turnover_cr < 2 and (market_cap_cr or 0) < 500):
        flags.append({"code": "ILLIQUID_BREAKOUT", "label": "ILLIQUID BREAKOUT - AVOID", "reason": "liquidity/circuit risk makes execution unreliable"})
    return flags


def _strategy_match(final_signal: str, catalyst: dict[str, Any], stage: dict[str, Any], vcp: dict[str, Any], event_types: set[str]) -> dict[str, Any]:
    catalyst_type = str(catalyst.get("catalyst_type") or "")
    if catalyst_type == "EARNINGS_BEAT" and vcp.get("score", 0) >= 4:
        strategy = "Earnings Momentum + VCP"
        code = "earnings_beat_gap_and_go"
    elif catalyst_type == "OVERHANG_REMOVAL":
        strategy = "Overhang Removal"
        code = "news_catalyst"
    elif stage.get("stage") == "Stage 2" and ("52_WEEK_HIGH" in event_types or "ALL_TIME_HIGH" in event_types):
        strategy = "Weinstein Stage 2 Breakout"
        code = "52_week_high_volume_breakout"
    elif catalyst_type == "HIGH_TIGHT_FLAG":
        strategy = "High-Tight Flag"
        code = "market_action_momentum"
    elif catalyst_type == "SECTOR_ROTATION":
        strategy = "Sector Rotation Surfing"
        code = "market_action_momentum"
    else:
        strategy = "Technical Breakout"
        code = "market_action_momentum"
    return {
        "name": strategy,
        "code": code,
        "applies": final_signal in {"STRONG BUY", "MODERATE BUY", "WATCH"},
    }


def _audit_trail(
    *,
    symbol: str,
    name: str,
    gain_pct: float | None,
    quant_score: int,
    stage: str,
    vcp_score: float,
    rs_rank: float | None,
    volume_ratio: float | None,
    levels: GainerLevels,
    catalyst: dict[str, Any],
) -> dict[str, str]:
    what = f"{name} ({symbol}) is a current NSE top gainer, up {gain_pct:.2f}% today with a {catalyst.get('catalyst_type', 'TECHNICAL_BREAKOUT').lower().replace('_', ' ')} tag." if gain_pct is not None else f"{name} ({symbol}) is on the current NSE top-gainers list."
    why = f"Quant score is {quant_score}/100: {stage}, VCP {vcp_score}/10, RS rank {rs_rank:.0f} and volume {volume_ratio:.2f}x average." if rs_rank is not None and volume_ratio is not None else f"Quant score is {quant_score}/100: {stage}, VCP {vcp_score}/10, with missing RS or volume fields shown in the audit."
    watch = f"Setup is invalid if price cannot hold above stop {levels.stop} after entry near pivot {levels.pivot}." if levels.stop and levels.pivot else "Setup needs complete levels before it can be traded."
    return {"what": what, "why_now": why, "watch": watch}


def _confidence_bucket(final_signal: str, quant_score: int) -> str:
    if final_signal == "STRONG BUY":
        return "Actionable"
    if final_signal == "MODERATE BUY":
        return "Small Size Only"
    if quant_score >= 50:
        return "Watch"
    return "Avoid"


def _headline_text(sentiment: dict[str, Any], labels: list[str], market_action: dict[str, Any]) -> str:
    parts = []
    parts.extend(_headlines(sentiment))
    parts.extend(labels)
    for key in ("reason", "source", "share_url"):
        value = market_action.get(key)
        if value:
            parts.append(str(value))
    return " | ".join(parts)


def _headlines(sentiment: dict[str, Any]) -> list[str]:
    raw = sentiment.get("headlines") if isinstance(sentiment, dict) else []
    return [str(item) for item in (raw or [])[:3] if str(item or "").strip()]


def _avoid_reason(item: dict[str, Any]) -> str:
    if item.get("hard_excludes"):
        return f"Do not buy {item.get('symbol')} - hard exclusion: {', '.join(item.get('hard_excludes') or [])}."
    anti = item.get("anti_patterns") or []
    if anti:
        return f"Do not buy {item.get('symbol')} - {anti[0].get('reason') or anti[0].get('label')}."
    return f"Do not buy {item.get('symbol')} - final signal is {item.get('final_signal')}."


def _signal_rank(value: Any) -> int:
    return {
        "STRONG BUY": 5,
        "MODERATE BUY": 4,
        "WATCH": 3,
        "QUANT HOLD": 2,
        "AVOID": 1,
    }.get(str(value or ""), 0)


def _counts(values: Any) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        key = str(value or "UNKNOWN")
        output[key] = output.get(key, 0) + 1
    return output


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            if value is None:
                continue
            raw = str(value).replace(",", "").replace("%", "").strip()
            if not raw or raw.lower() in {"nan", "none", "-", "infinity", "in,fin,ity.00"}:
                continue
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None else None
