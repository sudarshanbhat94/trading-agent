from __future__ import annotations

from datetime import datetime, time
from math import log1p
from typing import Any
from zoneinfo import ZoneInfo

from .market_day_regime import regime_allows_live_momentum


RAW_ENTRY_MODEL_VERSION = "raw_opportunity_v1"
ENTRY_AUTHORITY_VERSION = RAW_ENTRY_MODEL_VERSION
IST = ZoneInfo("Asia/Kolkata")

ENTRY_READY = "ENTRY_READY"
MANUAL_ONLY = "MANUAL_ONLY"
WATCH = "WATCH"
NO_TRADE = "NO_TRADE"


def evaluate_raw_entry(context: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    technical = context.get("technical_math") if isinstance(context.get("technical_math"), dict) else {}
    candle_summary = context.get("candlestick_analysis") if isinstance(context.get("candlestick_analysis"), dict) else {}
    sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
    liquidity = full.get("liquidity_profile") if isinstance(full.get("liquidity_profile"), dict) else {}
    data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    market = str(context.get("market_region") or scan.get("market_region") or data_ready.get("market_region") or "").upper() or "IN"
    data_quality = scan.get("data_quality") if isinstance(scan.get("data_quality"), dict) else {}
    market_day_regime = context.get("market_day_regime") if isinstance(context.get("market_day_regime"), dict) else {}

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
    candle_score = _score_pct(candle_summary.get("score"))
    candle_patterns = _candle_patterns(candle_summary, full)
    combined_score = _num(context.get("combined_score"))
    sentiment_score = _num(sentiment.get("score")) or 0.0
    positive_news_catalyst, negative_news_catalyst, sentiment_event_types = _sentiment_catalysts(sentiment, sentiment_score)
    market_action = scan.get("market_action") if isinstance(scan.get("market_action"), dict) else {}
    has_market_action_catalyst = _market_action_catalyst(market_action)
    big_runner = scan.get("big_runner") if isinstance(scan.get("big_runner"), dict) else {}
    has_big_runner_catalyst = _big_runner_catalyst(big_runner)
    early_alpha = scan.get("early_alpha") if isinstance(scan.get("early_alpha"), dict) else {}
    has_early_alpha_catalyst = _early_alpha_catalyst(early_alpha)
    rally_plan_promotion = scan.get("rally_plan_promotion") if isinstance(scan.get("rally_plan_promotion"), dict) else {}
    has_rally_plan_catalyst = _rally_plan_promotion_catalyst(rally_plan_promotion)
    sector = str(context.get("sector") or scan.get("sector") or "").strip()
    rs = context.get("universe_relative_strength") if isinstance(context.get("universe_relative_strength"), dict) else {}
    rs_percentile = _num(rs.get("percentile_63"))
    setup = str(scan.get("setup") or "raw_market_action").strip() or "raw_market_action"
    bucket = str(scan.get("bucket") or "").strip()
    late_chase = bool(scan.get("late_chase") or bucket.upper() == "LATE_CHASE_AVOID")
    quote_ts = _parse_ts(str(quote.get("asof") or ""))
    late_session_entry = _late_session_entry(market, quote_ts)
    btst_session_entry = _btst_session_entry(market, quote_ts)

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
    if late_session_entry:
        soft_penalty += 12.0
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
        price=price,
        rally_plan_promotion=rally_plan_promotion,
    )
    passed_setups = [item for item in setup_reviews if item.get("passed")]
    promoted_setups = [item for item in passed_setups if item.get("source") == "rally_plan"]
    best_setup = max(promoted_setups or passed_setups or setup_reviews, key=lambda item: float(item.get("score") or 0.0), default={})
    setup_score = float(best_setup.get("score") or 0.0)
    raw_score = round(_clamp(base_score * 0.72 + setup_score * 0.28, 0.0, 99.0), 4)
    entry_line = _entry_line(settings)
    watch_line = _watch_line(settings)
    confidence = round(_clamp(raw_score / 100.0, 0.05, 0.99), 4)
    grade = "A" if raw_score >= 82.0 else "B" if raw_score >= entry_line else "WATCH"
    setup_family = str(best_setup.get("family") or "none")
    trade_plan = _trade_plan(price, market, setup_family) if price and price > 0 else {}
    if settings is not None and getattr(settings, "market_day_regime_gate_enabled", True) is False:
        live_momentum_regime_allowed = True
        live_momentum_regime_gate = {"reason": "market_day_regime_gate_disabled", "state": market_day_regime.get("state")}
    else:
        live_momentum_regime_allowed, live_momentum_regime_gate = regime_allows_live_momentum(
            market_day_regime,
            sector=sector,
            has_catalyst=bool(
                positive_news_catalyst
                or has_market_action_catalyst
                or has_big_runner_catalyst
                or has_early_alpha_catalyst
                or has_rally_plan_catalyst
            ),
        )

    missing = [str(item or "").strip() for item in data_quality.get("missing") or [] if str(item or "").strip()]
    readiness_block = _readiness_block(data_ready=data_ready, data_quality=data_quality)
    confirmation_block = _confirmation_block(scan=scan, setup_family=setup_family, price=price)
    warnings: list[str] = []
    if readiness_block:
        warnings.append(readiness_block["reason"])
    if confirmation_block:
        warnings.append(confirmation_block["reason"])
    if "stale_quote" in missing:
        warnings.append("stale_quote_seen_in_scan_quality")
    if any(item in {"fresh_intraday_candles", "stale_intraday_candles"} for item in missing):
        warnings.append("intraday_candle_freshness_gap")
    if negative_news_catalyst:
        warnings.append("negative_news_catalyst_score_penalty")
    if late_chase:
        warnings.append("late_chase_score_penalty")
    if late_session_entry and setup_family != "delivery_btst" and best_setup.get("source") != "rally_plan":
        warnings.append("late_session_no_fresh_entry")
    if setup_family == "delivery_btst" and not btst_session_entry:
        warnings.append("btst_requires_late_non_friday_session")
    if market == "IN" and volume_ratio > 6.0:
        warnings.append("india_live_momentum_blowoff_volume_watch")
    if setup_family == "live_momentum" and not live_momentum_regime_allowed:
        warnings.append("market_day_regime_not_supportive_for_live_momentum")

    opportunity_ready_without_regime = _opportunity_ready(
        market=market,
        setup_family=setup_family,
        best_setup=best_setup,
        raw_score=raw_score,
        entry_line=entry_line,
        day_gain=day_gain,
        range_position=range_position,
        high_distance=high_distance,
        volume_ratio=volume_ratio,
        technical_score=technical_score,
        scan_score=scan_score,
        late_session_entry=late_session_entry,
        btst_session_entry=btst_session_entry,
        price=price,
        live_momentum_regime_allowed=True,
    )
    quality_block = (
        _raw_opportunity_quality_block(
            market=market,
            setup_family=setup_family,
            best_setup=best_setup,
            technical_score=technical_score,
            candle_score=candle_score,
            candle_patterns=candle_patterns,
            combined_score=combined_score,
            day_gain=day_gain,
            range_position=range_position,
            volume_ratio=volume_ratio,
            market_action=market_action,
            rally_plan_promotion=rally_plan_promotion,
            positive_news_catalyst=positive_news_catalyst,
            has_big_runner_catalyst=has_big_runner_catalyst,
            has_early_alpha_catalyst=has_early_alpha_catalyst,
            has_rally_plan_catalyst=has_rally_plan_catalyst,
        )
        if opportunity_ready_without_regime
        else None
    )
    if quality_block:
        warnings.append(quality_block["reason"])
    opportunity_ready = opportunity_ready_without_regime and (
        setup_family != "live_momentum" or live_momentum_regime_allowed
    ) and quality_block is None
    regime_block = (
        {
            "gate": "market_day_regime",
            "reason": "market_day_regime_not_supportive_for_live_momentum",
            "value": live_momentum_regime_gate,
        }
        if setup_family == "live_momentum" and opportunity_ready_without_regime and not live_momentum_regime_allowed
        else None
    )
    momentum_v2_block = _momentum_entry_v2_block(
        settings,
        setup_family=setup_family,
        day_gain=day_gain,
        volume_ratio=volume_ratio,
    )
    if momentum_v2_block:
        warnings.append(momentum_v2_block["reason"])
    if truth_blocks:
        decision_label = NO_TRADE
        reason = truth_blocks[0]["reason"]
    elif readiness_block and opportunity_ready_without_regime:
        decision_label = WATCH
        reason = readiness_block["reason"]
    elif confirmation_block and opportunity_ready_without_regime:
        decision_label = WATCH
        reason = confirmation_block["reason"]
    elif quality_block:
        decision_label = WATCH
        reason = quality_block["reason"]
    elif regime_block:
        decision_label = WATCH
        reason = "market_day_regime_not_supportive_for_live_momentum"
    elif momentum_v2_block and opportunity_ready:
        decision_label = WATCH
        reason = momentum_v2_block["reason"]
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
        "entry_blockers": [
            *truth_blocks,
            *([readiness_block] if readiness_block else []),
            *([confirmation_block] if confirmation_block else []),
            *([quality_block] if quality_block else []),
            *([regime_block] if regime_block else []),
        ],
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
            "candlestick_score_pct": round(candle_score, 4),
            "combined_score": round(combined_score, 4) if combined_score is not None else None,
            "day_gain_pct": round(day_gain, 4),
            "day_range_position": round(range_position, 4),
            "day_high_distance_pct": round(high_distance, 4) if high_distance is not None else None,
            "volume_ratio": round(volume_ratio, 4),
            "turnover": round(turnover, 2),
            "sentiment_score": round(sentiment_score, 4),
            "positive_news_catalyst": positive_news_catalyst,
            "negative_news_catalyst": negative_news_catalyst,
            "market_action_catalyst": has_market_action_catalyst,
            "big_runner_catalyst": has_big_runner_catalyst,
            "early_alpha_catalyst": has_early_alpha_catalyst,
            "rally_plan_catalyst": has_rally_plan_catalyst,
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
            "late_session_entry": late_session_entry,
            "btst_session_entry": btst_session_entry,
            "missing_data": missing,
            "candle_patterns": sorted(candle_patterns),
            "sentiment_event_types": sorted(sentiment_event_types),
            "raw_opportunity_quality_floor": quality_block,
            "market_day_regime": {
                key: market_day_regime.get(key)
                for key in (
                    "enabled",
                    "market_region",
                    "state",
                    "score",
                    "momentum_allowed",
                    "selective_momentum_allowed",
                    "reasons",
                    "checked_symbols",
                    "advancer_pct",
                    "above_open_pct",
                    "fade_pct",
                    "breadth_regime",
                    "allowed_setup_families",
                )
                if key in market_day_regime
            },
            "live_momentum_regime_gate": live_momentum_regime_gate,
            "rally_plan_promotion": rally_plan_promotion or None,
            "soft_penalty": round(soft_penalty, 4),
            "hard_block_policy": "invalid_quote_untradeable_hard_liquidity_data_readiness_or_confirmation",
            "removed_vetoes": "legacy_strategy_and_india_specific_entry_vetoes_removed",
        },
        "legacy_decision_logic_removed": True,
        "raw_opportunity_v1": True,
    }


def _raw_opportunity_quality_block(
    *,
    market: str,
    setup_family: str,
    best_setup: dict[str, Any],
    technical_score: float,
    candle_score: float,
    candle_patterns: set[str],
    combined_score: float | None,
    day_gain: float,
    range_position: float,
    volume_ratio: float,
    market_action: dict[str, Any],
    rally_plan_promotion: dict[str, Any],
    positive_news_catalyst: bool,
    has_big_runner_catalyst: bool,
    has_early_alpha_catalyst: bool,
    has_rally_plan_catalyst: bool,
) -> dict[str, Any] | None:
    market_key = str(market or "").upper()
    setup_key = str(setup_family or "").strip().lower()
    if setup_key not in {"live_momentum", "breakout"}:
        return None

    bearish_patterns = sorted(candle_patterns & _BEARISH_CANDLE_PATTERNS)
    constructive_patterns = sorted(candle_patterns & _CONSTRUCTIVE_CANDLE_PATTERNS)
    high_volatility = "high-volatility" in candle_patterns
    weak_combined_floor = 0.18 if market_key == "IN" else 0.05
    weak_combined = combined_score is not None and combined_score < weak_combined_floor
    severe_technical_floor = 30.0 if market_key == "US" else 28.0
    weak_technical_floor = 40.0 if market_key == "US" else 45.0
    severe_technical = technical_score < severe_technical_floor
    weak_technical = technical_score < weak_technical_floor
    bearish_candle = bool(bearish_patterns)
    volatile_without_constructive_candle = bool(high_volatility and not constructive_patterns)

    reasons: list[str] = []
    if severe_technical:
        reasons.append("technical_score_severely_weak")
    if weak_technical and weak_combined:
        reasons.append("weak_technical_and_low_combined_score")
    if weak_technical and bearish_candle:
        reasons.append("weak_technical_with_bearish_candle")
    if weak_combined and bearish_candle:
        reasons.append("low_combined_score_with_bearish_candle")
    if weak_technical and volatile_without_constructive_candle:
        reasons.append("weak_technical_high_volatility_without_constructive_candle")
    if setup_key == "breakout" and bearish_candle and candle_score < 45.0:
        reasons.append("breakout_has_bearish_candle_confirmation")

    if not reasons:
        return None

    if _strong_quality_confirmation(
        best_setup=best_setup,
        market_action=market_action,
        rally_plan_promotion=rally_plan_promotion,
        positive_news_catalyst=positive_news_catalyst,
        has_big_runner_catalyst=has_big_runner_catalyst,
        has_early_alpha_catalyst=has_early_alpha_catalyst,
        has_rally_plan_catalyst=has_rally_plan_catalyst,
        day_gain=day_gain,
        range_position=range_position,
        volume_ratio=volume_ratio,
        technical_score=technical_score,
        constructive_patterns=constructive_patterns,
        severe_technical=severe_technical,
    ):
        return None

    return {
        "gate": "raw_opportunity_quality_floor",
        "reason": "raw_opportunity_quality_floor_failed",
        "value": {
            "reasons": reasons,
            "technical_score_pct": round(technical_score, 4),
            "minimum_technical_score_pct": severe_technical_floor if severe_technical else weak_technical_floor,
            "combined_score": round(combined_score, 4) if combined_score is not None else None,
            "weak_combined_floor": weak_combined_floor,
            "candle_score_pct": round(candle_score, 4),
            "bearish_patterns": bearish_patterns,
            "constructive_patterns": constructive_patterns,
            "high_volatility": high_volatility,
            "override_policy": "requires rally-plan price readiness, strong market-action evidence, or strong catalyst/playbook confirmation",
        },
    }


def _strong_quality_confirmation(
    *,
    best_setup: dict[str, Any],
    market_action: dict[str, Any],
    rally_plan_promotion: dict[str, Any],
    positive_news_catalyst: bool,
    has_big_runner_catalyst: bool,
    has_early_alpha_catalyst: bool,
    has_rally_plan_catalyst: bool,
    day_gain: float,
    range_position: float,
    volume_ratio: float,
    technical_score: float,
    constructive_patterns: list[str],
    severe_technical: bool,
) -> bool:
    if has_rally_plan_catalyst and best_setup.get("source") == "rally_plan":
        return True

    event_types = {str(item or "").strip().upper() for item in market_action.get("event_types") or []}
    market_action_score = _score_pct(market_action.get("score") or market_action.get("market_action_score"))
    strong_market_action = bool(
        market_action_score >= 88.0
        or (
            market_action_score >= 78.0
            and event_types
            & {
                "TOP_GAINER",
                "VOLUME_SHOCKER",
                "52_WEEK_HIGH",
                "ALL_TIME_HIGH",
                "ONLY_BUYERS",
                "PRICE_SHOCKER",
                "STRONG_INTRADAY_GAIN",
            }
        )
    )
    strong_playbook = bool(has_big_runner_catalyst or has_early_alpha_catalyst)
    strong_price_volume = day_gain >= 3.0 and range_position >= 0.82 and volume_ratio >= 2.0
    strong_news = positive_news_catalyst and day_gain >= 1.2 and range_position >= 0.65 and volume_ratio >= 1.2
    constructive = bool(constructive_patterns)

    if severe_technical and not (constructive and strong_market_action and strong_price_volume):
        return False
    return bool(
        strong_news
        or has_rally_plan_catalyst
        or (strong_market_action and (strong_price_volume or constructive))
        or (strong_playbook and strong_price_volume and (technical_score >= 35.0 or constructive))
    )


_BEARISH_CANDLE_PATTERNS = {
    "bearish-engulfing",
    "bearish-volume-expansion",
    "bearish-marubozu-like",
    "evening-star-like",
    "gravestone-doji-like",
    "range-breakdown",
    "shooting-star-like",
    "three-black-crows-like",
    "tweezer-top-like",
}


_CONSTRUCTIVE_CANDLE_PATTERNS = {
    "bullish-engulfing",
    "bullish-volume-expansion",
    "bullish-marubozu-like",
    "dragonfly-doji-like",
    "hammer-like",
    "morning-star-like",
    "range-breakout",
    "three-white-soldiers-like",
    "tweezer-bottom-like",
}


def _candle_patterns(candle_summary: dict[str, Any], full: dict[str, Any]) -> set[str]:
    patterns: set[str] = set()
    for source in (
        candle_summary,
        full.get("candlestick_v2") if isinstance(full.get("candlestick_v2"), dict) else {},
    ):
        for item in source.get("patterns") or []:
            text = str(item or "").strip().lower()
            if text:
                patterns.add(text)
    return patterns


def _truth_blocks(*, price: float | None, quote: dict[str, Any], liquidity: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if price is None or price <= 0:
        blocks.append({"reason": "invalid_quote_price", "value": quote})
    if quote.get("tradeable") is False or quote.get("tradable") is False:
        blocks.append({"reason": "quote_marked_untradeable", "value": quote})
    if liquidity.get("tradeable") is False and liquidity.get("liquidity_tier") == "untradeable":
        blocks.append({"reason": "liquidity_marked_untradeable", "value": liquidity})
    return blocks


def _readiness_block(*, data_ready: dict[str, Any], data_quality: dict[str, Any]) -> dict[str, Any] | None:
    if data_ready and data_ready.get("trade_decision_ready") is False:
        return {
            "gate": "data_readiness",
            "reason": "data_not_trade_decision_ready",
            "value": {
                "trade_decision_ready": data_ready.get("trade_decision_ready"),
                "missing_data": data_ready.get("missing_data") or [],
                "hard_gaps": data_ready.get("hard_gaps") or [],
                "fresh_market_data_gate": data_ready.get("fresh_market_data_gate"),
            },
        }
    reject_reason = str(data_quality.get("reject_reason") or "").strip()
    if reject_reason:
        return {
            "gate": "scan_data_quality",
            "reason": "scan_data_quality_rejected",
            "value": {
                "reject_reason": reject_reason,
                "missing": data_quality.get("missing") or [],
                "actionable_data_ready": data_quality.get("actionable_data_ready"),
            },
        }
    return None


def _confirmation_block(*, scan: dict[str, Any], setup_family: str, price: float | None = None) -> dict[str, Any] | None:
    if setup_family != "live_momentum":
        return None
    promotion = scan.get("rally_plan_promotion") if isinstance(scan.get("rally_plan_promotion"), dict) else {}
    if _rally_plan_promotion_price_ready(promotion, price):
        return None
    for key in ("big_runner", "early_alpha"):
        review = scan.get(key) if isinstance(scan.get(key), dict) else {}
        action = str(review.get("action") or "").strip().upper()
        if action in {"CONFIRM", "WATCH"}:
            return {
                "gate": "setup_confirmation",
                "reason": "setup_requires_live_confirmation",
                "value": {
                    "source": key,
                    "action": action,
                    "stage": review.get("stage"),
                    "trade_window": review.get("trade_window"),
                    "what": review.get("what"),
                    "how": review.get("how"),
                },
            }
        if action == "AVOID":
            return {
                "gate": "setup_confirmation",
                "reason": "setup_marked_avoid",
                "value": {
                    "source": key,
                    "action": action,
                    "stage": review.get("stage"),
                    "blockers": review.get("blockers") or [],
                },
            }
    return None


def _momentum_entry_v2_block(
    settings: Any,
    *,
    setup_family: str,
    day_gain: float | None,
    volume_ratio: float | None,
) -> dict[str, Any] | None:
    """Momentum entry-quality gate v2 (IN + US), OFF unless explicitly enabled.

    A would-be live-momentum ENTRY_READY is demoted to WATCH when it is either
    chasing an already-extended move (day gain past the threshold) or lacks real
    volume confirmation. This targets the two failure modes seen in production:
    entries at a median +3.4% extension on ~1.5x volume. No effect when the flag
    is off, so it ships dark and is flipped on only after backtest validation.
    """
    if settings is None or not getattr(settings, "momentum_entry_v2_enabled", False):
        return None
    if str(setup_family or "").strip().lower() != "live_momentum":
        return None
    max_gain = float(getattr(settings, "momentum_max_chase_gain_pct", 2.5) or 2.5)
    min_vol = float(getattr(settings, "momentum_min_volume_ratio", 2.0) or 2.0)
    reasons: list[str] = []
    if day_gain is not None and day_gain > max_gain:
        reasons.append(f"chasing_extended_move_gain_{round(float(day_gain), 2)}pct_over_{max_gain}")
    if volume_ratio is not None and volume_ratio < min_vol:
        reasons.append(f"weak_volume_ratio_{round(float(volume_ratio), 2)}_under_{min_vol}")
    if not reasons:
        return None
    return {
        "gate": "momentum_entry_v2",
        "reason": reasons[0],
        "checks": reasons,
        "day_gain_pct": day_gain,
        "volume_ratio": volume_ratio,
        "max_chase_gain_pct": max_gain,
        "min_volume_ratio": min_vol,
    }


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


def _market_action_catalyst(market_action: dict[str, Any]) -> bool:
    if not isinstance(market_action, dict) or not market_action:
        return False
    event_types = {str(item or "").strip().upper() for item in market_action.get("event_types") or []}
    score = _score_pct(market_action.get("score") or market_action.get("market_action_score"))
    return bool(
        market_action.get("available")
        or score >= 70.0
        or event_types
        & {
            "TOP_GAINER",
            "VOLUME_SHOCKER",
            "52_WEEK_HIGH",
            "ALL_TIME_HIGH",
            "ONLY_BUYERS",
            "PRICE_SHOCKER",
            "STRONG_INTRADAY_GAIN",
        }
    )


def _big_runner_catalyst(big_runner: dict[str, Any]) -> bool:
    if not isinstance(big_runner, dict) or not big_runner:
        return False
    evidence = big_runner.get("evidence") if isinstance(big_runner.get("evidence"), dict) else {}
    catalyst_score = _num(evidence.get("catalyst_score")) or 0.0
    return bool(
        catalyst_score >= 0.55
        or str(big_runner.get("action") or "").upper() == "BUY CHECK"
        and catalyst_score >= 0.35
    )


def _early_alpha_catalyst(early_alpha: dict[str, Any]) -> bool:
    if not isinstance(early_alpha, dict) or not early_alpha:
        return False
    evidence = early_alpha.get("evidence") if isinstance(early_alpha.get("evidence"), dict) else {}
    tags = {str(item or "").strip().lower() for item in early_alpha.get("tags") or []}
    catalyst_score = _num(evidence.get("catalyst_score")) or 0.0
    score = _score_pct(early_alpha.get("score"))
    return bool(
        catalyst_score >= 0.55
        or (bool({"top_gainer_followthrough", "sector_leader"} & tags) and score >= 62.0)
        or (str(early_alpha.get("action") or "").upper() == "BUY CHECK" and score >= 68.0)
    )


def _rally_plan_promotion_catalyst(promotion: dict[str, Any]) -> bool:
    if not isinstance(promotion, dict) or promotion.get("ready") is not True:
        return False
    action = str(promotion.get("action") or "").strip().upper()
    section = str(promotion.get("section") or "").strip().lower()
    score = _score_pct(promotion.get("score"))
    evidence_sources = {str(item or "").strip().lower() for item in promotion.get("evidence_sources") or []}
    return bool(
        action in {"BUY CHECK", "BUY", "ENTRY_READY"}
        and section in {"opening_ignition", "live_momentum"}
        and score >= 68.0
        and evidence_sources
    )


def _rally_plan_promotion_price_ready(promotion: dict[str, Any], price: float | None) -> bool:
    if not _rally_plan_promotion_catalyst(promotion):
        return False
    current = _num(price)
    trigger = _num(promotion.get("trigger_price"))
    max_entry = _num(promotion.get("max_entry"))
    stop = _num(promotion.get("stop_loss"))
    target = _num(promotion.get("target1"))
    if None in (current, trigger, max_entry, stop, target):
        return False
    if not (stop < current <= max_entry * 1.0005 and target > current):
        return False
    return current >= trigger * 0.995


def _opportunity_ready(
    *,
    market: str,
    setup_family: str,
    best_setup: dict[str, Any],
    raw_score: float,
    entry_line: float,
    day_gain: float,
    range_position: float,
    high_distance: float | None,
    volume_ratio: float,
    technical_score: float,
    scan_score: float,
    late_session_entry: bool,
    btst_session_entry: bool = False,
    price: float | None = None,
    live_momentum_regime_allowed: bool = True,
) -> bool:
    if not bool(best_setup.get("passed")):
        return False
    if setup_family == "relative_strength_accumulation":
        return False
    setup_score = float(best_setup.get("score") or 0.0)
    base_line = max(float(entry_line), 72.0)
    if market == "IN":
        price_value = float(price or 0.0)
        high_ok = high_distance is None or high_distance <= 1.2
        if setup_family != "delivery_btst" and late_session_entry and best_setup.get("source") != "rally_plan":
            return False
        if setup_family == "live_momentum":
            if best_setup.get("source") == "rally_plan":
                promotion_high_ok = high_distance is None or high_distance <= 2.8
                return (
                    live_momentum_regime_allowed
                    and price_value >= 50.0
                    and raw_score >= max(base_line, 78.0)
                    and setup_score >= 70.0
                    and day_gain >= 1.2
                    and day_gain < 7.0
                    and range_position >= 0.64
                    and volume_ratio >= 1.10
                    and promotion_high_ok
                    and max(technical_score, scan_score) >= 62.0
                )
            return (
                live_momentum_regime_allowed
                and
                price_value >= 50.0
                and raw_score >= max(base_line, 92.0)
                and setup_score >= 87.0
                and day_gain >= 3.0
                and range_position >= 0.85
                and volume_ratio >= 2.0
                and volume_ratio <= 6.0
                and high_ok
                and max(technical_score, scan_score) >= 70.0
            )
        if setup_family == "breakout":
            return (
                price_value >= 50.0
                and raw_score >= max(base_line, 94.0)
                and setup_score >= 90.0
                and day_gain >= 2.0
                and range_position >= 0.80
                and volume_ratio >= 2.0
                and high_ok
                and max(technical_score, scan_score) >= 70.0
            )
        if setup_family == "delivery_btst":
            return (
                btst_session_entry
                and raw_score >= max(base_line, 86.0)
                and setup_score >= 82.0
                and 1.0 <= day_gain <= 3.8
                and range_position >= 0.76
                and (high_distance is None or high_distance <= 0.8)
                and 1.1 <= volume_ratio <= 4.5
            )
        if setup_family == "reversal_reclaim":
            return raw_score >= max(base_line, 88.0) and setup_score >= 76.0 and day_gain >= 1.5
        return False
    if setup_family == "live_momentum":
        return (
            live_momentum_regime_allowed
            and raw_score >= base_line
            and setup_score >= 60.0
            and day_gain >= 1.8
            and range_position >= 0.65
        )
    if setup_family in {"breakout", "smallcap_reclaim", "reversal_reclaim"}:
        return raw_score >= base_line and setup_score >= 64.0
    if setup_family == "delivery_btst":
        return raw_score >= base_line and setup_score >= 70.0
    return False


def _late_session_entry(market: str, quote_ts: datetime | None) -> bool:
    if quote_ts is None:
        return False
    local = quote_ts.astimezone(IST)
    if market == "IN":
        return local.time() >= time(14, 0)
    if market == "US":
        return False
    return False


def _btst_session_entry(market: str, quote_ts: datetime | None) -> bool:
    if market != "IN" or quote_ts is None:
        return False
    local = quote_ts.astimezone(IST)
    if local.weekday() >= 4:
        return False
    return time(14, 15) <= local.time() <= time(15, 20)


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    except ValueError:
        return None


def _trade_plan(price: float | None, market: str | None = "IN", setup_family: str | None = None) -> dict[str, Any]:
    if price is None or price <= 0:
        return {}
    market_key = str(market or "IN").upper()
    setup_key = str(setup_family or "").strip().lower()
    if market_key == "IN" and setup_key == "delivery_btst":
        stop_pct = 0.020
        targets = [
            ("BTST-T1", 0.022, 75),
            ("BTST-T2", 0.038, 25),
        ]
        label = "BTST-T1"
        holding_period = "BTST_next_session"
    elif market_key == "IN":
        stop_pct = 0.022
        targets = [
            ("RAW-IN-T1", 0.028, 70),
            ("RAW-IN-T2", 0.046, 30),
        ]
        label = "RAW-IN-T1"
        holding_period = "intraday_or_next_session"
    else:
        stop_pct = 0.030
        targets = [
            ("RAW-T1", 0.032, 70),
            ("RAW-T2", 0.055, 30),
        ]
        label = "RAW-T1"
        holding_period = "intraday_to_swing"
    stop = price * (1.0 - stop_pct)
    return {
        "entry_zone": [round(price * 0.995, 4), round(price * 1.005, 4)],
        "stop_loss": round(stop, 4),
        "targets": [
            {
                "label": target_label,
                "price": round(price * (1.0 + target_pct), 4),
                "distance_pct": round(target_pct * 100.0, 4),
                "suggested_exit_pct": exit_pct,
            }
            for target_label, target_pct, exit_pct in targets
        ],
        "holding_period": holding_period,
        "target_policy": {
            "profile": "closer_t1_profit_ladder_v2",
            "first_booking": label,
            "rule": "Book most size into the first reachable target; trail only the remainder.",
        },
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
    price: float | None = None,
    rally_plan_promotion: dict[str, Any] | None = None,
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
    promotion_review = _rally_plan_promotion_review(
        rally_plan_promotion or {},
        price=price,
        scan_score=scan_score,
        live_score=live_score,
        day_gain=day_gain,
        range_position=range_position,
        high_distance=high_distance,
        volume_ratio=volume_ratio,
    )
    reviews = [
        promotion_review,
        _review(
            "live_momentum",
            setup_key in {"opening_ignition", "intraday_momentum", "top_gainer_momentum", "market_action_momentum", "price_shocker_reversal_breakout", "big_runner_ignition", "early_alpha_ignition"}
            and day_gain >= 1.5
            and range_position >= 0.68
            and volume_ratio >= 1.25
            and max(live_score, scan_score) >= 42.0,
            score=_avg(max(live_score, scan_score), _norm(day_gain, 0.0, 5.0) * 100, range_position * 100, _norm(volume_ratio, 0.9, 2.5) * 100),
            reasons=["live price momentum", "upper-range trading", "volume participation"],
        ),
        _review(
            "breakout",
            setup_key in {"52_week_high_volume_breakout", "breakout_continuation", "near_breakout", "broker_re_rating_breakout", "earnings_beat_gap_and_go"}
            and near_high
            and volume_ratio >= 1.0
            and day_gain >= 0.8
            and range_position >= 0.65
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


def _rally_plan_promotion_review(
    promotion: dict[str, Any],
    *,
    price: float | None,
    scan_score: float,
    live_score: float,
    day_gain: float,
    range_position: float,
    high_distance: float | None,
    volume_ratio: float,
) -> dict[str, Any]:
    price_ready = _rally_plan_promotion_price_ready(promotion, price)
    score = _score_pct(promotion.get("score")) if isinstance(promotion, dict) else 0.0
    live_strength = max(scan_score, live_score, score)
    live_confirmed = (
        day_gain >= 1.2
        and day_gain < 7.0
        and range_position >= 0.64
        and (high_distance is None or high_distance <= 2.8)
        and volume_ratio >= 1.10
        and live_strength >= 68.0
    )
    return {
        "family": "live_momentum",
        "passed": bool(price_ready and live_confirmed),
        "score": round(
            _clamp(
                _avg(score, live_strength, _norm(day_gain, 0.8, 5.0) * 100, range_position * 100, _norm(volume_ratio, 0.9, 2.2) * 100),
                0.0,
                100.0,
            ),
            4,
        ),
        "reasons": [
            "rally plan buy-check promotion",
            "live price inside trigger/max-entry zone" if price_ready else "rally plan entry zone not live-ready",
            "live confirmation held" if live_confirmed else "live confirmation incomplete",
        ],
        "source": "rally_plan",
        "promotion": promotion if isinstance(promotion, dict) else {},
    }


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
