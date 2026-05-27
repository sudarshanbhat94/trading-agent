from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .india_top_gainers import (
    GainerLevels,
    _counts,
    _darvas_box,
    _first_float,
    _headlines,
    _headline_text,
    _mean,
    _round,
    _signal_rank,
    _vcp_score,
    _weinstein_stage,
)
from .market_regions import market_region_for_row
from .models import Candle, Quote, utc_now


POSITIVE_KEYWORDS = {
    "earnings",
    "eps",
    "revenue",
    "guidance",
    "guidance raised",
    "beat",
    "profit",
    "margin",
    "fda",
    "approval",
    "contract",
    "order",
    "partnership",
    "acquisition",
    "merger",
    "buyback",
    "dividend",
    "upgrade",
    "price target",
    "launch",
}

SUSPECT_KEYWORDS = {
    "offering",
    "dilution",
    "reverse split",
    "going concern",
    "meme",
    "reddit",
    "short squeeze",
    "investigation",
    "sec probe",
    "halt",
    "no news",
}

EARNINGS_TERMS = {"earnings", "eps", "revenue", "guidance", "beat", "profit", "margin"}
OVERHANG_TERMS = {"approval", "settlement", "dismissed", "cleared", "fda"}
NEWS_TERMS = {"contract", "order", "partnership", "acquisition", "merger", "buyback", "dividend", "upgrade", "launch"}


@dataclass(frozen=True)
class UsMoverLevels:
    values: GainerLevels
    stop_rule: str
    atr_pct: float | None


def evaluate_us_top_mover_playbook(
    *,
    row: dict[str, Any],
    quote: Quote,
    candles: list[Candle],
    market_action: dict[str, Any],
    sentiment: dict[str, Any] | None = None,
    rs_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic US top-movers playbook.

    Numeric rules produce the signal and levels. Text is used only to tag a
    catalyst and explain the setup to the user.
    """

    action_market = str(market_action.get("market_region") or "").upper()
    if market_region_for_row(row) != "US" or (action_market and action_market != "US"):
        return {"available": False, "reason": "not_us_market"}

    event_types = {str(item or "").upper() for item in market_action.get("event_types") or []}
    material = event_types & {"TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH", "ALL_TIME_HIGH", "PRICE_SHOCKER", "STRONG_INTRADAY_GAIN"}
    if not material:
        return {"available": False, "reason": "not_us_top_mover"}

    symbol = str(row.get("symbol") or quote.symbol or market_action.get("symbol") or "").upper()
    price = _first_float(quote.price, market_action.get("price"))
    gain_pct = _first_float(market_action.get("pct_change"))
    volume = _first_float(quote.volume, market_action.get("volume"))
    avg_volume = _first_float(market_action.get("avg_volume"))
    volume_ratio = _first_float(market_action.get("volume_multiplier"))
    if volume_ratio is None and volume and avg_volume:
        volume_ratio = volume / avg_volume if avg_volume else None
    market_cap = _first_float(
        row.get("market_cap"),
        row.get("market_cap_usd"),
        row.get("market_capitalization"),
        market_action.get("market_cap"),
    )
    avg_dollar_volume = (avg_volume * price) if avg_volume and price else None
    traded_value = (volume * price) if volume and price else None

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
    atr_pct = _atr_pct(highs, lows, closes)

    data_gaps: list[str] = []
    for key, value in (
        ("market_cap_usd", market_cap),
        ("avg_dollar_volume_30d", avg_dollar_volume),
        ("float_or_free_float", row.get("float_shares") or row.get("free_float_pct")),
        ("sec_news_filings", sentiment.get("headline_count") or sentiment.get("event_count")),
    ):
        if value in (None, ""):
            data_gaps.append(key)

    hard_excludes: list[str] = []
    exchange = str(row.get("exchange") or "").upper()
    if exchange in {"OTC", "OTCBB", "PINK"}:
        hard_excludes.append("otc_or_pink_sheet")
    if market_cap is not None and market_cap < 300_000_000:
        hard_excludes.append("market_cap_below_300m")
    if price is not None and price < 2:
        hard_excludes.append("price_below_2")
    if avg_dollar_volume is not None and avg_dollar_volume < 5_000_000:
        hard_excludes.append("avg_dollar_volume_below_5m")

    above_200dma = bool(price and stage.get("ma_200") and price > float(stage["ma_200"]))
    if not stage.get("ma_200") and stage.get("ma_150"):
        above_200dma = bool(price and price > float(stage["ma_150"]))
    if not stage.get("ma_200") and not stage.get("ma_150") and stage.get("ma_50"):
        above_200dma = bool(price and price > float(stage["ma_50"]))

    index_membership = str(row.get("index_membership") or row.get("index") or "").upper()
    tier = "TIER 3 - WATCH ONLY"
    tier_reasons: list[str] = []
    is_core = (
        any(token in index_membership for token in ("S&P500", "SP500", "NASDAQ100", "RUSSELL1000"))
        or ((market_cap or 0.0) >= 10_000_000_000 and (avg_dollar_volume or 0.0) >= 50_000_000)
    )
    tier2_core = (
        (market_cap is None or market_cap >= 1_000_000_000)
        and (avg_dollar_volume is None or avg_dollar_volume >= 10_000_000)
        and (price or 0.0) >= 5.0
    )
    tier2_precheck = (
        above_200dma
        and (volume_ratio or 0.0) >= 1.5
        and (rs_improving or (rs_rank is not None and rs_rank >= 70))
    )
    if hard_excludes:
        tier = "HARD EXCLUDE"
        tier_reasons.extend(hard_excludes)
    elif is_core:
        tier = "TIER 1"
        tier_reasons.append("core_us_liquid_leader")
    elif tier2_core and tier2_precheck:
        tier = "TIER 2"
        tier_reasons.append("us_extension_layer_passed_liquidity_and_technical_precheck")
        if data_gaps:
            tier_reasons.append("supporting_data_unavailable_size_down")
    else:
        if not tier2_core:
            tier_reasons.append("tier2_us_liquidity_market_cap_or_price_not_confirmed")
        if not tier2_precheck:
            tier_reasons.append("tier2_us_technical_precheck_failed")

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
    if avg_dollar_volume is not None and avg_dollar_volume >= 50_000_000:
        quant_score += 10
    elif avg_dollar_volume is not None and avg_dollar_volume >= 10_000_000:
        quant_score += 5

    darvas = _darvas_box(candles)
    levels = _levels(price, darvas.get("box_top"), high_52w, stage.get("stage") == "Stage 2", volume_ratio, atr_pct)
    catalyst = _catalyst_review(keyword_gate, quant_score, headline_text, event_types, gain_pct, pct_from_52w_high, volume_ratio)
    anti_patterns = _anti_patterns(
        price=price,
        gain_pct=gain_pct,
        pivot=levels.values.pivot,
        volume_ratio=volume_ratio,
        pct_from_52w_high=pct_from_52w_high,
        stage=stage.get("stage"),
        keyword_gate=keyword_gate,
        event_types=event_types,
        market_cap=market_cap,
        avg_dollar_volume=avg_dollar_volume,
    )
    final_signal = _final_signal(
        quant_score=quant_score,
        stage=str(stage.get("stage") or ""),
        volume_ratio=volume_ratio,
        catalyst=catalyst,
        tier=tier,
        hard_excludes=hard_excludes,
        anti_patterns=anti_patterns,
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
        levels=levels.values,
        catalyst=catalyst,
    )
    levels_dict = {
        **levels.values.__dict__,
        "stop_rule": levels.stop_rule,
        "atr_pct": _round(levels.atr_pct),
    }
    return {
        "available": True,
        "source": "yahoo_us_top_movers_playbook",
        "label": "US Top Movers Playbook",
        "market_region": "US",
        "generated_at": utc_now(),
        "symbol": symbol,
        "name": row.get("name") or market_action.get("name") or symbol,
        "gain_pct": _round(gain_pct),
        "cmp": _round(price),
        "volume": _round(volume),
        "avg_volume": _round(avg_volume),
        "volume_ratio": _round(volume_ratio),
        "sector": row.get("sector") or market_action.get("sector") or "",
        "market_cap_usd": _round(market_cap),
        "avg_dollar_volume": _round(avg_dollar_volume),
        "traded_value": _round(traded_value),
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
        "delivery": {"delivery_pct": None, "trend": "not_applicable_us"},
        "darvas": darvas,
        "levels": levels_dict,
        "keyword_gate": keyword_gate,
        "catalyst_review": catalyst,
        "final_signal": final_signal,
        "strategy_match": strategy_match,
        "anti_patterns": anti_patterns,
        "audit_trail": audit,
        "headlines": _headlines(sentiment),
        "rules_note": "US price levels, indicators, scores, and final signal are deterministic. Text is used only for catalyst tagging and user explanation.",
    }


def build_us_playbook_dashboard(records: list[dict[str, Any]]) -> dict[str, Any]:
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
        "source": "yahoo_us_top_movers_playbook",
        "label": "US Top Movers Playbook",
        "market_region": "US",
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
            "Past performance does not guarantee future results. Consult a properly licensed financial professional before making decisions."
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


def _levels(
    price: float | None,
    pivot: float | None,
    high_52w: float | None,
    stage2: bool,
    volume_ratio: float | None,
    atr_pct: float | None,
) -> UsMoverLevels:
    if not price or not pivot:
        return UsMoverLevels(GainerLevels(None, None, None, None, None, None, None), "missing_pivot", atr_pct)
    entry = max(pivot, price) if price <= pivot * 1.05 else pivot
    stop_pct = _clamp((atr_pct or 3.5) * 1.6, 5.0, 8.0) / 100.0
    stop = entry * (1.0 - stop_pct)
    risk = entry - stop
    target2 = high_52w if high_52w and high_52w > entry * 1.08 else entry + risk * 3.0
    target3 = entry + risk * 4.0 if stage2 and (volume_ratio or 0.0) > 2 else None
    return UsMoverLevels(
        GainerLevels(
            pivot=_round(pivot),
            entry=_round(entry),
            max_entry=_round(pivot * 1.05),
            stop=_round(stop),
            target1=_round(entry + risk * 2.0),
            target2=_round(target2),
            target3=_round(target3),
        ),
        "atr_aware_5_to_8_pct_below_entry",
        atr_pct,
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
    elif "52_WEEK_HIGH" in event_types or "ALL_TIME_HIGH" in event_types or (pct_from_52w_high is not None and pct_from_52w_high <= 5):
        catalyst_type = "TECHNICAL_BREAKOUT"
    else:
        catalyst_type = "TECHNICAL_BREAKOUT"
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
        "review_source": "deterministic_us_keyword_prefilter",
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
) -> str:
    anti_codes = {str(item.get("code") or "") for item in anti_patterns}
    if hard_excludes or tier == "TIER 3 - WATCH ONLY" or catalyst.get("catalyst_type") == "SHORT_COVER":
        return "AVOID" if hard_excludes or catalyst.get("catalyst_type") == "SHORT_COVER" else "WATCH"
    if stage in {"Stage 3", "Stage 4"} or anti_codes & {"OPERATOR_RISK", "PUMP_RISK"}:
        return "AVOID"
    if quant_score < 50:
        return "QUANT HOLD"
    strength = catalyst.get("catalyst_strength")
    confirmed = catalyst.get("catalyst_confirmed") is True
    if quant_score >= 70 and stage == "Stage 2" and strength == "STRONG" and confirmed and (volume_ratio or 0.0) > 2 and "CHASING" not in anti_codes:
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
    market_cap: float | None,
    avg_dollar_volume: float | None,
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if price and pivot and price > pivot * 1.05:
        flags.append({"code": "CHASING", "label": "CHASING - AVOID", "reason": "price is more than 5% above pivot"})
    if price and pivot and price > pivot and (volume_ratio or 0.0) < 1.5:
        flags.append({"code": "FAILED_BREAKOUT_RISK", "label": "FAILED BREAKOUT RISK", "reason": "breakout lacks 1.5x relative volume confirmation"})
    if pct_from_52w_high is not None and pct_from_52w_high > 40 and (gain_pct or 0.0) >= 5 and not keyword_gate["has_positive_keyword"]:
        flags.append({"code": "SHORT_COVER", "label": "SHORT COVER - AVOID", "reason": "large move from weak 52-week position without company catalyst"})
    if keyword_gate["has_suspect_keyword"] or ((volume_ratio or 0.0) > 8 and not keyword_gate["has_positive_keyword"] and (price or 0.0) < 10):
        flags.append({"code": "OPERATOR_RISK", "label": "PUMP / DILUTION RISK - AVOID", "reason": "suspect headline terms or extreme low-priced volume without catalyst"})
    if stage in {"Stage 3", "Stage 4"}:
        flags.append({"code": "STAGE_TRAP", "label": "STAGE 3/4 TRAP - AVOID", "reason": "Weinstein stage does not permit fresh longs"})
    if (avg_dollar_volume is not None and avg_dollar_volume < 10_000_000) or ((market_cap or 0.0) and market_cap < 1_000_000_000 and (price or 0.0) < 5):
        flags.append({"code": "ILLIQUID_BREAKOUT", "label": "ILLIQUID BREAKOUT - AVOID", "reason": "US mover lacks enough dollar liquidity for reliable execution"})
    if "MOST_ACTIVE" in event_types and not (event_types & {"TOP_GAINER", "VOLUME_SHOCKER", "52_WEEK_HIGH", "PRICE_SHOCKER"}):
        flags.append({"code": "PUMP_RISK", "label": "MOST ACTIVE ONLY - AVOID", "reason": "activity alone is not a directional setup"})
    return flags


def _strategy_match(final_signal: str, catalyst: dict[str, Any], stage: dict[str, Any], vcp: dict[str, Any], event_types: set[str]) -> dict[str, Any]:
    catalyst_type = str(catalyst.get("catalyst_type") or "")
    if catalyst_type == "EARNINGS_BEAT" and vcp.get("score", 0) >= 4:
        strategy = "Earnings Momentum + VCP"
        code = "earnings_beat_gap_and_go"
    elif catalyst_type == "OVERHANG_REMOVAL":
        strategy = "Regulatory Overhang Removal"
        code = "news_catalyst"
    elif stage.get("stage") == "Stage 2" and ("52_WEEK_HIGH" in event_types or "ALL_TIME_HIGH" in event_types):
        strategy = "US 52-Week High Volume Breakout"
        code = "52_week_high_volume_breakout"
    elif catalyst_type == "HIGH_TIGHT_FLAG":
        strategy = "High-Tight Flag"
        code = "market_action_momentum"
    elif catalyst_type == "NEWS_CATALYST":
        strategy = "News Catalyst Breakout"
        code = "news_catalyst"
    else:
        strategy = "US Top Mover Momentum"
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
    what = f"{name} ({symbol}) is a US top mover, up {gain_pct:.2f}% today with a {catalyst.get('catalyst_type', 'TECHNICAL_BREAKOUT').lower().replace('_', ' ')} tag." if gain_pct is not None else f"{name} ({symbol}) is on the current US top-movers list."
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


def _avoid_reason(item: dict[str, Any]) -> str:
    if item.get("hard_excludes"):
        return f"Do not buy {item.get('symbol')} - hard exclusion: {', '.join(item.get('hard_excludes') or [])}."
    anti = item.get("anti_patterns") or []
    if anti:
        return f"Do not buy {item.get('symbol')} - {anti[0].get('reason') or anti[0].get('label')}."
    return f"Do not buy {item.get('symbol')} - final signal is {item.get('final_signal')}."


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    limit = min(len(highs), len(lows), len(closes))
    if limit < period + 1:
        return None
    highs = highs[-limit:]
    lows = lows[-limit:]
    closes = closes[-limit:]
    true_ranges: list[float] = []
    start = max(1, limit - period)
    for index in range(start, limit):
        high = highs[index]
        low = lows[index]
        prev_close = closes[index - 1]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = _mean(true_ranges)
    last_close = closes[-1]
    return (atr / last_close) * 100 if last_close else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
