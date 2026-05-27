from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


IST = timezone(timedelta(hours=5, minutes=30))
SECTION_ORDER = {
    "ready_at_open": 10,
    "btst_buys": 15,
    "near_breakout": 20,
    "news_watch": 30,
    "position_actions": 40,
    "avoid": 50,
}


def build_tomorrow_plan(
    *,
    market_region: str,
    signal_ideas: list[dict[str, Any]],
    positions: list[dict[str, Any]] | None = None,
    pre_catalyst: dict[str, Any] | None = None,
    opportunity_scan: dict[str, Any] | None = None,
    macro_context: dict[str, Any] | None = None,
    market_session: dict[str, Any] | None = None,
    prepared_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic post-market plan for the next trading day.

    The plan does not create BUY/SELL decisions. It prepares exact validation
    work for pre-open and normal-open so the live cycle can act quickly.
    """

    now = _parse_dt(prepared_at) or datetime.now(IST)
    plan_date = _next_weekday(now.astimezone(IST).date()).isoformat()
    region = _normalize_market(market_region)
    ideas = [item for item in signal_ideas if _row_market(item) == region]
    positions = [item for item in positions or [] if _row_market(item) == region]
    pre_catalyst = pre_catalyst if isinstance(pre_catalyst, dict) else {}
    macro_context = macro_context if isinstance(macro_context, dict) else {}
    opportunity_scan = opportunity_scan if isinstance(opportunity_scan, dict) else {}
    sections = {
        "ready_at_open": _ready_at_open_items(ideas),
        "btst_buys": _btst_buy_items(ideas, opportunity_scan, region),
        "near_breakout": _near_breakout_items(ideas),
        "news_watch": _news_watch_items(pre_catalyst, region),
        "position_actions": _position_action_items(positions),
        "avoid": _avoid_items(ideas),
    }
    items: list[dict[str, Any]] = []
    enriched_sections: dict[str, list[dict[str, Any]]] = {key: [] for key in sections}
    for section, rows in sections.items():
        for rank, item in enumerate(rows, start=1):
            enriched = {
                **item,
                "section": section,
                "section_rank": rank,
                "sort_order": SECTION_ORDER.get(section, 99) * 1000 + rank,
                "market_region": region,
                "plan_date": plan_date,
                "prepared_at": now.isoformat(),
            }
            items.append(enriched)
            enriched_sections.setdefault(section, []).append(enriched)
    return {
        "enabled": True,
        "market_region": region,
        "plan_date": plan_date,
        "prepared_at": now.isoformat(),
        "mode": "post_market_tomorrow_plan",
        "summary": _summary(sections, macro_context, opportunity_scan),
        "preopen_rules": _preopen_rules(region),
        "market_session": market_session or {},
        "items": items,
        "sections": enriched_sections,
        "readiness_note": "Use this as tomorrow's prepared battle sheet. Pre-open and first-candle gates must validate price, volume, and news before any fresh entry.",
    }


def _ready_at_open_items(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for idea in ideas:
        if str(idea.get("signal_type") or "").upper() != "BUY":
            continue
        if str(idea.get("status") or "").upper() not in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
            continue
        if _quality_reason(idea) and not _quality_passed(idea):
            continue
        rows.append(_plan_item_from_idea(idea, "Validate at pre-open; buy only if price remains inside the entry zone and first live volume confirms."))
    return sorted(rows, key=_item_rank, reverse=True)[:12]


def _btst_buy_items(ideas: list[dict[str, Any]], opportunity_scan: dict[str, Any], region: str) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for idea in ideas:
        details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
        scan = details.get("opportunity_scan") if isinstance(details.get("opportunity_scan"), dict) else {}
        if str(scan.get("setup") or "").lower() != "btst_buy_candidate":
            continue
        if str(idea.get("signal_type") or "").upper() != "BUY":
            continue
        item = _plan_item_from_idea(idea, "BTST BUY: enter only inside the entry zone near close; sell/trim tomorrow if first strength fades or first 15-minute low breaks.")
        item["action"] = "BTST BUY"
        item["details"]["btst"] = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
        rows.append(item)
        seen.add(str(item.get("symbol") or "").upper())
    for candidate in opportunity_scan.get("btst_buy_candidates") or []:
        if not isinstance(candidate, dict) or _row_market(candidate) != region:
            continue
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        btst = candidate.get("btst") if isinstance(candidate.get("btst"), dict) else {}
        entry = btst.get("entry_zone") if isinstance(btst.get("entry_zone"), dict) else {}
        rows.append(
            {
                "symbol": symbol,
                "name": candidate.get("name") or symbol,
                "action": "BTST BUY",
                "trigger_price": _number(entry.get("low")) or _number(candidate.get("price")),
                "max_entry": _number(entry.get("high") or btst.get("max_entry")),
                "stop_loss": _number(btst.get("stop_loss")),
                "target1": _number(btst.get("target1")),
                "score": _score_pct(btst.get("score") or candidate.get("score")),
                "confidence": _score_pct(btst.get("confidence") or candidate.get("score")),
                "strategy": "btst_buy_candidate",
                "rationale": "; ".join((btst.get("reasons") or candidate.get("reasons") or [])[:3])
                or "BTST candidate with closing strength and controlled overnight risk.",
                "validation": "Buy only if final quote remains near high with volume participation; no chase above max entry.",
                "details": {"btst": btst, "opportunity_scan": candidate},
            }
        )
    return sorted(rows, key=_item_rank, reverse=True)[:12]


def _near_breakout_items(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for idea in ideas:
        if str(idea.get("signal_type") or "").upper() == "BUY":
            continue
        state = str(idea.get("lifecycle_status") or idea.get("status") or "").lower()
        details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
        setup = str((details.get("opportunity_scan") or {}).get("setup") or idea.get("strategy") or "").lower()
        if str(idea.get("status") or "").upper() not in {"WATCH", "MONITORING", "ACTIVE"} and "watch" not in state:
            continue
        if not any(token in setup for token in ("breakout", "momentum", "darvas", "vcp", "52_week", "near")):
            continue
        rows.append(_plan_item_from_idea(idea, "Wait for trigger. Do not chase if pre-open is already above the max entry."))
    return sorted(rows, key=_item_rank, reverse=True)[:20]


def _news_watch_items(pre_catalyst: dict[str, Any], region: str) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in pre_catalyst.get("candidates") or []
        if isinstance(item, dict) and _row_market(item) == region
    ]
    rows = []
    for item in candidates:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        trigger = _number(item.get("trigger_price") or item.get("pivot") or item.get("price"))
        score = _number(item.get("score") or item.get("confidence")) or 0.0
        rows.append(
            {
                "symbol": symbol,
                "name": item.get("name") or item.get("company_name") or symbol,
                "action": "NEWS WATCH",
                "trigger_price": trigger,
                "max_entry": _pct(trigger, 1.03),
                "stop_loss": None,
                "target1": None,
                "score": _score_pct(score),
                "confidence": _score_pct(score),
                "strategy": item.get("setup") or item.get("label") or "pre_catalyst",
                "rationale": item.get("reason") or item.get("note") or "Potential catalyst candidate; validate with fresh news and live price action.",
                "validation": "Check fresh headlines, exchange announcements, and 9:15 volume before converting to a trade.",
                "details": item,
            }
        )
    return sorted(rows, key=_item_rank, reverse=True)[:15]


def _position_action_items(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in positions:
        symbol = str(item.get("symbol") or "").upper()
        qty = int(_number(item.get("qty")) or 0)
        if not symbol or qty <= 0:
            continue
        entry = _number(item.get("avg_price") or item.get("entry_price")) or 0.0
        latest = _number(item.get("market_price") or item.get("latest_price") or item.get("price")) or entry
        pnl_pct = ((latest - entry) / entry) * 100 if entry > 0 else 0.0
        action = "TRAIL"
        rationale = "Position is active; trail the stop and reassess against tomorrow's first 15-minute candle."
        if pnl_pct <= -2.0:
            action = "RISK REVIEW"
            rationale = "Position is red beyond normal noise; reduce or exit if opening price does not reclaim entry."
        elif pnl_pct >= 5.0:
            action = "PROTECT PROFIT"
            rationale = "Position has meaningful open profit; book partial or tighten the trailing stop."
        rows.append(
            {
                "symbol": symbol,
                "name": item.get("name") or symbol,
                "action": action,
                "trigger_price": latest,
                "max_entry": None,
                "stop_loss": _number(item.get("stop_loss")),
                "target1": None,
                "score": max(0.0, 100.0 - abs(pnl_pct)),
                "confidence": 70.0,
                "strategy": item.get("strategy") or "position_manager",
                "rationale": rationale,
                "validation": "At 9:08 check indicative open; at 9:15-9:25 act only after live quote confirms stop/target behavior.",
                "details": {"qty": qty, "entry_price": entry, "latest_price": latest, "pnl_pct": round(pnl_pct, 4), "raw": item},
            }
        )
    return sorted(rows, key=lambda item: abs(float((item.get("details") or {}).get("pnl_pct") or 0.0)), reverse=True)[:20]


def _avoid_items(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for idea in ideas:
        status = str(idea.get("status") or "").upper()
        signal_type = str(idea.get("signal_type") or "").upper()
        details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
        quality = details.get("quality_gate") if isinstance(details.get("quality_gate"), dict) else {}
        risk_flags = [str(flag).lower() for flag in details.get("risk_flags") or []]
        hard_risk = any(any(token in flag for token in ("asm", "gsm", "operator", "circuit", "distribution", "stop_hit")) for flag in risk_flags)
        if status not in {"STOP_HIT", "EXPIRED", "REJECTED", "EXIT_SIGNAL"} and signal_type not in {"NO_TRADE", "EXIT"} and not hard_risk:
            continue
        item = _plan_item_from_idea(idea, "Do not enter tomorrow unless a new full scan creates a fresh valid setup.")
        item["action"] = "AVOID"
        item["rationale"] = quality.get("message") or idea.get("reason") or "Invalidated or high-risk setup."
        rows.append(item)
    return sorted(rows, key=_item_rank, reverse=True)[:20]


def _plan_item_from_idea(idea: dict[str, Any], validation: str) -> dict[str, Any]:
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    entry_zone = details.get("entry_zone") if isinstance(details.get("entry_zone"), list) else None
    entry = _number((entry_zone or [None])[0]) or _number(idea.get("entry_price") or idea.get("latest_price"))
    max_entry = _number((entry_zone or [None, None])[1] if entry_zone else None)
    if entry and not max_entry:
        max_entry = entry * 1.03
    targets = details.get("target_status") if isinstance(details.get("target_status"), list) else details.get("targets")
    target1 = None
    if isinstance(targets, list) and targets:
        target1 = _number((targets[0] or {}).get("price") if isinstance(targets[0], dict) else None)
    quality = details.get("quality_gate") if isinstance(details.get("quality_gate"), dict) else {}
    return {
        "idea_id": idea.get("id"),
        "symbol": str(idea.get("symbol") or "").upper(),
        "name": idea.get("company_name") or idea.get("name") or idea.get("symbol"),
        "action": "READY" if str(idea.get("signal_type") or "").upper() == "BUY" else "WATCH",
        "trigger_price": entry,
        "max_entry": max_entry,
        "stop_loss": _number(details.get("stop_loss")),
        "target1": target1,
        "score": _number(idea.get("overall_score_pct")) or _number(quality.get("overall_score_pct")) or 0.0,
        "confidence": (_number(idea.get("confidence")) or 0.0) * 100.0,
        "strategy": idea.get("strategy") or details.get("plan_code") or "signal_idea",
        "rationale": _rationale(idea, details, quality),
        "validation": validation,
        "details": {
            "signal_type": idea.get("signal_type"),
            "status": idea.get("status"),
            "current_return_pct": idea.get("current_return_pct"),
            "quality_gate": quality,
            "risk_flags": details.get("risk_flags") or [],
            "entry_zone": entry_zone,
            "target_status": details.get("target_status") or [],
        },
    }


def _summary(sections: dict[str, list[dict[str, Any]]], macro_context: dict[str, Any], opportunity_scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready_at_open": len(sections.get("ready_at_open") or []),
        "btst_buys": len(sections.get("btst_buys") or []),
        "near_breakout": len(sections.get("near_breakout") or []),
        "news_watch": len(sections.get("news_watch") or []),
        "position_actions": len(sections.get("position_actions") or []),
        "avoid": len(sections.get("avoid") or []),
        "macro_regime": macro_context.get("regime"),
        "macro_risk_score": macro_context.get("risk_score"),
        "last_scan_selected": opportunity_scan.get("selected_symbols"),
        "last_scan_tradeable": opportunity_scan.get("tradeable_screening_symbols"),
    }


def _preopen_rules(region: str) -> list[dict[str, Any]]:
    if region != "IN":
        return [
            {"time": "before open", "action": "Refresh overnight news, futures, and broker quotes."},
            {"time": "market open", "action": "Validate price is still inside entry zone and first candle confirms volume."},
        ]
    return [
        {"time": "post-market", "action": "Prepare pivots, entry zones, stops, targets, news checks, and avoid list."},
        {"time": "09:00 IST", "action": "Fetch pre-open indicative price and imbalance. Do not trade from this alone."},
        {"time": "09:08 IST", "action": "Freeze the pre-open validation: greenlight only if indicative price is within entry and max chase."},
        {"time": "09:15-09:20 IST", "action": "Confirm live quote, first candle direction, and volume pace before auto-paper/live follow."},
        {"time": "09:20-09:25 IST", "action": "Cancel stale ideas, chase gaps above max entry, and names breaking below stop/support."},
    ]


def _rationale(idea: dict[str, Any], details: dict[str, Any], quality: dict[str, Any]) -> str:
    state = details.get("opportunity_state") if isinstance(details.get("opportunity_state"), dict) else {}
    reason = quality.get("message") or state.get("plain_english") or idea.get("reason") or ""
    return str(reason)[:420] if reason else "Prepared from latest OpenStocks signal audit."


def _item_rank(item: dict[str, Any]) -> tuple[float, float]:
    return (float(item.get("score") or 0.0), float(item.get("confidence") or 0.0))


def _row_market(row: dict[str, Any]) -> str:
    market = str(row.get("market_region") or row.get("market") or "").upper()
    if market in {"IN", "US"}:
        return market
    exchange = str(row.get("exchange") or "").upper()
    return "US" if exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"} else "IN"


def _normalize_market(value: str) -> str:
    return "US" if str(value or "").upper() == "US" else "IN"


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pct(value: float | None, multiplier: float) -> float | None:
    return round(value * multiplier, 4) if value else None


def _score_pct(value: float | None) -> float:
    if value is None:
        return 0.0
    return round(value * 100.0 if value <= 1 else value, 4)


def _quality_reason(idea: dict[str, Any]) -> str:
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    quality = details.get("quality_gate") if isinstance(details.get("quality_gate"), dict) else {}
    return str(quality.get("reason") or "")


def _quality_passed(idea: dict[str, Any]) -> bool:
    details = idea.get("details") if isinstance(idea.get("details"), dict) else {}
    quality = details.get("quality_gate") if isinstance(details.get("quality_gate"), dict) else {}
    return quality.get("passed") is not False
