from __future__ import annotations

from math import log1p
from typing import Any


RAW_ENTRY_MODEL_VERSION = "raw_entry_model_v1"


def evaluate_raw_entry(context: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
    scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    technical = context.get("technical_math") if isinstance(context.get("technical_math"), dict) else {}
    sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
    liquidity = full.get("liquidity_profile") if isinstance(full.get("liquidity_profile"), dict) else {}
    data_ready = context.get("data_readiness") if isinstance(context.get("data_readiness"), dict) else {}
    market = str(context.get("market_region") or scan.get("market_region") or data_ready.get("market_region") or "").upper() or "IN"

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
    rs = context.get("universe_relative_strength") if isinstance(context.get("universe_relative_strength"), dict) else {}
    rs_percentile = _num(rs.get("percentile_63"))

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
    raw_score = round(_clamp(raw_score, 0.0, 99.0), 4)
    entry_line = float(getattr(settings, "raw_entry_min_score", 58.0) or 58.0) if settings is not None else 58.0
    confidence = round(_clamp(raw_score / 100.0, 0.05, 0.99), 4)
    grade = "A" if raw_score >= 78.0 else "B" if raw_score >= entry_line else "WATCH"
    trade_plan = _trade_plan(price) if price and price > 0 else {}
    passed = not truth_blocks and raw_score >= entry_line
    reason = "raw_entry_score_passed" if passed else truth_blocks[0]["reason"] if truth_blocks else "raw_entry_score_below_entry_line"

    return {
        "version": RAW_ENTRY_MODEL_VERSION,
        "passed": passed,
        "action": "BUY" if passed else "HOLD",
        "reason": reason,
        "entry_line": entry_line,
        "raw_score": raw_score,
        "grade": grade,
        "confidence": confidence,
        "truth_blocks": truth_blocks,
        "trade_plan": trade_plan,
        "market_region": market,
        "setup": str(scan.get("setup") or "raw_market_action").strip() or "raw_market_action",
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
            "relative_strength_percentile": round(rs_percentile, 4) if rs_percentile is not None else None,
        },
        "inputs": {
            "quote_source": quote.get("source"),
            "data_readiness": data_ready,
            "liquidity": liquidity,
            "opportunity_scan": scan,
        },
        "legacy_decision_logic_removed": True,
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
