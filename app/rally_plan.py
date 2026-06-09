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

RALLY_PLAN_ACTION_SECTIONS = {"opening_ignition", "live_momentum"}
RALLY_PLAN_PROMOTION_ACTIONS = {"BUY CHECK", "BUY", "ENTRY_READY"}
RALLY_PLAN_CONFIRM_ACTIONS = {*RALLY_PLAN_PROMOTION_ACTIONS, "CONFIRM"}
RALLY_PLAN_LEVEL_SECTIONS = {"preopen_confirm", "opening_ignition", "live_momentum"}


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
    items.extend(_early_alpha_items(opportunity_scan, region, regime))
    items.extend(_big_runner_items(opportunity_scan, region, regime))
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
            "early_alpha_candidates": len(opportunity_scan.get("top_early_alpha_candidates") or []),
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


def extract_rally_plan_promotions(
    plan: dict[str, Any],
    *,
    max_per_market: int = 8,
) -> dict[str, Any]:
    """Return actionable Rally Plan rows that can be considered by entry authority."""
    if not isinstance(plan, dict) or max_per_market <= 0:
        return {"enabled": False, "by_market": {}, "total": 0, "reason": "disabled_or_empty_plan"}
    by_market = plan.get("by_market") if isinstance(plan.get("by_market"), dict) else {}
    markets = by_market if by_market else {str(plan.get("market_region") or "IN").upper(): plan}
    result: dict[str, list[dict[str, Any]]] = {}
    blocked: dict[str, int] = {}
    for market, market_plan in markets.items():
        rows = []
        for item in (market_plan.get("items") if isinstance(market_plan, dict) else []) or []:
            promotion, reason = _promotion_from_item(item)
            if promotion:
                rows.append(promotion)
            elif reason:
                blocked[reason] = blocked.get(reason, 0) + 1
        rows.sort(key=lambda row: _promotion_rank(row), reverse=True)
        result[str(market).upper()] = rows[:max_per_market]
    return {
        "enabled": True,
        "by_market": result,
        "total": sum(len(rows) for rows in result.values()),
        "max_per_market": max_per_market,
        "blocked_counts": blocked,
    }


def _promotion_from_item(item: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(item, dict):
        return None, "invalid_item"
    section = str(item.get("section") or "").strip().lower()
    action = str(item.get("action") or "").strip().upper()
    if section not in RALLY_PLAN_ACTION_SECTIONS:
        return None, "not_action_section"
    if action not in RALLY_PLAN_PROMOTION_ACTIONS:
        return None, "not_buy_check_action"
    blockers = item.get("blockers") if isinstance(item.get("blockers"), list) else []
    if blockers:
        return None, "blocked_item"
    entry_plan = item.get("entry_plan") if isinstance(item.get("entry_plan"), dict) else {}
    if str(entry_plan.get("status") or "").strip().lower() != "entry_check":
        return None, "not_entry_check"
    trigger = _num(item.get("trigger_price"))
    max_entry = _num(item.get("max_entry"))
    stop = _num(item.get("stop_loss"))
    target = _num(item.get("target1"))
    if None in (trigger, max_entry, stop, target):
        return None, "incomplete_levels"
    if not (stop < trigger <= max_entry < target):
        return None, "invalid_level_order"
    risk_pct = ((trigger - stop) / trigger) * 100.0 if trigger else 999.0
    reward_pct = ((target - trigger) / trigger) * 100.0 if trigger else 0.0
    if risk_pct <= 0.0 or risk_pct > 7.5:
        return None, "risk_too_wide"
    if reward_pct < 1.2:
        return None, "target_too_small"
    score = _score(item.get("score"))
    if score < 68.0:
        return None, "score_below_promotion_floor"
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    return {
        "ready": True,
        "source": "rally_plan",
        "symbol": str(item.get("symbol") or "").upper(),
        "market_region": item.get("market_region"),
        "section": section,
        "stage": item.get("stage"),
        "action": action,
        "strategy": item.get("strategy"),
        "score": round(score, 4),
        "trigger_price": trigger,
        "max_entry": max_entry,
        "stop_loss": stop,
        "target1": target,
        "risk_pct": round(risk_pct, 4),
        "reward_pct": round(reward_pct, 4),
        "why": item.get("why"),
        "what": item.get("what"),
        "how": item.get("how"),
        "invalidation": item.get("invalidation"),
        "evidence_sources": sorted(evidence.keys()),
        "entry_plan": entry_plan,
        "exit_plan": item.get("exit_plan") if isinstance(item.get("exit_plan"), dict) else {},
    }, ""


def _promotion_rank(row: dict[str, Any]) -> float:
    section_bonus = 4.0 if row.get("section") == "live_momentum" else 2.0
    risk = float(row.get("risk_pct") or 0.0)
    risk_bonus = max(0.0, 4.0 - min(risk, 4.0))
    reward = min(float(row.get("reward_pct") or 0.0), 8.0) * 0.35
    return float(row.get("score") or 0.0) + section_bonus + risk_bonus + reward


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


def _big_runner_items(scan: dict[str, Any], region: str, regime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    candidates = scan.get("top_big_runner_candidates") or []
    if not candidates:
        candidates = [
            item
            for item in (scan.get("top_candidates") or [])
            if isinstance(item, dict) and (item.get("big_runner") or {}).get("available")
        ]
    for raw in candidates[:40]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        big_runner = raw.get("big_runner") if isinstance(raw.get("big_runner"), dict) else {}
        if not big_runner.get("available"):
            continue
        stage = str(big_runner.get("stage") or "")
        action = str(big_runner.get("action") or "WATCH").upper()
        if stage == "avoid" or action == "AVOID":
            section = "avoid"
        elif stage == "live_momentum":
            section = "live_momentum"
        elif stage == "opening_ignition":
            section = "opening_ignition"
        elif stage == "preopen_confirm":
            section = "preopen_confirm"
        else:
            section = "t1_pressure"
        blockers = big_runner.get("blockers") if isinstance(big_runner.get("blockers"), list) else []
        if section == "live_momentum" and not _regime_allows_momentum(regime):
            blockers = [*blockers, {"reason": "market_day_regime_not_supportive_for_live_momentum", "regime": regime.get("state")}]
            if action == "BUY CHECK":
                action = "WATCH"
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name"),
                market_region=region,
                section=section,
                stage=RALLY_PLAN_SECTIONS[section],
                action=action,
                strategy=big_runner.get("setup") or raw.get("setup") or "big_runner_detector",
                score=_score(big_runner.get("score") or raw.get("score")),
                why=big_runner.get("why") or _join_reasons(big_runner.get("reasons")) or "Big-runner fuel is visible.",
                what=big_runner.get("what") or "Wait for confirmation before acting.",
                how=big_runner.get("how") or "Use trigger, max entry, stop, and regime confirmation before entry.",
                trigger_price=_num(big_runner.get("trigger_price") or raw.get("price")),
                max_entry=_num(big_runner.get("max_entry") or raw.get("max_entry")),
                stop_loss=_num(big_runner.get("stop_loss") or raw.get("stop_loss")),
                target1=_num(big_runner.get("target1") or raw.get("target1")),
                invalidation=big_runner.get("invalidation") or "Invalid if trigger fails, volume fades, or market regime weakens.",
                evidence={"big_runner": big_runner, "opportunity_scan": raw, "regime": regime},
                blockers=blockers,
            )
        )
    return rows


def _early_alpha_items(scan: dict[str, Any], region: str, regime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    candidates = scan.get("top_early_alpha_candidates") or []
    if not candidates:
        candidates = [
            item
            for item in (scan.get("top_candidates") or [])
            if isinstance(item, dict) and (item.get("early_alpha") or {}).get("available")
        ]
    for raw in candidates[:50]:
        if not isinstance(raw, dict) or _row_market(raw) != region:
            continue
        symbol = _symbol(raw)
        if not symbol:
            continue
        early_alpha = raw.get("early_alpha") if isinstance(raw.get("early_alpha"), dict) else {}
        if not early_alpha.get("available"):
            continue
        stage = str(early_alpha.get("stage") or "")
        action = str(early_alpha.get("action") or "WATCH").upper()
        if stage == "avoid" or action == "AVOID":
            section = "avoid"
        elif stage == "opening_ignition":
            section = "opening_ignition"
        elif stage == "preopen_confirm":
            section = "preopen_confirm"
        elif stage == "live_momentum":
            section = "live_momentum"
        else:
            section = "t1_pressure"
        blockers = early_alpha.get("blockers") if isinstance(early_alpha.get("blockers"), list) else []
        if section in {"opening_ignition", "live_momentum"} and not _regime_allows_momentum(regime):
            blockers = [*blockers, {"reason": "market_day_regime_not_supportive_for_live_momentum", "regime": regime.get("state")}]
            if action == "BUY CHECK":
                action = "WATCH"
        rows.append(
            _item(
                symbol=symbol,
                name=raw.get("name"),
                market_region=region,
                section=section,
                stage=RALLY_PLAN_SECTIONS[section],
                action=action,
                strategy=early_alpha.get("setup") or raw.get("setup") or "early_alpha_detector",
                score=_score(early_alpha.get("score") or raw.get("score")),
                why=early_alpha.get("why") or _join_reasons(early_alpha.get("reasons")) or "Early alpha pressure is visible.",
                what=early_alpha.get("what") or "Wait for confirmation before acting.",
                how=early_alpha.get("how") or "Use trigger, max entry, stop, and regime confirmation before entry.",
                trigger_price=_num(early_alpha.get("trigger_price") or raw.get("price")),
                max_entry=_num(early_alpha.get("max_entry") or raw.get("max_entry")),
                stop_loss=_num(early_alpha.get("stop_loss") or raw.get("stop_loss")),
                target1=_num(early_alpha.get("target1") or raw.get("target1")),
                invalidation=early_alpha.get("invalidation") or "Invalid if trigger fails, volume fades, or market regime weakens.",
                evidence={"early_alpha": early_alpha, "opportunity_scan": raw, "regime": regime},
                blockers=blockers,
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
    blockers = kwargs.get("blockers") if isinstance(kwargs.get("blockers"), list) else []
    action = kwargs.get("action")
    section = kwargs.get("section")
    trigger_price = kwargs.get("trigger_price")
    max_entry = kwargs.get("max_entry")
    stop_loss = kwargs.get("stop_loss")
    target1 = kwargs.get("target1")
    trigger_price, max_entry, stop_loss, target1 = _complete_actionable_levels(
        market_region=kwargs.get("market_region"),
        action=action,
        section=section,
        trigger_price=trigger_price,
        max_entry=max_entry,
        stop_loss=stop_loss,
        target1=target1,
    )
    action = _normalized_rally_action(
        action=action,
        section=section,
        blockers=blockers,
        trigger_price=trigger_price,
        max_entry=max_entry,
        stop_loss=stop_loss,
        target1=target1,
    )
    invalidation = str(kwargs.get("invalidation") or "")[:600]
    return {
        "symbol": str(kwargs.get("symbol") or "").upper(),
        "name": kwargs.get("name") or kwargs.get("symbol"),
        "market_region": kwargs.get("market_region"),
        "section": section,
        "stage": kwargs.get("stage"),
        "action": action,
        "strategy": kwargs.get("strategy"),
        "score": round(float(kwargs.get("score") or 0.0), 4),
        "why": str(kwargs.get("why") or "")[:600],
        "what": str(kwargs.get("what") or "")[:600],
        "how": str(kwargs.get("how") or "")[:600],
        "trigger_price": trigger_price,
        "max_entry": max_entry,
        "stop_loss": stop_loss,
        "target1": target1,
        "invalidation": invalidation,
        "entry_plan": _entry_plan(
            action=action,
            section=section,
            trigger_price=trigger_price,
            max_entry=max_entry,
            stop_loss=stop_loss,
            blockers=blockers,
        ),
        "exit_plan": _exit_plan(
            action=action,
            section=section,
            trigger_price=trigger_price,
            max_entry=max_entry,
            stop_loss=stop_loss,
            target1=target1,
            invalidation=invalidation,
        ),
        "evidence": kwargs.get("evidence") if isinstance(kwargs.get("evidence"), dict) else {},
        "blockers": blockers,
    }


def _complete_actionable_levels(
    *,
    market_region: Any,
    action: Any,
    section: Any,
    trigger_price: Any,
    max_entry: Any,
    stop_loss: Any,
    target1: Any,
) -> tuple[float | None, float | None, float | None, float | None]:
    trigger = _num(trigger_price)
    max_price = _num(max_entry)
    stop = _num(stop_loss)
    target = _num(target1)
    action_text = str(action or "").strip().upper()
    section_key = str(section or "").strip().lower()
    if action_text not in RALLY_PLAN_CONFIRM_ACTIONS or section_key not in RALLY_PLAN_LEVEL_SECTIONS or trigger is None:
        return trigger, max_price, stop, target
    market = str(market_region or "").strip().upper()
    if max_price is None or max_price <= trigger:
        max_price = trigger * (1.006 if section_key == "live_momentum" else 1.008)
    if market == "US":
        stop_pct = 0.035 if section_key == "live_momentum" else 0.04
        target_pct = 0.055 if section_key == "live_momentum" else 0.06
    else:
        stop_pct = 0.024 if section_key == "live_momentum" else 0.027
        target_pct = 0.026 if section_key == "live_momentum" else 0.032
    if stop is None or stop >= trigger:
        stop = trigger * (1.0 - stop_pct)
    if target is None or target <= max_price:
        target = trigger * (1.0 + target_pct)
    return (
        round(trigger, 4),
        round(max_price, 4),
        round(stop, 4),
        round(target, 4),
    )


def _normalized_rally_action(
    *,
    action: Any,
    section: Any,
    blockers: list[Any],
    trigger_price: Any,
    max_entry: Any,
    stop_loss: Any,
    target1: Any,
) -> str:
    action_text = str(action or "WATCH").strip().upper()
    section_key = str(section or "").strip().lower()
    if action_text not in RALLY_PLAN_CONFIRM_ACTIONS:
        return action_text
    if blockers:
        return "WATCH"
    if action_text in RALLY_PLAN_CONFIRM_ACTIONS and section_key not in RALLY_PLAN_ACTION_SECTIONS:
        return "WATCH"
    if section_key not in RALLY_PLAN_LEVEL_SECTIONS:
        return "WATCH" if action_text == "CONFIRM" else action_text
    if any(_num(value) is None for value in (trigger_price, max_entry, stop_loss, target1)):
        return "WATCH"
    return action_text


def _entry_plan(
    *,
    action: Any,
    section: Any,
    trigger_price: Any,
    max_entry: Any,
    stop_loss: Any,
    blockers: list[Any],
) -> dict[str, Any]:
    action_text = str(action or "WATCH").upper()
    section_key = str(section or "").lower()
    trigger = _num(trigger_price)
    max_price = _num(max_entry)
    stop = _num(stop_loss)
    missing = [
        label
        for label, value in (("trigger_price", trigger), ("max_entry", max_price), ("stop_loss", stop))
        if value is None
    ]
    confirmations = [
        "price trades above trigger",
        "price holds below max entry",
        "volume/opening-range confirmation",
        "market regime allows the setup",
    ]
    if action_text == "AVOID" or section_key == "avoid":
        status = "no_entry"
        when = "Do not enter. This row is marked avoid/do-not-chase until a fresh rally setup appears."
    elif blockers:
        status = "blocked_watch"
        when = "Do not enter yet. Keep on watch until blockers clear and the entry trigger confirms."
    elif missing:
        status = "incomplete_levels"
        when = f"Do not enter until {', '.join(missing)} is available."
    elif section_key in {"t1_pressure", "preopen_confirm"} or action_text == "WATCH":
        status = "watch_only"
        when = "No entry from this watch row alone. Enter only after pre-open/open confirmation holds above trigger and stays under max entry."
    else:
        status = "entry_check"
        when = "Enter only while price is at or above trigger, still below max entry, and live confirmation remains valid."

    if trigger is not None and max_price is not None:
        price_rule = f"Entry zone: {trigger:.2f} to {max_price:.2f}. Do not chase above {max_price:.2f}."
    elif trigger is not None:
        price_rule = f"Entry trigger: {trigger:.2f}. Wait for max-entry guard before sizing."
    else:
        price_rule = "No actionable entry price yet."

    return {
        "status": status,
        "when": when,
        "price_rule": price_rule,
        "trigger_price": trigger,
        "max_entry": max_price,
        "entry_zone": {"low": trigger, "high": max_price} if trigger is not None and max_price is not None else None,
        "do_not_chase_above": max_price,
        "requires_stop_before_entry": stop is None,
        "confirmations": confirmations,
    }


def _exit_plan(
    *,
    action: Any,
    section: Any,
    trigger_price: Any,
    max_entry: Any,
    stop_loss: Any,
    target1: Any,
    invalidation: str,
) -> dict[str, Any]:
    action_text = str(action or "WATCH").upper()
    section_key = str(section or "").lower()
    trigger = _num(trigger_price)
    max_price = _num(max_entry)
    stop = _num(stop_loss)
    t1 = _num(target1)
    target2 = None
    target3 = None
    if trigger is not None and t1 is not None and t1 > trigger:
        reward_unit = t1 - trigger
        target2 = round(trigger + reward_unit * 2.0, 4)
        target3 = round(trigger + reward_unit * 3.0, 4)
    elif max_price is not None and t1 is not None and t1 > max_price:
        reward_unit = t1 - max_price
        target2 = round(max_price + reward_unit * 2.0, 4)
        target3 = round(max_price + reward_unit * 3.0, 4)

    rules: list[dict[str, Any]] = []
    if action_text == "AVOID" or section_key == "avoid":
        rules.append({"label": "No trade", "when": "Do not enter; no exit plan applies until a fresh valid setup appears."})
    if stop is not None:
        rules.append({"label": "Hard stop", "price": stop, "when": "Exit full if price trades below stop or the invalidation condition triggers."})
    else:
        rules.append({"label": "Stop required", "when": "Do not enter without a hard stop."})
    if t1 is not None:
        rules.append({"label": "T1", "price": t1, "exit_pct": 35, "when": "Book partial profit near T1 and move remaining risk to breakeven/trigger."})
    else:
        rules.append({"label": "T1 required", "when": "Do not enter without at least one profit target."})
    if target2 is not None:
        rules.append({"label": "T2", "price": target2, "exit_pct": 50, "when": "Book another tranche near T2 and trail the remainder."})
    if target3 is not None:
        rules.append({"label": "T3 / trail", "price": target3, "exit_pct": 100, "when": "Close final remainder near T3 or trail until momentum breaks."})
    rules.append({"label": "Invalidation", "when": invalidation or "Exit/watch-reset if confirmation fails or market regime fades."})

    if action_text == "AVOID" or section_key == "avoid":
        summary = "No entry. Avoid chasing; reset only on a fresh setup."
    elif stop is None or t1 is None:
        summary = "Entry not allowed until stop and target are defined."
    else:
        summary = "Manage by hard stop first, partial at T1, then trail/reduce remaining quantity."

    return {
        "summary": summary,
        "stop_loss": stop,
        "target1": t1,
        "target2": target2,
        "target3": target3,
        "partial_exit_pct": 35 if t1 is not None else None,
        "second_exit_pct": 50 if target2 is not None else None,
        "rules": rules,
        "invalidation": invalidation,
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
