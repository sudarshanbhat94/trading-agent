from __future__ import annotations

from math import log1p
from typing import Any


RAW_ENTRY_MODEL_VERSION = "entry_authority_v2"
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
    truth_blocks: list[dict[str, Any]] = []
    if price is None or price <= 0:
        truth_blocks.append({"reason": "invalid_quote_price", "value": quote})
    if quote.get("tradeable") is False or quote.get("tradable") is False:
        truth_blocks.append({"reason": "quote_marked_untradeable", "value": quote})
    if liquidity.get("tradeable") is False and liquidity.get("liquidity_tier") == "untradeable":
        truth_blocks.append({"reason": "liquidity_marked_untradeable", "value": liquidity})

    scan_score = _score_pct(scan.get("score"))
    live_score = _score_pct((scan.get("components") or {}).get("live_momentum") if isinstance(scan.get("components"), dict) else None)
    day_gain = _num(scan.get("day_gain_pct")) or 0.0
    range_position = _clamp(_num(scan.get("day_range_position")) or 0.0, 0.0, 1.0)
    high_distance = _num(scan.get("day_high_distance_pct"))
    volume_ratio = max(_num(scan.get("volume_ratio")) or 0.0, _num(scan.get("projected_volume_ratio")) or 0.0)
    turnover = max(_num(scan.get("turnover")) or 0.0, _num(scan.get("projected_turnover")) or 0.0)
    technical_score = _score_pct(technical.get("score"))
    sentiment_score = _num(sentiment.get("score")) or 0.0
    sentiment_event_types = {
        str((item or {}).get("type") or (item or {}).get("event_type") or "").strip().lower()
        for item in (sentiment.get("events") or [])
        if isinstance(item, dict)
    }
    positive_news_catalyst = bool(sentiment.get("positive_catalyst")) or (
        sentiment_score >= 0.22
        and int(sentiment.get("headline_count") or 0) > 0
        and bool(
            sentiment_event_types
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
    negative_news_catalyst = bool(sentiment.get("negative_catalyst")) or (
        sentiment_score <= -0.30
        and int(sentiment.get("headline_count") or 0) > 0
        and bool(
            sentiment_event_types
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
    rs = context.get("universe_relative_strength") if isinstance(context.get("universe_relative_strength"), dict) else {}
    rs_percentile = _num(rs.get("percentile_63"))
    setup = str(scan.get("setup") or "raw_market_action").strip() or "raw_market_action"
    bucket = str(scan.get("bucket") or "").strip()
    late_chase = bool(scan.get("late_chase") or bucket.upper() == "LATE_CHASE_AVOID")

    gain_component = _clamp((day_gain + 1.0) / 8.0, 0.0, 1.0) * 14.0
    range_component = range_position * 10.0
    high_component = 6.0 if high_distance is None else _clamp((4.0 - high_distance) / 4.0, 0.0, 1.0) * 6.0
    volume_component = _clamp(log1p(max(volume_ratio, 0.0)) / log1p(4.0), 0.0, 1.0) * 10.0
    turnover_floor = 2_000_000.0 if market == "US" else 50_000_000.0
    turnover_component = _clamp(turnover / max(turnover_floor, 1.0), 0.0, 2.0) * 3.0
    sentiment_component = _clamp((sentiment_score + 1.0) / 2.0, 0.0, 1.0) * 4.0
    rs_component = _clamp(((rs_percentile if rs_percentile is not None else 50.0) - 40.0) / 60.0, 0.0, 1.0) * 5.0

    raw_score = (
        18.0
        + scan_score * 0.32
        + live_score * 0.12
        + technical_score * 0.12
        + gain_component
        + range_component
        + high_component
        + volume_component
        + turnover_component
        + sentiment_component
        + rs_component
    )
    base_score = round(_clamp(raw_score, 0.0, 99.0), 4)
    setup_reviews = _setup_reviews(
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
    )
    passed_setups = [item for item in setup_reviews if item.get("passed")]
    best_setup = max(passed_setups or setup_reviews, key=lambda item: float(item.get("score") or 0.0), default={})
    setup_score = float(best_setup.get("score") or 0.0)
    authority_score = round(_clamp(base_score * 0.68 + setup_score * 0.32, 0.0, 99.0), 4)
    entry_line = (
        float(getattr(settings, "entry_authority_min_score", 72.0) or 72.0)
        if settings is not None
        else 72.0
    )
    watch_line = (
        float(getattr(settings, "entry_authority_watch_score", 58.0) or 58.0)
        if settings is not None
        else 58.0
    )
    confidence = round(_clamp(authority_score / 100.0, 0.05, 0.99), 4)
    grade = "A" if authority_score >= 82.0 else "B" if authority_score >= entry_line else "WATCH"
    trade_plan = _trade_plan(price) if price and price > 0 else {}
    blockers: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not bool(best_setup.get("passed")):
        blockers.append(
            {
                "reason": "no_positive_setup_family",
                "message": "Reviewed symbol did not meet live momentum, breakout, pullback, BTST/delivery, or reversal evidence.",
            }
        )
    if authority_score < entry_line:
        blockers.append(
            {
                "reason": "entry_authority_score_below_minimum",
                "score": authority_score,
                "minimum": entry_line,
            }
        )
    if bucket.upper() in {"AVOID", "LATE_CHASE_AVOID"}:
        blockers.append({"reason": "scanner_bucket_not_entry_ready", "bucket": bucket})
    if late_chase:
        blockers.append({"reason": "late_chase_not_entry_ready", "bucket": bucket})
    missing = [str(item or "").strip() for item in data_quality.get("missing") or [] if str(item or "").strip()]
    if "stale_quote" in missing:
        blockers.append({"reason": "stale_quote_not_entry_ready", "missing_data": missing})
    if any(item in {"fresh_intraday_candles", "stale_intraday_candles"} for item in missing):
        warnings.append("intraday_candle_freshness_gap")
    setup_family = str(best_setup.get("family") or "none")
    if setup_family == "market_action_event":
        blockers.append(
            {
                "reason": "market_action_event_manual_review",
                "message": "Market-action-only events were net-negative in the last completed cost-adjusted replay; require another setup family before auto-entry.",
            }
        )
    if market == "US" and setup_family == "live_momentum" and not truth_blocks:
        us_live_momentum_confirmed = day_gain >= 2.0 and (
            technical_score >= 60.0
            or (day_gain >= 3.0 and volume_ratio >= 2.0 and technical_score >= 45.0)
        )
        if not us_live_momentum_confirmed:
            blockers.append(
                {
                    "reason": "us_live_momentum_confirmation_filter",
                    "message": "US live-momentum entries require either technical confirmation or a stronger price move with volume.",
                    "day_gain_pct": round(day_gain, 4),
                    "technical_score_pct": round(technical_score, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "min_day_gain_pct": 2.0,
                    "min_technical_score_pct": 60.0,
                    "strong_move_min_day_gain_pct": 3.0,
                    "strong_move_min_volume_ratio": 2.0,
                    "strong_move_min_technical_score_pct": 45.0,
                }
            )
    if market == "IN" and negative_news_catalyst and not truth_blocks:
        blockers.append(
            {
                "reason": "india_negative_news_catalyst",
                "message": "India auto-entry is blocked when timestamped news sentiment shows a negative catalyst.",
                "sentiment_score": round(sentiment_score, 4),
                "event_types": sorted(sentiment_event_types),
            }
        )
    if market == "IN" and not truth_blocks:
        price_value = price or 0.0
        india_breakout_ok = (
            setup_family == "breakout"
            and authority_score >= 97.0
            and price_value >= 3000.0
            and positive_news_catalyst
        )
        india_live_ok = setup_family == "live_momentum" and price_value >= 3000.0 and (
            (
                authority_score >= 92.0
                and technical_score >= 85.0
                and day_gain >= 2.5
                and volume_ratio >= 4.5
                and range_position >= 0.85
                and (high_distance is None or high_distance <= 0.8)
            )
            or (
                authority_score >= 96.0
                and technical_score >= 55.0
                and day_gain >= 6.0
                and volume_ratio >= 10.0
                and range_position >= 0.85
                and (high_distance is None or high_distance <= 0.5)
            )
        )
        if not (india_breakout_ok or india_live_ok):
            blockers.append(
                {
                    "reason": "india_cost_adjusted_selectivity_filter",
                    "message": "India entries require stricter cost-adjusted candle evidence; breakouts also require a positive timestamped news/catalyst.",
                    "score": authority_score,
                    "setup_family": setup_family,
                    "min_breakout_score": 97.0,
                    "breakout_requires_positive_news_catalyst": True,
                    "live_momentum_min_price": 3000.0,
                    "positive_news_catalyst": positive_news_catalyst,
                    "day_gain_pct": round(day_gain, 4),
                    "technical_score_pct": round(technical_score, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "day_range_position": round(range_position, 4),
                    "day_high_distance_pct": round(high_distance, 4) if high_distance is not None else None,
                }
            )

    if truth_blocks:
        decision_label = NO_TRADE
        reason = truth_blocks[0]["reason"]
    elif not blockers and authority_score >= entry_line:
        decision_label = ENTRY_READY
        reason = "entry_authority_setup_passed"
    elif bool(best_setup.get("passed")) and authority_score >= watch_line:
        decision_label = MANUAL_ONLY
        reason = blockers[0]["reason"] if blockers else "entry_authority_manual_review"
    elif authority_score >= watch_line or setup_score >= 45.0:
        decision_label = WATCH
        reason = blockers[0]["reason"] if blockers else "entry_authority_watch"
    else:
        decision_label = NO_TRADE
        reason = blockers[0]["reason"] if blockers else "entry_authority_no_trade"

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
        "raw_score": authority_score,
        "base_score": base_score,
        "setup_score": round(setup_score, 4),
        "grade": grade,
        "confidence": confidence,
        "truth_blocks": truth_blocks,
        "entry_blockers": blockers,
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
        "legacy_decision_logic_removed": True,
        "entry_authority_v2": True,
    }


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


def _setup_reviews(
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
) -> list[dict[str, Any]]:
    setup_key = setup.lower()
    rally = scan.get("rally_evidence") if isinstance(scan.get("rally_evidence"), dict) else {}
    market_action = scan.get("market_action") if isinstance(scan.get("market_action"), dict) else {}
    btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
    distance_to_near_high = _num(rally.get("distance_to_near_high_pct"))
    near_high = (
        high_distance is not None
        and high_distance <= 3.0
        or distance_to_near_high is not None
        and distance_to_near_high <= 5.0
    )
    rs_value = rs_percentile if rs_percentile is not None else _num((btst.get("evidence") or {}).get("rs_rank")) or 50.0
    reviews = [
        _review(
            "live_momentum",
            setup_key in {"opening_ignition", "intraday_momentum", "top_gainer_momentum", "market_action_momentum", "price_shocker_reversal_breakout"}
            and day_gain >= 1.5
            and range_position >= 0.65
            and volume_ratio >= 1.4
            and near_high
            and max(live_score, scan_score) >= 55.0,
            score=_avg(max(live_score, scan_score), _norm(day_gain, 0.0, 6.0) * 100, range_position * 100, _norm(volume_ratio, 1.0, 3.0) * 100),
            reasons=["fresh momentum setup", "price near session/high breakout area", "volume expansion required"],
        ),
        _review(
            "breakout",
            setup_key in {"52_week_high_volume_breakout", "breakout_continuation", "near_breakout", "broker_re_rating_breakout", "earnings_beat_gap_and_go"}
            and near_high
            and volume_ratio >= 1.15
            and technical_score >= 55.0
            and scan_score >= 50.0,
            score=_avg(scan_score, technical_score, _norm(volume_ratio, 1.0, 2.5) * 100, 95.0 if near_high else 35.0),
            reasons=["breakout setup", "near resistance or high", "trend and volume confirmation"],
        ),
        _review(
            "pullback_continuation",
            setup_key in {"pullback_buy", "ema_pullback_continuation", "vwap_reclaim_pullback"}
            and technical_score >= 60.0
            and rs_value >= 50.0
            and volume_ratio >= 0.8
            and day_gain >= -1.5,
            score=_avg(scan_score, technical_score, rs_value, _norm(volume_ratio, 0.7, 1.8) * 100),
            reasons=["uptrend pullback or reclaim setup", "relative strength confirmation", "volume not weak"],
        ),
        _review(
            "delivery_btst",
            market == "IN"
            and setup_key in {"btst_buy_candidate", "delivery_accumulation", "accumulation_breakout"}
            and (_num(btst.get("score")) or 0.0) >= 0.70
            and volume_ratio >= 1.1
            and range_position >= 0.55,
            score=_avg(scan_score, (_num(btst.get("score")) or 0.0) * 100, range_position * 100, _norm(volume_ratio, 1.0, 2.5) * 100),
            reasons=["India BTST/delivery setup", "close strength", "volume participation"],
        ),
        _review(
            "reversal_reclaim",
            ("reversal" in setup_key or "reclaim" in setup_key or "failed_breakdown" in setup_key or "price_shocker" in setup_key)
            and day_gain >= 1.0
            and volume_ratio >= 1.8
            and range_position >= 0.55
            and technical_score >= 40.0,
            score=_avg(scan_score, technical_score, _norm(day_gain, 0.0, 5.0) * 100, _norm(volume_ratio, 1.0, 3.5) * 100),
            reasons=["reversal/reclaim setup", "strong volume", "price recovered into upper range"],
        ),
        _review(
            "market_action_event",
            bool(market_action.get("available"))
            and (_score_pct(market_action.get("score")) >= 65.0 or day_gain >= 3.0)
            and volume_ratio >= 1.4
            and range_position >= 0.55,
            score=_avg(_score_pct(market_action.get("score")), scan_score, _norm(volume_ratio, 1.0, 3.0) * 100, _norm(day_gain, 0.0, 6.0) * 100),
            reasons=["market-action event", "volume shock", "price response"],
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
