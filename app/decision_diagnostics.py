from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Iterable

from .models import Decision, utc_now


LIVE_QUOTE_STALE_INTRADAY_OVERRIDES = {
    "live_quote_ohlcv_used_for_probe",
    "live_momentum_review_with_trade_ready_data",
}


def build_cycle_decision_diagnostics(
    scan_summary: dict[str, Any] | None,
    decisions: Iterable[Decision],
    *,
    shared_auto_trade: dict[str, Any] | None = None,
    executed_orders: int = 0,
    market_region: str | None = None,
    generated_at: str | None = None,
    cycle_duration_seconds: float | None = None,
    missed_move_review_row_id: int | None = None,
) -> dict[str, Any]:
    scan = scan_summary if isinstance(scan_summary, dict) else {}
    auto_trade = shared_auto_trade if isinstance(shared_auto_trade, dict) else {}
    decision_rows = list(decisions)
    action_counts = Counter(str(decision.action or "UNKNOWN").upper() for decision in decision_rows)
    blocker_counts: Counter[str] = Counter()
    primary_blocker_counts: Counter[str] = Counter()
    absorbed_counts: Counter[str] = Counter()
    blocker_symbols: dict[str, set[str]] = defaultdict(set)
    primary_blocker_symbols: dict[str, set[str]] = defaultdict(set)
    live_quote_stale_intraday_symbols: set[str] = set()
    live_quote_stale_intraday_only_symbols: set[str] = set()
    top_holds: list[dict[str, Any]] = []

    for decision in decision_rows:
        symbol = str(decision.symbol or "").upper()
        audit = _json_object(decision.details_json)
        gate_context = _decision_gate_context(audit)
        probe = gate_context.get("opportunity_probe") if isinstance(gate_context.get("opportunity_probe"), dict) else {}
        blocking_gates = _gate_names(gate_context.get("blocking_failed_gates") or gate_context.get("failed_gates"))
        absorbed_gates = _gate_names(probe.get("absorbed_gates"))
        primary_gate = _primary_gate_name(gate_context, blocking_gates)
        blocker_counts.update(blocking_gates)
        absorbed_counts.update(absorbed_gates)
        for gate in blocking_gates:
            blocker_symbols[gate].add(symbol)
        if primary_gate:
            primary_blocker_counts.update([primary_gate])
            primary_blocker_symbols[primary_gate].add(symbol)

        override = str(probe.get("data_quality_override") or "")
        if override in LIVE_QUOTE_STALE_INTRADAY_OVERRIDES and "fresh_market_data_gate" in blocking_gates:
            live_quote_stale_intraday_symbols.add(symbol)
            if set(blocking_gates) == {"fresh_market_data_gate"}:
                live_quote_stale_intraday_only_symbols.add(symbol)

        if str(decision.action or "").upper() == "HOLD":
            top_holds.append(_hold_summary(decision, audit, blocking_gates, probe, primary_gate))

    top_holds.sort(key=lambda item: (item["technical_score"], item["confidence"], item["combined_score"]), reverse=True)
    raw_symbols = _int(scan.get("raw_symbols") or scan.get("scanned_symbols_this_cycle"))
    quoted_symbols = _int(scan.get("quoted_symbols"))
    tradeable_symbols = _int(scan.get("tradeable_screening_symbols"))
    selected_symbols = _int(scan.get("selected_symbols"))
    target_decision_symbols = _int(scan.get("target_decision_symbols") or scan.get("slot_budget_target") or scan.get("candidate_limit"))
    decisions_created = len(decision_rows)
    buy_decisions = action_counts.get("BUY", 0)
    buy_symbols = {
        str(decision.symbol or "").upper()
        for decision in decision_rows
        if str(decision.action or "").upper() == "BUY" and str(decision.symbol or "").strip()
    }
    followed = _int(auto_trade.get("followed"))
    users_checked = _int(auto_trade.get("users_checked"))
    skipped = auto_trade.get("skipped") if isinstance(auto_trade.get("skipped"), list) else []
    follow_opportunities = users_checked * len(buy_symbols)
    funnel = {
        "raw_symbols": raw_symbols,
        "quoted_symbols": quoted_symbols,
        "tradeable_screening_symbols": tradeable_symbols,
        "scanner_selected_symbols": selected_symbols,
        "decisions_created": decisions_created,
        "target_decision_symbols": target_decision_symbols,
        "decision_target_shortfall": max(target_decision_symbols - decisions_created, 0) if target_decision_symbols else 0,
        "buy_decisions": buy_decisions,
        "buy_symbols": len(buy_symbols),
        "sell_decisions": action_counts.get("SELL", 0),
        "hold_decisions": action_counts.get("HOLD", 0),
        "auto_followed_user_actions": followed,
        "paper_followed_user_actions": followed,
        "auto_follow_skipped": len(skipped),
        "central_broker_orders": max(int(executed_orders or 0), 0),
        "executed_orders": max(int(executed_orders or 0), 0),
        "paper_trade_source": "user_idea_follows",
        "quote_coverage_pct": _pct(quoted_symbols, raw_symbols),
        "scanner_selection_pct": _pct(selected_symbols, raw_symbols),
        "decision_buy_rate_pct": _pct(buy_decisions, decisions_created),
        "auto_follow_user_conversion_pct": _pct(followed, follow_opportunities),
        "auto_follows_per_buy_symbol": round(followed / len(buy_symbols), 2) if buy_symbols else None,
    }
    top_blockers = [
        {
            "gate": gate,
            "count": count,
            "unique_symbols": len(primary_blocker_symbols.get(gate, set())),
            "sample_symbols": sorted(primary_blocker_symbols.get(gate, set()))[:12],
        }
        for gate, count in primary_blocker_counts.most_common(15)
    ]
    diagnostics = {
        "generated_at": generated_at or utc_now(),
        "market_region": str(market_region or scan.get("market_region") or "BOTH").upper(),
        "mode": scan.get("mode"),
        "cycle_duration_seconds": cycle_duration_seconds,
        "funnel": funnel,
        "action_counts": dict(action_counts),
        "top_blockers": top_blockers,
        "primary_blocker_counts": dict(primary_blocker_counts.most_common(15)),
        "all_blocking_gate_counts": dict(blocker_counts.most_common(15)),
        "absorbed_gate_counts": dict(absorbed_counts.most_common(12)),
        "slot_fill_counts": scan.get("slot_fill_counts") if isinstance(scan.get("slot_fill_counts"), dict) else {},
        "slot_budgets": scan.get("slot_budgets") if isinstance(scan.get("slot_budgets"), dict) else {},
        "slot_shortfalls": scan.get("slot_shortfalls") if isinstance(scan.get("slot_shortfalls"), dict) else {},
        "target_decision_symbols_by_market": scan.get("target_decision_symbols_by_market")
        if isinstance(scan.get("target_decision_symbols_by_market"), dict)
        else {},
        "slot_fill_counts_by_market": scan.get("slot_fill_counts_by_market")
        if isinstance(scan.get("slot_fill_counts_by_market"), dict)
        else {},
        "slot_budgets_by_market": scan.get("slot_budgets_by_market")
        if isinstance(scan.get("slot_budgets_by_market"), dict)
        else {},
        "slot_shortfalls_by_market": scan.get("slot_shortfalls_by_market")
        if isinstance(scan.get("slot_shortfalls_by_market"), dict)
        else {},
        "missed_move_review_row_id": missed_move_review_row_id or _int(scan.get("missed_move_review_row_id")),
        "live_quote_stale_intraday": {
            "blocked_decision_symbols": len(live_quote_stale_intraday_symbols),
            "only_blocker_symbols": len(live_quote_stale_intraday_only_symbols),
            "sample_only_blocker_symbols": sorted(live_quote_stale_intraday_only_symbols)[:25],
        },
        "scanner_rejections": _top_mapping(scan.get("rejected_counts"), 15),
        "scanner_setups": _top_mapping(scan.get("setup_counts"), 15),
        "auto_follow": {
            "users_checked": _int(auto_trade.get("users_checked")),
            "active_buy_ideas_checked": _int(auto_trade.get("active_buy_ideas_checked")),
            "followed": followed,
            "exited": _int(auto_trade.get("exited")),
            "skip_reasons": _skip_reason_counts(skipped),
        },
        "top_hold_candidates": top_holds[:20],
    }
    diagnostics["health_flags"] = _health_flags(diagnostics)
    diagnostics["summary"] = _summary(diagnostics)
    return diagnostics


def _decision_gate_context(audit: dict[str, Any]) -> dict[str, Any]:
    risk_gates = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
    gate_context = risk_gates.get("decision_gate_context") if isinstance(risk_gates.get("decision_gate_context"), dict) else {}
    if gate_context:
        return gate_context
    context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
    return context.get("decision_gate_context") if isinstance(context.get("decision_gate_context"), dict) else {}


def _gate_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("gate") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return names


def _primary_gate_name(gate_context: dict[str, Any], blocking_gates: list[str]) -> str:
    explicit = gate_context.get("primary_blocker") if isinstance(gate_context.get("primary_blocker"), dict) else {}
    explicit_name = str(explicit.get("gate") or gate_context.get("primary_blocker_gate") or "").strip()
    if explicit_name:
        return explicit_name
    priority = {
        "invalid_price": 0,
        "phase2_data_readiness": 10,
        "fresh_market_data_gate": 20,
        "technical_score_gate": 30,
        "actionable_strategy_gate": 40,
        "opportunity_scan_entry_window": 50,
        "overall_quality_gate": 60,
        "entry_grade_gate": 70,
    }
    if not blocking_gates:
        return ""
    return sorted(blocking_gates, key=lambda gate: (priority.get(gate, 500), blocking_gates.index(gate)))[0]


def _hold_summary(
    decision: Decision,
    audit: dict[str, Any],
    blocking_gates: list[str],
    probe: dict[str, Any],
    primary_gate: str,
) -> dict[str, Any]:
    score_breakdown = audit.get("score_breakdown") if isinstance(audit.get("score_breakdown"), dict) else {}
    return {
        "symbol": decision.symbol,
        "strategy": decision.strategy,
        "confidence": round(float(decision.confidence or 0.0), 4),
        "technical_score": round(float(decision.technical_score or 0.0), 4),
        "combined_score": round(float(score_breakdown.get("combined") or 0.0), 4),
        "price": round(float(decision.price or 0.0), 4),
        "reason": decision.reason,
        "primary_blocker": primary_gate,
        "blocking_gates": blocking_gates[:8],
        "secondary_blockers": [gate for gate in blocking_gates if gate != primary_gate][:8],
        "opportunity_probe": {
            "ready": bool(probe.get("ready")),
            "source": probe.get("source"),
            "setup": probe.get("setup"),
            "scan_score": probe.get("scan_score"),
            "data_quality_override": probe.get("data_quality_override"),
        },
    }


def _health_flags(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    funnel = diagnostics.get("funnel") if isinstance(diagnostics.get("funnel"), dict) else {}
    market_region = str(diagnostics.get("market_region") or "").upper()
    raw = _int(funnel.get("raw_symbols"))
    selected = _int(funnel.get("scanner_selected_symbols"))
    decisions = _int(funnel.get("decisions_created"))
    target = _int(funnel.get("target_decision_symbols"))
    buys = _int(funnel.get("buy_decisions"))
    if raw >= 500 and selected > 0 and selected / raw < 0.05 and (target <= 0 or selected < target):
        flags.append(
            {
                "severity": "warning",
                "code": "scanner_shortlist_too_narrow",
                "message": "Less than 5% of raw symbols reached full strategy decisions.",
            }
        )
    if market_region in {"IN", "US", "BOTH"} and target >= 100 and decisions < target:
        code = (
            "nse_full_decision_target_missed"
            if market_region == "IN"
            else "us_full_decision_target_missed"
            if market_region == "US"
            else "market_full_decision_target_missed"
        )
        label = "India" if market_region == "IN" else "US" if market_region == "US" else "Configured market"
        flags.append(
            {
                "severity": "critical",
                "code": code,
                "message": f"{label} cycle produced {decisions} full decisions below the configured target of {target}.",
            }
        )
    if market_region in {"IN", "US", "BOTH"} and target >= 100 and diagnostics.get("mode") == "dynamic_opportunity_scan" and not _int(
        diagnostics.get("missed_move_review_row_id")
    ):
        flags.append(
            {
                "severity": "critical",
                "code": "missed_move_review_not_persisted",
                "message": "Open-market cycle did not persist a missed-move review row.",
            }
        )
    if decisions >= 100 and buys == 0:
        flags.append(
            {
                "severity": "critical",
                "code": "no_buys_from_large_decision_set",
                "message": "A large decision set produced zero BUYs; inspect top blockers and missed movers.",
            }
        )
    fresh = diagnostics.get("live_quote_stale_intraday") if isinstance(diagnostics.get("live_quote_stale_intraday"), dict) else {}
    if _int(fresh.get("only_blocker_symbols")) > 0:
        flags.append(
            {
                "severity": "critical",
                "code": "live_quote_blocked_by_stale_intraday_only",
                "message": "Live quote probes were blocked only because cached intraday candles were stale.",
            }
        )
    if buys > 0 and _int(funnel.get("auto_followed_user_actions")) == 0 and _int(funnel.get("auto_follow_user_conversion_pct")) == 0:
        users_checked = _int((diagnostics.get("auto_follow") or {}).get("users_checked")) if isinstance(diagnostics.get("auto_follow"), dict) else 0
        code = "buy_decisions_not_followed" if users_checked > 0 else "buy_decisions_no_eligible_auto_follow_users"
        flags.append(
            {
                "severity": "warning",
                "code": code,
                "message": "BUY decisions were created, but no eligible user auto-follow opened a paper trade.",
            }
        )
    return flags


def _summary(diagnostics: dict[str, Any]) -> str:
    funnel = diagnostics.get("funnel") if isinstance(diagnostics.get("funnel"), dict) else {}
    raw = _int(funnel.get("raw_symbols"))
    selected = _int(funnel.get("scanner_selected_symbols"))
    decisions = _int(funnel.get("decisions_created"))
    target = _int(funnel.get("target_decision_symbols"))
    buys = _int(funnel.get("buy_decisions"))
    followed = _int(funnel.get("auto_followed_user_actions"))
    top_blockers = diagnostics.get("top_blockers") if isinstance(diagnostics.get("top_blockers"), list) else []
    blocker = top_blockers[0]["gate"] if top_blockers and isinstance(top_blockers[0], dict) else "none"
    return (
        f"{raw} raw symbols -> {selected} scanner selections -> {decisions}/{target or decisions} decisions -> "
        f"{buys} BUYs -> {followed} auto-follows. Top blocker: {blocker}."
    )


def _skip_reason_counts(skipped: Any) -> dict[str, int]:
    if not isinstance(skipped, list):
        return {}
    counts = Counter(
        str(item.get("reason") or "unknown").strip() if isinstance(item, dict) else "unknown"
        for item in skipped
    )
    return dict(counts.most_common(12))


def _top_mapping(value: Any, limit: int) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts = Counter({str(key): _int(raw) for key, raw in value.items()})
    return dict(counts.most_common(limit))


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((max(int(numerator or 0), 0) / denominator) * 100.0, 2)


def _int(value: Any) -> int:
    try:
        return max(int(float(value or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
