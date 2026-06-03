from __future__ import annotations

from math import log1p
from typing import Any


RAW_ENTRY_MODEL_VERSION = "raw_opportunity_v1"
ENTRY_AUTHORITY_VERSION = RAW_ENTRY_MODEL_VERSION

ENTRY_READY = "ENTRY_READY"
MANUAL_ONLY = "MANUAL_ONLY"
WATCH = "WATCH"
NO_TRADE = "NO_TRADE"


def evaluate_raw_entry(context: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    technical = context.get("technical_math") if isinstance(context.get("technical_math"), dict) else {}
    sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
    liquidity = full.get("liquidity_profile") if isinstance(full.get("liquidity_profile"), dict) else {}
    data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    market = str(context.get("market_region") or scan.get("market_region") or data_ready.get("market_region") or "").upper() or "IN"
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}

    price = _num(quote.get("price"))
    truth_blocks = _truth_blocks(price=price, quote=quote, liquidity=liquidity)

    scan_score = _score_pct(scan.get("score"))
    live_score = _score_pct((scan.get("components") or {}).get("live_momentum") if isinstance(scan.get("components"), dict) else None)
    day_gain = _num(scan.get("day_gain_pct")) or 0.0
    range_position = _clamp(_num(scan.get("day_range_position")) or 0.0, 0.0, 1.0)
    high_distance = _num(scan.get("day_high_distance_pct"))
    volume_ratio = max(_num(scan.get("volume_ratio")) or 0.0, _num(scan.get("projected_volume_ratio")) or 0.0)
    turnover = max(_num(scan.get("turnover")) or 0.0, _num(scan.get("projected_turnover")) or 0.0)
    technical_score = _score_pct(technical.get("score"))
    sentiment_score = _num(sentiment.get("score")) or 0.0
    positive_news_catalyst, negative_news_catalyst, sentiment_event_types = _sentiment_catalysts(sentiment, sentiment_score)
    rs = context.get("universe_relative_strength") if isinstance(context.get("universe_relative_strength"), dict) else {}
    rs_percentile = _num(rs.get("percentile_63"))
    setup = str(scan.get("setup") or "raw_market_action").strip() or "raw_market_action"
    bucket = str(scan.get("bucket") or "").strip()
    late_chase = bool(scan.get("late_chase") or bucket.upper() == "LATE_CHASE_AVOID")

    gain_component = _clamp((day_gain + 1.0) / 8.0, 0.0, 1.0) * 13.0
    range_component = range_position * 10.0
    high_component = 6.0 if high_distance is None else _clamp((5.0 - high_distance) / 5.0, 0.0, 1.0) * 7.0
    volume_component = _clamp(log1p(max(volume_ratio, 0.0)) / log1p(4.0), 0.0, 1.0) * 10.0
    turnover_floor = 2_000_000.0 if market == "US" else 40_000_000.0
    turnover_component = _clamp(turnover / max(turnover_floor, 1.0), 0.0, 2.0) * 4.0
    sentiment_component = _clamp((sentiment_score + 1.0) / 2.0, 0.0, 1.0) * 4.0
    rs_component = _clamp(((rs_percentile if rs_percentile is not None else 50.0) - 35.0) / 65.0, 0.0, 1.0) * 5.0
    soft_penalty = 0.0
    if late_chase:
        soft_penalty += 8.0
    if bucket.upper() == "AVOID":
        soft_penalty += 10.0
    if negative_news_catalyst:
        soft_penalty += 10.0

    base_score = round(
        _clamp(
            18.0
            + scan_score * 0.30
            + live_score * 0.14
            + technical_score * 0.12
            + gain_component
            + range_component
            + high_component
            + volume_component
            + turnover_component
            + sentiment_component
            + rs_component
            - soft_penalty,
            0.0,
            99.0,
        ),
        4,
    )
    setup_reviews = _opportunity_reviews(
        setup=setup,
        market=market,
        scan=scan,
        technical_score=technical_score,
        scan_score=scan_score,
        live_score=live_score,
        day_gain=day_gain,
        range_position=range_position,
        high_distance=high_distance,
        volume_ratio=volume_ratio,
        rs_percentile=rs_percentile,
        turnover=turnover,
    )
    passed_setups = [item for item in setup_reviews if item.get("passed")]
    best_setup = max(passed_setups or setup_reviews, key=lambda item: float(item.get("score") or 0.0), default={})
    setup_score = float(best_setup.get("score") or 0.0)
    raw_score = round(_clamp(base_score * 0.72 + setup_score * 0.28, 0.0, 99.0), 4)
    entry_line = _entry_line(settings)
    watch_line = _watch_line(settings)
    confidence = round(_clamp(raw_score / 100.0, 0.05, 0.99), 4)
    grade = "A" if raw_score >= 82.0 else "B" if raw_score >= entry_line else "WATCH"
    trade_plan = _trade_plan(price) if price and price > 0 else {}

    missing = [str(item or "").strip() for item in data_quality.get("missing") or [] if str(item or "").strip()]
    warnings: list[str] = []
    if "stale_quote" in missing:
        warnings.append("stale_quote_seen_in_scan_quality")
    if any(item in {"fresh_intraday_candles", "stale_intraday_candles"} for item in missing):
        warnings.append("intraday_candle_freshness_gap")
    if negative_news_catalyst:
        warnings.append("negative_news_catalyst_score_penalty")
    if late_chase:
        warnings.append("late_chase_score_penalty")

    setup_family = str(best_setup.get("family") or "none")
    opportunity_ready = bool(best_setup.get("passed")) and raw_score >= entry_line
    if truth_blocks:
        decision_label = NO_TRADE
        reason = truth_blocks[0]["reason"]
    elif opportunity_ready:
        decision_label = ENTRY_READY
        reason = "raw_opportunity_ready"
    elif raw_score >= watch_line or setup_score >= 42.0:
        decision_label = WATCH
        reason = "raw_opportunity_watch"
    else:
        decision_label = NO_TRADE
        reason = "raw_opportunity_not_enough_evidence"

    passed = decision_label == ENTRY_READY

    return {
        "version": RAW_ENTRY_MODEL_VERSION,
        "passed": passed,
        "action": "BUY" if passed else "HOLD",
        "reason": reason,
        "decision_label": decision_label,
        "auto_follow_ready": passed,
        "entry_line": entry_line,
        "watch_line": watch_line,
        "raw_score": raw_score,
        "base_score": base_score,
        "setup_score": round(setup_score, 4),
        "grade": grade,
        "confidence": confidence,
        "truth_blocks": truth_blocks,
        "entry_blockers": truth_blocks,
        "warnings": warnings,
        "trade_plan": trade_plan,
        "market_region": market,
        "setup": setup,
        "setup_family": setup_family,
        "setup_evidence": best_setup,
        "setup_reviews": setup_reviews,
        "components": {
            "scan_score_pct": round(scan_score, 4),
            "live_score_pct": round(live_score, 4),
            "technical_score_pct": round(technical_score, 4),
            "day_gain_pct": round(day_gain, 4),
            "day_range_position": round(range_position, 4),
            "day_high_distance_pct": round(high_distance, 4) if high_distance is not None else None,
            "volume_ratio": round(volume_ratio, 4),
            "turnover": round(turnover, 2),
            "sentiment_score": round(sentiment_score, 4),
            "positive_news_catalyst": positive_news_catalyst,
            "negative_news_catalyst": negative_news_catalyst,
            "relative_strength_percentile": round(rs_percentile, 4) if rs_percentile is not None else None,
        },
        "inputs": {
            "quote_source": quote.get("source"),
            "data_readiness": data_ready,
            "liquidity": liquidity,
            "opportunity_scan": scan,
        },
        "diagnostics": {
            "bucket": bucket,
            "late_chase": late_chase,
            "missing_data": missing,
            "sentiment_event_types": sorted(sentiment_event_types),
            "soft_penalty": round(soft_penalty, 4),
            "hard_block_policy": "invalid_quote_untradeable_or_hard_liquidity_only",
            "removed_vetoes": "legacy_strategy_and_india_specific_entry_vetoes_removed",
        },
        "legacy_decision_logic_removed": True,
        "raw_opportunity_v1": True,
    }


def _truth_blocks(*, price: float | None, quote: dict[str, Any], liquidity: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if price is None or price <= 0:
        blocks.append({"reason": "invalid_quote_price", "value": quote})
    if quote.get("tradeable") is False or quote.get("tradable") is False:
        blocks.append({"reason": "quote_marked_untradeable", "value": quote})
    if liquidity.get("tradeable") is False and liquidity.get("liquidity_tier") == "untradeable":
        blocks.append({"reason": "liquidity_marked_untradeable", "value": liquidity})
    return blocks


def _entry_line(settings: Any = None) -> float:
    if settings is None:
        return 64.0
    return float(
        getattr(settings, "raw_entry_min_score", None)
        or getattr(settings, "entry_authority_min_score", None)
        or 64.0
    )


def _watch_line(settings: Any = None) -> float:
    if settings is None:
        return 52.0
    return float(getattr(settings, "entry_authority_watch_score", None) or 52.0)


def _sentiment_catalysts(sentiment: dict[str, Any], sentiment_score: float) -> tuple[bool, bool, set[str]]:
    event_types = {
        str((item or {}).get("type") or (item or {}).get("event_type") or "").strip().lower()
        for item in (sentiment.get("events") or [])
        if isinstance(item, dict)
    }
    positive = bool(sentiment.get("positive_catalyst")) or (
        sentiment_score >= 0.22
        and int(sentiment.get("headline_count") or 0) > 0
        and bool(
            event_types
            & {
                "analyst_upgrade",
                "broker_re_rating",
                "contract_win",
                "earnings",
                "earnings_beat",
                "guidance",
                "guidance_raise",
                "order_win",
            }
        )
    )
    negative = bool(sentiment.get("negative_catalyst")) or (
        sentiment_score <= -0.30
        and int(sentiment.get("headline_count") or 0) > 0
        and bool(
            event_types
            & {
                "analyst_downgrade",
                "debt",
                "downgrade",
                "fraud",
                "lawsuit",
                "probe",
                "regulatory_action",
                "resignation",
            }
        )
    )
    return positive, negative, event_types


def _trade_plan(price: float | None) -> dict[str, Any]:
    if price is None or price <= 0:
        return {}
    stop = price * 0.965
    risk = price - stop
    target = price + risk * 1.8
    return {
        "entry_zone": [round(price * 0.995, 4), round(price * 1.005, 4)],
        "stop_loss": round(stop, 4),
        "targets": [
            {
                "label": "RAW-T1",
                "price": round(target, 4),
                "distance_pct": round(((target - price) / price) * 100.0, 4),
            }
        ],
        "holding_period": "intraday_to_swing",
        "source": RAW_ENTRY_MODEL_VERSION,
    }


def _opportunity_reviews(
    *,
    setup: str,
    market: str,
    scan: dict[str, Any],
    technical_score: float,
    scan_score: float,
    live_score: float,
    day_gain: float,
    range_position: float,
    high_distance: float | None,
    volume_ratio: float,
    rs_percentile: float | None,
    turnover: float,
) -> list[dict[str, Any]]:
    setup_key = setup.lower()
    rally = scan.get("rally_evidence") if isinstance(scan.get("rally_evidence"), dict) else {}
    market_action = scan.get("market_action") if isinstance(scan.get("market_action"), dict) else {}
    btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
    btst_evidence = btst.get("evidence") if isinstance(btst.get("evidence"), dict) else {}
    distance_to_near_high = _num(rally.get("distance_to_near_high_pct"))
    distance_to_sma20 = _num(rally.get("distance_to_sma20_pct"))
    if distance_to_sma20 is None:
        distance_to_sma20 = _num(btst_evidence.get("distance_to_sma20_pct"))
    return_5d = _num(rally.get("return_5d_pct"))
    if return_5d is None:
        return_5d = _num(btst_evidence.get("return_5d_pct"))
    near_high = (
        high_distance is not None
        and high_distance <= 5.0
        or distance_to_near_high is not None
        and distance_to_near_high <= 8.0
    )
    rs_value = rs_percentile if rs_percentile is not None else _num(btst_evidence.get("rs_rank")) or 50.0
    volume_supported = bool(rally.get("volume_support")) or volume_ratio >= 1.2
    traded_value_floor = 2_000_000.0 if market == "US" else 30_000_000.0
    smallcap_reclaim_shape = (
        market == "US"
        and setup_key in {"smallcap_momentum", "volume_price_accumulation", "us_smallcap_reclaim"}
        and technical_score >= 45.0
        and scan_score >= 35.0
        and rs_value >= 60.0
        and volume_ratio >= 1.4
        and turnover >= 2_000_000.0
        and volume_supported
        and (distance_to_sma20 is None or -18.0 <= distance_to_sma20 <= 6.0)
        and (distance_to_near_high is None or distance_to_near_high <= 30.0)
        and (return_5d is None or return_5d >= 2.0)
        and day_gain < 10.0
    )
    market_action_available = bool(market_action.get("available")) or setup_key in {
        "market_action_momentum",
        "price_shocker_reversal_breakout",
        "top_gainer_momentum",
        "circuit_demand_lock",
    }
    reviews = [
        _review(
            "live_momentum",
            setup_key in {"opening_ignition", "intraday_momentum", "top_gainer_momentum", "market_action_momentum", "price_shocker_reversal_breakout"}
            and day_gain >= 1.2
            and range_position >= 0.55
            and volume_ratio >= 1.15
            and max(live_score, scan_score) >= 42.0,
            score=_avg(max(live_score, scan_score), _norm(day_gain, 0.0, 5.0) * 100, range_position * 100, _norm(volume_ratio, 0.9, 2.5) * 100),
            reasons=["live price momentum", "upper-range trading", "volume participation"],
        ),
        _review(
            "breakout",
            setup_key in {"52_week_high_volume_breakout", "breakout_continuation", "near_breakout", "broker_re_rating_breakout", "earnings_beat_gap_and_go"}
            and near_high
            and volume_ratio >= 1.0
            and max(technical_score, scan_score) >= 42.0,
            score=_avg(scan_score, technical_score, _norm(volume_ratio, 0.9, 2.2) * 100, 90.0 if near_high else 35.0),
            reasons=["breakout or near-breakout", "price near high", "volume not weak"],
        ),
        _review(
            "pullback_continuation",
            setup_key in {"pullback_buy", "ema_pullback_continuation", "vwap_reclaim_pullback"}
            and technical_score >= 42.0
            and rs_value >= 45.0
            and volume_ratio >= 0.75
            and day_gain >= -1.5,
            score=_avg(scan_score, technical_score, rs_value, _norm(volume_ratio, 0.7, 1.6) * 100),
            reasons=["pullback/reclaim", "relative strength", "participation not weak"],
        ),
        _review(
            "smallcap_reclaim",
            smallcap_reclaim_shape,
            score=_avg(
                technical_score,
                rs_value,
                _norm(volume_ratio, 1.2, 2.6) * 100,
                _norm(turnover, 1_500_000.0, 7_000_000.0) * 100,
                86.0,
            ),
            reasons=["smallcap reclaim", "relative strength", "volume and traded value"],
        ),
        _review(
            "delivery_btst",
            market == "IN"
            and setup_key in {"btst_buy_candidate", "delivery_accumulation", "accumulation_breakout"}
            and (_num(btst.get("score")) or 0.0) >= 0.55
            and volume_ratio >= 0.8
            and range_position >= 0.45,
            score=_avg(scan_score, (_num(btst.get("score")) or 0.0) * 100, range_position * 100, _norm(volume_ratio, 0.8, 2.0) * 100),
            reasons=["delivery/BTST accumulation", "close strength", "volume participation"],
        ),
        _review(
            "reversal_reclaim",
            ("reversal" in setup_key or "reclaim" in setup_key or "failed_breakdown" in setup_key or "price_shocker" in setup_key)
            and day_gain >= 0.8
            and volume_ratio >= 1.25
            and range_position >= 0.45
            and technical_score >= 35.0,
            score=_avg(scan_score, technical_score, _norm(day_gain, 0.0, 4.0) * 100, _norm(volume_ratio, 0.9, 2.8) * 100),
            reasons=["reversal/reclaim", "volume expansion", "price recovered"],
        ),
        _review(
            "market_action_event",
            market_action_available
            and (day_gain >= 2.0 or _score_pct(market_action.get("score")) >= 58.0)
            and volume_ratio >= 1.1
            and range_position >= 0.45,
            score=_avg(_score_pct(market_action.get("score")), scan_score, _norm(volume_ratio, 0.9, 2.5) * 100, _norm(day_gain, 0.0, 5.0) * 100),
            reasons=["market-action event", "price response", "volume participation"],
        ),
        _review(
            "relative_strength_accumulation",
            rs_value >= 70.0
            and turnover >= traded_value_floor
            and volume_ratio >= 0.8
            and technical_score >= 40.0
            and day_gain >= -0.8,
            score=_avg(scan_score, technical_score, rs_value, _norm(volume_ratio, 0.8, 1.8) * 100),
            reasons=["relative strength", "adequate traded value", "accumulation candidate"],
        ),
    ]
    return reviews


def _review(family: str, passed: bool, *, score: float, reasons: list[str]) -> dict[str, Any]:
    return {
        "family": family,
        "passed": bool(passed),
        "score": round(_clamp(score, 0.0, 100.0), 4),
        "reasons": reasons,
    }


def _avg(*values: float) -> float:
    nums = [float(value) for value in values if value is not None]
    return sum(nums) / len(nums) if nums else 0.0


def _norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp((float(value) - low) / (high - low), 0.0, 1.0)


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_pct(value: Any) -> float:
    score = _num(value)
    if score is None:
        return 0.0
    if -1.0 <= score <= 1.0:
        score = (score + 1.0) * 50.0 if score < 0 else score * 100.0
    return _clamp(score, 0.0, 100.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(min(float(value), high), low)
