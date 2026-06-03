from __future__ import annotations

from collections import defaultdict
from typing import Any

from .market_regions import market_region_for_row


REGIME_BROAD_RALLY = "broad_rally"
REGIME_SELECTIVE_RALLY = "selective_rally"
REGIME_NEUTRAL_CHOP = "neutral_chop"
REGIME_FADE_RISK = "fade_risk"
REGIME_RISK_OFF = "risk_off"


def compute_market_day_regimes(
    universe: list[dict[str, Any]],
    quotes: dict[str, Any],
    candles_by_symbol: dict[str, Any] | None,
    market_breadth: dict[str, Any] | None = None,
    *,
    market_region: str = "BOTH",
) -> dict[str, Any]:
    region = str(market_region or "BOTH").upper()
    if region != "BOTH":
        return compute_market_day_regime(
            [row for row in universe if market_region_for_row(row) == region],
            quotes,
            candles_by_symbol or {},
            _market_specific_context(market_breadth or {}, region),
            market_region=region,
        )
    by_market = {
        key: compute_market_day_regime(
            [row for row in universe if market_region_for_row(row) == key],
            quotes,
            candles_by_symbol or {},
            _market_specific_context(market_breadth or {}, key),
            market_region=key,
        )
        for key in ("IN", "US")
    }
    return {
        "enabled": True,
        "market_region": "BOTH",
        "by_market": by_market,
        "state": by_market.get("IN", {}).get("state", REGIME_NEUTRAL_CHOP),
        "data_note": "Use by_market.IN or by_market.US for symbol decisions.",
    }


def compute_market_day_regime(
    universe: list[dict[str, Any]],
    quotes: dict[str, Any],
    candles_by_symbol: dict[str, Any] | None,
    market_breadth: dict[str, Any] | None = None,
    *,
    market_region: str = "IN",
) -> dict[str, Any]:
    candles_by_symbol = candles_by_symbol or {}
    market_breadth = market_breadth or {}
    rows = [row for row in universe if market_region_for_row(row) == str(market_region or "IN").upper()]
    checked = 0
    advancers = 0
    decliners = 0
    above_open = 0
    below_open = 0
    fade_count = 0
    strong_gain_count = 0
    new_highs = 0
    new_lows = 0
    sector_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"checked": 0, "advancers": 0, "strong": 0})
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        quote = quotes.get(symbol)
        price = _num(_field(quote, "price") if quote is not None else None)
        if price is None or price <= 0:
            continue
        open_price = _num(_field(quote, "open"))
        high = _num(_field(quote, "high"))
        low = _num(_field(quote, "low"))
        candles = _candles_for_symbol(candles_by_symbol.get(symbol))
        prev_close = _previous_close(candles)
        if prev_close is None:
            prev_close = _num(_field(quote, "close"))
        if prev_close is None:
            prev_close = open_price
        if prev_close is None or prev_close <= 0:
            continue
        day_change = ((price - prev_close) / prev_close) * 100.0
        open_change = ((price - open_price) / open_price) * 100.0 if open_price and open_price > 0 else 0.0
        checked += 1
        if day_change > 0:
            advancers += 1
        elif day_change < 0:
            decliners += 1
        if open_change > 0:
            above_open += 1
        elif open_change < 0:
            below_open += 1
        if day_change >= 3.0:
            strong_gain_count += 1
        if _is_fading(day_change, open_change, price, high, low):
            fade_count += 1
        lookback_closes = [_num(getattr(candle, "close", None)) for candle in candles[-52:]]
        lookback_closes = [value for value in lookback_closes if value is not None and value > 0]
        if lookback_closes:
            if price >= max(lookback_closes):
                new_highs += 1
            if price <= min(lookback_closes):
                new_lows += 1
        sector = str(row.get("sector") or "Unknown").strip() or "Unknown"
        sector_counts[sector]["checked"] += 1
        if day_change > 0:
            sector_counts[sector]["advancers"] += 1
        if day_change >= 3.0:
            sector_counts[sector]["strong"] += 1

    breadth_regime = str(market_breadth.get("breadth_regime") or "neutral").lower()
    breadth_ad_ratio = _num(market_breadth.get("advance_decline_ratio"))
    breadth_pct50 = _num(market_breadth.get("pct_above_50dma"))
    checked_safe = max(checked, 1)
    advancer_pct = advancers / checked_safe
    above_open_pct = above_open / checked_safe
    fade_pct = fade_count / checked_safe
    strong_pct = strong_gain_count / checked_safe
    new_high_low_pressure = (new_highs - new_lows) / checked_safe
    sector_participation = {
        sector: {
            "checked": stats["checked"],
            "advancer_pct": round(stats["advancers"] / max(stats["checked"], 1), 4),
            "strong_gain_pct": round(stats["strong"] / max(stats["checked"], 1), 4),
        }
        for sector, stats in sector_counts.items()
    }
    best_sector_pct = max((item["advancer_pct"] for item in sector_participation.values()), default=0.0)
    score = 0.0
    reasons: list[str] = []
    score += _score_advancers(advancer_pct, reasons)
    score += _score_ad_ratio(breadth_ad_ratio, reasons)
    score += _score_above_open(above_open_pct, reasons)
    score += _score_fade(fade_pct, reasons)
    score += _score_high_low_pressure(new_high_low_pressure, reasons)
    score += _score_breadth_regime(breadth_regime, breadth_pct50, reasons)
    if best_sector_pct >= 0.68:
        score += 8.0
        reasons.append("at least one sector has strong participation")
    if strong_pct >= 0.035:
        score += 6.0
        reasons.append("multiple symbols are already showing 3% plus demand")
    state = _state_from_score(
        score=score,
        advancer_pct=advancer_pct,
        above_open_pct=above_open_pct,
        fade_pct=fade_pct,
        breadth_regime=breadth_regime,
        breadth_ad_ratio=breadth_ad_ratio,
        best_sector_pct=best_sector_pct,
    )
    return {
        "enabled": checked > 0,
        "market_region": str(market_region or "IN").upper(),
        "state": state,
        "score": round(score, 4),
        "momentum_allowed": state == REGIME_BROAD_RALLY,
        "selective_momentum_allowed": state == REGIME_SELECTIVE_RALLY,
        "reasons": reasons[:8] or ["not enough live market evidence; defaulting to neutral chop"],
        "checked_symbols": checked,
        "advancers": advancers,
        "decliners": decliners,
        "advancer_pct": round(advancer_pct, 4),
        "above_open_pct": round(above_open_pct, 4),
        "fade_pct": round(fade_pct, 4),
        "strong_gain_pct": round(strong_pct, 4),
        "new_highs": new_highs,
        "new_lows": new_lows,
        "new_high_low_pressure": round(new_high_low_pressure, 4),
        "breadth_regime": breadth_regime,
        "breadth_advance_decline_ratio": round(breadth_ad_ratio, 4) if breadth_ad_ratio is not None else None,
        "breadth_pct_above_50dma": round(breadth_pct50, 4) if breadth_pct50 is not None else None,
        "sector_participation": sector_participation,
        "allowed_setup_families": _allowed_setup_families(state),
    }


def regime_allows_live_momentum(
    market_day_regime: dict[str, Any] | None,
    *,
    sector: str | None = None,
    has_catalyst: bool = False,
) -> tuple[bool, dict[str, Any]]:
    regime = market_day_regime if isinstance(market_day_regime, dict) else {}
    if not regime:
        return True, {"reason": "market_day_regime_unavailable", "mode": "fail_open"}
    state = str(regime.get("state") or REGIME_NEUTRAL_CHOP)
    if state == REGIME_BROAD_RALLY:
        return True, {"reason": "broad_rally_allows_live_momentum", "state": state}
    if state == REGIME_SELECTIVE_RALLY:
        sector_stats = (regime.get("sector_participation") or {}).get(str(sector or "").strip(), {})
        sector_ok = float(sector_stats.get("advancer_pct") or 0.0) >= 0.65
        allowed = bool(sector_ok and has_catalyst)
        return allowed, {
            "reason": "selective_rally_requires_sector_and_catalyst" if not allowed else "selective_rally_confirmed_by_sector_and_catalyst",
            "state": state,
            "sector": sector,
            "sector_advancer_pct": sector_stats.get("advancer_pct"),
            "has_catalyst": bool(has_catalyst),
        }
    return False, {"reason": "market_day_regime_not_supportive_for_live_momentum", "state": state}


def _market_specific_context(payload: dict[str, Any], region: str) -> dict[str, Any]:
    by_market = payload.get("by_market") if isinstance(payload.get("by_market"), dict) else {}
    scoped = by_market.get(region)
    return scoped if isinstance(scoped, dict) else payload


def _candles_for_symbol(value: Any) -> list[Any]:
    if isinstance(value, dict):
        for key in ("daily", "analysis", "intraday"):
            candles = value.get(key)
            if isinstance(candles, list) and candles:
                return candles
        return []
    return value if isinstance(value, list) else []


def _previous_close(candles: list[Any]) -> float | None:
    if not candles:
        return None
    candle = candles[-1]
    return _num(getattr(candle, "close", None))


def _is_fading(day_change: float, open_change: float, price: float, high: float | None, low: float | None) -> bool:
    if day_change <= 0:
        return open_change < -0.25
    if open_change <= -0.55 and day_change >= 1.0:
        return True
    if high is not None and low is not None and high > low:
        range_position = (price - low) / (high - low)
        return day_change >= 1.5 and range_position <= 0.42
    return False


def _score_advancers(value: float, reasons: list[str]) -> float:
    if value >= 0.62:
        reasons.append("most quoted symbols are advancing")
        return 25.0
    if value >= 0.52:
        reasons.append("advancers are modestly ahead")
        return 10.0
    if value <= 0.35:
        reasons.append("advancers are weak across the market")
        return -30.0
    if value <= 0.42:
        reasons.append("decliners are controlling the tape")
        return -15.0
    return 0.0


def _score_ad_ratio(value: float | None, reasons: list[str]) -> float:
    if value is None:
        return 0.0
    if value >= 1.6:
        reasons.append("breadth advance/decline ratio is strong")
        return 15.0
    if value >= 1.15:
        reasons.append("breadth advance/decline ratio is positive")
        return 8.0
    if value <= 0.65:
        reasons.append("breadth advance/decline ratio is defensive")
        return -18.0
    if value <= 0.8:
        reasons.append("breadth advance/decline ratio is soft")
        return -8.0
    return 0.0


def _score_above_open(value: float, reasons: list[str]) -> float:
    if value >= 0.56:
        reasons.append("many symbols are holding above their open")
        return 10.0
    if value <= 0.42:
        reasons.append("many symbols are fading below their open")
        return -10.0
    return 0.0


def _score_fade(value: float, reasons: list[str]) -> float:
    if value <= 0.15:
        reasons.append("fade pressure is contained")
        return 10.0
    if value >= 0.35:
        reasons.append("too many early movers are fading")
        return -20.0
    if value >= 0.28:
        reasons.append("fade pressure is elevated")
        return -10.0
    return 0.0


def _score_high_low_pressure(value: float, reasons: list[str]) -> float:
    if value >= 0.035:
        reasons.append("new-high pressure is positive")
        return 10.0
    if value <= -0.025:
        reasons.append("new lows are pressuring the market")
        return -10.0
    return 0.0


def _score_breadth_regime(regime: str, pct50: float | None, reasons: list[str]) -> float:
    if regime == "bull_confirmed":
        reasons.append("stored breadth regime is bull confirmed")
        return 20.0
    if regime == "bull_weakening":
        reasons.append("stored breadth regime is still constructive but weakening")
        return 8.0
    if regime == "bear_warning":
        reasons.append("stored breadth regime is bear warning")
        return -15.0
    if regime == "bear_confirmed":
        reasons.append("stored breadth regime is bear confirmed")
        return -25.0
    if pct50 is not None and pct50 >= 60.0:
        reasons.append("many symbols remain above 50-DMA")
        return 8.0
    return 0.0


def _state_from_score(
    *,
    score: float,
    advancer_pct: float,
    above_open_pct: float,
    fade_pct: float,
    breadth_regime: str,
    breadth_ad_ratio: float | None,
    best_sector_pct: float,
) -> str:
    ad_ratio = breadth_ad_ratio if breadth_ad_ratio is not None else 1.0
    if score <= -25.0 or (breadth_regime == "bear_confirmed" and advancer_pct < 0.45) or ad_ratio <= 0.55:
        return REGIME_RISK_OFF
    if score <= -8.0 or fade_pct >= 0.30 or advancer_pct < 0.45 or above_open_pct < 0.42:
        return REGIME_FADE_RISK
    if score >= 45.0 and advancer_pct >= 0.58 and ad_ratio >= 1.20 and fade_pct < 0.25:
        return REGIME_BROAD_RALLY
    if score >= 18.0 and (advancer_pct >= 0.50 or best_sector_pct >= 0.68):
        return REGIME_SELECTIVE_RALLY
    return REGIME_NEUTRAL_CHOP


def _allowed_setup_families(state: str) -> list[str]:
    if state == REGIME_BROAD_RALLY:
        return ["live_momentum", "breakout", "delivery_btst", "reversal_reclaim"]
    if state == REGIME_SELECTIVE_RALLY:
        return ["live_momentum_with_sector_and_catalyst", "breakout", "delivery_btst", "reversal_reclaim"]
    if state == REGIME_NEUTRAL_CHOP:
        return ["breakout", "delivery_btst", "reversal_reclaim"]
    return ["delivery_btst"]


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
