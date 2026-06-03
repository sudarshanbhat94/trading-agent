from __future__ import annotations

from typing import Any

from .market_day_regime import REGIME_BROAD_RALLY, REGIME_SELECTIVE_RALLY
from .market_regions import market_region_for_row, normalize_market_region
from .models import utc_now


RALLY_PLAN_SECTIONS = {
    "t1_pressure": "T-1 Pressure",
    "preopen_confirm": "Pre-open Confirm",
    "opening_ignition": "Opening Ignition",
    "live_momentum": "Live Momentum",
    "avoid": "Avoid / Do Not Chase",
}


def build_rally_plan(
    *,
    market_region: str,
    market_day_regime: dict[str, Any] | None = None,
    pre_catalyst: dict[str, Any] | None = None,
    tomorrow_plan: dict[str, Any] | None = None,
    opportunity_scan: dict[str, Any] | None = None,
    market_action_radar: dict[str, Any] | None = None,
    signal_ideas: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    region = normalize_market_region(market_region, default="IN")
    regime = _scoped(market_day_regime or {}, region)
    pre_catalyst = _scoped(pre_catalyst or {}, region)
    tomorrow_plan = _scoped(tomorrow_plan or {}, region)
    opportunity_scan = _scoped(opportunity_scan or {}, region)
    market_action_radar = _scoped(market_action_radar or {}, region)
    items: list[dict[str, Any]] = []
    items.extend(_pre_catalyst_items(pre_catalyst, region))
    items.extend(_tomorrow_plan_items(tomorrow_plan, region))
    items.extend(_opportunity_scan_items(opportunity_scan, region, regime))
    items.extend(_market_action_items(market_action_radar, region, regime))
    items.extend(_signal_avoid_items(signal_ideas or [], region))
    items = _dedupe_items(items)
    sections = {
        key: [item for item in items if item.get("section") == key]
        for key in RALLY_PLAN_SECTIONS
    }
    momentum_allowed = str(regime.get("state") or "") in {REGIME_BROAD_RALLY, REGIME_SELECTIVE_RALLY}
    return {
        "enabled": True,
        "market_region": region,
        "generated_at": generated_at or utc_now(),
        "regime": {
            "state": regime.get("state") or "neutral_chop",
            "score": regime.get("score"),
            "momentum_allowed": momentum_allowed,
            "summary": _regime_summary(regime),
            "reasons": regime.get("reasons") or [],
        },
        "source_status": {
            "pre_catalyst_candidates": len(pre_catalyst.get("candidates") or []),
            "tomorrow_plan_items": len(tomorrow_plan.get("items") or []),
            "opportunity_candidates": len(opportunity_scan.get("top_candidates") or []),
            "market_action_events": len(market_action_radar.get("events") or []),
        },
        "sections": sections,
        "items": items,
        "section_labels": RALLY_PLAN_SECTIONS,
    }


def build_rally_plan_by_market(**kwargs: Any) -> dict[str, Any]:
    generated_at = kwargs.get("generated_at") or utc_now()
    by_market = {
        region: build_rally_plan(**{**kwargs, "market_region": region, "generated_at": generated_at})
        for region in ("IN", "US")
    }
    return {
        "enabled": True,
        "market_region": "BOTH",
        "generated_at": generated_at,
        "by_market": by_market,
        "regime": by_market.get("IN", {}).get("regime", {}),
        "source_status": {
            region: by_market.get(region, {}).get("source_status", {})
            for region in ("IN", "US")
        },
    }


def _pre_catalyst_items(pre_catalyst: dict[str, Any], region: str) -> list[dict[str, Any]]:
    rows = []
    for raw in (pre_catalyst.get("candidates") or [])[:40]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name") or raw.get("company_name"),
                market_region=region,
                section="t1_pressure",
                stage="T-1 Pressure",
                action="WATCH",
                strategy=raw.get("setup") or raw.get("label") or "pre_catalyst",
                score=_score(raw.get("score") or raw.get("confidence")),
                why=_join_reasons(raw.get("key_reasons")) or raw.get("reason") or "Pressure is building before a confirmed live move.",
                what="Prepare levels and wait for pre-open/open confirmation.",
                how="Do not buy from the overnight idea alone; promote only if price, volume, and market regime confirm.",
                trigger_price=_num(raw.get("trigger_price") or raw.get("pivot") or raw.get("price")),
                max_entry=_num(raw.get("max_entry")),
                stop_loss=_num(raw.get("stop_loss")),
                target1=_num(raw.get("target1")),
                invalidation=raw.get("invalidation") or "Invalid if opening price breaks the prepared level or live volume fails.",
                evidence={"pre_catalyst": raw},
                blockers=[],
            )
        )
    for raw in (pre_catalyst.get("live_confirmations") or [])[:30]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name") or raw.get("company_name"),
                market_region=region,
                section="opening_ignition",
                stage="Opening Ignition",
                action="CONFIRM",
                strategy=raw.get("setup") or raw.get("label") or "live_confirmation",
                score=_score(raw.get("score") or raw.get("confidence")),
                why=_join_reasons(raw.get("key_reasons")) or "Pre-rally idea has started confirming live.",
                what="Check VWAP/opening range and regime before entry.",
                how="Enter only on hold above trigger with volume pace; otherwise keep as watch.",
                trigger_price=_num(raw.get("trigger_price") or raw.get("pivot") or raw.get("price")),
                max_entry=_num(raw.get("max_entry")),
                stop_loss=_num(raw.get("stop_loss")),
                target1=_num(raw.get("target1")),
                invalidation=raw.get("invalidation") or "Invalid if confirmation candle fails or broader market fades.",
                evidence={"live_confirmation": raw},
                blockers=[],
            )
        )
    return rows


def _tomorrow_plan_items(plan: dict[str, Any], region: str) -> list[dict[str, Any]]:
    rows = []
    for raw in (plan.get("items") or [])[:80]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        section = str(raw.get("section") or "").lower()
        target_section = "preopen_confirm"
        action = "CONFIRM"
        if section == "avoid" or str(raw.get("action") or "").upper() == "AVOID":
            target_section = "avoid"
            action = "AVOID"
        elif section in {"ready_at_open", "btst_buys"}:
            target_section = "preopen_confirm"
        elif section == "near_breakout":
            target_section = "t1_pressure"
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name"),
                market_region=region,
                section=target_section,
                stage=RALLY_PLAN_SECTIONS[target_section],
                action=action,
                strategy=raw.get("strategy") or "tomorrow_plan",
                score=_score(raw.get("score") or raw.get("confidence")),
                why=raw.get("rationale") or "Prepared in tomorrow plan.",
                what=raw.get("validation") or "Validate price, volume, and market context before acting.",
                how="Use the prepared trigger/max/stop levels; do not chase above max entry.",
                trigger_price=_num(raw.get("trigger_price")),
                max_entry=_num(raw.get("max_entry")),
                stop_loss=_num(raw.get("stop_loss")),
                target1=_num(raw.get("target1")),
                invalidation="Invalid if pre-open or first live candle breaks the setup level.",
                evidence={"tomorrow_plan": raw},
                blockers=raw.get("failed_gates") if isinstance(raw.get("failed_gates"), list) else [],
            )
        )
    return rows


def _opportunity_scan_items(scan: dict[str, Any], region: str, regime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in (scan.get("top_fast_movers") or scan.get("top_candidates") or [])[:40]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        setup = str(raw.get("setup") or "").lower()
        section = "live_momentum" if setup in {"intraday_momentum", "opening_ignition", "top_gainer_momentum"} else "opening_ignition"
        blockers = [] if _regime_allows_momentum(regime) else [{"reason": "market_day_regime_not_supportive_for_live_momentum", "regime": regime.get("state")}]
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name"),
                market_region=region,
                section=section,
                stage=RALLY_PLAN_SECTIONS[section],
                action="BUY CHECK" if not blockers else "WATCH",
                strategy=raw.get("setup") or "opportunity_scan",
                score=_score(raw.get("score")),
                why=_scan_reason(raw),
                what="Confirm the move is holding above VWAP/opening range while the market regime supports momentum.",
                how="Promote to entry only when raw-entry rules and regime gate both pass.",
                trigger_price=_num(raw.get("price")),
                max_entry=_num(raw.get("max_entry") or raw.get("price")),
                stop_loss=_num(raw.get("stop_loss")),
                target1=_num(raw.get("target1")),
                invalidation="Invalid if the move loses VWAP, fades below open, or regime moves to fade/risk-off.",
                evidence={"opportunity_scan": raw, "regime": regime},
                blockers=blockers,
            )
        )
    for raw in (scan.get("top_market_action") or [])[:30]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        rows.append(
            _market_action_plan_item(raw, region, regime, source_key="opportunity_market_action")
        )
    return [row for row in rows if row]


def _market_action_items(radar: dict[str, Any], region: str, regime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in (radar.get("events") or [])[:40]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        item = _market_action_plan_item(raw, region, regime, source_key="market_action_radar")
        if item:
            rows.append(item)
    return rows


def _market_action_plan_item(raw: dict[str, Any], region: str, regime: dict[str, Any], *, source_key: str) -> dict[str, Any] | None:
    symbol = _symbol(raw)
    if not symbol:
        return None
    event_types = {str(item or "").upper() for item in raw.get("event_types") or []}
    chase = bool(raw.get("late_chase") or "ONLY_BUYERS" in event_types or (_num(raw.get("pct_change")) or 0.0) >= 8.0)
    section = "avoid" if chase else "live_momentum"
    blockers = []
    if not chase and not _regime_allows_momentum(regime):
        blockers.append({"reason": "market_day_regime_not_supportive_for_live_momentum", "regime": regime.get("state")})
    action = "AVOID" if chase else "BUY CHECK" if not blockers else "WATCH"
    return _item(
        symbol=symbol,
        name=raw.get("name"),
        market_region=region,
        section=section,
        stage=RALLY_PLAN_SECTIONS[section],
        action=action,
        strategy=raw.get("strategy") or "market_action",
        score=_score(raw.get("market_action_score") or raw.get("score")),
        why=raw.get("reason") or "Market-action event detected.",
        what="Treat this as confirmation evidence, not an automatic buy.",
        how="Buy only if it holds VWAP/opening range and the regime gate allows live momentum.",
        trigger_price=_num(raw.get("price")),
        max_entry=_num(raw.get("max_entry") or raw.get("price")),
        stop_loss=_num(raw.get("stop_loss")),
        target1=_num(raw.get("target1")),
        invalidation="Invalid if the move is only a spike, upper-circuit chase, or broader market fades.",
        evidence={source_key: raw, "regime": regime},
        blockers=blockers or ([{"reason": "do_not_chase_extended_market_action"}] if chase else []),
    )


def _signal_avoid_items(ideas: list[dict[str, Any]], region: str) -> list[dict[str, Any]]:
    rows = []
    for raw in ideas[:80]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        status = str(raw.get("status") or "").upper()
        if status not in {"STOP_HIT", "EXIT_SIGNAL", "EXPIRED", "REJECTED"}:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name") or raw.get("company_name"),
                market_region=region,
                section="avoid",
                stage=RALLY_PLAN_SECTIONS["avoid"],
                action="AVOID",
                strategy=raw.get("strategy") or "signal_idea",
                score=0.0,
                why=raw.get("display_reason") or raw.get("reason") or "Prior idea is invalidated.",
                what="Do not re-enter without a fresh setup.",
                how="Wait for a new rally plan row with clear confirmation.",
                trigger_price=_num(raw.get("latest_price") or raw.get("entry_price")),
                max_entry=None,
                stop_loss=None,
                target1=None,
                invalidation="Already invalidated.",
                evidence={"signal_idea": raw},
                blockers=[{"reason": status.lower()}],
            )
        )
    return rows


def _item(**kwargs: Any) -> dict[str, Any]:
    return {
        "symbol": str(kwargs.get("symbol") or "").upper(),
        "name": kwargs.get("name") or kwargs.get("symbol"),
        "market_region": kwargs.get("market_region"),
        "section": kwargs.get("section"),
        "stage": kwargs.get("stage"),
        "action": kwargs.get("action"),
        "strategy": kwargs.get("strategy"),
        "score": round(float(kwargs.get("score") or 0.0), 4),
        "why": str(kwargs.get("why") or "")[:600],
        "what": str(kwargs.get("what") or "")[:600],
        "how": str(kwargs.get("how") or "")[:600],
        "trigger_price": kwargs.get("trigger_price"),
        "max_entry": kwargs.get("max_entry"),
        "stop_loss": kwargs.get("stop_loss"),
        "target1": kwargs.get("target1"),
        "invalidation": str(kwargs.get("invalidation") or "")[:600],
        "evidence": kwargs.get("evidence") if isinstance(kwargs.get("evidence"), dict) else {},
        "blockers": kwargs.get("blockers") if isinstance(kwargs.get("blockers"), list) else [],
    }


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    section_order = {key: index for index, key in enumerate(RALLY_PLAN_SECTIONS)}
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("section") or ""), str(item.get("symbol") or ""))
        current = best.get(key)
        if current is None or float(item.get("score") or 0.0) > float(current.get("score") or 0.0):
            best[key] = item
    return sorted(
        best.values(),
        key=lambda item: (section_order.get(str(item.get("section") or ""), 99), -float(item.get("score") or 0.0), str(item.get("symbol") or "")),
    )


def _regime_allows_momentum(regime: dict[str, Any]) -> bool:
    return str(regime.get("state") or "") in {REGIME_BROAD_RALLY, REGIME_SELECTIVE_RALLY}


def _regime_summary(regime: dict[str, Any]) -> str:
    state = str(regime.get("state") or "neutral_chop")
    if state == REGIME_BROAD_RALLY:
        return "Broad market participation supports live momentum entries."
    if state == REGIME_SELECTIVE_RALLY:
        return "Momentum is allowed only for sector/catalyst-backed names."
    if state == "fade_risk":
        return "Early strength is fading; live momentum buys should stay on watch."
    if state == "risk_off":
        return "Market risk is defensive; new live momentum buys are blocked."
    return "Market is mixed; wait for stronger confirmation."


def _scan_reason(raw: dict[str, Any]) -> str:
    parts = []
    for key, label in (
        ("day_gain_pct", "day gain"),
        ("volume_ratio", "volume"),
        ("day_range_position", "range hold"),
    ):
        value = _num(raw.get(key))
        if value is not None:
            parts.append(f"{label} {value:.2f}")
    return ", ".join(parts) or "Live opportunity scan detected a candidate."


def _join_reasons(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(str(item) for item in value[:4] if str(item or "").strip())


def _scoped(payload: dict[str, Any], region: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    by_market = payload.get("by_market") if isinstance(payload.get("by_market"), dict) else {}
    scoped = by_market.get(region)
    if isinstance(scoped, dict):
        return scoped
    if str(payload.get("market_region") or payload.get("market") or "").upper() in {"", region, "BOTH"}:
        return payload
    return {}


def _row_market(row: dict[str, Any]) -> str:
    explicit = str(row.get("market_region") or row.get("market") or "").upper()
    if explicit in {"IN", "US"}:
        return explicit
    return market_region_for_row(row)


def _symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").strip().upper()


def _score(value: Any) -> float:
    number = _num(value) or 0.0
    return number * 100.0 if 0.0 <= number <= 1.0 else number


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
