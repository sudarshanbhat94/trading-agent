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
    canonical_primary_blocker_counts: Counter[str] = Counter()
    canonical_version_counts: Counter[str] = Counter()
    absorbed_counts: Counter[str] = Counter()
    blocker_symbols: dict[str, set[str]] = defaultdict(set)
    primary_blocker_symbols: dict[str, set[str]] = defaultdict(set)
    canonical_primary_blocker_symbols: dict[str, set[str]] = defaultdict(set)
    live_quote_stale_intraday_symbols: set[str] = set()
    live_quote_stale_intraday_only_symbols: set[str] = set()
    canonical_gate_seen = 0
    canonical_gate_passed = 0
    canonical_gate_blocked = 0
    duplicate_active_buy_monitors = 0
    duplicate_active_buy_symbols: set[str] = set()
    top_holds: list[dict[str, Any]] = []

    for decision in decision_rows:
        symbol = str(decision.symbol or "").upper()
        audit = _json_object(decision.details_json)
        duplicate_suppression = (
            audit.get("duplicate_buy_suppression")
            if isinstance(audit.get("duplicate_buy_suppression"), dict)
            else {}
        )
        if duplicate_suppression.get("suppressed"):
            duplicate_active_buy_monitors += 1
            if symbol:
                duplicate_active_buy_symbols.add(symbol)
        gate_context = _decision_gate_context(audit)
        probe = gate_context.get("opportunity_probe") if isinstance(gate_context.get("opportunity_probe"), dict) else {}
        canonical_gate = gate_context.get("canonical_trade_gate") if isinstance(gate_context.get("canonical_trade_gate"), dict) else {}
        blocking_gates = _gate_names(gate_context.get("blocking_failed_gates") or gate_context.get("failed_gates"))
        absorbed_gates = _gate_names(probe.get("absorbed_gates"))
        primary_gate = _primary_gate_name(gate_context, blocking_gates)
        if canonical_gate:
            canonical_gate_seen += 1
            version = str(canonical_gate.get("canonical_version") or "unknown").strip() or "unknown"
            canonical_version_counts.update([version])
            if canonical_gate.get("passed"):
                canonical_gate_passed += 1
            else:
                canonical_gate_blocked += 1
                canonical_primary = str(
                    canonical_gate.get("primary_blocker") or canonical_gate.get("reason") or "canonical_trade_not_ready"
                ).strip()
                if canonical_primary:
                    canonical_primary_blocker_counts.update([canonical_primary])
                    canonical_primary_blocker_symbols[canonical_primary].add(symbol)
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
        if (
            canonical_gate
            and canonical_gate.get("primary_blocker") == "stale_market_data"
            and override in LIVE_QUOTE_STALE_INTRADAY_OVERRIDES
        ):
            live_quote_stale_intraday_symbols.add(symbol)
            if not blocking_gates or set(blocking_gates) == {"canonical_trade_contract"}:
                live_quote_stale_intraday_only_symbols.add(symbol)

        if str(decision.action or "").upper() == "HOLD":
            top_holds.append(_hold_summary(decision, audit, blocking_gates, probe, primary_gate, canonical_gate))

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
    canonical_buy_symbols = {
        str(decision.symbol or "").upper()
        for decision in decision_rows
        if _decision_has_canonical_buy_intent(decision) and str(decision.symbol or "").strip()
    }
    buy_intent_symbols = set(buy_symbols) | duplicate_active_buy_symbols
    buy_intent_symbols.update(canonical_buy_symbols)
    buy_intent_decisions = buy_decisions + duplicate_active_buy_monitors + len(canonical_buy_symbols - set(buy_symbols) - duplicate_active_buy_symbols)
    followed = _int(auto_trade.get("followed"))
    users_checked = _int(auto_trade.get("users_checked"))
    skipped = auto_trade.get("skipped") if isinstance(auto_trade.get("skipped"), list) else []
    follow_opportunities = users_checked * len(buy_intent_symbols)
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
        "buy_intent_decisions": buy_intent_decisions,
        "buy_intent_symbols": len(buy_intent_symbols),
        "canonical_buy_intent_symbols": len(canonical_buy_symbols),
        "duplicate_active_buy_monitors": duplicate_active_buy_monitors,
        "duplicate_active_buy_symbols": len(duplicate_active_buy_symbols),
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
        "canonical_trade": {
            "gate_seen": canonical_gate_seen,
            "gate_passed": canonical_gate_passed,
            "gate_blocked": canonical_gate_blocked,
            "version_counts": dict(canonical_version_counts.most_common(5)),
            "primary_blocker_counts": dict(canonical_primary_blocker_counts.most_common(15)),
            "top_blockers": [
                {
                    "blocker": blocker,
                    "count": count,
                    "unique_symbols": len(canonical_primary_blocker_symbols.get(blocker, set())),
                    "sample_symbols": sorted(canonical_primary_blocker_symbols.get(blocker, set()))[:12],
                }
                for blocker, count in canonical_primary_blocker_counts.most_common(15)
            ],
        },
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


def _decision_has_canonical_buy_intent(decision: Decision) -> bool:
    if str(decision.action or "").upper() == "BUY":
        return False
    audit = _json_object(decision.details_json)
    duplicate = audit.get("duplicate_buy_suppression") if isinstance(audit.get("duplicate_buy_suppression"), dict) else {}
    if duplicate.get("suppressed"):
        return False
    gate_context = _decision_gate_context(audit)
    canonical_gate = gate_context.get("canonical_trade_gate") if isinstance(gate_context.get("canonical_trade_gate"), dict) else {}
    return canonical_gate.get("passed") is True


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
    canonical_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score_breakdown = audit.get("score_breakdown") if isinstance(audit.get("score_breakdown"), dict) else {}
    canonical_gate = canonical_gate if isinstance(canonical_gate, dict) else {}
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
        "canonical_trade": {
            "version": canonical_gate.get("canonical_version"),
            "passed": canonical_gate.get("passed"),
            "primary_blocker": canonical_gate.get("primary_blocker"),
            "reason": canonical_gate.get("reason"),
            "secondary_blockers": canonical_gate.get("secondary_blockers") or [],
        }
        if canonical_gate
        else {},
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
    buy_intents = _int(funnel.get("buy_intent_decisions")) or buys
    followed_actions = _int(funnel.get("auto_followed_user_actions"))
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
        if buy_intents > 0:
            auto_follow = diagnostics.get("auto_follow") if isinstance(diagnostics.get("auto_follow"), dict) else {}
            skip_reasons = auto_follow.get("skip_reasons") if isinstance(auto_follow.get("skip_reasons"), dict) else {}
            if followed_actions <= 0 and not _all_auto_follow_skips_explained(skip_reasons):
                flags.append(
                    {
                        "severity": "warning",
                        "code": "all_buy_intents_already_active",
                        "message": "The cycle found BUY-grade ideas, but all were already active monitors rather than fresh entries.",
                    }
                )
        else:
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
        auto_follow = diagnostics.get("auto_follow") if isinstance(diagnostics.get("auto_follow"), dict) else {}
        users_checked = _int(auto_follow.get("users_checked"))
        active_checked = _int(auto_follow.get("active_buy_ideas_checked"))
        skip_reasons = auto_follow.get("skip_reasons") if isinstance(auto_follow.get("skip_reasons"), dict) else {}
        if users_checked > 0 and active_checked > 0 and _all_auto_follow_skips_explained(skip_reasons):
            return flags
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
    duplicate_monitors = _int(funnel.get("duplicate_active_buy_monitors"))
    followed = _int(funnel.get("auto_followed_user_actions"))
    top_blockers = diagnostics.get("top_blockers") if isinstance(diagnostics.get("top_blockers"), list) else []
    blocker = top_blockers[0]["gate"] if top_blockers and isinstance(top_blockers[0], dict) else "none"
    buy_text = f"{buys} BUYs"
    if duplicate_monitors:
        buy_text = f"{buy_text} (+{duplicate_monitors} already-active BUY monitors)"
    return (
        f"{raw} raw symbols -> {selected} scanner selections -> {decisions}/{target or decisions} decisions -> "
        f"{buy_text} -> {followed} auto-follows. Top blocker: {blocker}."
    )


def _skip_reason_counts(skipped: Any) -> dict[str, int]:
    if not isinstance(skipped, list):
        return {}
    counts = Counter(
        str(item.get("reason") or "unknown").strip() if isinstance(item, dict) else "unknown"
        for item in skipped
    )
    return dict(counts.most_common(12))


def _all_auto_follow_skips_explained(skip_reasons: dict[str, Any]) -> bool:
    if not skip_reasons:
        return False
    allowed = {
        "already_followed",
        "already_followed_symbol",
        "active_buy_not_fresh_enough_for_auto_follow",
        "outside_custom_monitor_list",
        "phase1_quality_gate",
        "position_size_below_minimum_trade_economics",
        "recent_risk_exit_cooldown",
    }
    return all(str(reason or "").strip() in allowed for reason in skip_reasons)


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
