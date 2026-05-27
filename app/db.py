from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .decision_contract import current_decision_rows, normalize_trade_targets, ranked_decision_rows
from .llm_usage import DEFAULT_SIGNAL_TOKEN_ESTIMATE, DEFAULT_TOKENS_PER_CREDIT
from .models import Candle, Decision, Quote, utc_now
from .market_regions import INDIA_EXCHANGES, normalize_market_region
from .opportunity_state import is_signal_candidate_state, opportunity_state_from_signal_details
from .signal_quality import (
    AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
    DUPLICATE_BUY_COOLDOWN_HOURS,
    FRESH_BUY_WINDOW_MINUTES,
    active_follow_safety_gate,
    auto_follow_quality_gate,
    fresh_buy_quality_gate,
    trade_readiness_gate,
)
from .trade_economics import (
    entry_size_economics,
    exit_economics,
    should_block_low_value_profit_exit,
)
from .trading_rules import _score_grade


def _market_region_case(alias: str = "u") -> str:
    india_values = ",".join(f"'{exchange}'" for exchange in sorted(INDIA_EXCHANGES))
    return (
        f"case "
        f"when upper(coalesce({alias}.exchange,'')) in ({india_values}) then 'IN' "
        f"when {alias}.exchange is null then 'IN' "
        f"else 'US' end"
    )


def _market_region_where(alias: str, market_region: str | None) -> tuple[str, list[Any]]:
    region = normalize_market_region(market_region or "BOTH", default="BOTH")
    if region == "BOTH":
        return "", []
    placeholders = ",".join("?" for _ in INDIA_EXCHANGES)
    if region == "IN":
        return f"upper(coalesce({alias}.exchange,'')) in ({placeholders})", sorted(INDIA_EXCHANGES)
    return (
        f"{alias}.exchange is not null and upper(coalesce({alias}.exchange,'')) not in ({placeholders})",
        sorted(INDIA_EXCHANGES),
    )


def _normalize_signal_execution_mode(value: Any) -> str:
    mode = str(value or "SIGNAL_ONLY").strip().upper()
    aliases = {
        "SIGNALS": "SIGNAL_ONLY",
        "SIGNAL": "SIGNAL_ONLY",
        "SIGNAL_ONLY": "SIGNAL_ONLY",
        "PAPER": "AUTO_PAPER",
        "AUTO_PAPER": "AUTO_PAPER",
        "LIVE": "AUTO_LIVE",
        "AUTO_LIVE": "AUTO_LIVE",
    }
    return aliases.get(mode, "SIGNAL_ONLY")


def _normalize_monitor_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = []
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        token = str(raw or "").strip().upper()
        if not token:
            continue
        if ":" in token:
            token = token.rsplit(":", 1)[-1]
        for suffix in (".NS", ".BO", ".NSE", ".BSE"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        token = "".join(char for char in token if char.isalnum() or char in {"&", "-", "_"})
        if not token or token in seen:
            continue
        seen.add(token)
        symbols.append(token[:32])
    return symbols


NSE_INDUSTRY_FALLBACKS: dict[str, str] = {
    "ABCAPITAL": "Financial Services Holding",
    "ADANIENT": "Diversified Holdings",
    "ADANIPORTS": "Ports & Logistics",
    "ASIANPAINT": "Paints",
    "AXISBANK": "Private Sector Bank",
    "BAJAJFINSV": "Financial Services Holding",
    "BAJFINANCE": "NBFC",
    "BANKINDIA": "Public Sector Bank",
    "BEL": "Defence Electronics",
    "BHEL": "Electrical Equipment",
    "BHARTIARTL": "Telecom Services",
    "CANBK": "Public Sector Bank",
    "CENTRALBK": "Public Sector Bank",
    "COALINDIA": "Coal",
    "GAIL": "Gas Transmission & Marketing",
    "GMRAIRPORT": "Airport Infrastructure",
    "HCLTECH": "IT Services",
    "HDFCBANK": "Private Sector Bank",
    "HFCL": "Telecom Equipment",
    "HINDCOPPER": "Copper",
    "HINDUNILVR": "FMCG",
    "HUDCO": "Housing Finance",
    "ICICIBANK": "Private Sector Bank",
    "IDEA": "Telecom Services",
    "IDFCFIRSTB": "Private Sector Bank",
    "IFCI": "Development Finance",
    "INFY": "IT Services",
    "IOB": "Public Sector Bank",
    "IRCON": "Rail EPC",
    "IREDA": "Renewable Energy Finance",
    "IRFC": "Railway Finance",
    "ITC": "FMCG - Tobacco",
    "JPPOWER": "Power Generation",
    "KOTAKBANK": "Private Sector Bank",
    "LT": "Engineering & Construction",
    "LTF": "NBFC",
    "M&M": "Automobiles",
    "MANAPPURAM": "Gold Loan NBFC",
    "MARUTI": "Passenger Cars",
    "MOREPENLAB": "Pharmaceuticals",
    "NATIONALUM": "Aluminium",
    "NBCC": "Construction & Real Estate",
    "NESTLEIND": "Packaged Foods",
    "NMDC": "Iron Ore Mining",
    "NTPC": "Power Generation",
    "ONGC": "Oil & Gas Exploration",
    "PCJEWELLER": "Jewellery Retail",
    "PNB": "Public Sector Bank",
    "POWERGRID": "Power Transmission",
    "RAILTEL": "Telecom Infrastructure",
    "RBLBANK": "Private Sector Bank",
    "RELIANCE": "Integrated Oil & Gas",
    "RPOWER": "Power Generation",
    "RVNL": "Rail Infrastructure",
    "SAIL": "Steel",
    "SBIN": "Public Sector Bank",
    "SJVN": "Hydropower Generation",
    "SOUTHBANK": "Private Sector Bank",
    "SUNPHARMA": "Pharmaceuticals",
    "SUZLON": "Wind Energy Equipment",
    "TCS": "IT Services",
    "TEXRAIL": "Rail Equipment",
    "TITAN": "Jewellery & Watches",
    "TMPV": "Automobiles",
    "TRIDENT": "Textiles & Home Furnishing",
    "UJJIVANSFB": "Small Finance Bank",
    "ULTRACEMCO": "Cement",
    "UNIONBANK": "Public Sector Bank",
    "WIPRO": "IT Services",
    "YESBANK": "Private Sector Bank",
}


REENTRY_BLOCK_EXIT_KEYS = {
    "STOP_LOSS",
    "STOP_HIT",
    "RISK_EXIT_BEFORE_T1",
    "EXIT_SIGNAL",
    "EXPIRED",
    "SAFETY_EXIT",
    "auto_exit_signal_sell",
}


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _recent_dt(value: Any, *, minutes: int = FRESH_BUY_WINDOW_MINUTES) -> bool:
    parsed = _parse_dt(value)
    if not parsed:
        return False
    age = datetime.now(timezone.utc) - parsed
    return age <= timedelta(minutes=max(int(minutes or FRESH_BUY_WINDOW_MINUTES), 1))


def _sector_from_industry(industry: Any) -> str:
    text = str(industry or "").lower()
    if not text:
        return ""
    if any(token in text for token in ("bank", "nbfc", "finance", "insurance", "housing finance", "gold loan")):
        return "Financial Services"
    if any(token in text for token in ("it services", "software", "technology", "telecom", "electronics")):
        return "Technology & Telecom"
    if any(token in text for token in ("pharma", "healthcare", "hospital", "diagnostic")):
        return "Healthcare"
    if any(token in text for token in ("auto", "passenger cars", "vehicle", "tyre", "component")):
        return "Automobiles"
    if any(token in text for token in ("power", "renewable", "hydro", "wind", "gas", "oil", "coal", "transmission")):
        return "Energy & Utilities"
    if any(token in text for token in ("steel", "copper", "aluminium", "iron ore", "cement", "chemical", "fertil", "mining")):
        return "Materials"
    if any(token in text for token in ("construction", "infra", "rail", "port", "airport", "logistics", "engineering", "epc")):
        return "Infrastructure"
    if any(token in text for token in ("fmcg", "foods", "paint", "jewellery", "textiles", "retail", "consumer")):
        return "Consumer"
    return ""


def _json_load(value: Any) -> Any:
    try:
        return json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return {}


def _bounded_for_storage(value: Any, depth: int = 0, max_depth: int = 5, dict_limit: int = 12, list_limit: int = 6) -> Any:
    if depth >= max_depth:
        return _storage_scalar(value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= dict_limit:
                output["_truncated_keys"] = max(len(value) - dict_limit, 0)
                break
            output[str(key)] = _bounded_for_storage(item, depth + 1, max_depth, dict_limit, list_limit)
        return output
    if isinstance(value, list):
        trimmed = [_bounded_for_storage(item, depth + 1, max_depth, dict_limit, list_limit) for item in value[:list_limit]]
        if len(value) > list_limit:
            trimmed.append({"_truncated_items": len(value) - list_limit})
        return trimmed
    return _storage_scalar(value)


def _storage_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:300]}... [truncated {len(value) - 300} chars]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:800]


def _pick_keys(source: Any, keys: Iterable[str]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: source.get(key) for key in keys if key in source}


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _compact_full_spectrum(full: Any) -> dict[str, Any]:
    if not isinstance(full, dict):
        return {}
    trend = full.get("trend_context") if isinstance(full.get("trend_context"), dict) else {}
    scorecard = full.get("institutional_scorecard") if isinstance(full.get("institutional_scorecard"), dict) else {}
    signal_plan = full.get("signal_plan") if isinstance(full.get("signal_plan"), dict) else {}
    trade_plan = full.get("trade_plan") if isinstance(full.get("trade_plan"), dict) else {}
    risk_overrides = full.get("risk_overrides") if isinstance(full.get("risk_overrides"), dict) else {}
    strategy_logic = full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {}
    return _bounded_for_storage(
        {
            "confluence_score": full.get("confluence_score"),
            "institutional_scorecard": _pick_keys(
                scorecard,
                ["total_score", "grade", "buy_ready", "must_pass_failed", "warnings", "hard_veto"],
            ),
            "signal_plan": _pick_keys(
                signal_plan,
                ["direction", "decision_readiness", "institutional_grade", "failed_must_pass", "confluence"],
            ),
            "trade_plan": _pick_keys(trade_plan, ["entry_zone", "stop_loss", "targets", "risk_reward"]),
            "risk_overrides": _pick_keys(risk_overrides, ["no_new_longs", "flags", "size_multiplier"]),
            "strategy_logic_filters": _pick_keys(
                strategy_logic,
                ["passed", "hard_blocks", "penalties", "score_penalty", "pivot_extension", "breakout_volume", "event_driven_thesis", "institutional_sponsorship"],
            ),
            "stage_analysis": full.get("stage_analysis"),
            "entry_quality": full.get("entry_quality"),
            "breakout_quality": full.get("breakout_quality"),
            "price_volume_divergence": full.get("price_volume_divergence"),
            "performance_feedback": full.get("performance_feedback"),
            "timeframe_alignment": trend.get("timeframe_alignment"),
            "relative_strength": full.get("relative_strength"),
            "sector_rotation": full.get("sector_rotation"),
            "delivery_accumulation": full.get("delivery_accumulation"),
            "options_intelligence": full.get("options_intelligence"),
        }
    )


def _compact_llm_prompt_audit(audit: Any) -> dict[str, Any] | None:
    if not isinstance(audit, dict):
        return None
    user_context = audit.get("user_context") if isinstance(audit.get("user_context"), dict) else {}
    return _bounded_for_storage(
        {
            "storage_compacted": True,
            "market_region": audit.get("market_region"),
            "currency": audit.get("currency"),
            "model": audit.get("model"),
            "mode": audit.get("mode"),
            "system_prompt_chars": audit.get("system_prompt_chars"),
            "context_chars": audit.get("context_chars"),
            "estimated_input_tokens": audit.get("estimated_input_tokens"),
            "included_sections": audit.get("included_sections"),
            "context_sha256": audit.get("context_sha256"),
            "system_prompt": audit.get("system_prompt"),
            "user_context": user_context,
        },
        max_depth=6,
        dict_limit=24,
        list_limit=8,
    )


def _compact_decision_details(row: dict[str, Any], raw_details: Any) -> str:
    raw_text = raw_details or "{}"
    audit = _json_load(raw_text)
    if not isinstance(audit, dict):
        audit = {}
    context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
    full = context.get("full_spectrum_analysis") if isinstance(context.get("full_spectrum_analysis"), dict) else {}
    risk_gates = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
    score_breakdown = audit.get("score_breakdown") if isinstance(audit.get("score_breakdown"), dict) else {}
    system_gate = audit.get("system_gate_audit") or context.get("system_gate_audit")
    if isinstance(system_gate, dict):
        system_gate = _pick_keys(
            system_gate,
            [
                "overall_score_pct",
                "overall_grade",
                "recommended_action",
                "classification",
                "active_flags",
                "hard_blocked",
                "grade_violation_count",
                "delivery_conflict_count",
                "price_mismatch_count",
                "data_readiness",
            ],
        )
    else:
        system_gate = None
    technical = context.get("technical_math") if isinstance(context.get("technical_math"), dict) else {}
    sentiment = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    candle = context.get("candlestick_analysis") if isinstance(context.get("candlestick_analysis"), dict) else {}
    compact = {
        "audit_version": audit.get("audit_version", 1),
        "storage_compacted": True,
        "compacted_at": utc_now(),
        "original_details_bytes": len(str(raw_text).encode("utf-8", "ignore")),
        "decision_path": audit.get("decision_path"),
        "final_action": row.get("action") or audit.get("final_action"),
        "action_reason": audit.get("action_reason") or row.get("reason"),
        "score_breakdown": _pick_keys(score_breakdown, ["combined", "score_percent", "score_percent_note", "formula", "components", "data_gaps"]),
        "overall_score_pct": audit.get("overall_score_pct"),
        "overall_grade": audit.get("overall_grade"),
        "pre_filter": audit.get("pre_filter") or context.get("pre_filter"),
        "system_gate_audit": system_gate,
        "data_readiness": audit.get("data_readiness") or context.get("data_readiness"),
        "performance_feedback": context.get("performance_feedback"),
        "sizing_grade": audit.get("sizing_grade") or context.get("sizing_grade"),
        "llm_primary_fallback": audit.get("llm_primary_fallback") or context.get("llm_primary_fallback"),
        "llm_prompt_audit": _compact_llm_prompt_audit(audit.get("llm_prompt_audit")),
        "risk_gates": _pick_keys(
            risk_gates,
            [
                "has_existing_position",
                "current_open_positions",
                "max_positions",
                "buy_combined_threshold",
                "buy_confluence_threshold",
                "institutional_scorecard",
                "pre_filter",
                "decision_gate_context",
                "portfolio_correlation_gate",
                "sizing_grade",
                "system_gate_audit",
                "data_readiness",
                "performance_feedback",
                "llm_primary_fallback",
                "llm_primary_rule_blocked",
            ],
        ),
        "context_summary": {
            "symbol": context.get("symbol") or row.get("symbol"),
            "company": context.get("company"),
            "sector": context.get("sector"),
            "exchange": context.get("exchange"),
            "quote": context.get("quote"),
            "technical_math": _pick_keys(
                technical,
                ["score", "trend", "rsi", "sma_fast", "sma_slow", "momentum_pct", "atr_pct", "adx"],
            ),
            "candlestick_analysis": _pick_keys(candle, ["score", "patterns", "bias", "confidence"]),
            "best_strategy": context.get("best_strategy"),
            "sentiment": _pick_keys(sentiment, ["score", "bias", "headline_count", "confidence", "source"]),
            "market_breadth_context": context.get("market_breadth_context"),
            "macro_event_context": context.get("macro_event_context"),
            "sector_rotation": context.get("sector_rotation"),
            "delivery_data": context.get("delivery_data"),
            "data_readiness": context.get("data_readiness"),
            "performance_feedback": context.get("performance_feedback"),
            "full_spectrum_summary": _compact_full_spectrum(full),
            "universe_relative_strength": context.get("universe_relative_strength"),
            "universe_scan": context.get("universe_scan"),
            "recent_candle_count": context.get("recent_candle_count"),
        },
    }
    return json.dumps(_bounded_for_storage(compact, dict_limit=32), default=str, separators=(",", ":"))


def _plan_max_days(plan_code: str, fallback_status: str = "") -> int:
    code = str(plan_code or "").lower()
    status = str(fallback_status or "").upper()
    if status in {"WATCH", "MONITORING"}:
        return 7
    if code == "aggressive_rs_breakout":
        return 8
    if code == "smallcap_momentum":
        return 10
    if code == "confirmed_breakout":
        return 15
    if code == "pullback_to_strength":
        return 20
    if code == "defensive_exit_manager":
        return 5
    return 25


def _normalize_targets(targets: Any) -> list[dict[str, Any]]:
    return normalize_trade_targets(targets)


def _refresh_idea_lifecycle(
    previous_details: dict[str, Any] | None,
    incoming_details: dict[str, Any] | None,
    entry_price: float,
    latest_price: float,
    status: str,
    now_iso: str,
    plan_code: str,
    first_seen_at: str | None = None,
) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {}
    if isinstance(previous_details, dict):
        details.update(previous_details)
    if isinstance(incoming_details, dict):
        details.update(incoming_details)

    targets = _normalize_targets(details.get("targets"))
    details["targets"] = targets
    previous_statuses = {
        str(item.get("label") or "").upper(): item
        for item in details.get("target_status", [])
        if isinstance(item, dict)
    }
    target_status: list[dict[str, Any]] = []
    highest_hit = ""
    for target in targets:
        label = str(target.get("label") or "").upper()
        price = float(target.get("price") or 0.0)
        previous = previous_statuses.get(label, {})
        hit = bool(previous.get("hit")) or (latest_price >= price > 0)
        if hit:
            highest_hit = label
        target_status.append(
            {
                "label": label,
                "price": price,
                "hit": hit,
                "hit_at": previous.get("hit_at") or (now_iso if hit else None),
                "distance_pct": _return_pct(latest_price, price) if latest_price > 0 else 0.0,
                "basis": target.get("basis"),
                "probability_label": target.get("probability_label"),
                "suggested_exit_pct": target.get("suggested_exit_pct"),
            }
        )

    stop_loss = _optional_float(details.get("stop_loss"))
    stop_hit = bool(details.get("stop_status", {}).get("hit")) if isinstance(details.get("stop_status"), dict) else False
    if stop_loss is not None and stop_loss > 0 and latest_price > 0:
        stop_hit = stop_hit or latest_price <= stop_loss
    stop_status = {
        "price": stop_loss,
        "hit": bool(stop_hit),
        "hit_at": (
            details.get("stop_status", {}).get("hit_at")
            if isinstance(details.get("stop_status"), dict)
            else None
        )
        or (now_iso if stop_hit else None),
    }

    first_seen = _parse_dt(first_seen_at) or _parse_dt(details.get("generated_at")) or _parse_dt(now_iso) or datetime.now(timezone.utc)
    expires_at = details.get("expires_at")
    if not expires_at:
        expires_at = (first_seen + timedelta(days=_plan_max_days(plan_code, status))).isoformat()
    expires_dt = _parse_dt(expires_at)
    now_dt = _parse_dt(now_iso) or datetime.now(timezone.utc)
    expired = bool(expires_dt and now_dt >= expires_dt)
    days_left = max(0, int((expires_dt - now_dt).total_seconds() // 86400)) if expires_dt else None
    overall_score = _optional_float(details.get("overall_score_pct")) or 0.0
    current_return_pct = _return_pct(entry_price, latest_price)

    lifecycle_status = "active"
    new_status = str(status or "ACTIVE")
    if stop_status["hit"]:
        lifecycle_status = "stopped"
        new_status = "STOP_HIT"
    elif str(status or "").upper() == "EXIT_SIGNAL":
        lifecycle_status = "exit_signal"
        new_status = "EXIT_SIGNAL"
    elif highest_hit == "T3":
        lifecycle_status = "target_3_hit"
        new_status = "TARGET_3_HIT"
    elif highest_hit == "T2":
        lifecycle_status = "target_2_hit"
    elif highest_hit == "T1":
        lifecycle_status = "target_1_hit"
    elif expired:
        lifecycle_status = "expired"
        new_status = "EXPIRED"
    elif str(status or "").upper() in {"WATCH", "MONITORING"} and overall_score < 55:
        lifecycle_status = "rejected_low_quality"
        new_status = "REJECTED"
    elif str(status or "").upper() in {"WATCH", "MONITORING"}:
        lifecycle_status = str(status).lower()

    details.update(
        {
            "generated_at": details.get("generated_at") or first_seen.isoformat(),
            "expires_at": expires_at,
            "days_to_expiry": days_left,
            "target_status": target_status,
            "highest_target_hit": highest_hit or "NONE",
            "stop_status": stop_status,
            "drawdown_status": {
                "return_pct": current_return_pct,
                "in_red": current_return_pct < 0,
                "near_stop": bool(stop_loss and latest_price > 0 and latest_price <= (entry_price + stop_loss) / 2),
            },
            "lifecycle_status": lifecycle_status,
            "timeline": {
                "plan_code": plan_code,
                "max_days": _plan_max_days(plan_code, status),
                "started_at": first_seen.isoformat(),
                "expires_at": expires_at,
                "days_left": days_left,
            },
        }
    )
    return new_status, details


def _public_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    paper_cash_by_market = {
        "IN": round(float(row["paper_cash_in"]), 2) if row.get("paper_cash_in") is not None else None,
        "US": round(float(row["paper_cash_us"]), 2) if row.get("paper_cash_us") is not None else None,
    }
    broker_accounts = {
        "indstocks": {
            "access_token_saved": bool(row.get("indstocks_access_token")),
            "connected": bool(row.get("indstocks_access_token")),
            "base_url": row.get("indstocks_api_base_url") or "",
            "updated_at": row.get("broker_updated_at"),
        },
        "upstox": {
            "api_key_saved": bool(row.get("upstox_api_key")),
            "api_secret_saved": bool(row.get("upstox_api_secret")),
            "access_token_saved": bool(row.get("upstox_access_token")),
            "redirect_uri_saved": bool(row.get("upstox_redirect_uri")),
            "connected": bool(row.get("upstox_access_token")),
            "base_url": row.get("upstox_api_base_url") or "",
            "scope": row.get("upstox_token_scope") or "",
            "updated_at": row.get("broker_updated_at"),
        },
        "kite": {
            "api_key_saved": bool(row.get("kite_api_key")),
            "access_token_saved": bool(row.get("kite_access_token")),
            "connected": bool(row.get("kite_access_token")),
            "scope": row.get("kite_token_scope") or "",
            "updated_at": row.get("broker_updated_at"),
        },
    }
    monitor_symbols = _normalize_monitor_symbols(_json_load(row.get("monitor_symbols_json")) or [])
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "role": row.get("role") or "user",
        "assigned_llm": {
            "provider": row.get("assigned_llm_provider") or "",
            "model": row.get("assigned_llm_model") or "",
        },
        "active": bool(row.get("active")),
        "signal_execution_mode": _normalize_signal_execution_mode(row.get("signal_execution_mode")),
        "credit_balance": round(float(row.get("credit_balance") or 0.0), 6),
        "daily_credit_limit": round(float(row.get("daily_credit_limit") or 0.0), 6),
        "paper_cash_by_market": paper_cash_by_market,
        "broker_accounts": broker_accounts,
        "monitor_symbols": monitor_symbols,
        "monitor_symbols_count": len(monitor_symbols),
        "monitor_scope": "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
    }


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _decode_json(value: Any) -> Any:
        try:
            return json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    @contextmanager
    def connect(self):
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists universe (
                    symbol text primary key,
                    name text not null,
                    exchange text not null default 'NSE',
                    yahoo_symbol text,
                    kite_symbol text,
                    indstocks_scrip_code text,
                    indstocks_security_id text,
                    upstox_instrument_key text,
                    nubra_symbol text,
                    nubra_ref_id integer,
                    sector text,
                    industry text,
                    base_price real not null default 100,
                    enabled integer not null default 1
                );

                create table if not exists latest_quotes (
                    symbol text primary key,
                    ts text not null,
                    price real not null,
                    open real,
                    high real,
                    low real,
                    close real,
                    volume real,
                    source text not null
                );

                create table if not exists market_ticks (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    price real not null,
                    source text not null
                );

                create table if not exists candles (
                    symbol text not null,
                    ts text not null,
                    open real not null,
                    high real not null,
                    low real not null,
                    close real not null,
                    volume real not null,
                    source text not null,
                    primary key (symbol, ts, source)
                );

                create table if not exists decisions (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    action text not null,
                    strategy text not null default 'unknown',
                    confidence real not null,
                    price real not null,
                    technical_score real not null,
                    sentiment_score real not null,
                    reason text not null,
                    details_json text not null default '{}'
                );

                create table if not exists orders (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    side text not null,
                    strategy text not null default 'unknown',
                    qty integer not null,
                    price real not null,
                    notional real not null,
                    status text not null,
                    reason text not null,
                    details_json text not null default '{}'
                );

                create table if not exists positions (
                    symbol text primary key,
                    strategy text not null default 'unknown',
                    qty integer not null,
                    avg_price real not null,
                    market_price real not null,
                    realized_pnl real not null default 0,
                    updated_at text not null,
                    details_json text not null default '{}'
                );

                create table if not exists portfolio_snapshots (
                    id integer primary key autoincrement,
                    ts text not null,
                    cash real not null,
                    invested real not null,
                    market_value real not null,
                    equity real not null,
                    realized_pnl real not null,
                    unrealized_pnl real not null
                );

                create table if not exists signal_ideas (
                    id integer primary key autoincrement,
                    first_seen_at text not null,
                    last_seen_at text not null,
                    symbol text not null,
                    strategy text not null,
                    plan_code text not null default '',
                    signal_type text not null,
                    status text not null default 'ACTIVE',
                    entry_price real not null,
                    latest_price real not null,
                    current_return_pct real not null default 0,
                    peak_return_pct real not null default 0,
                    worst_return_pct real not null default 0,
                    confidence real not null default 0,
                    combined_score real not null default 0,
                    confluence real not null default 0,
                    overall_score_pct real not null default 0,
                    overall_grade text not null default '',
                    decision_id integer,
                    latest_decision_id integer,
                    reason text not null default '',
                    details_json text not null default '{}'
                );

                create table if not exists user_idea_follows (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    idea_id integer not null,
                    mode text not null default 'TRACK',
                    status text not null default 'ACTIVE',
                    qty integer not null default 0,
                    entry_price real not null default 0,
                    latest_price real not null default 0,
                    invested_amount real not null default 0,
                    unrealized_pnl real not null default 0,
                    return_pct real not null default 0,
                    created_at text not null,
                    updated_at text not null,
                    details_json text not null default '{}'
                );

                create table if not exists strategy_plans (
                    id integer primary key autoincrement,
                    code text not null unique,
                    name text not null,
                    description text not null,
                    risk_level text not null,
                    holding_period text not null,
                    capital_rule text not null,
                    enabled integer not null default 1,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists tomorrow_plan_items (
                    id integer primary key autoincrement,
                    plan_date text not null,
                    market_region text not null,
                    prepared_at text not null,
                    section text not null,
                    section_rank integer not null default 0,
                    sort_order integer not null default 0,
                    symbol text not null,
                    action text not null,
                    trigger_price real,
                    max_entry real,
                    stop_loss real,
                    target1 real,
                    score real not null default 0,
                    confidence real not null default 0,
                    strategy text not null default '',
                    rationale text not null default '',
                    validation text not null default '',
                    details_json text not null default '{}',
                    unique(plan_date, market_region, section, symbol)
                );

                create table if not exists agent_state (
                    key text primary key,
                    value text not null
                );

                create table if not exists runtime_settings (
                    key text primary key,
                    value text not null,
                    updated_at text not null
                );

                create table if not exists users (
                    id integer primary key autoincrement,
                    username text not null unique,
                    password_hash text not null,
                    role text not null default 'user',
                    account_plan text not null default 'standard',
                    assigned_llm_provider text not null default '',
                    assigned_llm_model text not null default '',
                    active integer not null default 1,
                    credit_balance real not null default 0,
                    daily_credit_limit real not null default 0,
                    paper_cash_in real,
                    paper_cash_us real,
                    upstox_api_key text not null default '',
                    upstox_api_secret text not null default '',
                    upstox_redirect_uri text not null default '',
                    upstox_access_token text not null default '',
                    upstox_api_base_url text not null default '',
                    upstox_token_scope text not null default '',
                    indstocks_access_token text not null default '',
                    indstocks_api_base_url text not null default '',
                    kite_api_key text not null default '',
                    kite_access_token text not null default '',
                    kite_token_scope text not null default '',
                    monitor_symbols_json text not null default '[]',
                    broker_updated_at text,
                    created_at text not null,
                    updated_at text not null,
                    last_login_at text
                );

                create table if not exists user_credit_ledger (
                    id integer primary key autoincrement,
                    ts text not null,
                    user_id integer not null,
                    entry_type text not null,
                    amount real not null,
                    balance_after real not null,
                    base_cost real not null default 0,
                    platform_margin real not null default 0,
                    description text not null,
                    details_json text not null default '{}',
                    foreign key(user_id) references users(id)
                );

                create table if not exists sentiment_events (
                    id integer primary key autoincrement,
                    ts text not null,
                    symbol text not null,
                    score real not null,
                    headline_count integer not null,
                    headlines_json text not null,
                    confidence real not null default 0,
                    events_json text not null default '[]'
                );

                create table if not exists agent_logs (
                    id integer primary key autoincrement,
                    ts text not null,
                    level text not null,
                    component text not null,
                    event text not null,
                    message text not null,
                    details_json text not null default '{}'
                );

                create table if not exists llm_usage_events (
                    id integer primary key autoincrement,
                    ts text not null,
                    component text not null,
                    purpose text not null,
                    provider text not null,
                    model text not null,
                    prompt_tokens integer not null default 0,
                    completion_tokens integer not null default 0,
                    total_tokens integer not null default 0,
                    cache_hit_tokens integer not null default 0,
                    cache_miss_tokens integer not null default 0,
                    estimated_tokens integer not null default 0,
                    input_chars integer not null default 0,
                    output_chars integer not null default 0,
                    cost_usd real not null default 0,
                    latency_ms integer not null default 0,
                    user_id integer,
                    scope_id text not null default '',
                    details_json text not null default '{}'
                );

                create table if not exists delivery_data (
                    symbol text not null,
                    date text not null,
                    close real,
                    total_volume real,
                    delivery_volume real,
                    delivery_pct real,
                    primary key (symbol, date)
                );

                create table if not exists pattern_states (
                    symbol text not null,
                    pattern text not null,
                    state_json text not null default '{}',
                    updated_at text not null,
                    primary key (symbol, pattern)
                );

                create index if not exists idx_market_ticks_symbol_ts
                    on market_ticks(symbol, ts);
                create index if not exists idx_candles_symbol_ts
                    on candles(symbol, ts);
                create index if not exists idx_decisions_ts
                    on decisions(ts);
                create index if not exists idx_orders_ts
                    on orders(ts);
                create index if not exists idx_agent_logs_ts
                    on agent_logs(ts);
                create index if not exists idx_users_username
                    on users(username);
                create index if not exists idx_llm_usage_ts
                    on llm_usage_events(ts);
                create index if not exists idx_llm_usage_purpose_ts
                    on llm_usage_events(purpose, ts);
                create index if not exists idx_user_credit_ledger_user_ts
                    on user_credit_ledger(user_id, ts);
                create index if not exists idx_delivery_symbol_date
                    on delivery_data(symbol, date);
                create index if not exists idx_pattern_states_pattern
                    on pattern_states(pattern, updated_at);
                create index if not exists idx_signal_ideas_symbol_status
                    on signal_ideas(symbol, status);
                create index if not exists idx_user_idea_follows_user
                    on user_idea_follows(user_id, status);
                create index if not exists idx_tomorrow_plan_market_date
                    on tomorrow_plan_items(market_region, plan_date, sort_order);
                """
            )
            self._ensure_column(conn, "universe", "upstox_instrument_key", "text")
            self._ensure_column(conn, "universe", "indstocks_scrip_code", "text")
            self._ensure_column(conn, "universe", "indstocks_security_id", "text")
            self._ensure_column(conn, "universe", "nubra_symbol", "text")
            self._ensure_column(conn, "universe", "nubra_ref_id", "integer")
            self._ensure_column(conn, "universe", "industry", "text")
            self._ensure_column(conn, "decisions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "decisions", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "orders", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "orders", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "positions", "strategy", "text not null default 'unknown'")
            self._ensure_column(conn, "positions", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "signal_ideas", "latest_decision_id", "integer")
            self._ensure_column(conn, "signal_ideas", "plan_code", "text not null default ''")
            self._ensure_column(conn, "signal_ideas", "current_return_pct", "real not null default 0")
            self._ensure_column(conn, "signal_ideas", "peak_return_pct", "real not null default 0")
            self._ensure_column(conn, "signal_ideas", "worst_return_pct", "real not null default 0")
            self._ensure_column(conn, "signal_ideas", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "user_idea_follows", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "tomorrow_plan_items", "validation", "text not null default ''")
            self._ensure_column(conn, "tomorrow_plan_items", "details_json", "text not null default '{}'")
            self._ensure_column(conn, "sentiment_events", "confidence", "real not null default 0")
            self._ensure_column(conn, "sentiment_events", "events_json", "text not null default '[]'")
            self._ensure_column(conn, "delivery_data", "close", "real")
            self._ensure_column(conn, "delivery_data", "total_volume", "real")
            self._ensure_column(conn, "delivery_data", "delivery_volume", "real")
            self._ensure_column(conn, "delivery_data", "delivery_pct", "real")
            self._ensure_column(conn, "llm_usage_events", "cache_hit_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "cache_miss_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "estimated_tokens", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "input_chars", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "output_chars", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "cost_usd", "real not null default 0")
            self._ensure_column(conn, "llm_usage_events", "latency_ms", "integer not null default 0")
            self._ensure_column(conn, "llm_usage_events", "user_id", "integer")
            self._ensure_column(conn, "llm_usage_events", "scope_id", "text not null default ''")
            self._ensure_column(conn, "users", "paper_cash_in", "real")
            self._ensure_column(conn, "users", "paper_cash_us", "real")
            self._ensure_column(conn, "users", "signal_execution_mode", "text not null default 'SIGNAL_ONLY'")
            self._ensure_column(conn, "users", "monitor_symbols_json", "text not null default '[]'")
            conn.execute(
                """
                create index if not exists idx_llm_usage_user_ts
                    on llm_usage_events(user_id, ts)
                """
            )
            conn.execute(
                """
                create index if not exists idx_llm_usage_scope
                    on llm_usage_events(scope_id)
                """
            )
            self._ensure_column(conn, "users", "role", "text not null default 'user'")
            self._ensure_column(conn, "users", "account_plan", "text not null default 'standard'")
            self._ensure_column(conn, "users", "assigned_llm_provider", "text not null default ''")
            self._ensure_column(conn, "users", "assigned_llm_model", "text not null default ''")
            self._ensure_column(conn, "users", "active", "integer not null default 1")
            self._ensure_column(conn, "users", "credit_balance", "real not null default 0")
            self._ensure_column(conn, "users", "daily_credit_limit", "real not null default 0")
            self._ensure_column(conn, "users", "upstox_api_key", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_api_secret", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_redirect_uri", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_access_token", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_api_base_url", "text not null default ''")
            self._ensure_column(conn, "users", "upstox_token_scope", "text not null default ''")
            self._ensure_column(conn, "users", "indstocks_access_token", "text not null default ''")
            self._ensure_column(conn, "users", "indstocks_api_base_url", "text not null default ''")
            self._ensure_column(conn, "users", "kite_api_key", "text not null default ''")
            self._ensure_column(conn, "users", "kite_access_token", "text not null default ''")
            self._ensure_column(conn, "users", "kite_token_scope", "text not null default ''")
            self._ensure_column(conn, "users", "broker_updated_at", "text")
            self._seed_strategy_plans(conn)
            self._backfill_signal_plan_codes(conn)
            self._backfill_universe_metadata(conn)
            self._ensure_column(conn, "users", "created_at", "text not null default ''")
            self._ensure_column(conn, "users", "updated_at", "text not null default ''")
            self._ensure_column(conn, "users", "last_login_at", "text")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def _backfill_universe_metadata(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select symbol, name, exchange, sector, industry
            from universe
            where coalesce(sector, '') = '' or coalesce(industry, '') = ''
            """
        ).fetchall()
        for row in rows:
            normalized = self._normalize_universe_row(dict(row))
            conn.execute(
                """
                update universe
                set sector = case when coalesce(sector, '') = '' then ? else sector end,
                    industry = case when coalesce(industry, '') = '' then ? else industry end
                where symbol = ?
                """,
                (normalized["sector"], normalized["industry"], normalized["symbol"]),
            )

    def _seed_strategy_plans(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        plans = [
            (
                "aggressive_rs_breakout",
                "Aggressive RS Breakout",
                "High-momentum leaders breaking out near fresh highs with stacked moving averages, volume expansion, and strict volatility control.",
                "Aggressive",
                "2-8 sessions",
                "Use only A/B/C entries; start smaller when volatility is wide and trail quickly after Target 1.",
            ),
            (
                "institutional_quality_swing",
                "Institutional Quality Swing",
                "Stage 2 stocks with A/B/C entry, clean market breadth, no hard rule flags, and delivery accumulation or neutral bias.",
                "Medium",
                "5-20 sessions",
                "Normal allocation only when classification is FUNDAMENTAL; MOMENTUM capped at 60%.",
            ),
            (
                "confirmed_breakout",
                "Confirmed Breakout",
                "Pivot breakout with volume expansion, two-day rule intact, no climax top, and resistance/support reward above 2:1.",
                "Medium-High",
                "3-15 sessions",
                "Start half size until follow-through confirms; never buy D-grade extended entries.",
            ),
            (
                "btst_next_day",
                "BTST Next-Day Buy",
                "Buy-today-sell-tomorrow candidates with strong closing range, volume participation, trend alignment, controlled overnight gap risk, and a clear next-day exit plan.",
                "Medium-High",
                "1-2 sessions",
                "Use guarded size, enter only near the close/entry zone, and sell or trim tomorrow if first strength or first 15-minute support fails.",
            ),
            (
                "pullback_to_strength",
                "Pullback To Strength",
                "Uptrend pullback near 20DMA/50DMA support where MTF is B or better and selling pressure is fading.",
                "Medium",
                "5-25 sessions",
                "Scale only after price reclaims strength; stop below support or ATR hard stop.",
            ),
            (
                "smallcap_momentum",
                "Smallcap Momentum",
                "Low-capital ideas with strong price/volume action, strict liquidity check, and speculative sizing caps.",
                "High",
                "1-10 sessions",
                "SPECULATIVE cap is 30% of normal allocation; exit quickly on delivery distribution or failed breakout.",
            ),
            (
                "defensive_exit_manager",
                "Defensive Exit Manager",
                "Protects open positions with ATR stops, time stops, delivery conflicts, and market breadth risk-off gates.",
                "Defensive",
                "Continuous",
                "No fresh capital; trail stops, partial exits, or full exit when hard gates fail.",
            ),
        ]
        conn.executemany(
            """
            insert into strategy_plans (
                code, name, description, risk_level, holding_period, capital_rule, enabled, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, 1, ?, ?)
            on conflict(code) do update set
                name = excluded.name,
                description = excluded.description,
                risk_level = excluded.risk_level,
                holding_period = excluded.holding_period,
                capital_rule = excluded.capital_rule,
                updated_at = excluded.updated_at
            """,
            [(code, name, description, risk, hold, capital, now, now) for code, name, description, risk, hold, capital in plans],
        )

    def _backfill_signal_plan_codes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            update signal_ideas
            set plan_code = case
                when upper(signal_type) = 'EXIT' then 'defensive_exit_manager'
                when lower(strategy) like '%btst%' then 'btst_next_day'
                when lower(strategy) like '%breakout%' or lower(strategy) like '%darvas%' or lower(strategy) like '%vcp%' then 'confirmed_breakout'
                when lower(strategy) like '%pullback%' or lower(strategy) like '%ema%' or lower(strategy) like '%continuation%' then 'pullback_to_strength'
                when latest_price > 0 and latest_price <= 250 then 'smallcap_momentum'
                else 'institutional_quality_swing'
            end
            where coalesce(plan_code, '') = ''
            """
        )

    def ensure_default_admin_user(self, username: str, password_hash: str | None) -> None:
        username = (username or "admin").strip()
        if not username or not password_hash:
            return
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "select id from users where lower(username) = lower(?)",
                (username,),
            ).fetchone()
            if existing:
                return
            count = conn.execute("select count(*) as count from users").fetchone()["count"]
            if count:
                return
            conn.execute(
                """
                insert into users (username, password_hash, role, account_plan, active, created_at, updated_at)
                values (?, ?, 'admin', 'standard', 1, ?, ?)
                """,
                (username, password_hash, now, now),
            )

    def has_active_users(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1").fetchone()
        return bool(row and row["count"])

    def has_admin_user(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1 and role = 'admin'").fetchone()
        return bool(row and row["count"])

    def active_admin_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select count(*) as count from users where active = 1 and role = 'admin'").fetchone()
        return int(row["count"] or 0) if row else 0

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from users where lower(username) = lower(?)",
                ((username or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from users
                order by role = 'admin' desc, username collate nocase
                """
            ).fetchall()
        users = []
        for row in rows:
            public = _public_user(dict(row)) or {}
            public["credit_usage"] = self.user_credit_summary(int(row["id"]), include_ledger=False)
            users.append(public)
        return users

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        active: bool = True,
        assigned_llm_provider: str = "",
        assigned_llm_model: str = "",
        signal_execution_mode: str = "SIGNAL_ONLY",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into users (
                    username, password_hash, role, account_plan, assigned_llm_provider,
                    assigned_llm_model, signal_execution_mode, active, created_at, updated_at
                )
                values (?, ?, ?, 'standard', ?, ?, ?, ?, ?, ?)
                """,
                (
                    username.strip(),
                    password_hash,
                    role,
                    str(assigned_llm_provider or "").strip().lower(),
                    str(assigned_llm_model or "").strip(),
                    _normalize_signal_execution_mode(signal_execution_mode),
                    1 if active else 0,
                    now,
                    now,
                ),
            )
            user_id = int(cursor.lastrowid)
        user = self.user_by_id(user_id)
        return _public_user(user) if user else {}

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        assigned_llm_provider: str | None = None,
        assigned_llm_model: str | None = None,
        signal_execution_mode: str | None = None,
        active: bool | None = None,
        password_hash: str | None = None,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if role is not None:
            assignments.append("role = ?")
            values.append(role)
        if assigned_llm_provider is not None:
            assignments.append("assigned_llm_provider = ?")
            values.append(str(assigned_llm_provider or "").strip().lower())
        if assigned_llm_model is not None:
            assignments.append("assigned_llm_model = ?")
            values.append(str(assigned_llm_model or "").strip())
        if signal_execution_mode is not None:
            assignments.append("signal_execution_mode = ?")
            values.append(_normalize_signal_execution_mode(signal_execution_mode))
        if active is not None:
            assignments.append("active = ?")
            values.append(1 if active else 0)
        if password_hash is not None:
            assignments.append("password_hash = ?")
            values.append(password_hash)
        if not assignments:
            user = self.user_by_id(user_id)
            return _public_user(user) if user else None
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(f"update users set {', '.join(assignments)} where id = ?", values)
        user = self.user_by_id(user_id)
        return _public_user(user) if user else None

    def mark_user_login(self, user_id: int) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "update users set last_login_at = ?, updated_at = ? where id = ?",
                (now, now, user_id),
            )

    def update_user_daily_credit_limit(self, user_id: int, daily_credit_limit: float) -> dict[str, Any]:
        limit = max(float(daily_credit_limit or 0.0), 0.0)
        with self.connect() as conn:
            conn.execute(
                "update users set daily_credit_limit = ?, updated_at = ? where id = ?",
                (limit, utc_now(), user_id),
            )
        return self.user_credit_summary(user_id)

    def update_user_signal_execution_mode(self, user_id: int, mode: str) -> dict[str, Any] | None:
        normalized = _normalize_signal_execution_mode(mode)
        with self.connect() as conn:
            conn.execute(
                "update users set signal_execution_mode = ?, updated_at = ? where id = ?",
                (normalized, utc_now(), user_id),
            )
        user = self.user_by_id(user_id)
        return _public_user(user) if user else None

    def normalize_monitor_symbols(self, value: Any) -> list[str]:
        return _normalize_monitor_symbols(value)

    def user_monitor_symbols(self, user_id: int) -> list[str]:
        user = self.user_by_id(user_id)
        if not user:
            return []
        return _normalize_monitor_symbols(_json_load(user.get("monitor_symbols_json")) or [])

    def update_user_monitor_symbols(self, user_id: int, symbols: Any) -> dict[str, Any] | None:
        normalized = _normalize_monitor_symbols(symbols)
        with self.connect() as conn:
            conn.execute(
                "update users set monitor_symbols_json = ?, updated_at = ? where id = ?",
                (json.dumps(normalized), utc_now(), user_id),
            )
        user = self.user_by_id(user_id)
        return _public_user(user) if user else None

    def update_user_paper_cash(self, user_id: int, cash_in: float | None = None, cash_us: float | None = None) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if cash_in is not None:
            assignments.append("paper_cash_in = ?")
            values.append(round(max(float(cash_in), 0.0), 6))
        if cash_us is not None:
            assignments.append("paper_cash_us = ?")
            values.append(round(max(float(cash_us), 0.0), 6))
        if not assignments:
            user = self.user_by_id(user_id)
            return _public_user(user) if user else None
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(f"update users set {', '.join(assignments)} where id = ?", values)
        user = self.user_by_id(user_id)
        return _public_user(user) if user else None

    def adjust_user_credits(
        self,
        user_id: int,
        amount: float,
        description: str,
        details: Any | None = None,
        entry_type: str = "allocation",
    ) -> dict[str, Any]:
        delta = float(amount or 0.0)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("select credit_balance from users where id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("user not found")
            balance = max(float(row["credit_balance"] or 0.0) + delta, 0.0)
            conn.execute(
                "update users set credit_balance = ?, updated_at = ? where id = ?",
                (balance, now, user_id),
            )
            conn.execute(
                """
                insert into user_credit_ledger
                    (ts, user_id, entry_type, amount, balance_after, base_cost, platform_margin, description, details_json)
                values (?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (now, user_id, entry_type, delta, balance, description, json.dumps(details or {}, default=str, separators=(",", ":"))),
            )
        return self.user_credit_summary(user_id)

    def charge_user_credits(
        self,
        user_id: int,
        base_cost: float,
        description: str,
        details: Any | None = None,
        *,
        margin_pct: float = 0.20,
        minimum_charge: float = 0.01,
    ) -> dict[str, Any]:
        base = max(float(base_cost or 0.0), 0.0)
        charge = max(base * (1.0 + max(float(margin_pct or 0.0), 0.0)), float(minimum_charge or 0.0))
        platform_margin = max(charge - base, 0.0)
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("select credit_balance from users where id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("user not found")
            balance = float(row["credit_balance"] or 0.0)
            if balance + 1e-9 < charge:
                raise ValueError("insufficient user credits")
            balance_after = balance - charge
            conn.execute(
                "update users set credit_balance = ?, updated_at = ? where id = ?",
                (balance_after, now, user_id),
            )
            conn.execute(
                """
                insert into user_credit_ledger
                    (ts, user_id, entry_type, amount, balance_after, base_cost, platform_margin, description, details_json)
                values (?, ?, 'usage', ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    user_id,
                    -charge,
                    balance_after,
                    base,
                    platform_margin,
                    description,
                    json.dumps(details or {}, default=str, separators=(",", ":")),
                ),
            )
        return self.user_credit_summary(user_id)

    def user_credit_summary(self, user_id: int, include_ledger: bool = True, ledger_limit: int = 80) -> dict[str, Any]:
        today = utc_now()[:10]
        with self.connect() as conn:
            user = conn.execute(
                "select id, username, credit_balance, daily_credit_limit from users where id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return {}
            usage_today = conn.execute(
                """
                select
                    coalesce(sum(case when amount < 0 then -amount else 0 end), 0) as credits_used,
                    coalesce(sum(base_cost), 0) as base_cost,
                    coalesce(sum(platform_margin), 0) as platform_margin,
                    count(*) as entries
                from user_credit_ledger
                where user_id = ? and substr(ts, 1, 10) = ?
                """,
                (user_id, today),
            ).fetchone()
            usage_all = conn.execute(
                """
                select
                    coalesce(sum(case when amount < 0 then -amount else 0 end), 0) as credits_used,
                    coalesce(sum(base_cost), 0) as base_cost,
                    coalesce(sum(platform_margin), 0) as platform_margin,
                    count(*) as entries
                from user_credit_ledger
                where user_id = ?
                """,
                (user_id,),
            ).fetchone()
            ledger_rows = []
            if include_ledger:
                ledger_rows = conn.execute(
                    """
                    select id, ts, entry_type, amount, balance_after, description, details_json
                    from user_credit_ledger
                    where user_id = ?
                    order by id desc
                    limit ?
                    """,
                    (user_id, ledger_limit),
                ).fetchall()
        balance = float(user["credit_balance"] or 0.0)
        daily_limit = float(user["daily_credit_limit"] or 0.0)
        used_today = float(usage_today["credits_used"] or 0.0)
        daily_remaining = max(daily_limit - used_today, 0.0) if daily_limit > 0 else balance
        return {
            "user_id": int(user_id),
            "username": user["username"],
            "credit_balance": round(balance, 6),
            "daily_credit_limit": round(daily_limit, 6),
            "credits_used_today": round(used_today, 6),
            "daily_credits_remaining": round(min(balance, daily_remaining), 6),
            "today": {
                "credits_used": round(used_today, 6),
                "base_cost": round(float(usage_today["base_cost"] or 0.0), 8),
                "platform_margin": round(float(usage_today["platform_margin"] or 0.0), 8),
                "entries": int(usage_today["entries"] or 0),
            },
            "all_time": {
                "credits_used": round(float(usage_all["credits_used"] or 0.0), 6),
                "base_cost": round(float(usage_all["base_cost"] or 0.0), 8),
                "platform_margin": round(float(usage_all["platform_margin"] or 0.0), 8),
                "entries": int(usage_all["entries"] or 0),
            },
            "ledger": [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "entry_type": row["entry_type"],
                    "amount": round(float(row["amount"] or 0.0), 6),
                    "balance_after": round(float(row["balance_after"] or 0.0), 6),
                    "description": row["description"],
                    "details": self._decode_json(row["details_json"]),
                }
                for row in ledger_rows
            ],
        }

    def admin_credit_usage_summary(self) -> dict[str, Any]:
        today = utc_now()[:10]
        with self.connect() as conn:
            rows = conn.execute(
                """
                select u.id, u.username, u.role, u.active,
                    u.credit_balance, u.daily_credit_limit,
                    coalesce(sum(case when l.amount < 0 and substr(l.ts, 1, 10) = ? then -l.amount else 0 end), 0) as today_credits,
                    coalesce(sum(case when substr(l.ts, 1, 10) = ? then l.base_cost else 0 end), 0) as today_base,
                    coalesce(sum(case when substr(l.ts, 1, 10) = ? then l.platform_margin else 0 end), 0) as today_margin,
                    coalesce(sum(case when l.amount < 0 then -l.amount else 0 end), 0) as all_credits,
                    coalesce(sum(l.base_cost), 0) as all_base,
                    coalesce(sum(l.platform_margin), 0) as all_margin
                from users u
                left join user_credit_ledger l on l.user_id = u.id
                group by u.id
                order by today_credits desc, all_credits desc, u.username collate nocase
                """,
                (today, today, today),
            ).fetchall()
        return {
            "updated_at": utc_now(),
            "today_utc": today,
            "margin_policy": "Admin view includes OpenStocks platform margin.",
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                    "active": bool(row["active"]),
                    "credit_balance": round(float(row["credit_balance"] or 0.0), 6),
                    "daily_credit_limit": round(float(row["daily_credit_limit"] or 0.0), 6),
                    "today_credits_used": round(float(row["today_credits"] or 0.0), 6),
                    "today_base_cost": round(float(row["today_base"] or 0.0), 8),
                    "today_platform_margin": round(float(row["today_margin"] or 0.0), 8),
                    "all_time_credits_used": round(float(row["all_credits"] or 0.0), 6),
                    "all_time_base_cost": round(float(row["all_base"] or 0.0), 8),
                    "all_time_platform_margin": round(float(row["all_margin"] or 0.0), 8),
                }
                for row in rows
            ],
        }

    def user_has_credit_for(self, user_id: int, estimated_charge: float) -> tuple[bool, dict[str, Any]]:
        summary = self.user_credit_summary(user_id, include_ledger=False)
        charge = max(float(estimated_charge or 0.0), 0.0)
        ok = (
            summary.get("credit_balance", 0.0) + 1e-9 >= charge
            and summary.get("daily_credits_remaining", 0.0) + 1e-9 >= charge
        )
        return ok, summary

    def average_signal_credit_charge(
        self,
        fallback: float | None = None,
        *,
        tokens_per_credit: float = DEFAULT_TOKENS_PER_CREDIT,
    ) -> float:
        rate = max(float(tokens_per_credit or DEFAULT_TOKENS_PER_CREDIT), 1.0)
        fallback_value = float(fallback) if fallback is not None else DEFAULT_SIGNAL_TOKEN_ESTIMATE / rate
        with self.connect() as conn:
            ledger_row = conn.execute(
                """
                select avg(charge) as avg_charge
                from (
                    select -amount as charge
                    from user_credit_ledger
                    where entry_type = 'usage' and amount < 0
                    order by id desc
                    limit 50
                )
                """
            ).fetchone()
            token_row = conn.execute(
                """
                select avg(total_tokens) as avg_tokens
                from (
                    select total_tokens
                    from llm_usage_events
                    where user_id is not null and total_tokens > 0
                    order by id desc
                    limit 50
                )
                """
            ).fetchone()
        ledger_value = ledger_row["avg_charge"] if ledger_row else None
        token_value = token_row["avg_tokens"] if token_row else None
        values: list[float] = []
        try:
            if ledger_value is not None and float(ledger_value) > fallback_value * 0.25:
                values.append(float(ledger_value))
        except (TypeError, ValueError):
            pass
        try:
            if token_value is not None:
                values.append(max(float(token_value), 0.0) / rate)
        except (TypeError, ValueError):
            pass
        try:
            return round(max(values or [fallback_value]), 6)
        except (TypeError, ValueError):
            return round(fallback_value, 6)

    def latest_llm_usage_id(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select coalesce(max(id), 0) as id from llm_usage_events").fetchone()
        return int(row["id"] or 0) if row else 0

    def llm_usage_cost_since(self, user_id: int, after_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as calls,
                    coalesce(sum(cost_usd), 0) as cost_usd,
                    coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(input_chars), 0) as input_chars,
                    coalesce(sum(output_chars), 0) as output_chars
                from llm_usage_events
                where id > ? and user_id = ?
                """,
                (after_id, user_id),
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 8),
            "total_tokens": int(row["total_tokens"] or 0),
            "input_chars": int(row["input_chars"] or 0),
            "output_chars": int(row["output_chars"] or 0),
        }

    def llm_usage_cost_for_scope(self, user_id: int, scope_id: str, after_id: int = 0) -> dict[str, Any]:
        if not scope_id:
            return self.llm_usage_cost_since(user_id, after_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as calls,
                    coalesce(sum(cost_usd), 0) as cost_usd,
                    coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(input_chars), 0) as input_chars,
                    coalesce(sum(output_chars), 0) as output_chars
                from llm_usage_events
                where id > ? and user_id = ? and scope_id = ?
                """,
                (after_id, user_id, scope_id),
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 8),
            "total_tokens": int(row["total_tokens"] or 0),
            "input_chars": int(row["input_chars"] or 0),
            "output_chars": int(row["output_chars"] or 0),
        }

    def llm_usage_cost_for_system_scope(self, scope_id: str, after_id: int = 0) -> dict[str, Any]:
        with self.connect() as conn:
            if scope_id:
                row = conn.execute(
                    """
                    select count(*) as calls,
                        coalesce(sum(cost_usd), 0) as cost_usd,
                        coalesce(sum(total_tokens), 0) as total_tokens,
                        coalesce(sum(input_chars), 0) as input_chars,
                        coalesce(sum(output_chars), 0) as output_chars
                    from llm_usage_events
                    where id > ? and user_id is null and scope_id = ?
                    """,
                    (after_id, scope_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    select count(*) as calls,
                        coalesce(sum(cost_usd), 0) as cost_usd,
                        coalesce(sum(total_tokens), 0) as total_tokens,
                        coalesce(sum(input_chars), 0) as input_chars,
                        coalesce(sum(output_chars), 0) as output_chars
                    from llm_usage_events
                    where id > ? and user_id is null
                    """,
                    (after_id,),
                ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 8),
            "total_tokens": int(row["total_tokens"] or 0),
            "input_chars": int(row["input_chars"] or 0),
            "output_chars": int(row["output_chars"] or 0),
        }

    def update_user_broker(self, user_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "indstocks_access_token",
            "indstocks_api_base_url",
            "upstox_api_key",
            "upstox_api_secret",
            "upstox_redirect_uri",
            "upstox_access_token",
            "upstox_api_base_url",
            "upstox_token_scope",
            "kite_api_key",
            "kite_access_token",
            "kite_token_scope",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in values:
                assignments.append(f"{key} = ?")
                params.append(str(values.get(key) or "").strip())
        if not assignments:
            return _public_user(self.user_by_id(user_id))
        assignments.extend(["broker_updated_at = ?", "updated_at = ?"])
        now = utc_now()
        params.extend([now, now, user_id])
        with self.connect() as conn:
            conn.execute(f"update users set {', '.join(assignments)} where id = ?", params)
        return _public_user(self.user_by_id(user_id))

    def assign_runtime_upstox_to_user(self, user_id: int, runtime_settings: dict[str, Any]) -> dict[str, Any] | None:
        return self.update_user_broker(
            user_id,
            {
                "upstox_api_key": runtime_settings.get("upstox_api_key", ""),
                "upstox_api_secret": runtime_settings.get("upstox_api_secret", ""),
                "upstox_redirect_uri": runtime_settings.get("upstox_redirect_uri", ""),
                "upstox_access_token": runtime_settings.get("upstox_access_token", ""),
                "upstox_api_base_url": runtime_settings.get("upstox_api_base_url", ""),
                "upstox_token_scope": runtime_settings.get("upstox_token_scope", "shared_analytics"),
            },
        )

    def assign_runtime_indstocks_to_user(self, user_id: int, runtime_settings: dict[str, Any]) -> dict[str, Any] | None:
        return self.update_user_broker(
            user_id,
            {
                "indstocks_access_token": runtime_settings.get("indstocks_access_token", ""),
                "indstocks_api_base_url": runtime_settings.get("indstocks_api_base_url", ""),
            },
        )

    def seed_universe(self, csv_path: Path, disable_missing: bool = True) -> None:
        if not csv_path.exists():
            raise FileNotFoundError(f"Universe CSV not found: {csv_path}")
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            rows = [self._normalize_universe_row(row) for row in csv.DictReader(handle)]
        self.upsert_universe_rows(rows, disable_missing=disable_missing)

    def upsert_universe_rows(self, rows: Iterable[dict[str, Any]], disable_missing: bool = False) -> int:
        normalized = [self._normalize_universe_row(row) for row in rows]
        if not normalized:
            return 0
        symbols = [row["symbol"] for row in normalized]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into universe (
                    symbol, name, exchange, yahoo_symbol, kite_symbol,
                    indstocks_scrip_code, indstocks_security_id, upstox_instrument_key,
                    nubra_symbol, nubra_ref_id,
                    sector, industry, base_price, enabled
                ) values (
                    :symbol, :name, :exchange, :yahoo_symbol, :kite_symbol,
                    :indstocks_scrip_code, :indstocks_security_id, :upstox_instrument_key,
                    :nubra_symbol, :nubra_ref_id,
                    :sector, :industry, :base_price, :enabled
                )
                on conflict(symbol) do update set
                    name = excluded.name,
                    exchange = excluded.exchange,
                    yahoo_symbol = excluded.yahoo_symbol,
                    kite_symbol = excluded.kite_symbol,
                    indstocks_scrip_code = case
                        when excluded.indstocks_scrip_code != '' then excluded.indstocks_scrip_code
                        else universe.indstocks_scrip_code
                    end,
                    indstocks_security_id = case
                        when excluded.indstocks_security_id != '' then excluded.indstocks_security_id
                        else universe.indstocks_security_id
                    end,
                    upstox_instrument_key = case
                        when excluded.upstox_instrument_key != '' then excluded.upstox_instrument_key
                        else universe.upstox_instrument_key
                    end,
                    nubra_symbol = excluded.nubra_symbol,
                    nubra_ref_id = excluded.nubra_ref_id,
                    sector = case when excluded.sector != '' then excluded.sector else universe.sector end,
                    industry = case when excluded.industry != '' then excluded.industry else universe.industry end,
                    base_price = case when excluded.base_price != 100 then excluded.base_price else universe.base_price end,
                    enabled = excluded.enabled
                """,
                normalized,
            )
            if disable_missing and symbols:
                placeholders = ",".join("?" for _ in symbols)
                conn.execute(f"update universe set enabled = 0 where symbol not in ({placeholders})", symbols)
        return len(normalized)

    def _normalize_universe_row(self, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get("symbol", "")).strip().upper()
        exchange = str(row.get("exchange") or "NSE").strip().upper() or "NSE"
        default_yahoo_symbol = f"{symbol}.NS" if exchange == "NSE" else f"{symbol}.BO" if exchange == "BSE" else symbol
        industry = row.get("industry") or (NSE_INDUSTRY_FALLBACKS.get(symbol, "") if exchange in INDIA_EXCHANGES else "")
        sector = row.get("sector") or _sector_from_industry(industry)
        if not sector and exchange in INDIA_EXCHANGES:
            sector = "NSE Listed Equity"
            industry = industry or "NSE Listed Equity"
        return {
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "exchange": exchange,
            "yahoo_symbol": row.get("yahoo_symbol") or default_yahoo_symbol,
            "kite_symbol": row.get("kite_symbol") or f"{exchange}:{symbol}",
            "indstocks_scrip_code": row.get("indstocks_scrip_code") or row.get("scrip_code") or row.get("scrip-code") or "",
            "indstocks_security_id": row.get("indstocks_security_id") or row.get("security_id") or "",
            "upstox_instrument_key": row.get("upstox_instrument_key") or "",
            "nubra_symbol": row.get("nubra_symbol") or symbol,
            "nubra_ref_id": _optional_int(row.get("nubra_ref_id")),
            "sector": sector,
            "industry": industry or sector,
            "base_price": _optional_float(row.get("base_price")) or 100,
            "enabled": int(float(row.get("enabled"))) if row.get("enabled") not in (None, "") else 1,
        }

    def upsert_candles(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        rows = [candle.to_dict() for candles in candles_by_symbol.values() for candle in candles]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into candles (symbol, ts, open, high, low, close, volume, source)
                values (:symbol, :ts, :open, :high, :low, :close, :volume, :source)
                on conflict(symbol, ts, source) do update set
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )

    def upsert_delivery_data(self, rows: Iterable[dict[str, Any]]) -> None:
        normalized = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            date = str(row.get("date") or "").strip()
            if not symbol or not date:
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "close": _optional_float(row.get("close")),
                    "total_volume": _optional_float(row.get("total_volume")),
                    "delivery_volume": _optional_float(row.get("delivery_volume")),
                    "delivery_pct": _optional_float(row.get("delivery_pct")),
                }
            )
        if not normalized:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into delivery_data (symbol, date, close, total_volume, delivery_volume, delivery_pct)
                values (:symbol, :date, :close, :total_volume, :delivery_volume, :delivery_pct)
                on conflict(symbol, date) do update set
                    close = excluded.close,
                    total_volume = excluded.total_volume,
                    delivery_volume = excluded.delivery_volume,
                    delivery_pct = excluded.delivery_pct
                """,
                normalized,
            )

    def delivery_rows(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from delivery_data
                where symbol = ?
                order by date desc
                limit ?
                """,
                (symbol.upper(), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def candles_for_symbols(
        self,
        symbols: list[str],
        limit_per_symbol: int = 80,
        source: str | None = None,
        source_like: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not symbols:
            return {}
        output: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            for symbol in symbols:
                where = "where symbol = ?"
                params: list[Any] = [symbol]
                if source is not None:
                    where += " and source = ?"
                    params.append(source)
                elif source_like is not None:
                    where += " and source like ?"
                    params.append(source_like)
                params.append(limit_per_symbol)
                rows = conn.execute(
                    f"""
                    select * from candles
                    {where}
                    order by ts desc
                    limit ?
                    """,
                    params,
                ).fetchall()
                output[symbol] = [dict(row) for row in reversed(rows)]
        return output

    def recent_candles_by_symbol(
        self,
        symbols: list[str],
        limit_per_symbol: int = 96,
        source: str | None = None,
        source_like: str | None = None,
    ) -> dict[str, list[Candle]]:
        raw = self.candles_for_symbols(
            symbols,
            limit_per_symbol=limit_per_symbol,
            source=source,
            source_like=source_like,
        )
        return self._candle_rows_to_models(raw)

    def candle_coverage_by_symbol(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        normalized_symbols = []
        seen: set[str] = set()
        for symbol in symbols:
            normalized = str(symbol or "").strip().upper()
            if normalized and normalized not in seen:
                normalized_symbols.append(normalized)
                seen.add(normalized)
        output: dict[str, dict[str, Any]] = {symbol: _empty_candle_coverage() for symbol in normalized_symbols}
        if not normalized_symbols:
            return output
        with self.connect() as conn:
            for chunk in _chunks(normalized_symbols, 500):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    select symbol, source, count(*) as candle_count, max(ts) as latest_ts
                    from candles
                    where symbol in ({placeholders})
                    group by symbol, source
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    symbol = str(row["symbol"] or "").upper()
                    source = str(row["source"] or "")
                    bucket = _candle_source_bucket(source)
                    if not bucket:
                        continue
                    coverage = output.setdefault(symbol, _empty_candle_coverage())
                    source_count = int(row["candle_count"] or 0)
                    latest_ts = row["latest_ts"]
                    bucket_payload = coverage[bucket]
                    bucket_payload["count"] += source_count
                    bucket_payload["latest_ts"] = _latest_ts(bucket_payload.get("latest_ts"), latest_ts)
                    source_payload = coverage["sources"].setdefault(source, {"count": 0, "latest_ts": None})
                    source_payload["count"] += source_count
                    source_payload["latest_ts"] = _latest_ts(source_payload.get("latest_ts"), latest_ts)
        for coverage in output.values():
            daily = coverage["daily"]
            intraday = coverage["intraday"]
            analysis = coverage["analysis"]
            best = daily if int(daily.get("count") or 0) >= int(intraday.get("count") or 0) else intraday
            analysis["count"] = int(best.get("count") or 0)
            analysis["latest_ts"] = best.get("latest_ts")
        return output

    def recent_candle_sets_by_symbol(self, symbols: list[str]) -> dict[str, dict[str, list[Candle]]]:
        if not symbols:
            return {}
        intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=120, source_like="upstox-live:%minute")
        alpaca_intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=240, source_like="alpaca%live:%minute")
        polygon_intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=240, source_like="polygon-live:%minute")
        legacy_intraday = self.recent_candles_by_symbol(symbols, limit_per_symbol=120, source="upstox-live")
        daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=260, source="upstox-live:day")
        alpaca_daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=320, source_like="alpaca%live:day")
        polygon_daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=320, source="polygon-live:day")
        indstocks_daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=320, source_like="indstocks-live:%day")
        yahoo_daily = self.recent_candles_by_symbol(symbols, limit_per_symbol=320, source="yahoo-delayed")
        weekly = self.recent_candles_by_symbol(symbols, limit_per_symbol=160, source="upstox-live:week")
        indstocks_weekly = self.recent_candles_by_symbol(symbols, limit_per_symbol=160, source_like="indstocks-live:%week")
        output: dict[str, dict[str, list[Candle]]] = {}
        for symbol in symbols:
            intraday_candles = intraday.get(symbol) or alpaca_intraday.get(symbol) or polygon_intraday.get(symbol) or legacy_intraday.get(symbol) or []
            daily_candles = (
                daily.get(symbol)
                or alpaca_daily.get(symbol)
                or polygon_daily.get(symbol)
                or indstocks_daily.get(symbol)
                or yahoo_daily.get(symbol)
                or self._resample_daily(intraday_candles)
            )
            weekly_candles = weekly.get(symbol) or indstocks_weekly.get(symbol) or self._resample_weekly(daily_candles)
            analysis_candles = daily_candles or intraday_candles
            output[symbol] = {
                "intraday": intraday_candles,
                "daily": daily_candles,
                "weekly": weekly_candles,
                "analysis": analysis_candles,
            }
        return output

    def _candle_rows_to_models(self, raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        for symbol, rows in raw.items():
            candles: list[Candle] = []
            for row in rows:
                try:
                    candles.append(
                        Candle(
                            symbol=str(row["symbol"]),
                            ts=str(row["ts"]),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"] or 0),
                            source=str(row["source"]),
                        )
                    )
                except (TypeError, ValueError, KeyError):
                    continue
            if candles:
                output[symbol] = candles
        return output

    def _resample_daily(self, candles: list[Candle]) -> list[Candle]:
        return self._resample_candles(candles, "day")

    def _resample_weekly(self, candles: list[Candle]) -> list[Candle]:
        return self._resample_candles(candles, "week")

    def _resample_candles(self, candles: list[Candle], timeframe: str) -> list[Candle]:
        grouped: dict[str, list[Candle]] = {}
        for candle in candles:
            key = self._candle_bucket(candle.ts, timeframe)
            if not key:
                continue
            grouped.setdefault(key, []).append(candle)
        output: list[Candle] = []
        for key in sorted(grouped):
            bucket = sorted(grouped[key], key=lambda item: item.ts)
            first = bucket[0]
            last = bucket[-1]
            output.append(
                Candle(
                    symbol=first.symbol,
                    ts=key,
                    open=first.open,
                    high=max(candle.high for candle in bucket),
                    low=min(candle.low for candle in bucket),
                    close=last.close,
                    volume=sum(float(candle.volume or 0) for candle in bucket),
                    source=f"{first.source}:resampled_{timeframe}",
                )
            )
        return output

    def _candle_bucket(self, ts: str, timeframe: str) -> str | None:
        value = str(ts or "")
        if not value:
            return None
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timeframe == "week":
                year, week, _ = parsed.isocalendar()
                return f"{year}-W{week:02d}"
            return parsed.date().isoformat()
        except ValueError:
            return value[:10] if timeframe == "day" else None

    def get_universe(self, enabled_only: bool = True, market_region: str | None = None) -> list[dict[str, Any]]:
        sql = "select * from universe"
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        region = normalize_market_region(market_region or "BOTH", default="BOTH")
        if region == "IN":
            placeholders = ",".join("?" for _ in INDIA_EXCHANGES)
            clauses.append(f"upper(exchange) in ({placeholders})")
            params.extend(sorted(INDIA_EXCHANGES))
        elif region == "US":
            placeholders = ",".join("?" for _ in INDIA_EXCHANGES)
            clauses.append(f"upper(exchange) not in ({placeholders})")
            params.extend(sorted(INDIA_EXCHANGES))
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by symbol"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def universe_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("select count(*) from universe").fetchone()[0]
            enabled = conn.execute("select count(*) from universe where enabled = 1").fetchone()[0]
            india_enabled = conn.execute(
                "select count(*) from universe where enabled = 1 and upper(exchange) in ('NSE','BSE')"
            ).fetchone()[0]
            us_enabled = conn.execute(
                "select count(*) from universe where enabled = 1 and upper(exchange) not in ('NSE','BSE')"
            ).fetchone()[0]
            priced = conn.execute(
                """
                select count(*)
                from universe u
                join latest_quotes q on q.symbol = u.symbol
                where u.enabled = 1
                """
            ).fetchone()[0]
            priced_low = conn.execute(
                """
                select count(*)
                from universe u
                join latest_quotes q on q.symbol = u.symbol
                where u.enabled = 1 and q.price <= 100
                """
            ).fetchone()[0]
            sectors = conn.execute(
                """
                select coalesce(nullif(sector, ''), 'Unknown') as sector, count(*) as count
                from universe
                where enabled = 1
                group by coalesce(nullif(sector, ''), 'Unknown')
                order by count desc, sector
                limit 12
                """
            ).fetchall()
        return {
            "total": total,
            "enabled": enabled,
            "india_enabled": india_enabled,
            "us_enabled": us_enabled,
            "priced_symbols": priced,
            "low_price_enabled": priced_low,
            "top_sectors": [dict(row) for row in sectors],
        }

    def universe_row(self, symbol: str, market_region: str | None = None) -> dict[str, Any] | None:
        region = normalize_market_region(market_region or "BOTH", default="BOTH")
        clauses = ["symbol = ?"]
        params: list[Any] = [symbol]
        if region == "IN":
            clauses.append("upper(exchange) in ('NSE','BSE')")
        elif region == "US":
            clauses.append("upper(exchange) not in ('NSE','BSE')")
        with self.connect() as conn:
            row = conn.execute(f"select * from universe where {' and '.join(clauses)}", params).fetchone()
        return dict(row) if row else None

    def active_position_universe(self, market_region: str | None = None) -> list[dict[str, Any]]:
        market_clause, market_params = _market_region_where("u", market_region)
        market_sql = f"and {market_clause}" if market_clause else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select distinct u.*
                from universe u
                where u.enabled = 1
                  and (
                    exists (
                        select 1
                        from signal_ideas i
                        join user_idea_follows f on f.idea_id = i.id
                        where i.symbol = u.symbol
                          and f.status in ('ACTIVE','LIVE_REQUESTED','LIVE_EXIT_REQUESTED')
                          and coalesce(f.qty, 0) > 0
                    )
                    or exists (
                        select 1
                        from positions p
                        where p.symbol = u.symbol
                          and coalesce(p.qty, 0) > 0
                    )
                  )
                  {market_sql}
                order by u.symbol
                """,
                market_params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_quotes(self, quotes: dict[str, Quote]) -> None:
        rows = [quote.to_dict() for quote in quotes.values()]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                insert into latest_quotes (
                    symbol, ts, price, open, high, low, close, volume, source
                ) values (
                    :symbol, :asof, :price, :open, :high, :low, :close, :volume, :source
                )
                on conflict(symbol) do update set
                    ts = excluded.ts,
                    price = excluded.price,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source
                """,
                rows,
            )
            conn.executemany(
                """
                insert into market_ticks (ts, symbol, price, source)
                values (:asof, :symbol, :price, :source)
                """,
                rows,
            )

    def insert_decisions(self, decisions: Iterable[Decision]) -> None:
        rows = [decision.to_dict() for decision in decisions]
        if not rows:
            return
        for row in rows:
            raw_details = row.get("details_json") or "{}"
            if str(row.get("action") or "").upper() == "HOLD" and len(raw_details) > 5000:
                row["details_json"] = _compact_decision_details(row, raw_details)
        with self.connect() as conn:
            conn.executemany(
                """
                insert into decisions (
                    ts, symbol, action, strategy, confidence, price, technical_score,
                    sentiment_score, reason, details_json
                ) values (
                    :asof, :symbol, :action, :strategy, :confidence, :price,
                    :technical_score, :sentiment_score, :reason, :details_json
                )
                """,
                rows,
            )

    def suppress_repeated_buy_decisions(
        self,
        decisions: Iterable[Decision],
        cooldown_hours: int = DUPLICATE_BUY_COOLDOWN_HOURS,
    ) -> list[Decision]:
        """Convert repeated active BUY decisions into HOLD/monitor decisions.

        This keeps the decision feed from publishing the same BUY every cycle
        while preserving the existing active signal idea as position monitoring.
        """

        items = list(decisions)
        if not items:
            return []
        cooldown = max(int(cooldown_hours or DUPLICATE_BUY_COOLDOWN_HOURS), 1)
        now_dt = datetime.now(timezone.utc)
        with self.connect() as conn:
            active_rows = conn.execute(
                """
                select symbol, strategy, status, first_seen_at, last_seen_at, reason
                from signal_ideas
                where signal_type = 'BUY'
                  and status in ('ACTIVE','MONITORING','TARGET_1_HIT','TARGET_2_HIT')
                order by last_seen_at desc, id desc
                """
            ).fetchall()
        active_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        active_by_symbol: dict[str, dict[str, Any]] = {}
        for row in active_rows:
            item = _row_dict(row)
            symbol = str(item.get("symbol") or "").upper()
            strategy = str(item.get("strategy") or "")
            if not symbol:
                continue
            active_by_symbol.setdefault(symbol, item)
            active_by_key.setdefault((symbol, strategy), item)

        output: list[Decision] = []
        seen_buy_keys: set[tuple[str, str]] = set()
        seen_buy_symbols: set[str] = set()
        for decision in items:
            if str(decision.action or "").upper() != "BUY":
                output.append(decision)
                continue
            symbol = str(decision.symbol or "").upper()
            strategy = str(decision.strategy or "")
            key = (symbol, strategy)
            active = active_by_key.get(key) or active_by_symbol.get(symbol)
            duplicate_in_batch = key in seen_buy_keys or symbol in seen_buy_symbols
            if not active:
                if not duplicate_in_batch:
                    seen_buy_keys.add(key)
                    seen_buy_symbols.add(symbol)
                    output.append(decision)
                    continue
                active = {"status": "CURRENT_BATCH", "strategy": strategy, "last_seen_at": None}
            last_seen = _parse_dt(active.get("last_seen_at")) or _parse_dt(active.get("first_seen_at"))
            within_cooldown = True
            minutes_left = None
            if last_seen:
                cooldown_until = last_seen + timedelta(hours=cooldown)
                within_cooldown = cooldown_until > now_dt
                minutes_left = max(int((cooldown_until - now_dt).total_seconds() // 60), 0)
            if not within_cooldown:
                output.append(decision)
                continue

            audit = self._decode_json(decision.details_json)
            audit["final_action"] = "HOLD"
            audit["action_reason"] = "Already active; repeated BUY is position monitoring, not a new entry."
            audit["duplicate_buy_suppression"] = {
                "suppressed": True,
                "reason": "already_active_buy_cooldown",
                "cooldown_hours": cooldown,
                "cooldown_minutes_left": minutes_left,
                "active_status": active.get("status"),
                "active_strategy": active.get("strategy"),
                "active_last_seen_at": active.get("last_seen_at"),
            }
            context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
            context["signal_continuity"] = {
                "duplicate_active_buy": True,
                "already_active_buy": True,
                "reason": "Already active. Repeated BUY is treated as monitor/no fresh add.",
                "cooldown_hours": cooldown,
                "cooldown_minutes_left": minutes_left,
            }
            audit["context"] = context
            output.append(
                replace(
                    decision,
                    action="HOLD",
                    confidence=min(float(decision.confidence or 0.0), 0.5),
                    reason="Already active; repeated BUY is position monitoring, not a new entry.",
                    details_json=json.dumps(audit, default=str, separators=(",", ":")),
                )
            )
        return output

    def upsert_signal_ideas_from_decisions(self, decisions: Iterable[Decision]) -> None:
        rows = [decision.to_dict() for decision in decisions]
        if not rows:
            return
        now = utc_now()
        with self.connect() as conn:
            for row in rows:
                idea = _signal_idea_from_decision(row)
                if idea is None:
                    continue
                latest_decision = conn.execute(
                    """
                    select id from decisions
                    where symbol = ? and strategy = ? and action = ?
                    order by id desc
                    limit 1
                    """,
                    (idea["symbol"], idea["strategy"], row.get("action")),
                ).fetchone()
                latest_decision_id = int(latest_decision["id"]) if latest_decision else None
                existing = conn.execute(
                    """
                    select * from signal_ideas
                    where symbol = ? and strategy = ? and status in ('ACTIVE','WATCH','MONITORING')
                    order by id desc
                    limit 1
                    """,
                    (idea["symbol"], idea["strategy"]),
                ).fetchone()
                latest_price = float(idea["latest_price"])
                if existing:
                    entry_price = float(existing["entry_price"] or latest_price or 0.0)
                    current_return = _return_pct(entry_price, latest_price)
                    peak_return = max(float(existing["peak_return_pct"] or 0.0), current_return)
                    worst_return = min(float(existing["worst_return_pct"] or 0.0), current_return)
                    status = idea["status"]
                    if row.get("action") == "SELL":
                        status = "EXIT_SIGNAL"
                    existing_details = self._decode_json(existing["details_json"])
                    preserve_active_buy = _should_preserve_active_buy(existing, idea, row)
                    duplicate_active_buy = _is_duplicate_active_buy_refresh(existing, idea, row, now)
                    if preserve_active_buy:
                        status = "ACTIVE"
                        idea["signal_type"] = "BUY"
                        incoming_monitor_reason = idea["details"].get("reason") or row.get("reason")
                        original_buy_reason = (
                            existing_details.get("original_buy_reason")
                            or existing_details.get("reason")
                            or existing["reason"]
                        )
                        if original_buy_reason:
                            idea["reason"] = str(original_buy_reason)[:1000]
                            idea["details"]["reason"] = original_buy_reason
                            idea["details"]["original_buy_reason"] = original_buy_reason
                        if incoming_monitor_reason:
                            idea["details"]["latest_monitor_reason"] = incoming_monitor_reason
                        idea["details"]["why_changed"] = _why_changed_payload(
                            original_buy_reason,
                            incoming_monitor_reason,
                            str(row.get("action") or ""),
                            {"preserved": True},
                        )
                        idea["details"]["signal_continuity"] = {
                            "preserved": True,
                            "previous_signal_type": existing["signal_type"],
                            "previous_status": existing["status"],
                            "latest_engine_action": row.get("action"),
                            "latest_engine_status": idea["status"],
                            "reason": "A live BUY idea remains active until stop, expiry, target completion, or explicit exit. HOLD means monitor/no add.",
                        }
                        idea["details"]["latest_system_action"] = row.get("action")
                    elif duplicate_active_buy:
                        status = "ACTIVE"
                        idea["signal_type"] = "BUY"
                        original_buy_reason = (
                            existing_details.get("original_buy_reason")
                            or existing_details.get("reason")
                            or existing["reason"]
                        )
                        latest_buy_reason = idea["details"].get("reason") or row.get("reason")
                        if original_buy_reason:
                            idea["reason"] = str(original_buy_reason)[:1000]
                            idea["details"]["reason"] = original_buy_reason
                            idea["details"]["original_buy_reason"] = original_buy_reason
                        if latest_buy_reason:
                            idea["details"]["latest_monitor_reason"] = latest_buy_reason
                        continuity = {
                            "preserved": True,
                            "duplicate_active_buy": True,
                            "previous_signal_type": existing["signal_type"],
                            "previous_status": existing["status"],
                            "latest_engine_action": row.get("action"),
                            "latest_engine_status": idea["status"],
                            "cooldown_hours": DUPLICATE_BUY_COOLDOWN_HOURS,
                            "reason": "BUY is already active; repeated BUY refresh is monitor/no fresh add during cooldown.",
                        }
                        idea["details"]["why_changed"] = _why_changed_payload(
                            original_buy_reason,
                            latest_buy_reason,
                            str(row.get("action") or ""),
                            continuity,
                        )
                        idea["details"]["signal_continuity"] = continuity
                        idea["details"]["latest_system_action"] = row.get("action")
                    status, idea_details = _refresh_idea_lifecycle(
                        existing_details,
                        idea["details"],
                        entry_price,
                        latest_price,
                        status,
                        now,
                        idea["plan_code"],
                        existing["first_seen_at"],
                    )
                    if (preserve_active_buy or duplicate_active_buy) and status in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
                        idea["signal_type"] = "BUY"
                    conn.execute(
                        """
                        update signal_ideas
                        set last_seen_at = ?, plan_code = ?, signal_type = ?, status = ?, latest_price = ?,
                            current_return_pct = ?, peak_return_pct = ?, worst_return_pct = ?,
                            confidence = ?, combined_score = ?, confluence = ?, overall_score_pct = ?,
                            overall_grade = ?, latest_decision_id = ?, reason = ?, details_json = ?
                        where id = ?
                        """,
                        (
                            now,
                            idea["plan_code"],
                            idea["signal_type"],
                            status,
                            latest_price,
                            current_return,
                            peak_return,
                            worst_return,
                            idea["confidence"],
                            idea["combined_score"],
                            idea["confluence"],
                            idea["overall_score_pct"],
                            idea["overall_grade"],
                            latest_decision_id,
                            idea["reason"],
                            json.dumps(idea_details, default=str, separators=(",", ":")),
                            existing["id"],
                        ),
                    )
                else:
                    entry_price = latest_price
                    current_return = 0.0
                    status, idea_details = _refresh_idea_lifecycle(
                        {},
                        idea["details"],
                        entry_price,
                        latest_price,
                        idea["status"],
                        now,
                        idea["plan_code"],
                        now,
                    )
                    conn.execute(
                        """
                        insert into signal_ideas (
                            first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                            entry_price, latest_price, current_return_pct, peak_return_pct,
                            worst_return_pct, confidence, combined_score, confluence,
                            overall_score_pct, overall_grade, decision_id, latest_decision_id,
                            reason, details_json
                        )
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            now,
                            now,
                            idea["symbol"],
                            idea["strategy"],
                            idea["plan_code"],
                            idea["signal_type"],
                            idea["status"],
                            entry_price,
                            latest_price,
                            current_return,
                            current_return,
                            current_return,
                            idea["confidence"],
                            idea["combined_score"],
                            idea["confluence"],
                            idea["overall_score_pct"],
                            idea["overall_grade"],
                            latest_decision_id,
                            latest_decision_id,
                            idea["reason"],
                            json.dumps(idea_details, default=str, separators=(",", ":")),
                        ),
                    )
            self._refresh_user_follow_marks(conn)

    def refresh_signal_idea_marks(self) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select i.*, q.price as quote_price
                from signal_ideas i
                left join latest_quotes q on q.symbol = i.symbol
                where i.status in ('ACTIVE','WATCH','MONITORING')
                """
            ).fetchall()
            for row in rows:
                latest_price = _optional_float(row["quote_price"]) or float(row["latest_price"] or row["entry_price"] or 0)
                entry_price = float(row["entry_price"] or latest_price or 0)
                current_return = _return_pct(entry_price, latest_price)
                peak_return = max(float(row["peak_return_pct"] or 0.0), current_return)
                worst_return = min(float(row["worst_return_pct"] or 0.0), current_return)
                details = self._decode_json(row["details_json"])
                next_status, details = _refresh_idea_lifecycle(
                    details,
                    {},
                    entry_price,
                    latest_price,
                    row["status"],
                    utc_now(),
                    row["plan_code"],
                    row["first_seen_at"],
                )
                conn.execute(
                    """
                    update signal_ideas
                    set latest_price = ?, current_return_pct = ?, peak_return_pct = ?, worst_return_pct = ?,
                        status = ?, details_json = ?, last_seen_at = ?
                    where id = ?
                    """,
                    (
                        latest_price,
                        current_return,
                        peak_return,
                        worst_return,
                        next_status,
                        json.dumps(details, default=str, separators=(",", ":")),
                        utc_now(),
                        row["id"],
                    ),
                )
            self._refresh_user_follow_marks(conn)

    def refresh_active_position_marks(self, symbols: Iterable[str] | None = None) -> int:
        symbol_values = sorted({str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
        symbol_sql = ""
        params: list[Any] = []
        if symbol_values:
            placeholders = ",".join("?" for _ in symbol_values)
            symbol_sql = f"and upper(i.symbol) in ({placeholders})"
            params.extend(symbol_values)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select distinct i.*, q.price as quote_price
                from signal_ideas i
                join user_idea_follows f on f.idea_id = i.id
                left join latest_quotes q on q.symbol = i.symbol
                where f.status in ('ACTIVE','LIVE_REQUESTED','LIVE_EXIT_REQUESTED')
                  and coalesce(f.qty, 0) > 0
                  and q.price is not null
                  {symbol_sql}
                """,
                params,
            ).fetchall()
            now = utc_now()
            for row in rows:
                latest_price = _optional_float(row["quote_price"]) or float(row["latest_price"] or row["entry_price"] or 0)
                entry_price = float(row["entry_price"] or latest_price or 0)
                current_return = _return_pct(entry_price, latest_price)
                peak_return = max(float(row["peak_return_pct"] or 0.0), current_return)
                worst_return = min(float(row["worst_return_pct"] or 0.0), current_return)
                details = self._decode_json(row["details_json"])
                next_status, details = _refresh_idea_lifecycle(
                    details,
                    {},
                    entry_price,
                    latest_price,
                    row["status"],
                    now,
                    row["plan_code"],
                    row["first_seen_at"],
                )
                conn.execute(
                    """
                    update signal_ideas
                    set latest_price = ?, current_return_pct = ?, peak_return_pct = ?, worst_return_pct = ?,
                        status = ?, details_json = ?
                    where id = ?
                    """,
                    (
                        latest_price,
                        current_return,
                        peak_return,
                        worst_return,
                        next_status,
                        json.dumps(details, default=str, separators=(",", ":")),
                        row["id"],
                    ),
                )
            self._refresh_user_follow_marks(conn, symbol_values if symbol_values else None)
        return len(rows)

    def latest_signal_ideas(
        self,
        limit: int = 20,
        user_id: int | None = None,
        market_region: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        market_clause, market_params = _market_region_where("u", market_region)
        where_parts = ["i.status != 'REJECTED'"]
        if market_clause:
            where_parts.append(market_clause)
        symbol_params: list[str] = []
        if symbols is not None:
            symbol_params = _normalize_monitor_symbols(symbols)
            if not symbol_params:
                return []
            where_parts.append(f"upper(i.symbol) in ({','.join('?' for _ in symbol_params)})")
        where_sql = "where " + " and ".join(where_parts)
        requested_limit = max(1, min(int(limit), 500))
        query_limit = max(requested_limit, min(requested_limit * 4, 500))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select i.*, {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name,
                    u.sector as sector,
                    u.industry as industry
                from signal_ideas i
                left join universe u on u.symbol = i.symbol
                {where_sql}
                order by
                    overall_score_pct desc,
                    confluence desc,
                    combined_score desc,
                    confidence desc,
                    case status when 'ACTIVE' then 0 when 'WATCH' then 1 when 'MONITORING' then 2 else 3 end,
                    signal_type = 'BUY' desc,
                    current_return_pct desc,
                    last_seen_at desc
                limit ?
                """,
                (*market_params, *symbol_params, query_limit),
            ).fetchall()
            follow_rows: list[sqlite3.Row] = []
            if user_id is not None:
                follow_rows = conn.execute(
                    """
                    select *
                    from user_idea_follows
                    where user_id = ? and idea_id in ({})
                    order by id desc
                    """.format(",".join("?" for _ in rows) or "0"),
                    (user_id, *[row["id"] for row in rows]) if rows else (user_id,),
                ).fetchall() if rows else []
        follows_by_idea: dict[int, dict[str, Any]] = {}
        for follow in follow_rows:
            follows_by_idea.setdefault(int(follow["idea_id"]), _row_dict(follow))
        output: list[dict[str, Any]] = []
        seen_symbols: set[str] = set()
        for row in rows:
            item = _row_dict(row)
            item["details"] = self._decode_json(item.pop("details_json", "{}"))
            item["targets"] = item["details"].get("targets", [])
            item["target_status"] = item["details"].get("target_status", [])
            item["highest_target_hit"] = item["details"].get("highest_target_hit", "NONE")
            item["lifecycle_status"] = item["details"].get("lifecycle_status", item.get("status"))
            item["expires_at"] = item["details"].get("expires_at")
            item["days_to_expiry"] = item["details"].get("days_to_expiry")
            item["timeline"] = item["details"].get("timeline", {})
            item["stop_status"] = item["details"].get("stop_status", {})
            item["entry_zone"] = item["details"].get("entry_zone")
            item["stop_loss"] = item["details"].get("stop_loss")
            item["risk_flags"] = item["details"].get("risk_flags", [])
            item["decision_readiness"] = item["details"].get("decision_readiness", "monitor_only")
            item["tier"] = item["details"].get("tier", "")
            item["suggestion"] = item.get("signal_type")
            item["price"] = item.get("latest_price")
            item["id"] = int(item["id"])
            if item.get("latest_decision_id"):
                item["detail_url"] = f"/api/decisions/{item['latest_decision_id']}"
            item["user_follow"] = follows_by_idea.get(int(item["id"]))
            symbol = str(item.get("symbol") or "").upper()
            if symbol and symbol in seen_symbols:
                continue
            if symbol:
                seen_symbols.add(symbol)
            output.append(_decorate_signal_idea_item(item))
            if len(output) >= requested_limit:
                break
        return output

    def monitor_watchlist_rows(
        self,
        symbols: Iterable[str],
        *,
        user_id: int | None = None,
        market_region: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        symbol_params = _normalize_monitor_symbols(symbols)
        if not symbol_params:
            return []
        requested_limit = max(1, min(int(limit or 100), 500))
        symbol_params = symbol_params[:requested_limit]
        market_clause, market_params = _market_region_where("u", market_region)
        where_parts = ["u.enabled = 1", f"upper(u.symbol) in ({','.join('?' for _ in symbol_params)})"]
        if market_clause:
            where_parts.append(market_clause)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    u.symbol,
                    u.name as company_name,
                    u.exchange,
                    u.sector,
                    u.industry,
                    {_market_region_case("u")} as market_region,
                    q.price as latest_price,
                    q.open as quote_open,
                    q.high as quote_high,
                    q.low as quote_low,
                    q.close as quote_close,
                    q.volume as quote_volume,
                    q.ts as quote_updated_at,
                    q.source as quote_source
                from universe u
                left join latest_quotes q on q.symbol = u.symbol
                where {" and ".join(where_parts)}
                """,
                (*symbol_params, *market_params),
            ).fetchall()
        universe_by_symbol = {str(row["symbol"] or "").upper(): _row_dict(row) for row in rows}
        ideas = self.latest_signal_ideas(
            requested_limit,
            user_id=user_id,
            market_region=market_region,
            symbols=symbol_params,
        )
        ideas_by_symbol = {str(row.get("symbol") or "").upper(): dict(row) for row in ideas}
        output: list[dict[str, Any]] = []
        for symbol in symbol_params:
            base = universe_by_symbol.get(symbol)
            if not base:
                continue
            idea = ideas_by_symbol.get(symbol)
            if idea:
                item = dict(idea)
                if item.get("latest_price") in (None, "") and base.get("latest_price") not in (None, ""):
                    item["latest_price"] = base.get("latest_price")
                    item["price"] = base.get("latest_price")
                item.setdefault("company_name", base.get("company_name"))
                item.setdefault("exchange", base.get("exchange"))
                item.setdefault("sector", base.get("sector"))
                item.setdefault("industry", base.get("industry"))
                item.setdefault("market_region", base.get("market_region"))
                item["quote_updated_at"] = base.get("quote_updated_at")
                item["quote_source"] = base.get("quote_source")
                item["watchlist_source"] = "monitor_symbols"
                output.append(item)
                continue

            latest_price = _optional_float(base.get("latest_price"))
            close_price = _optional_float(base.get("quote_close"))
            current_return = _return_pct(close_price or latest_price or 0.0, latest_price or 0.0)
            output.append(
                {
                    "id": None,
                    "symbol": symbol,
                    "company_name": base.get("company_name"),
                    "name": base.get("company_name"),
                    "exchange": base.get("exchange"),
                    "sector": base.get("sector"),
                    "industry": base.get("industry"),
                    "market_region": base.get("market_region"),
                    "strategy": "custom_monitor_list",
                    "plan_code": "custom_monitor_list",
                    "signal_type": "WATCH",
                    "suggestion": "WATCH",
                    "status": "MONITORING",
                    "latest_price": latest_price,
                    "price": latest_price,
                    "entry_price": latest_price,
                    "current_return_pct": current_return,
                    "peak_return_pct": current_return,
                    "worst_return_pct": current_return,
                    "confidence": 0.0,
                    "combined_score": 0.0,
                    "confluence": 0,
                    "overall_score_pct": 0.0,
                    "overall_grade": "WATCH",
                    "reason": "Custom monitor symbol. Waiting for deterministic setup or BUY signal.",
                    "decision_readiness": "monitor_only",
                    "details": {
                        "decision_readiness": "monitor_only",
                        "monitor_scope": "CUSTOM",
                        "watchlist_source": "monitor_symbols",
                    },
                    "watchlist_source": "monitor_symbols",
                    "quote_open": base.get("quote_open"),
                    "quote_high": base.get("quote_high"),
                    "quote_low": base.get("quote_low"),
                    "quote_close": base.get("quote_close"),
                    "quote_volume": base.get("quote_volume"),
                    "quote_updated_at": base.get("quote_updated_at"),
                    "quote_source": base.get("quote_source"),
                }
            )
        return output

    def user_followed_signal_ideas(
        self,
        user_id: int,
        limit: int = 50,
        market_region: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        market_clause, market_params = _market_region_where("u", market_region)
        market_sql = f"and {market_clause}" if market_clause else ""
        symbol_params: list[str] = []
        symbol_sql = ""
        if symbols is not None:
            symbol_params = _normalize_monitor_symbols(symbols)
            if not symbol_params:
                return []
            symbol_sql = f"and upper(i.symbol) in ({','.join('?' for _ in symbol_params)})"
        today_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    f.id as follow_id,
                    f.user_id,
                    f.idea_id,
                    f.mode,
                    f.status as follow_status,
                    f.qty,
                    f.entry_price as follow_entry_price,
                    f.latest_price as follow_latest_price,
                    f.invested_amount,
                    f.unrealized_pnl,
                    f.return_pct,
                    f.created_at as followed_at,
                    f.updated_at as follow_updated_at,
                    f.details_json as follow_details_json,
                    i.*,
                    {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name,
                    u.sector as sector,
                    u.industry as industry,
                    q.ts as quote_updated_at,
                    q.source as quote_source,
                    (
                        select c.close
                        from candles c
                        where c.symbol = i.symbol
                          and substr(c.ts, 1, 10) = (
                              select max(substr(c2.ts, 1, 10))
                              from candles c2
                              where c2.symbol = i.symbol
                                and substr(c2.ts, 1, 10) < ?
                          )
                          and (
                              c.source like '%:day'
                              or c.source like '%:30minute'
                              or c.source like '%:15minute'
                              or c.source like '%:5minute'
                              or c.source like '%:1minute'
                          )
                        order by
                          case when c.source like '%:day' then 0 else 1 end,
                          c.ts desc
                        limit 1
                    ) as previous_close,
                    (
                        select c.ts
                        from candles c
                        where c.symbol = i.symbol
                          and substr(c.ts, 1, 10) = (
                              select max(substr(c2.ts, 1, 10))
                              from candles c2
                              where c2.symbol = i.symbol
                                and substr(c2.ts, 1, 10) < ?
                          )
                          and (
                              c.source like '%:day'
                              or c.source like '%:30minute'
                              or c.source like '%:15minute'
                              or c.source like '%:5minute'
                              or c.source like '%:1minute'
                          )
                        order by c.ts desc
                        limit 1
                    ) as previous_close_at
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                left join latest_quotes q on q.symbol = i.symbol
                where f.user_id = ? and f.status in ('ACTIVE','LIVE_REQUESTED','LIVE_EXIT_REQUESTED')
                {market_sql}
                {symbol_sql}
                order by f.updated_at desc, f.id desc
                limit ?
                """,
                (today_ist, today_ist, int(user_id), *market_params, *symbol_params, max(1, min(int(limit), 200))),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = _row_dict(row)
            idea_details = self._decode_json(item.pop("details_json", "{}"))
            follow_details = self._decode_json(item.pop("follow_details_json", "{}"))
            item["details"] = idea_details
            item["follow_details"] = follow_details
            item["id"] = int(item["idea_id"])
            item["suggestion"] = item.get("signal_type")
            item["price"] = item.get("latest_price")
            item["targets"] = idea_details.get("targets", [])
            item["target_status"] = idea_details.get("target_status", [])
            item["highest_target_hit"] = idea_details.get("highest_target_hit", "NONE")
            item["lifecycle_status"] = idea_details.get("lifecycle_status", item.get("status"))
            item["expires_at"] = idea_details.get("expires_at")
            item["days_to_expiry"] = idea_details.get("days_to_expiry")
            item["timeline"] = idea_details.get("timeline", {})
            item["stop_status"] = idea_details.get("stop_status", {})
            item["entry_zone"] = idea_details.get("entry_zone")
            item["stop_loss"] = idea_details.get("stop_loss")
            item["risk_flags"] = idea_details.get("risk_flags", [])
            item["decision_readiness"] = idea_details.get("decision_readiness", "monitor_only")
            item["tier"] = idea_details.get("tier", "")
            if item.get("latest_decision_id"):
                item["detail_url"] = f"/api/decisions/{item['latest_decision_id']}"
            item["user_follow"] = {
                "id": item.get("follow_id"),
                "user_id": item.get("user_id"),
                "idea_id": item.get("idea_id"),
                "mode": item.get("mode"),
                "status": item.get("follow_status"),
                "qty": item.get("qty"),
                "entry_price": item.get("follow_entry_price"),
                "latest_price": item.get("follow_latest_price"),
                "invested_amount": item.get("invested_amount"),
                "unrealized_pnl": item.get("unrealized_pnl"),
                "return_pct": item.get("return_pct"),
                "created_at": item.get("followed_at"),
                "updated_at": item.get("follow_updated_at"),
            }
            output.append(_decorate_signal_idea_item(item))
        return output

    def user_follow_history(
        self,
        user_id: int,
        limit: int = 100,
        market_region: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        market_clause, market_params = _market_region_where("u", market_region)
        market_sql = f"and {market_clause}" if market_clause else ""
        symbol_params: list[str] = []
        symbol_sql = ""
        if symbols is not None:
            symbol_params = _normalize_monitor_symbols(symbols)
            if not symbol_params:
                return []
            symbol_sql = f"and upper(i.symbol) in ({','.join('?' for _ in symbol_params)})"
        with self.connect() as conn:
            self._refresh_user_follow_marks(conn)
            rows = conn.execute(
                f"""
                select
                    f.id as follow_id,
                    f.user_id,
                    f.idea_id,
                    f.mode,
                    f.status as follow_status,
                    f.qty,
                    f.entry_price,
                    f.latest_price as follow_latest_price,
                    f.invested_amount,
                    f.unrealized_pnl,
                    f.return_pct,
                    f.created_at as opened_at,
                    f.updated_at as updated_at,
                    f.details_json as follow_details_json,
                    i.symbol,
                    i.strategy,
                    i.signal_type,
                    i.status as idea_status,
                    i.latest_price as idea_latest_price,
                    i.last_seen_at as idea_last_seen_at,
                    {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name,
                    u.sector as sector,
                    u.industry as industry
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                where f.user_id = ?
                  and f.mode in ('PAPER','LIVE')
                  {market_sql}
                  {symbol_sql}
                order by f.updated_at desc, f.id desc
                limit ?
                """,
                (int(user_id), *market_params, *symbol_params, max(1, min(int(limit), 500))),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            item = _row_dict(row)
            follow_details = self._decode_json(item.pop("follow_details_json", "{}"))
            if not isinstance(follow_details, dict):
                follow_details = {}
            management = follow_details.get("exit_management") if isinstance(follow_details.get("exit_management"), dict) else {}
            events = [event for event in management.get("events", []) if isinstance(event, dict)]
            latest_event = events[-1] if events else {}
            manual_exit = follow_details.get("manual_exit") if isinstance(follow_details.get("manual_exit"), dict) else {}
            safety_exit = follow_details.get("safety_exit") if isinstance(follow_details.get("safety_exit"), dict) else {}
            mode = str(item.get("mode") or "TRACK").upper()
            status = str(item.get("follow_status") or "").upper()
            current_qty = int(item.get("qty") or 0)
            closed_qty = int(_optional_float(management.get("closed_qty_total")) or 0)
            manual_qty = int(_optional_float(manual_exit.get("qty")) or 0)
            safety_qty = int(_optional_float(safety_exit.get("qty")) or 0)
            entry_qty = max(current_qty + closed_qty, manual_qty, safety_qty, current_qty)
            remaining_qty = current_qty if status in {"ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"} else 0
            entry_price = float(item.get("entry_price") or 0.0)
            latest_price = float(item.get("idea_latest_price") or item.get("follow_latest_price") or entry_price or 0.0)
            exit_price = (
                _optional_float(manual_exit.get("exit_price"))
                or _optional_float(latest_event.get("exit_price"))
                or _optional_float(safety_exit.get("exit_price"))
                or latest_price
            )
            closed_at = (
                manual_exit.get("exited_at")
                or management.get("last_action_at")
                or safety_exit.get("exited_at")
                or (item.get("updated_at") if status in {"EXITED", "LIVE_EXIT_REQUESTED"} else None)
            )
            realized_pnl = _follow_realized_pnl(follow_details)
            return_pct = (
                _optional_float(manual_exit.get("return_pct"))
                or _optional_float(latest_event.get("return_pct"))
                or _optional_float(safety_exit.get("return_pct"))
                or _optional_float(item.get("return_pct"))
                or _return_pct(entry_price, exit_price)
            )
            exit_reason = (
                manual_exit.get("reason")
                or management.get("last_reason")
                or latest_event.get("reason")
                or safety_exit.get("quality_reason")
                or safety_exit.get("reason")
                or management.get("last_action_label")
                or status
            )
            exit_action = (
                manual_exit.get("action")
                or latest_event.get("action")
                or safety_exit.get("action")
                or management.get("last_action")
                or ("SELL" if status in {"EXITED", "LIVE_EXIT_REQUESTED"} else "")
            )
            history.append(
                {
                    "follow_id": item.get("follow_id"),
                    "idea_id": item.get("idea_id"),
                    "symbol": item.get("symbol"),
                    "company_name": item.get("company_name"),
                    "market_region": normalize_market_region(item.get("market_region") or "IN", default="IN"),
                    "exchange": item.get("exchange"),
                    "sector": item.get("sector"),
                    "industry": item.get("industry"),
                    "mode": mode,
                    "mode_label": "Paper" if mode == "PAPER" else "Live request",
                    "status": status,
                    "state": "OPEN" if status in {"ACTIVE", "LIVE_REQUESTED"} else "EXIT_PENDING" if status == "LIVE_EXIT_REQUESTED" else "CLOSED",
                    "qty": remaining_qty,
                    "entry_qty": entry_qty,
                    "closed_qty": (closed_qty or manual_qty or safety_qty) if status == "EXITED" else closed_qty,
                    "entry_price": entry_price,
                    "latest_price": latest_price,
                    "exit_price": exit_price if status in {"EXITED", "LIVE_EXIT_REQUESTED"} or realized_pnl else None,
                    "invested_amount": float(item.get("invested_amount") or 0.0),
                    "entry_notional": round(entry_qty * entry_price, 2),
                    "market_value": round(remaining_qty * latest_price, 2),
                    "unrealized_pnl": float(item.get("unrealized_pnl") or 0.0) if remaining_qty > 0 else 0.0,
                    "realized_pnl": realized_pnl,
                    "cash_effect": realized_pnl if mode == "PAPER" else 0.0,
                    "return_pct": round(float(return_pct), 4),
                    "exit_reason": str(exit_reason or "").strip(),
                    "exit_action": str(exit_action or "").strip().upper(),
                    "exit_economics": latest_event.get("economics") if isinstance(latest_event.get("economics"), dict) else {},
                    "last_skipped_exit": management.get("last_skipped_action") if isinstance(management.get("last_skipped_action"), dict) else {},
                    "strategy": item.get("strategy"),
                    "signal_type": item.get("signal_type"),
                    "opened_at": item.get("opened_at"),
                    "closed_at": closed_at,
                    "updated_at": item.get("updated_at"),
                }
            )
        return history

    def user_follow_realized_pnl_by_market(self, user_id: int) -> dict[str, float]:
        totals: dict[str, float] = {"IN": 0.0, "US": 0.0}
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    f.details_json,
                    {_market_region_case("u")} as market_region
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                where f.user_id = ?
                  and f.mode = 'PAPER'
                """,
                (int(user_id),),
            ).fetchall()
        for row in rows:
            item = _row_dict(row)
            market = normalize_market_region(item.get("market_region") or "IN", default="IN")
            details = self._decode_json(item.get("details_json"))
            totals[market] = round(float(totals.get(market, 0.0)) + _follow_realized_pnl(details), 2)
        return totals

    def strategy_plans(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            plan_rows = conn.execute(
                """
                select p.*,
                    count(i.id) as idea_count,
                    coalesce(avg(i.current_return_pct), 0) as avg_return_pct,
                    coalesce(max(i.peak_return_pct), 0) as best_return_pct,
                    coalesce(min(i.worst_return_pct), 0) as worst_return_pct
                from strategy_plans p
                left join signal_ideas i on i.plan_code = p.code and i.status != 'REJECTED'
                group by p.id
                order by p.id
                """
            ).fetchall()
            stats_rows = conn.execute(
                f"""
                select
                    i.plan_code,
                    {_market_region_case("u")} as market_region,
                    count(i.id) as idea_count,
                    coalesce(avg(i.current_return_pct), 0) as avg_return_pct,
                    coalesce(max(i.peak_return_pct), 0) as best_return_pct,
                    coalesce(min(i.worst_return_pct), 0) as worst_return_pct
                from signal_ideas i
                left join universe u on u.symbol = i.symbol
                where i.plan_code != '' and i.status != 'REJECTED'
                group by i.plan_code, market_region
                """
            ).fetchall()
            idea_rows = conn.execute(
                f"""
                select i.id, i.symbol, i.plan_code, i.signal_type, i.status, i.latest_price,
                    i.current_return_pct, i.peak_return_pct, i.overall_score_pct, i.overall_grade,
                    i.confluence, i.last_seen_at, i.details_json,
                    {_market_region_case("u")} as market_region,
                    u.name as company_name,
                    u.sector as sector
                from signal_ideas i
                left join universe u on u.symbol = i.symbol
                where i.plan_code != '' and i.status != 'REJECTED'
                order by
                    case i.status when 'ACTIVE' then 0 when 'WATCH' then 1 when 'MONITORING' then 2 else 3 end,
                    i.current_return_pct desc,
                    i.overall_score_pct desc,
                    i.last_seen_at desc
                limit 120
                """
            ).fetchall()
        plans = [_row_dict(row) for row in plan_rows]
        ideas_by_plan: dict[str, list[dict[str, Any]]] = {}
        ideas_by_plan_market: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in idea_rows:
            item = _row_dict(row)
            details = self._decode_json(item.pop("details_json", "{}"))
            item.update(
                {
                    "targets": details.get("targets", []),
                    "target_status": details.get("target_status", []),
                    "highest_target_hit": details.get("highest_target_hit", "NONE"),
                    "lifecycle_status": details.get("lifecycle_status", item.get("status")),
                    "entry_zone": details.get("entry_zone"),
                    "stop_loss": details.get("stop_loss"),
                    "days_to_expiry": details.get("days_to_expiry"),
                    "expires_at": details.get("expires_at"),
                    "timeline": details.get("timeline", {}),
                }
            )
            plan_code = str(item.get("plan_code") or "")
            market = normalize_market_region(item.get("market_region") or "IN", default="IN")
            item["market_region"] = market
            ideas_by_plan.setdefault(plan_code, []).append(item)
            ideas_by_plan_market.setdefault(plan_code, {}).setdefault(market, []).append(item)
        stats_by_plan: dict[str, dict[str, dict[str, Any]]] = {}
        for row in stats_rows:
            item = _row_dict(row)
            plan_code = str(item.get("plan_code") or "")
            market = normalize_market_region(item.get("market_region") or "IN", default="IN")
            stats_by_plan.setdefault(plan_code, {})[market] = {
                "idea_count": int(item.get("idea_count") or 0),
                "avg_return_pct": round(float(item.get("avg_return_pct") or 0.0), 4),
                "best_return_pct": round(float(item.get("best_return_pct") or 0.0), 4),
                "worst_return_pct": round(float(item.get("worst_return_pct") or 0.0), 4),
            }
        for plan in plans:
            plan_code = str(plan.get("code") or "")
            constituents = ideas_by_plan.get(plan_code, [])[:6]
            market_stats = {
                "IN": {"idea_count": 0, "avg_return_pct": 0.0, "best_return_pct": 0.0, "worst_return_pct": 0.0},
                "US": {"idea_count": 0, "avg_return_pct": 0.0, "best_return_pct": 0.0, "worst_return_pct": 0.0},
            }
            for market, stats in (stats_by_plan.get(plan_code) or {}).items():
                market_stats[market] = stats
            constituents_by_market = {
                "IN": (ideas_by_plan_market.get(plan_code, {}).get("IN") or [])[:6],
                "US": (ideas_by_plan_market.get(plan_code, {}).get("US") or [])[:6],
            }
            plan["constituents"] = constituents
            plan["constituents_by_market"] = constituents_by_market
            plan["market_stats"] = market_stats
            plan["top_symbols"] = [item.get("symbol") for item in constituents[:5]]
            plan["active_idea_count"] = len(constituents)
            plan["timeline"] = plan.get("holding_period")
        return plans

    def follow_signal_idea(
        self,
        user_id: int,
        idea_id: int,
        mode: str = "TRACK",
        amount: float = 0.0,
        qty: int = 0,
        cost_settings: Any = None,
        manual_override: bool = False,
    ) -> dict[str, Any]:
        mode = str(mode or "TRACK").strip().upper()
        if mode not in {"TRACK", "PAPER", "LIVE"}:
            mode = "TRACK"
        with self.connect() as conn:
            idea = conn.execute(
                f"""
                select i.*, {_market_region_case("u")} as market_region
                from signal_ideas i
                left join universe u on u.symbol = i.symbol
                where i.id = ?
                """,
                (idea_id,),
            ).fetchone()
            if idea is None:
                raise ValueError("idea not found")
            latest_price = float(idea["latest_price"] or idea["entry_price"] or 0.0)
            market_region = normalize_market_region(idea["market_region"] or "IN", default="IN")
            idea_details = self._decode_json(idea["details_json"])
            quality_gate: dict[str, Any] | None = None
            if mode in {"PAPER", "LIVE"}:
                reentry_block = self.recent_user_symbol_exit(
                    user_id,
                    str(idea["symbol"] or ""),
                    cooldown_hours=AUTO_FOLLOW_REENTRY_COOLDOWN_HOURS,
                )
                if reentry_block:
                    raise ValueError(
                        "recent_risk_exit_cooldown:"
                        f"{reentry_block.get('exit_reason') or reentry_block.get('exit_key') or 'risk_exit'}"
                    )
                quality_gate = auto_follow_quality_gate(
                    {
                        "action": idea_details.get("action") or idea["signal_type"],
                        "signal_type": idea["signal_type"],
                        "status": idea["status"],
                        "overall_score_pct": idea["overall_score_pct"],
                        "overall_grade": idea["overall_grade"],
                        "confluence": idea["confluence"],
                        "data_readiness": idea_details.get("data_readiness"),
                        "hard_blocked": idea_details.get("hard_blocked"),
                        "details": idea_details,
                    }
                )
                if not quality_gate.get("passed") and not (manual_override and mode == "PAPER"):
                    raise ValueError(f"phase1_quality_gate:{quality_gate.get('reason')}")
            if qty <= 0 and amount > 0 and latest_price > 0:
                qty = int(float(amount) // latest_price)
            if mode in {"PAPER", "LIVE"} and qty <= 0:
                raise ValueError("amount is too small for one share at the current idea price")
            if mode in {"PAPER", "LIVE"}:
                entry_economics = entry_size_economics(latest_price, qty, market_region, cost_settings)
                if not entry_economics.get("passed"):
                    raise ValueError(
                        "trade_economics_min_notional:"
                        f"notional={entry_economics.get('notional')},"
                        f"minimum={entry_economics.get('minimum_notional')}"
                    )
            invested = float(qty * latest_price)
            status = "ACTIVE" if mode != "LIVE" else "LIVE_REQUESTED"
            existing_follow = conn.execute(
                """
                select *
                from user_idea_follows
                where user_id = ? and idea_id = ? and status in ('ACTIVE','LIVE_REQUESTED')
                order by id desc
                limit 1
                """,
                (user_id, idea_id),
            ).fetchone()
            details = {
                "symbol": idea["symbol"],
                "strategy": idea["strategy"],
                "signal_type": idea["signal_type"],
                "note": {
                    "TRACK": "Tracking only. No paper cash or live broker order is used.",
                    "PAPER": "Paper position entered. P&L is simulated and managed by OpenStocks.",
                    "LIVE": "Live order requested. Broker guard and user broker session must approve routing.",
                }.get(mode, "Tracking only."),
                "quality_gate": quality_gate,
                "manual_override": bool(manual_override and mode == "PAPER"),
                "manual_override_note": (
                    "User manually confirmed this paper entry from the product UI. Quality warnings are recorded; this is not a live broker order."
                    if manual_override and mode == "PAPER"
                    else ""
                ),
            }
            now = utc_now()
            if existing_follow:
                previous_mode = str(existing_follow["mode"] or "TRACK").upper()
                next_mode = previous_mode if mode == "TRACK" and previous_mode in {"PAPER", "LIVE"} else mode
                next_status = "LIVE_REQUESTED" if next_mode == "LIVE" else "ACTIVE"
                next_qty = qty or int(existing_follow["qty"] or 0)
                next_entry = (
                    float(existing_follow["entry_price"] or latest_price)
                    if int(existing_follow["qty"] or 0) > 0 and next_qty == int(existing_follow["qty"] or 0)
                    else latest_price
                )
                invested = float(next_qty * next_entry)
                conn.execute(
                    """
                    update user_idea_follows
                    set mode = ?, status = ?, qty = ?, entry_price = ?, latest_price = ?,
                        invested_amount = ?, updated_at = ?, details_json = ?
                    where id = ?
                    """,
                    (
                        next_mode,
                        next_status,
                        next_qty,
                        next_entry,
                        latest_price,
                        invested,
                        now,
                        json.dumps(details, default=str, separators=(",", ":")),
                        existing_follow["id"],
                    ),
                )
                self._refresh_user_follow_marks(conn)
                row = conn.execute("select * from user_idea_follows where id = ?", (existing_follow["id"],)).fetchone()
                return _row_dict(row) if row else {}
            if mode in {"PAPER", "LIVE"}:
                existing_symbol_follow = conn.execute(
                    """
                    select f.*
                    from user_idea_follows f
                    join signal_ideas i on i.id = f.idea_id
                    where f.user_id = ?
                      and upper(i.symbol) = upper(?)
                      and f.status in ('ACTIVE','LIVE_REQUESTED')
                      and f.mode in ('PAPER','LIVE')
                      and f.qty > 0
                    order by f.id desc
                    limit 1
                    """,
                    (user_id, idea["symbol"]),
                ).fetchone()
                if existing_symbol_follow:
                    self._refresh_user_follow_marks(conn)
                    row = conn.execute("select * from user_idea_follows where id = ?", (existing_symbol_follow["id"],)).fetchone()
                    return _row_dict(row) if row else {}
            conn.execute(
                """
                insert into user_idea_follows (
                    user_id, idea_id, mode, status, qty, entry_price, latest_price,
                    invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    idea_id,
                    mode,
                    status,
                    qty,
                    latest_price,
                    latest_price,
                    invested,
                    now,
                    now,
                    json.dumps(details, default=str, separators=(",", ":")),
                ),
            )
            self._refresh_user_follow_marks(conn)
            row = conn.execute("select * from user_idea_follows where id = last_insert_rowid()").fetchone()
        return _row_dict(row) if row else {}

    def exit_unsafe_active_follows(self, reason: str = "quality_gate_failed_after_follow") -> list[dict[str, Any]]:
        exited: list[dict[str, Any]] = []
        with self.connect() as conn:
            self._refresh_user_follow_marks(conn)
            rows = conn.execute(
                """
                select
                    f.*,
                    i.symbol,
                    i.signal_type,
                    i.status as idea_status,
                    i.overall_score_pct,
                    i.overall_grade,
                    i.confluence,
                    i.latest_price as idea_latest_price,
                    i.details_json as idea_details_json
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                where f.status in ('ACTIVE','LIVE_REQUESTED')
                  and upper(f.mode) in ('PAPER','LIVE')
                  and f.qty > 0
                order by f.id desc
                """
            ).fetchall()
            now = utc_now()
            for row in rows:
                item = _row_dict(row)
                idea_details = self._decode_json(item.get("idea_details_json"))
                quality_gate = active_follow_safety_gate(
                    {
                        "action": idea_details.get("action") or item.get("signal_type"),
                        "signal_type": item.get("signal_type"),
                        "status": item.get("idea_status"),
                        "overall_score_pct": item.get("overall_score_pct"),
                        "overall_grade": item.get("overall_grade"),
                        "confluence": item.get("confluence"),
                        "data_readiness": idea_details.get("data_readiness"),
                        "hard_blocked": idea_details.get("hard_blocked"),
                        "details": idea_details,
                    }
                )
                if quality_gate.get("passed"):
                    continue
                qty = int(item.get("qty") or 0)
                entry_price = float(item.get("entry_price") or 0.0)
                latest_price = float(item.get("idea_latest_price") or item.get("latest_price") or entry_price or 0.0)
                realized_pnl = round((latest_price - entry_price) * qty, 2)
                return_pct = _return_pct(entry_price, latest_price)
                follow_details = self._decode_json(item.get("details_json"))
                follow_details["safety_exit"] = {
                    "reason": reason,
                    "quality_reason": quality_gate.get("reason"),
                    "quality_message": quality_gate.get("message"),
                    "exited_at": now,
                    "exit_price": latest_price,
                    "qty": qty,
                    "realized_pnl": realized_pnl,
                    "return_pct": return_pct,
                }
                next_status = "LIVE_EXIT_REQUESTED" if str(item.get("mode") or "").upper() == "LIVE" else "EXITED"
                conn.execute(
                    """
                    update user_idea_follows
                    set status = ?, latest_price = ?, unrealized_pnl = ?, return_pct = ?,
                        updated_at = ?, details_json = ?
                    where id = ?
                    """,
                    (
                        next_status,
                        latest_price,
                        realized_pnl,
                        return_pct,
                        now,
                        json.dumps(follow_details, default=str, separators=(",", ":")),
                        item["id"],
                    ),
                )
                item.update(
                    {
                        "status": next_status,
                        "latest_price": latest_price,
                        "unrealized_pnl": realized_pnl,
                        "return_pct": return_pct,
                        "quality_gate": quality_gate,
                    }
                )
                exited.append(item)
        return exited

    def downgrade_non_tradeable_buy_ideas(self, reason: str = "tradeability_gate_cleanup") -> list[dict[str, Any]]:
        """Move stale or weak BUY rows back to WATCH when they cannot be traded.

        A BUY row should mean a currently actionable entry. Older preserved BUY
        theses that fail today's tradeability gate remain useful, but only as
        watchlist context.
        """

        downgraded: list[dict[str, Any]] = []
        now = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                select i.*
                from signal_ideas i
                where i.signal_type = 'BUY'
                  and i.status in ('ACTIVE','MONITORING')
                  and not exists (
                      select 1
                      from user_idea_follows f
                      where f.idea_id = i.id
                        and f.status in ('ACTIVE','LIVE_REQUESTED','LIVE_EXIT_REQUESTED')
                        and upper(f.mode) in ('PAPER','LIVE')
                        and f.qty > 0
                  )
                order by i.last_seen_at desc, i.id desc
                """
            ).fetchall()
            for row in rows:
                item = _row_dict(row)
                details = self._decode_json(item.get("details_json"))
                quality_gate = fresh_buy_quality_gate(
                    {
                        "action": details.get("action") or item.get("signal_type"),
                        "signal_type": item.get("signal_type"),
                        "status": item.get("status"),
                        "overall_score_pct": item.get("overall_score_pct"),
                        "overall_grade": item.get("overall_grade"),
                        "confluence": item.get("confluence"),
                        "data_readiness": details.get("data_readiness"),
                        "hard_blocked": details.get("hard_blocked"),
                        "details": details,
                    }
                )
                if quality_gate.get("passed"):
                    continue
                details["quality_gate"] = quality_gate
                details["decision_readiness"] = "monitor_only"
                details["quality_downgrade"] = {
                    "from": "BUY",
                    "to": "WATCH",
                    "reason": quality_gate.get("reason"),
                    "message": quality_gate.get("message"),
                    "cleanup_reason": reason,
                    "downgraded_at": now,
                }
                details["latest_system_action"] = details.get("latest_system_action") or "HOLD"
                details["display_note"] = quality_gate.get("message")
                conn.execute(
                    """
                    update signal_ideas
                    set signal_type = 'WATCH',
                        status = 'WATCH',
                        last_seen_at = ?,
                        reason = ?,
                        details_json = ?
                    where id = ?
                    """,
                    (
                        now,
                        str(quality_gate.get("message") or item.get("reason") or "BUY downgraded to watch by tradeability gate.")[:1000],
                        json.dumps(details, default=str, separators=(",", ":")),
                        item["id"],
                    ),
                )
                item.update({"signal_type": "WATCH", "status": "WATCH", "quality_gate": quality_gate})
                downgraded.append(item)
        return downgraded

    def exit_user_follow_position(
        self,
        user_id: int,
        symbol: str,
        market_region: str | None = None,
        reason: str = "manual_exit",
    ) -> list[dict[str, Any]]:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return []
        market_clause, market_params = _market_region_where("u", market_region)
        market_sql = f"and {market_clause}" if market_clause else ""
        exited: list[dict[str, Any]] = []
        with self.connect() as conn:
            self._refresh_user_follow_marks(conn)
            rows = conn.execute(
                f"""
                select
                    f.*,
                    i.symbol,
                    i.latest_price as idea_latest_price,
                    {_market_region_case("u")} as market_region,
                    u.exchange
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                where f.user_id = ?
                  and upper(i.symbol) = ?
                  and f.status in ('ACTIVE','LIVE_REQUESTED')
                  and f.qty > 0
                  {market_sql}
                order by f.id desc
                """,
                (int(user_id), symbol, *market_params),
            ).fetchall()
            now = utc_now()
            for row in rows:
                item = _row_dict(row)
                qty = int(item.get("qty") or 0)
                entry_price = float(item.get("entry_price") or 0.0)
                latest_price = float(item.get("idea_latest_price") or item.get("latest_price") or entry_price or 0.0)
                realized_pnl = round((latest_price - entry_price) * qty, 2)
                return_pct = _return_pct(entry_price, latest_price)
                details = self._decode_json(item.get("details_json"))
                details["manual_exit"] = {
                    "reason": reason,
                    "exited_at": now,
                    "exit_price": latest_price,
                    "qty": qty,
                    "realized_pnl": realized_pnl,
                    "return_pct": return_pct,
                    "mode": item.get("mode"),
                }
                next_status = "LIVE_EXIT_REQUESTED" if str(item.get("mode") or "").upper() == "LIVE" else "EXITED"
                conn.execute(
                    """
                    update user_idea_follows
                    set status = ?, latest_price = ?, unrealized_pnl = ?, return_pct = ?,
                        updated_at = ?, details_json = ?
                    where id = ?
                    """,
                    (
                        next_status,
                        latest_price,
                        realized_pnl,
                        return_pct,
                        now,
                        json.dumps(details, default=str, separators=(",", ":")),
                        item["id"],
                    ),
                )
                item.update(
                    {
                        "status": next_status,
                        "latest_price": latest_price,
                        "unrealized_pnl": realized_pnl,
                        "return_pct": return_pct,
                        "updated_at": now,
                    }
                )
                exited.append(item)
        return exited

    def recent_user_symbol_exit(
        self,
        user_id: int,
        symbol: str,
        cooldown_hours: int = 24,
    ) -> dict[str, Any] | None:
        symbol = str(symbol or "").strip().upper()
        if not symbol or cooldown_hours <= 0:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(int(cooldown_hours), 1))
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    f.*,
                    i.symbol,
                    i.first_seen_at as idea_first_seen_at,
                    i.last_seen_at as idea_last_seen_at
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                where f.user_id = ?
                  and upper(i.symbol) = ?
                  and f.status in ('EXITED','LIVE_EXIT_REQUESTED')
                order by f.updated_at desc, f.id desc
                limit 8
                """,
                (int(user_id), symbol),
            ).fetchall()
        for row in rows:
            item = _row_dict(row)
            details = self._decode_json(item.get("details_json"))
            management = details.get("exit_management") if isinstance(details.get("exit_management"), dict) else {}
            events = [event for event in management.get("events", []) if isinstance(event, dict)]
            latest_event = events[-1] if events else {}
            manual_exit = details.get("manual_exit") if isinstance(details.get("manual_exit"), dict) else {}
            safety_exit = details.get("safety_exit") if isinstance(details.get("safety_exit"), dict) else {}
            key = str(
                latest_event.get("key")
                or manual_exit.get("reason")
                or safety_exit.get("key")
                or ("SAFETY_EXIT" if safety_exit else "")
                or management.get("last_action")
                or ""
            ).strip()
            if key not in REENTRY_BLOCK_EXIT_KEYS:
                continue
            exited_at = (
                _parse_dt(latest_event.get("at"))
                or _parse_dt(manual_exit.get("exited_at"))
                or _parse_dt(safety_exit.get("exited_at"))
                or _parse_dt(management.get("last_action_at"))
                or _parse_dt(item.get("updated_at"))
            )
            if not exited_at or exited_at < cutoff:
                continue
            minutes_left = max(int(((exited_at + timedelta(hours=cooldown_hours)) - datetime.now(timezone.utc)).total_seconds() // 60), 0)
            return {
                "symbol": symbol,
                "follow_id": item.get("id"),
                "idea_id": item.get("idea_id"),
                "exit_key": key,
                "exit_reason": (
                    latest_event.get("reason")
                    or manual_exit.get("reason")
                    or safety_exit.get("quality_reason")
                    or safety_exit.get("reason")
                    or management.get("last_reason")
                    or key
                ),
                "exited_at": exited_at.isoformat(),
                "cooldown_hours": int(cooldown_hours),
                "cooldown_minutes_left": minutes_left,
                "return_pct": item.get("return_pct"),
                "realized_pnl": (
                    latest_event.get("realized_pnl")
                    if latest_event
                    else manual_exit.get("realized_pnl")
                    if manual_exit
                    else safety_exit.get("realized_pnl")
                ),
            }
        return None

    def manage_user_follow_exits(
        self,
        user_id: int,
        market_region: str | None = None,
        cost_settings: Any = None,
    ) -> dict[str, Any]:
        market_clause, market_params = _market_region_where("u", market_region)
        market_sql = f"and {market_clause}" if market_clause else ""
        actions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with self.connect() as conn:
            self._refresh_user_follow_marks(conn)
            rows = conn.execute(
                f"""
                select
                    f.*,
                    i.symbol,
                    i.status as idea_status,
                    i.latest_price as idea_latest_price,
                    i.entry_price as idea_entry_price,
                    i.current_return_pct,
                    i.peak_return_pct,
                    i.worst_return_pct,
                    i.details_json as idea_details_json,
                    {_market_region_case("u")} as market_region
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                where f.user_id = ?
                  and f.status in ('ACTIVE','LIVE_REQUESTED')
                  and f.qty > 0
                  {market_sql}
                order by f.updated_at desc, f.id desc
                """,
                (int(user_id), *market_params),
            ).fetchall()
            now = utc_now()
            for row in rows:
                item = _row_dict(row)
                idea_details = self._decode_json(item.get("idea_details_json"))
                follow_details = self._decode_json(item.get("details_json"))
                action = _paper_exit_action(item, idea_details, follow_details)
                if not action:
                    continue
                qty = int(item.get("qty") or 0)
                entry_price = float(item.get("entry_price") or item.get("idea_entry_price") or 0.0)
                latest_price = float(item.get("idea_latest_price") or item.get("latest_price") or entry_price or 0.0)
                mode = str(item.get("mode") or "TRACK").upper()
                event_key = str(action["key"])
                exit_pct = float(action.get("exit_pct") or 100.0)
                exit_qty = qty if action.get("full") else max(1, int(round(qty * max(min(exit_pct, 100.0), 0.0) / 100.0)))
                exit_qty = min(qty, exit_qty)
                if exit_qty <= 0:
                    continue
                remaining_qty = max(qty - exit_qty, 0)
                realized_pnl = round((latest_price - entry_price) * exit_qty, 2)
                remaining_invested = round(remaining_qty * entry_price, 2)
                remaining_unrealized = round((latest_price - entry_price) * remaining_qty, 2)
                return_pct = _return_pct(entry_price, latest_price)
                management = follow_details.setdefault("exit_management", {})
                economics = exit_economics(entry_price, latest_price, exit_qty, item.get("market_region"), cost_settings)
                if should_block_low_value_profit_exit(event_key, economics):
                    skip_reason = (
                        "Skipped low-value profit exit: estimated net P&L "
                        f"{economics.get('estimated_net_pnl')} is below required "
                        f"{economics.get('minimum_net_profit')} after brokerage, taxes, slippage, and spread."
                    )
                    skip_event = {
                        "key": event_key,
                        "action": "SKIP",
                        "label": "Skip Low-Value Exit",
                        "reason": skip_reason,
                        "at": now,
                        "mode": mode,
                        "qty_before": qty,
                        "proposed_exit_qty": exit_qty,
                        "entry_price": entry_price,
                        "exit_price": latest_price,
                        "return_pct": return_pct,
                        "economics": economics,
                    }
                    management["last_skipped_action"] = skip_event
                    management["last_skip_reason"] = skip_reason
                    management["last_skip_at"] = now
                    conn.execute(
                        """
                        update user_idea_follows
                        set latest_price = ?, unrealized_pnl = ?, return_pct = ?, updated_at = ?, details_json = ?
                        where id = ?
                        """,
                        (
                            latest_price,
                            round((latest_price - entry_price) * qty, 2),
                            return_pct,
                            now,
                            json.dumps(follow_details, default=str, separators=(",", ":")),
                            item["id"],
                        ),
                    )
                    skipped.append(
                        {
                            "follow_id": item.get("id"),
                            "idea_id": item.get("idea_id"),
                            "symbol": item.get("symbol"),
                            "market_region": item.get("market_region"),
                            "mode": mode,
                            "action": "SKIP",
                            "label": "Skip Low-Value Exit",
                            "reason": skip_reason,
                            "qty_before": qty,
                            "proposed_exit_qty": exit_qty,
                            "exit_price": latest_price,
                            "return_pct": return_pct,
                            "economics": economics,
                        }
                    )
                    continue
                events = management.setdefault("events", [])
                events.append(
                    {
                        "key": event_key,
                        "action": action.get("action"),
                        "reason": action.get("reason"),
                        "at": now,
                        "mode": mode,
                        "qty_before": qty,
                        "exit_qty": exit_qty,
                        "remaining_qty": remaining_qty,
                        "entry_price": entry_price,
                        "exit_price": latest_price,
                        "realized_pnl": realized_pnl,
                        "return_pct": return_pct,
                        "economics": economics,
                    }
                )
                management["last_action"] = action.get("action")
                management["last_action_label"] = action.get("label")
                management["last_reason"] = action.get("reason")
                management["last_action_at"] = now
                management["realized_pnl_total"] = round(float(management.get("realized_pnl_total") or 0.0) + realized_pnl, 2)
                management["closed_qty_total"] = int(management.get("closed_qty_total") or 0) + exit_qty
                next_status = "EXITED" if remaining_qty <= 0 or action.get("full") else "ACTIVE"
                if mode == "LIVE":
                    next_status = "LIVE_EXIT_REQUESTED"
                    management["live_note"] = "Live exit needs broker order confirmation; OpenStocks only records the guarded request."
                conn.execute(
                    """
                    update user_idea_follows
                    set status = ?, qty = ?, latest_price = ?, invested_amount = ?,
                        unrealized_pnl = ?, return_pct = ?, updated_at = ?, details_json = ?
                    where id = ?
                    """,
                    (
                        next_status,
                        remaining_qty if mode != "LIVE" else qty,
                        latest_price,
                        remaining_invested if mode != "LIVE" else float(item.get("invested_amount") or 0.0),
                        remaining_unrealized if mode != "LIVE" else round((latest_price - entry_price) * qty, 2),
                        return_pct,
                        now,
                        json.dumps(follow_details, default=str, separators=(",", ":")),
                        item["id"],
                    ),
                )
                action_row = {
                    "follow_id": item.get("id"),
                    "idea_id": item.get("idea_id"),
                    "symbol": item.get("symbol"),
                    "market_region": item.get("market_region"),
                    "mode": mode,
                    "status": next_status,
                    "action": action.get("action"),
                    "label": action.get("label"),
                    "reason": action.get("reason"),
                    "qty_before": qty,
                    "exit_qty": exit_qty,
                    "remaining_qty": remaining_qty if mode != "LIVE" else qty,
                    "exit_price": latest_price,
                    "return_pct": return_pct,
                    "realized_pnl": realized_pnl,
                }
                actions.append(action_row)
        return {
            "checked": len(rows),
            "actions": actions,
            "action_count": len(actions),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    def _refresh_user_follow_marks(self, conn: sqlite3.Connection, symbols: Iterable[str] | None = None) -> None:
        symbol_values = sorted({str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
        symbol_sql = ""
        params: list[Any] = []
        if symbol_values:
            placeholders = ",".join("?" for _ in symbol_values)
            symbol_sql = f"and upper(i.symbol) in ({placeholders})"
            params.extend(symbol_values)
        rows = conn.execute(
            f"""
            select f.id, f.qty, f.entry_price, f.invested_amount, f.updated_at, f.details_json,
                i.latest_price, q.price as quote_price, q.ts as quote_ts
            from user_idea_follows f
            join signal_ideas i on i.id = f.idea_id
            left join latest_quotes q on q.symbol = i.symbol
            where f.status in ('ACTIVE','LIVE_REQUESTED')
              {symbol_sql}
            """,
            params,
        ).fetchall()
        now = utc_now()
        for row in rows:
            if not symbol_values:
                quote_dt = _parse_dt(row["quote_ts"])
                updated_dt = _parse_dt(row["updated_at"])
                if not quote_dt or (updated_dt and quote_dt <= updated_dt):
                    continue
            latest_price = float(row["quote_price"] or row["latest_price"] or row["entry_price"] or 0.0)
            entry_price = float(row["entry_price"] or latest_price or 0.0)
            qty = int(row["qty"] or 0)
            invested = float(row["invested_amount"] or (qty * entry_price))
            pnl = (latest_price - entry_price) * qty
            return_pct = _return_pct(entry_price, latest_price)
            details = self._decode_json(row["details_json"])
            mark_state = details.setdefault("mark_state", {})
            previous_peak = _optional_float(mark_state.get("peak_return_pct"))
            previous_worst = _optional_float(mark_state.get("worst_return_pct"))
            mark_state["peak_return_pct"] = round(max(return_pct, previous_peak if previous_peak is not None else return_pct), 4)
            mark_state["worst_return_pct"] = round(min(return_pct, previous_worst if previous_worst is not None else return_pct), 4)
            mark_state["last_mark_at"] = now
            conn.execute(
                """
                update user_idea_follows
                set latest_price = ?, invested_amount = ?, unrealized_pnl = ?, return_pct = ?, updated_at = ?, details_json = ?
                where id = ?
                """,
                (latest_price, invested, round(pnl, 2), return_pct, now, json.dumps(details, default=str, separators=(",", ":")), row["id"]),
            )

    def insert_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        status: str,
        reason: str,
        strategy: str = "unknown",
        details_json: str = "{}",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into orders (ts, symbol, side, strategy, qty, price, notional, status, reason, details_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), symbol, side, strategy, qty, price, qty * price, status, reason, details_json),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("select value from agent_state where key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value)
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_state (key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (key, encoded),
            )

    def upsert_tomorrow_plan(self, plan: dict[str, Any]) -> None:
        market_region = str(plan.get("market_region") or "IN").upper()
        plan_date = str(plan.get("plan_date") or "")[:32]
        prepared_at = str(plan.get("prepared_at") or utc_now())
        if not plan_date:
            return
        rows = []
        for item in plan.get("items") or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            section = str(item.get("section") or "").strip().lower()
            if not symbol or not section:
                continue
            details_payload = item.get("details") if isinstance(item.get("details"), dict) else {}
            if item.get("name") and not details_payload.get("name"):
                details_payload = {**details_payload, "name": item.get("name")}
            if item.get("idea_id") and not details_payload.get("idea_id"):
                details_payload = {**details_payload, "idea_id": item.get("idea_id")}
            rows.append(
                {
                    "plan_date": plan_date,
                    "market_region": market_region,
                    "prepared_at": prepared_at,
                    "section": section,
                    "section_rank": int(_optional_int(item.get("section_rank")) or 0),
                    "sort_order": int(_optional_int(item.get("sort_order")) or 0),
                    "symbol": symbol,
                    "action": str(item.get("action") or ""),
                    "trigger_price": _optional_float(item.get("trigger_price")),
                    "max_entry": _optional_float(item.get("max_entry")),
                    "stop_loss": _optional_float(item.get("stop_loss")),
                    "target1": _optional_float(item.get("target1")),
                    "score": _optional_float(item.get("score")) or 0.0,
                    "confidence": _optional_float(item.get("confidence")) or 0.0,
                    "strategy": str(item.get("strategy") or ""),
                    "rationale": str(item.get("rationale") or "")[:1000],
                    "validation": str(item.get("validation") or "")[:1000],
                    "details_json": json.dumps(details_payload, default=str, separators=(",", ":")),
                }
            )
        with self.connect() as conn:
            conn.execute(
                "delete from tomorrow_plan_items where plan_date = ? and market_region = ?",
                (plan_date, market_region),
            )
            if rows:
                conn.executemany(
                    """
                    insert into tomorrow_plan_items (
                        plan_date, market_region, prepared_at, section, section_rank, sort_order,
                        symbol, action, trigger_price, max_entry, stop_loss, target1,
                        score, confidence, strategy, rationale, validation, details_json
                    )
                    values (
                        :plan_date, :market_region, :prepared_at, :section, :section_rank, :sort_order,
                        :symbol, :action, :trigger_price, :max_entry, :stop_loss, :target1,
                        :score, :confidence, :strategy, :rationale, :validation, :details_json
                    )
                    on conflict(plan_date, market_region, section, symbol) do update set
                        prepared_at = excluded.prepared_at,
                        section_rank = excluded.section_rank,
                        sort_order = excluded.sort_order,
                        action = excluded.action,
                        trigger_price = excluded.trigger_price,
                        max_entry = excluded.max_entry,
                        stop_loss = excluded.stop_loss,
                        target1 = excluded.target1,
                        score = excluded.score,
                        confidence = excluded.confidence,
                        strategy = excluded.strategy,
                        rationale = excluded.rationale,
                        validation = excluded.validation,
                        details_json = excluded.details_json
                    """,
                    rows,
                )
        state = self.get_state("tomorrow_plan_context", {}) or {}
        by_market = state.get("by_market") if isinstance(state, dict) and isinstance(state.get("by_market"), dict) else {}
        by_market[market_region] = plan
        merged = {
            "enabled": True,
            "updated_at": utc_now(),
            "latest_market_region": market_region,
            "by_market": by_market,
        }
        merged.update(plan)
        merged["by_market"] = by_market
        self.set_state("tomorrow_plan_context", merged)

    def latest_tomorrow_plan(self, market_region: str | None = None) -> dict[str, Any]:
        region = normalize_market_region(market_region or "BOTH", default="BOTH")
        state = self.get_state("tomorrow_plan_context", {}) or {}
        by_market = state.get("by_market") if isinstance(state, dict) and isinstance(state.get("by_market"), dict) else {}
        if region in {"IN", "US"}:
            if by_market.get(region):
                return by_market[region]
            return self._tomorrow_plan_from_rows(region)
        if by_market:
            return state
        in_plan = self._tomorrow_plan_from_rows("IN")
        us_plan = self._tomorrow_plan_from_rows("US")
        return {
            "enabled": bool(in_plan.get("items") or us_plan.get("items")),
            "market_region": "BOTH",
            "by_market": {"IN": in_plan, "US": us_plan},
            "updated_at": max(str(in_plan.get("prepared_at") or ""), str(us_plan.get("prepared_at") or "")) or None,
        }

    def _tomorrow_plan_from_rows(self, market_region: str) -> dict[str, Any]:
        with self.connect() as conn:
            latest = conn.execute(
                "select max(plan_date) as plan_date from tomorrow_plan_items where market_region = ?",
                (market_region,),
            ).fetchone()
            plan_date = latest["plan_date"] if latest else None
            if not plan_date:
                return {"enabled": False, "market_region": market_region, "items": [], "sections": {}, "summary": {}}
            rows = conn.execute(
                """
                select *
                from tomorrow_plan_items
                where market_region = ? and plan_date = ?
                order by sort_order asc, score desc
                """,
                (market_region, plan_date),
            ).fetchall()
        items: list[dict[str, Any]] = []
        sections: dict[str, list[dict[str, Any]]] = {}
        prepared_at = None
        for row in rows:
            item = _row_dict(row)
            prepared_at = prepared_at or item.get("prepared_at")
            item["details"] = self._decode_json(item.pop("details_json", "{}"))
            if isinstance(item["details"], dict):
                item["name"] = item["details"].get("name") or item.get("symbol")
                item["idea_id"] = item["details"].get("idea_id")
            items.append(item)
            sections.setdefault(str(item.get("section") or ""), []).append(item)
        return {
            "enabled": True,
            "market_region": market_region,
            "plan_date": plan_date,
            "prepared_at": prepared_at,
            "items": items,
            "sections": sections,
            "summary": {section: len(values) for section, values in sections.items()},
        }

    def get_pattern_state(self, symbol: str, pattern: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute(
                "select state_json from pattern_states where symbol = ? and pattern = ?",
                (str(symbol or "").upper(), pattern),
            ).fetchone()
        if row is None:
            return default
        return self._decode_json(row["state_json"])

    def upsert_pattern_state(self, symbol: str, pattern: str, state: dict[str, Any]) -> None:
        if not symbol or not pattern:
            return
        with self.connect() as conn:
            conn.execute(
                """
                insert into pattern_states (symbol, pattern, state_json, updated_at)
                values (?, ?, ?, ?)
                on conflict(symbol, pattern) do update set
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (str(symbol).upper(), pattern, json.dumps(state, default=str), utc_now()),
            )

    def runtime_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("select key, value from runtime_settings").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
        return settings

    def update_runtime_settings(self, values: dict[str, Any]) -> None:
        if not values:
            return
        rows = [(key, json.dumps(value), utc_now()) for key, value in values.items()]
        with self.connect() as conn:
            conn.executemany(
                """
                insert into runtime_settings (key, value, updated_at)
                values (?, ?, ?)
                on conflict(key) do update set
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def insert_agent_log(
        self,
        level: str,
        component: str,
        event: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_logs (ts, level, component, event, message, details_json)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    level.upper(),
                    component,
                    event,
                    message,
                    json.dumps(details or {}, default=str, separators=(",", ":")),
                ),
            )
            conn.execute(
                """
                delete from agent_logs
                where id not in (
                    select id from agent_logs order by id desc limit 2000
                )
                """
            )

    def insert_llm_usage(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into llm_usage_events (
                    ts, component, purpose, provider, model, prompt_tokens,
                    completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens,
                    estimated_tokens, input_chars, output_chars, cost_usd, latency_ms, user_id, scope_id, details_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("ts") or utc_now(),
                    event.get("component") or "llm",
                    event.get("purpose") or "chat",
                    event.get("provider") or "unknown",
                    event.get("model") or "unknown",
                    int(event.get("prompt_tokens") or 0),
                    int(event.get("completion_tokens") or 0),
                    int(event.get("total_tokens") or 0),
                    int(event.get("cache_hit_tokens") or 0),
                    int(event.get("cache_miss_tokens") or 0),
                    1 if event.get("estimated_tokens") else 0,
                    int(event.get("input_chars") or 0),
                    int(event.get("output_chars") or 0),
                    float(event.get("cost_usd") or 0),
                    int(event.get("latency_ms") or 0),
                    _optional_int(event.get("user_id")),
                    str(event.get("scope_id") or ""),
                    json.dumps(event.get("details") or {}, default=str, separators=(",", ":")),
                ),
            )

    def llm_usage_summary(self, recent_limit: int = 25) -> dict[str, Any]:
        def aggregate(conn: sqlite3.Connection, where: str = "", params: tuple[Any, ...] = ()) -> dict[str, Any]:
            row = conn.execute(
                f"""
                select
                    count(*) as calls,
                    coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                    coalesce(sum(completion_tokens), 0) as completion_tokens,
                    coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cache_hit_tokens), 0) as cache_hit_tokens,
                    coalesce(sum(cache_miss_tokens), 0) as cache_miss_tokens,
                    coalesce(sum(estimated_tokens), 0) as estimated_calls,
                    coalesce(sum(input_chars), 0) as input_chars,
                    coalesce(sum(output_chars), 0) as output_chars,
                    coalesce(sum(cost_usd), 0) as cost_usd,
                    coalesce(avg(latency_ms), 0) as avg_latency_ms
                from llm_usage_events
                {where}
                """,
                params,
            ).fetchone()
            return {
                "calls": int(row["calls"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
                "cache_miss_tokens": int(row["cache_miss_tokens"] or 0),
                "estimated_calls": int(row["estimated_calls"] or 0),
                "input_chars": int(row["input_chars"] or 0),
                "output_chars": int(row["output_chars"] or 0),
                "cost_usd": round(float(row["cost_usd"] or 0), 8),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
            }

        today = utc_now()[:10]
        with self.connect() as conn:
            today_summary = aggregate(conn, "where substr(ts, 1, 10) = ?", (today,))
            all_time_summary = aggregate(conn)
            recent_rows = conn.execute(
                """
                select id, ts, component, purpose, provider, model, prompt_tokens,
                    completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens,
                    estimated_tokens, input_chars, output_chars, cost_usd, latency_ms, user_id, details_json
                from llm_usage_events
                order by id desc
                limit ?
                """,
                (recent_limit,),
            ).fetchall()
            by_purpose_rows = conn.execute(
                """
                select purpose, count(*) as calls, coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cost_usd), 0) as cost_usd
                from llm_usage_events
                where substr(ts, 1, 10) = ?
                group by purpose
                order by cost_usd desc
                """,
                (today,),
            ).fetchall()
            by_model_rows = conn.execute(
                """
                select model, count(*) as calls, coalesce(sum(total_tokens), 0) as total_tokens,
                    coalesce(sum(cost_usd), 0) as cost_usd
                from llm_usage_events
                where substr(ts, 1, 10) = ?
                group by model
                order by cost_usd desc
                """,
                (today,),
            ).fetchall()

        return {
            "updated_at": utc_now(),
            "token_rule": "fallback estimate uses tokens = english_characters * 0.3",
            "currency": "USD",
            "today_utc": today_summary,
            "all_time": all_time_summary,
            "by_purpose_today": [
                {
                    "purpose": row["purpose"],
                    "calls": int(row["calls"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                }
                for row in by_purpose_rows
            ],
            "by_model_today": [
                {
                    "model": row["model"],
                    "calls": int(row["calls"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                }
                for row in by_model_rows
            ],
            "recent": [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "component": row["component"],
                    "purpose": row["purpose"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "cache_hit_tokens": row["cache_hit_tokens"],
                    "cache_miss_tokens": row["cache_miss_tokens"],
                    "estimated_tokens": bool(row["estimated_tokens"]),
                    "input_chars": row["input_chars"],
                    "output_chars": row["output_chars"],
                    "cost_usd": round(float(row["cost_usd"] or 0), 8),
                    "latency_ms": row["latency_ms"],
                    "user_id": row["user_id"],
                    "details": self._decode_json(row["details_json"]),
                }
                for row in recent_rows
            ],
        }

    def latest_agent_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from agent_logs
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_data_retention(self, policy: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
        policy = policy or {}
        enabled = bool(policy.get("enabled", True))
        if not enabled:
            return {"ran": False, "reason": "disabled"}

        now = datetime.now(timezone.utc)
        interval_hours = max(int(policy.get("interval_hours", 168) or 168), 1)
        last_run = self.get_state("db_maintenance_last_run_at")
        last_dt = _parse_dt(last_run)
        if not force and last_dt and now - last_dt < timedelta(hours=interval_hours):
            return {
                "ran": False,
                "reason": "not_due",
                "last_run_at": last_dt.isoformat(),
                "next_due_at": (last_dt + timedelta(hours=interval_hours)).isoformat(),
            }

        full_audit_keep_latest = max(int(policy.get("full_audit_keep_latest", 500) or 0), 0)
        hold_days = max(int(policy.get("hold_decision_days", 7) or 7), 1)
        full_audit_days = max(int(policy.get("full_audit_days", 30) or 30), 1)
        market_tick_days = max(int(policy.get("market_tick_days", 7) or 7), 1)
        sentiment_days = max(int(policy.get("sentiment_days", 30) or 30), 1)
        llm_usage_days = max(int(policy.get("llm_usage_days", 180) or 180), 1)
        delivery_days = max(int(policy.get("delivery_days", 90) or 90), 20)
        candle_rows = max(int(policy.get("candle_rows_per_symbol_source", 320) or 320), 30)
        compact_threshold = max(int(policy.get("compact_threshold_chars", 5000) or 5000), 1000)
        vacuum_enabled = bool(policy.get("vacuum", True))

        cutoffs = {
            "hold": (now - timedelta(days=hold_days)).isoformat(),
            "full_audit": (now - timedelta(days=full_audit_days)).isoformat(),
            "market_ticks": (now - timedelta(days=market_tick_days)).isoformat(),
            "sentiment": (now - timedelta(days=sentiment_days)).isoformat(),
            "llm_usage": (now - timedelta(days=llm_usage_days)).isoformat(),
            "delivery": (now - timedelta(days=delivery_days)).date().isoformat(),
        }

        before = self.storage_summary()
        deleted: dict[str, int] = {}
        compacted = 0
        with self.connect() as conn:
            cursor = conn.execute("delete from market_ticks where ts < ?", (cutoffs["market_ticks"],))
            deleted["market_ticks"] = max(cursor.rowcount, 0)
            cursor = conn.execute("delete from sentiment_events where ts < ?", (cutoffs["sentiment"],))
            deleted["sentiment_events"] = max(cursor.rowcount, 0)
            cursor = conn.execute("delete from llm_usage_events where ts < ?", (cutoffs["llm_usage"],))
            deleted["llm_usage_events"] = max(cursor.rowcount, 0)
            cursor = conn.execute("delete from delivery_data where date < ?", (cutoffs["delivery"],))
            deleted["delivery_data"] = max(cursor.rowcount, 0)
            cursor = conn.execute(
                """
                delete from candles
                where rowid in (
                    select rowid from (
                        select rowid,
                            row_number() over (partition by symbol, source order by ts desc) as rn
                        from candles
                    )
                    where rn > ?
                )
                """,
                (candle_rows,),
            )
            deleted["candles"] = max(cursor.rowcount, 0)
            cursor = conn.execute(
                """
                delete from decisions
                where action = 'HOLD'
                    and ts < ?
                    and id not in (select id from decisions order by id desc limit ?)
                    and not exists (
                        select 1 from signal_ideas s
                        where s.decision_id = decisions.id
                           or s.latest_decision_id = decisions.id
                    )
                """,
                (cutoffs["hold"], full_audit_keep_latest),
            )
            deleted["hold_decisions"] = max(cursor.rowcount, 0)

            while True:
                rows = conn.execute(
                    """
                    select id, ts, symbol, action, strategy, confidence, price,
                        technical_score, sentiment_score, reason, details_json
                    from decisions
                    where length(details_json) > ?
                        and (
                            (action = 'HOLD' and details_json not like '%"llm_prompt_audit"%')
                            or ts < ?
                        )
                        and id not in (select id from decisions order by id desc limit ?)
                    order by id asc
                    limit 250
                    """,
                    (compact_threshold, cutoffs["full_audit"], full_audit_keep_latest),
                ).fetchall()
                if not rows:
                    break
                updates = []
                for row in rows:
                    raw = row["details_json"] or "{}"
                    parsed = _json_load(raw)
                    if isinstance(parsed, dict) and parsed.get("storage_compacted") and len(raw) <= 12000:
                        continue
                    updates.append((_compact_decision_details(dict(row), raw), row["id"]))
                if not updates:
                    compact_threshold = 12000
                    continue
                conn.executemany("update decisions set details_json = ? where id = ?", updates)
                compacted += len(updates)

            cursor = conn.execute(
                """
                delete from signal_ideas
                where status not in ('ACTIVE','WATCH','MONITORING')
                    and last_seen_at < ?
                    and not exists (
                        select 1 from user_idea_follows f
                        where f.idea_id = signal_ideas.id
                    )
                """,
                ((now - timedelta(days=180)).isoformat(),),
            )
            deleted["inactive_signal_ideas"] = max(cursor.rowcount, 0)
            conn.execute(
                """
                insert into agent_state (key, value) values ('db_maintenance_last_run_at', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (json.dumps(now.isoformat()),),
            )

        vacuumed = False
        if vacuum_enabled and (compacted or any(deleted.values())):
            self.vacuum()
            vacuumed = True
        after = self.storage_summary()
        summary = {
            "ran": True,
            "ran_at": now.isoformat(),
            "deleted": deleted,
            "compacted_decisions": compacted,
            "vacuumed": vacuumed,
            "before": before,
            "after": after,
            "policy": {
                "hold_decision_days": hold_days,
                "full_audit_keep_latest": full_audit_keep_latest,
                "full_audit_days": full_audit_days,
                "market_tick_days": market_tick_days,
                "sentiment_days": sentiment_days,
                "delivery_days": delivery_days,
                "candle_rows_per_symbol_source": candle_rows,
            },
        }
        self.insert_agent_log(
            "INFO",
            "maintenance",
            "db_retention_complete",
            "Database retention maintenance completed",
            summary,
        )
        return summary

    def storage_summary(self) -> dict[str, Any]:
        file_bytes = self.path.stat().st_size if self.path.exists() else 0
        with self.connect() as conn:
            page_size = int(conn.execute("pragma page_size").fetchone()[0])
            page_count = int(conn.execute("pragma page_count").fetchone()[0])
            freelist_count = int(conn.execute("pragma freelist_count").fetchone()[0])
        return {
            "path": str(self.path),
            "file_bytes": file_bytes,
            "file_mb": round(file_bytes / 1024 / 1024, 3),
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "reclaimable_mb": round((page_size * freelist_count) / 1024 / 1024, 3),
            "active_mb": round((page_size * max(page_count - freelist_count, 0)) / 1024 / 1024, 3),
        }

    def vacuum(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("vacuum")
                conn.execute("pragma optimize")
            finally:
                conn.close()

    def reset_trading_ledger(self, cash: float) -> None:
        cash_by_market = {"IN": float(cash), "US": float(cash)}
        with self.connect() as conn:
            conn.execute("delete from decisions")
            conn.execute("delete from orders")
            conn.execute("delete from positions")
            conn.execute("delete from portfolio_snapshots")
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash_by_market', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (json.dumps(cash_by_market),),
            )
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (json.dumps(sum(cash_by_market.values())),),
            )

    def latest_quotes(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select q.*, {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name
                from latest_quotes q
                join universe u on u.symbol = q.symbol
                where u.enabled = 1
                order by q.symbol
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_decisions(
        self,
        limit: int = 80,
        market_region: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 80), 500))
        fetch_limit = max(limit, min(limit * 4, 1000))
        where_sql, params = _market_region_where("u", market_region)
        clauses: list[str] = []
        if where_sql:
            clauses.append(where_sql)
        symbol_params: list[str] = []
        if symbols is not None:
            symbol_params = _normalize_monitor_symbols(symbols)
            if not symbol_params:
                return []
            clauses.append(f"upper(d.symbol) in ({','.join('?' for _ in symbol_params)})")
        where_clause = f"where {' and '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select d.*, {_market_region_case("u")} as market_region
                from decisions d
                left join universe u on u.symbol = d.symbol
                {where_clause}
                order by d.id desc
                limit ?
                """,
                (*params, *symbol_params, fetch_limit),
            ).fetchall()
        return current_decision_rows(dict(row) for row in rows)[:limit]

    def latest_decision_summaries(
        self,
        limit: int = 80,
        market_region: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 80), 500))
        fetch_limit = max(limit, min(limit * 4, 1000))
        where_sql, params = _market_region_where("u", market_region)
        clauses: list[str] = []
        if where_sql:
            clauses.append(where_sql)
        symbol_params: list[str] = []
        if symbols is not None:
            symbol_params = _normalize_monitor_symbols(symbols)
            if not symbol_params:
                return []
            clauses.append(f"upper(d.symbol) in ({','.join('?' for _ in symbol_params)})")
        where_clause = f"where {' and '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select d.id, d.ts, d.symbol, d.action, d.strategy, d.confidence, d.price,
                    d.technical_score, d.sentiment_score, d.reason, d.details_json,
                    u.name as company_name,
                    {_market_region_case("u")} as market_region
                from decisions d
                left join universe u on u.symbol = d.symbol
                {where_clause}
                order by d.id desc
                limit ?
                """,
                (*params, *symbol_params, fetch_limit),
            ).fetchall()
        ranked_rows = current_decision_rows(dict(row) for row in rows)[:limit]
        for row in ranked_rows:
            row.pop("details_json", None)
        return ranked_rows

    def search_decision_summaries(
        self,
        query: str,
        limit: int = 120,
        market_region: str | None = None,
    ) -> list[dict[str, Any]]:
        term = str(query or "").strip()
        if not term:
            return []
        limit = max(1, min(int(limit or 120), 300))
        like = f"%{term.upper()}%"
        market_sql, market_params = _market_region_where("u", market_region)
        clauses = []
        params: list[Any] = []
        if market_sql:
            clauses.append(market_sql)
            params.extend(market_params)
        clauses.append(
            """
            (
                upper(coalesce(d.symbol,'')) like ?
                or upper(coalesce(u.name,'')) like ?
                or upper(coalesce(d.action,'')) like ?
                or upper(coalesce(d.strategy,'')) like ?
                or upper(coalesce(d.reason,'')) like ?
            )
            """
        )
        params.extend([like, like, like, like, like])
        where_clause = f"where {' and '.join(clauses)}"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select d.id, d.ts, d.symbol, d.action, d.strategy, d.confidence, d.price,
                    d.technical_score, d.sentiment_score, d.reason, d.details_json,
                    u.name as company_name,
                    {_market_region_case("u")} as market_region
                from decisions d
                left join universe u on u.symbol = d.symbol
                {where_clause}
                order by d.id desc
                limit ?
                """,
                (*params, limit),
            ).fetchall()
        output = [dict(row) for row in rows]
        for row in output:
            row.pop("details_json", None)
        return output

    def decision_by_id(self, decision_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select d.*, {_market_region_case("u")} as market_region
                from decisions d
                left join universe u on u.symbol = d.symbol
                where d.id = ?
                """,
                (decision_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_orders(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select o.*, {_market_region_case("u")} as market_region
                from orders o
                left join universe u on u.symbol = o.symbol
                order by o.id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_order_summaries(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select o.id, o.ts, o.symbol, o.side, o.strategy, o.qty, o.price, o.notional,
                    o.status, o.reason, {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name
                from orders o
                left join universe u on u.symbol = o.symbol
                order by o.id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def order_by_id(self, order_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select o.*, {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name
                from orders o
                left join universe u on u.symbol = o.symbol
                where o.id = ?
                """,
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def cancel_order(self, order_id: int, reason: str = "cancelled by user") -> dict[str, Any]:
        open_statuses = {"OPEN", "PENDING", "SUBMITTED", "WORKING", "REQUESTED", "ACCEPTED", "PARTIAL"}
        with self.connect() as conn:
            row = conn.execute(
                "select id, status from orders where id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("order not found")
            status = str(row["status"] or "").strip().upper()
            if status not in open_statuses:
                raise ValueError("only open orders can be cancelled")
            conn.execute(
                """
                update orders
                set status = 'cancelled',
                    reason = ?
                where id = ?
                """,
                (reason, order_id),
            )
        cancelled = self.order_by_id(order_id)
        if cancelled is None:
            raise ValueError("order not found after cancel")
        return cancelled

    def positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select p.*, {_market_region_case("u")} as market_region,
                    u.exchange as exchange,
                    u.name as company_name,
                    u.sector as sector
                from positions p
                left join universe u on u.symbol = p.symbol
                where p.qty != 0
                order by p.symbol
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_portfolio(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from portfolio_snapshots order by id desc limit 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_equity(self, limit: int = 120) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select ts, equity from portfolio_snapshots order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def strategy_metrics(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            open_rows = conn.execute(
                """
                select
                    strategy,
                    count(*) as open_positions,
                    sum(qty * market_price) as exposure,
                    sum((market_price - avg_price) * qty) as unrealized_pnl
                from positions
                where qty > 0
                group by strategy
                """
            ).fetchall()
            realized_rows = conn.execute(
                """
                select strategy, sum(realized_pnl) as realized_pnl
                from positions
                group by strategy
                """
            ).fetchall()
            order_rows = conn.execute(
                """
                select
                    strategy,
                    count(*) as filled_orders,
                    sum(case when side = 'BUY' then notional else 0 end) as buy_notional,
                    sum(case when side = 'SELL' then notional else 0 end) as sell_notional
                from orders
                where status = 'FILLED'
                group by strategy
                """
            ).fetchall()
        by_strategy: dict[str, dict[str, Any]] = {}
        for row in order_rows:
            strategy = row["strategy"] or "unknown"
            by_strategy[strategy] = {
                "strategy": strategy,
                "filled_orders": row["filled_orders"] or 0,
                "buy_notional": round(row["buy_notional"] or 0, 2),
                "sell_notional": round(row["sell_notional"] or 0, 2),
                "open_positions": 0,
                "exposure": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }
        for row in open_rows:
            strategy = row["strategy"] or "unknown"
            metrics = by_strategy.setdefault(
                strategy,
                {
                    "strategy": strategy,
                    "filled_orders": 0,
                    "buy_notional": 0.0,
                    "sell_notional": 0.0,
                    "open_positions": 0,
                    "exposure": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            metrics["open_positions"] = row["open_positions"] or 0
            metrics["exposure"] = round(row["exposure"] or 0, 2)
            metrics["unrealized_pnl"] = round(row["unrealized_pnl"] or 0, 2)
        for row in realized_rows:
            strategy = row["strategy"] or "unknown"
            metrics = by_strategy.setdefault(
                strategy,
                {
                    "strategy": strategy,
                    "filled_orders": 0,
                    "buy_notional": 0.0,
                    "sell_notional": 0.0,
                    "open_positions": 0,
                    "exposure": 0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            metrics["realized_pnl"] = round(row["realized_pnl"] or 0, 2)
        return sorted(by_strategy.values(), key=lambda item: abs(item["unrealized_pnl"]), reverse=True)

    def performance_summary(self, user_id: int | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            orders = conn.execute(
                """
                select
                    count(*) as total_orders,
                    sum(case when status = 'FILLED' then 1 else 0 end) as filled_orders,
                    sum(case when status = 'VETOED' then 1 else 0 end) as vetoed_orders,
                    sum(case when status = 'FILLED' and side = 'BUY' then 1 else 0 end) as buy_fills,
                    sum(case when status = 'FILLED' and side = 'SELL' then 1 else 0 end) as sell_fills,
                    sum(case when status = 'FILLED' then notional else 0 end) as filled_notional
                from orders
                """
            ).fetchone()
            positions = conn.execute(
                """
                select
                    count(*) as tracked_positions,
                    sum(case when qty > 0 then 1 else 0 end) as open_positions,
                    sum(realized_pnl) as realized_pnl,
                    sum((market_price - avg_price) * qty) as unrealized_pnl,
                    sum(case when qty = 0 and realized_pnl > 0 then 1 else 0 end) as closed_winners,
                    sum(case when qty = 0 and realized_pnl < 0 then 1 else 0 end) as closed_losers
                from positions
                """
            ).fetchone()
            equity_rows = conn.execute(
                "select equity from portfolio_snapshots order by id asc"
            ).fetchall()
        order_data = dict(orders) if orders else {}
        position_data = dict(positions) if positions else {}
        equity_values = [float(row["equity"]) for row in equity_rows if row["equity"] is not None]
        max_drawdown = 0.0
        peak = equity_values[0] if equity_values else 0.0
        for equity in equity_values:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = min(max_drawdown, (equity - peak) / peak)
        winners = int(position_data.get("closed_winners") or 0)
        losers = int(position_data.get("closed_losers") or 0)
        closed = winners + losers
        realized = float(position_data.get("realized_pnl") or 0.0)
        learning = self.post_trade_learning_summary(user_id=user_id)
        strategy_feedback = self.strategy_performance_feedback(user_id=user_id)
        return {
            "orders": {
                "total": int(order_data.get("total_orders") or 0),
                "filled": int(order_data.get("filled_orders") or 0),
                "vetoed": int(order_data.get("vetoed_orders") or 0),
                "buy_fills": int(order_data.get("buy_fills") or 0),
                "sell_fills": int(order_data.get("sell_fills") or 0),
                "filled_notional": round(float(order_data.get("filled_notional") or 0.0), 2),
            },
            "positions": {
                "tracked": int(position_data.get("tracked_positions") or 0),
                "open": int(position_data.get("open_positions") or 0),
                "closed": closed,
                "closed_winners": winners,
                "closed_losers": losers,
            },
            "pnl": {
                "realized": round(realized, 2),
                "unrealized": round(float(position_data.get("unrealized_pnl") or 0.0), 2),
                "win_rate": round(winners / closed, 4) if closed else 0.0,
                "expectancy_per_closed_trade": round(realized / closed, 2) if closed else 0.0,
                "max_drawdown_pct": round(max_drawdown * 100, 4),
            },
            "post_trade_learning": learning,
            "strategy_performance_feedback": strategy_feedback,
        }

    def strategy_performance_feedback(self, user_id: int | None = None, limit: int = 2000) -> dict[str, Any]:
        where_parts = ["f.mode in ('PAPER','LIVE')"]
        params: list[Any] = []
        if user_id is not None:
            where_parts.append("f.user_id = ?")
            params.append(int(user_id))
        where_sql = " and ".join(where_parts)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    f.id as follow_id,
                    f.user_id,
                    f.mode,
                    f.status as follow_status,
                    f.qty,
                    f.entry_price as follow_entry_price,
                    f.latest_price as follow_latest_price,
                    f.return_pct as follow_return_pct,
                    f.unrealized_pnl,
                    f.created_at as followed_at,
                    f.updated_at as follow_updated_at,
                    f.details_json as follow_details_json,
                    i.id as idea_id,
                    i.symbol,
                    i.strategy,
                    i.plan_code,
                    i.signal_type,
                    i.status as idea_status,
                    i.first_seen_at,
                    i.last_seen_at,
                    i.entry_price as idea_entry_price,
                    i.latest_price as idea_latest_price,
                    i.current_return_pct,
                    i.peak_return_pct,
                    i.worst_return_pct,
                    i.details_json as idea_details_json,
                    {_market_region_case("u")} as market_region,
                    u.exchange,
                    u.sector,
                    u.industry
                from user_idea_follows f
                join signal_ideas i on i.id = f.idea_id
                left join universe u on u.symbol = i.symbol
                where {where_sql}
                order by f.updated_at desc, f.id desc
                limit ?
                """,
                (*params, max(1, min(int(limit or 2000), 5000))),
            ).fetchall()
            plan_rows = conn.execute(
                """
                select code, name
                from strategy_plans
                where enabled = 1
                order by code
                """
            ).fetchall()

        groups = {
            "overall": _empty_performance_group("overall", "all"),
            "by_market": {},
            "by_strategy": {},
            "by_plan": {},
            "by_strategy_market": {},
        }
        recent_closed: list[dict[str, Any]] = []
        for row in rows:
            record = _performance_feedback_record(_row_dict(row))
            _add_performance_record(groups["overall"], record)
            market = record["market_region"]
            strategy = record["strategy"]
            plan = record["plan_code"]
            strategy_market = f"{strategy}|{market}"
            _add_performance_record(
                groups["by_market"].setdefault(market, _empty_performance_group("market", market)),
                record,
            )
            _add_performance_record(
                groups["by_strategy"].setdefault(strategy, _empty_performance_group("strategy", strategy)),
                record,
            )
            _add_performance_record(
                groups["by_plan"].setdefault(plan, _empty_performance_group("plan", plan)),
                record,
            )
            _add_performance_record(
                groups["by_strategy_market"].setdefault(
                    strategy_market,
                    _empty_performance_group("strategy_market", strategy_market, strategy=strategy, market_region=market),
                ),
                record,
            )
            if record["closed"] and len(recent_closed) < 20:
                recent_closed.append(_compact_performance_record(record))
        for plan_row in plan_rows:
            code = str(plan_row["code"] or "").strip()
            if code:
                group = groups["by_plan"].setdefault(code, _empty_performance_group("plan", code))
                group["name"] = plan_row["name"]

        finalized_by_market = [_finalize_performance_group(group) for group in groups["by_market"].values()]
        finalized_by_strategy = [_finalize_performance_group(group) for group in groups["by_strategy"].values()]
        finalized_by_plan = [_finalize_performance_group(group) for group in groups["by_plan"].values()]
        finalized_by_strategy_market = [_finalize_performance_group(group) for group in groups["by_strategy_market"].values()]
        return {
            "version": "phase4-performance-feedback-v1",
            "scope": "user" if user_id is not None else "all_users",
            "user_id": user_id,
            "sampled_follows": len(rows),
            "overall": _finalize_performance_group(groups["overall"]),
            "by_market": sorted(finalized_by_market, key=lambda item: (item["closed_trades"], item["expectancy_pct"]), reverse=True),
            "by_strategy": sorted(finalized_by_strategy, key=lambda item: (item["closed_trades"], item["expectancy_pct"]), reverse=True),
            "by_plan": sorted(finalized_by_plan, key=lambda item: (item["closed_trades"], item["expectancy_pct"]), reverse=True),
            "by_strategy_market": sorted(finalized_by_strategy_market, key=lambda item: (item["closed_trades"], item["expectancy_pct"]), reverse=True),
            "recent_closed_trades": recent_closed,
            "definitions": {
                "win_rate": "closed winners divided by closed trades.",
                "average_gain_loss": "average closed-trade return for winners and losers.",
                "stop_hit_rate": "closed trades exited by stop/risk-stop divided by closed trades.",
                "time_to_target_1": "hours from follow open to first T1 hit when T1 data exists.",
                "mae_mfe": "MAE/MFE use marked worst/peak return percentages while the idea or follow was active.",
                "expectancy": "average closed-trade return percentage, grouped by market, strategy, plan, and strategy-market.",
            },
        }

    def post_trade_learning_summary(self, user_id: int | None = None, limit: int = 800) -> dict[str, Any]:
        join_sql = ""
        follow_columns = "null as follow_id, '' as follow_mode, '' as follow_status, 0 as follow_return_pct"
        params: tuple[Any, ...] = (max(1, min(int(limit), 2000)),)
        if user_id is not None:
            join_sql = "left join user_idea_follows f on f.idea_id = i.id and f.user_id = ?"
            follow_columns = "f.id as follow_id, f.mode as follow_mode, f.status as follow_status, f.return_pct as follow_return_pct"
            params = (int(user_id), max(1, min(int(limit), 2000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    i.id, i.symbol, i.strategy, i.plan_code, i.signal_type, i.status,
                    i.first_seen_at, i.last_seen_at, i.current_return_pct, i.peak_return_pct,
                    i.worst_return_pct, i.details_json,
                    {_market_region_case("u")} as market_region,
                    u.sector, u.industry,
                    {follow_columns}
                from signal_ideas i
                left join universe u on u.symbol = i.symbol
                {join_sql}
                where i.status != 'REJECTED'
                order by i.last_seen_at desc, i.id desc
                limit ?
                """,
                params,
            ).fetchall()

        now = datetime.now(timezone.utc)
        total_current: list[float] = []
        total_peak: list[float] = []
        total_worst: list[float] = []
        groups: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        winners_list: list[dict[str, Any]] = []
        summary = {
            "ideas_analyzed": 0,
            "buy_ideas": 0,
            "followed_ideas": 0,
            "quick_red": 0,
            "hit_t1": 0,
            "stopped": 0,
            "failed_after_entry": 0,
        }

        for row in rows:
            item = _row_dict(row)
            details = self._decode_json(item.pop("details_json", "{}"))
            signal_type = str(item.get("signal_type") or "").upper()
            if signal_type not in {"BUY", "WATCH", "EXIT"}:
                continue
            status = str(item.get("status") or "").upper()
            lifecycle = str(details.get("lifecycle_status") or item.get("status") or "").lower()
            summary["ideas_analyzed"] += 1
            is_buy = (
                signal_type == "BUY"
                or status in {"STOP_HIT", "TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT"}
                or lifecycle in {"stopped", "target_1_hit", "target_2_hit", "target_3_hit"}
            )
            if is_buy:
                summary["buy_ideas"] += 1
            if item.get("follow_id"):
                summary["followed_ideas"] += 1
            current = _optional_float(item.get("current_return_pct")) or 0.0
            peak = _optional_float(item.get("peak_return_pct")) or current
            worst = _optional_float(item.get("worst_return_pct")) or current
            total_current.append(current)
            total_peak.append(peak)
            total_worst.append(worst)
            highest = str(details.get("highest_target_hit") or "NONE").upper()
            hit_t1 = highest in {"T1", "T2", "T3"} or status in {"TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT"}
            stopped = status == "STOP_HIT" or lifecycle == "stopped"
            first_seen = _parse_dt(item.get("first_seen_at"))
            age_hours = ((now - first_seen).total_seconds() / 3600) if first_seen else 9999.0
            quick_red = is_buy and ((age_hours <= 48 and current < -0.5) or worst <= -1.5)
            failed = is_buy and not hit_t1 and (stopped or current <= -2.0 or worst <= -3.0)
            if quick_red:
                summary["quick_red"] += 1
            if hit_t1:
                summary["hit_t1"] += 1
            if stopped:
                summary["stopped"] += 1
            if failed:
                summary["failed_after_entry"] += 1

            market = str(item.get("market_region") or "IN")
            plan = str(item.get("plan_code") or item.get("strategy") or "unclassified")
            group_key = f"{plan}|{market}"
            group = groups.setdefault(
                group_key,
                {
                    "plan_code": plan,
                    "market_region": market,
                    "ideas": 0,
                    "buy_ideas": 0,
                    "followed_ideas": 0,
                    "quick_red": 0,
                    "hit_t1": 0,
                    "stopped": 0,
                    "failed_after_entry": 0,
                    "_current": [],
                    "_peak": [],
                    "_worst": [],
                    "sample_symbols": [],
                },
            )
            group["ideas"] += 1
            group["buy_ideas"] += 1 if is_buy else 0
            group["followed_ideas"] += 1 if item.get("follow_id") else 0
            group["quick_red"] += 1 if quick_red else 0
            group["hit_t1"] += 1 if hit_t1 else 0
            group["stopped"] += 1 if stopped else 0
            group["failed_after_entry"] += 1 if failed else 0
            group["_current"].append(current)
            group["_peak"].append(peak)
            group["_worst"].append(worst)
            if len(group["sample_symbols"]) < 6:
                group["sample_symbols"].append(item.get("symbol"))

            event = {
                "symbol": item.get("symbol"),
                "plan_code": plan,
                "market_region": market,
                "current_return_pct": round(current, 4),
                "peak_return_pct": round(peak, 4),
                "worst_return_pct": round(worst, 4),
                "highest_target_hit": highest,
                "status": status,
                "last_seen_at": item.get("last_seen_at"),
            }
            if failed:
                event["lesson"] = "Failed after entry before T1; review entry freshness, stop distance, and adverse move threshold."
                failures.append(event)
            elif quick_red:
                event["lesson"] = "Went red quickly; reduce size unless price is still inside entry zone with fresh confirmation."
                failures.append(event)
            if hit_t1 or peak >= 2.0:
                event["lesson"] = "Reached T1 or meaningful MFE; check whether partial exit/trailing stop captured the move."
                winners_list.append(event)

        def avg(values: list[float]) -> float:
            return round(sum(values) / len(values), 4) if values else 0.0

        by_strategy_market: list[dict[str, Any]] = []
        for group in groups.values():
            buy_count = max(int(group["buy_ideas"]), 1)
            current_values = group.pop("_current")
            peak_values = group.pop("_peak")
            worst_values = group.pop("_worst")
            group["avg_current_return_pct"] = avg(current_values)
            group["avg_mfe_pct"] = avg(peak_values)
            group["avg_mae_pct"] = avg(worst_values)
            group["t1_rate"] = round(float(group["hit_t1"]) / buy_count, 4)
            group["quick_red_rate"] = round(float(group["quick_red"]) / buy_count, 4)
            group["failure_rate"] = round(float(group["failed_after_entry"]) / buy_count, 4)
            group["expectancy_proxy_pct"] = group["avg_current_return_pct"]
            by_strategy_market.append(group)

        summary["avg_current_return_pct"] = avg(total_current)
        summary["avg_mfe_pct"] = avg(total_peak)
        summary["avg_mae_pct"] = avg(total_worst)
        summary["evidence_quality"] = (
            "thin" if summary["buy_ideas"] < 20 else "moderate" if summary["buy_ideas"] < 100 else "good"
        )
        return {
            "summary": summary,
            "by_strategy_market": sorted(
                by_strategy_market,
                key=lambda item: (item["failed_after_entry"], item["quick_red"], item["ideas"]),
                reverse=True,
            )[:20],
            "recent_failures": sorted(failures, key=lambda item: item.get("last_seen_at") or "", reverse=True)[:15],
            "recent_winners": sorted(winners_list, key=lambda item: item.get("last_seen_at") or "", reverse=True)[:15],
            "definitions": {
                "quick_red": "BUY went negative within roughly two days or had MAE worse than -1.5%.",
                "failed_after_entry": "BUY did not hit T1 and is stopped, below -2%, or had MAE worse than -3%.",
                "mfe_mae": "MFE uses peak return since signal; MAE uses worst return since signal.",
            },
        }

    def latest_sentiment(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select s.*, {_market_region_case("u")} as market_region
                from sentiment_events s
                left join universe u on u.symbol = s.symbol
                order by s.id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_sentiment_by_symbol(self, symbols: list[str], max_age_days: int = 7) -> dict[str, dict[str, Any]]:
        normalized = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()))
        if not normalized:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(max_age_days or 7), 1))).isoformat()
        rows: list[sqlite3.Row] = []
        with self.connect() as conn:
            for index in range(0, len(normalized), 800):
                chunk = normalized[index : index + 800]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        select s.*, {_market_region_case("u")} as market_region
                        from sentiment_events s
                        left join universe u on u.symbol = s.symbol
                        where s.symbol in ({placeholders}) and s.ts >= ?
                        order by s.symbol, s.id desc
                        """,
                        (*chunk, cutoff),
                    ).fetchall()
                )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or symbol in latest:
                continue
            headlines = _json_load(item.get("headlines_json"))
            events = _json_load(item.get("events_json"))
            item["headlines"] = headlines if isinstance(headlines, list) else []
            item["events"] = events if isinstance(events, list) else []
            item["source"] = _first_event_source(item["events"])
            latest[symbol] = item
        return latest


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _first_event_source(events: Any) -> str | None:
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("source"):
            return str(event["source"])
    return None


def _return_pct(entry_price: float, latest_price: float) -> float:
    try:
        entry = float(entry_price)
        latest = float(latest_price)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0:
        return 0.0
    return round(((latest - entry) / entry) * 100, 4)


def _follow_realized_pnl(details: Any) -> float:
    if not isinstance(details, dict):
        return 0.0
    realized = 0.0
    management = details.get("exit_management") if isinstance(details.get("exit_management"), dict) else {}
    management_total = _optional_float(management.get("realized_pnl_total"))
    if management_total is not None:
        realized += management_total
    else:
        events = management.get("events", [])
        if isinstance(events, list):
            realized += sum(_optional_float(event.get("realized_pnl")) or 0.0 for event in events if isinstance(event, dict))
    manual_exit = details.get("manual_exit") if isinstance(details.get("manual_exit"), dict) else {}
    manual_realized = _optional_float(manual_exit.get("realized_pnl"))
    if manual_realized is not None:
        realized += manual_realized
    safety_exit = details.get("safety_exit") if isinstance(details.get("safety_exit"), dict) else {}
    safety_realized = _optional_float(safety_exit.get("realized_pnl"))
    if safety_realized is not None:
        realized += safety_realized
    return round(realized, 2)


def _performance_feedback_record(item: dict[str, Any]) -> dict[str, Any]:
    follow_details = _json_load(item.get("follow_details_json"))
    follow_details = follow_details if isinstance(follow_details, dict) else {}
    idea_details = _json_load(item.get("idea_details_json"))
    idea_details = idea_details if isinstance(idea_details, dict) else {}
    management = follow_details.get("exit_management") if isinstance(follow_details.get("exit_management"), dict) else {}
    events = [event for event in management.get("events", []) if isinstance(event, dict)]
    latest_event = events[-1] if events else {}
    manual_exit = follow_details.get("manual_exit") if isinstance(follow_details.get("manual_exit"), dict) else {}
    mark_state = follow_details.get("mark_state") if isinstance(follow_details.get("mark_state"), dict) else {}
    status = str(item.get("follow_status") or "").upper()
    mode = str(item.get("mode") or "").upper()
    closed = status in {"EXITED", "LIVE_EXIT_REQUESTED"}
    entry_price = _optional_float(item.get("follow_entry_price") or item.get("idea_entry_price")) or 0.0
    latest_price = _optional_float(item.get("follow_latest_price") or item.get("idea_latest_price")) or entry_price
    return_pct = (
        _optional_float(manual_exit.get("return_pct"))
        or _optional_float(latest_event.get("return_pct"))
        or _optional_float(item.get("follow_return_pct"))
        or _return_pct(entry_price, latest_price)
    )
    realized_pnl = _follow_realized_pnl(follow_details)
    peak = (
        _optional_float(mark_state.get("peak_return_pct"))
        or _optional_float(item.get("peak_return_pct"))
        or max(return_pct, _optional_float(item.get("current_return_pct")) or return_pct)
    )
    worst = (
        _optional_float(mark_state.get("worst_return_pct"))
        or _optional_float(item.get("worst_return_pct"))
        or min(return_pct, _optional_float(item.get("current_return_pct")) or return_pct)
    )
    target_status = [target for target in idea_details.get("target_status", []) if isinstance(target, dict)]
    t1 = next((target for target in target_status if str(target.get("label") or "").upper() == "T1"), {})
    t1_hit = bool(t1.get("hit"))
    t1_hit_at = _parse_dt(t1.get("hit_at"))
    opened_at = _parse_dt(item.get("followed_at")) or _parse_dt(item.get("first_seen_at"))
    time_to_t1_hours = None
    if t1_hit and t1_hit_at and opened_at:
        time_to_t1_hours = max((t1_hit_at - opened_at).total_seconds() / 3600, 0.0)
    lifecycle = str(idea_details.get("lifecycle_status") or "").lower()
    exit_keys = {
        str(event.get("key") or "").upper()
        for event in events
        if str(event.get("key") or "").strip()
    }
    manual_reason = str(manual_exit.get("reason") or "").upper()
    stop_hit = (
        status == "STOP_HIT"
        or str(item.get("idea_status") or "").upper() == "STOP_HIT"
        or lifecycle == "stopped"
        or any(key in {"STOP_LOSS", "STOP_HIT", "RISK_EXIT_BEFORE_T1"} for key in exit_keys)
        or "STOP" in manual_reason
    )
    strategy = str(item.get("strategy") or "unknown").strip() or "unknown"
    plan = str(item.get("plan_code") or strategy or "unclassified").strip() or "unclassified"
    market = normalize_market_region(item.get("market_region") or "IN", default="IN")
    return {
        "follow_id": item.get("follow_id"),
        "idea_id": item.get("idea_id"),
        "symbol": item.get("symbol"),
        "market_region": market,
        "exchange": item.get("exchange"),
        "strategy": strategy,
        "plan_code": plan,
        "mode": mode,
        "status": status,
        "closed": closed,
        "entry_price": round(entry_price, 4),
        "latest_price": round(latest_price, 4),
        "return_pct": round(float(return_pct or 0.0), 4),
        "realized_pnl": round(realized_pnl, 2),
        "mae_pct": round(float(worst or 0.0), 4),
        "mfe_pct": round(float(peak or 0.0), 4),
        "t1_hit": t1_hit,
        "time_to_t1_hours": round(time_to_t1_hours, 4) if time_to_t1_hours is not None else None,
        "stop_hit": stop_hit,
        "opened_at": item.get("followed_at") or item.get("first_seen_at"),
        "closed_at": manual_exit.get("exited_at") or management.get("last_action_at") or (item.get("follow_updated_at") if closed else None),
        "exit_keys": sorted(exit_keys),
    }


def _empty_performance_group(
    group_type: str,
    key: str,
    strategy: str | None = None,
    market_region: str | None = None,
) -> dict[str, Any]:
    return {
        "group_type": group_type,
        "key": key,
        "strategy": strategy,
        "market_region": market_region,
        "trades": 0,
        "closed_trades": 0,
        "open_trades": 0,
        "winners": 0,
        "losers": 0,
        "stop_hits": 0,
        "target_1_hits": 0,
        "_closed_returns": [],
        "_gains": [],
        "_losses": [],
        "_realized": [],
        "_mae": [],
        "_mfe": [],
        "_time_to_t1": [],
        "sample_symbols": [],
    }


def _add_performance_record(group: dict[str, Any], record: dict[str, Any]) -> None:
    group["trades"] += 1
    if record.get("closed"):
        group["closed_trades"] += 1
        ret = float(record.get("return_pct") or 0.0)
        group["_closed_returns"].append(ret)
        group["_realized"].append(float(record.get("realized_pnl") or 0.0))
        if ret > 0:
            group["winners"] += 1
            group["_gains"].append(ret)
        else:
            group["losers"] += 1
            group["_losses"].append(ret)
        if record.get("stop_hit"):
            group["stop_hits"] += 1
    else:
        group["open_trades"] += 1
    if record.get("t1_hit"):
        group["target_1_hits"] += 1
    if record.get("time_to_t1_hours") is not None:
        group["_time_to_t1"].append(float(record["time_to_t1_hours"]))
    group["_mae"].append(float(record.get("mae_pct") or 0.0))
    group["_mfe"].append(float(record.get("mfe_pct") or 0.0))
    symbol = str(record.get("symbol") or "").upper()
    if symbol and len(group["sample_symbols"]) < 8 and symbol not in group["sample_symbols"]:
        group["sample_symbols"].append(symbol)
    if group.get("strategy") is None:
        group["strategy"] = record.get("strategy")
    if group.get("market_region") is None:
        group["market_region"] = record.get("market_region")


def _finalize_performance_group(group: dict[str, Any]) -> dict[str, Any]:
    closed = int(group.get("closed_trades") or 0)
    trades = int(group.get("trades") or 0)
    returns = list(group.pop("_closed_returns", []))
    gains = list(group.pop("_gains", []))
    losses = list(group.pop("_losses", []))
    realized = list(group.pop("_realized", []))
    mae_values = list(group.pop("_mae", []))
    mfe_values = list(group.pop("_mfe", []))
    t1_times = list(group.pop("_time_to_t1", []))
    output = dict(group)
    output.update(
        {
            "win_rate": round(float(output["winners"]) / closed, 4) if closed else 0.0,
            "average_gain_pct": _avg(gains),
            "average_loss_pct": _avg(losses),
            "average_realized_pnl": _avg(realized, digits=2),
            "stop_hit_rate": round(float(output["stop_hits"]) / closed, 4) if closed else 0.0,
            "target_1_hit_rate": round(float(output["target_1_hits"]) / trades, 4) if trades else 0.0,
            "avg_time_to_target_1_hours": _avg(t1_times),
            "max_adverse_excursion_pct": round(min(mae_values), 4) if mae_values else 0.0,
            "avg_mae_pct": _avg(mae_values),
            "max_favorable_excursion_pct": round(max(mfe_values), 4) if mfe_values else 0.0,
            "avg_mfe_pct": _avg(mfe_values),
            "expectancy_pct": _avg(returns),
            "expectancy_amount": _avg(realized, digits=2),
            "feedback_score": _performance_feedback_score(returns, output["winners"], closed, output["stop_hits"]),
            "evidence_quality": _performance_evidence_quality(closed),
        }
    )
    return output


def _compact_performance_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": record.get("symbol"),
        "market_region": record.get("market_region"),
        "strategy": record.get("strategy"),
        "plan_code": record.get("plan_code"),
        "return_pct": record.get("return_pct"),
        "realized_pnl": record.get("realized_pnl"),
        "mae_pct": record.get("mae_pct"),
        "mfe_pct": record.get("mfe_pct"),
        "t1_hit": record.get("t1_hit"),
        "time_to_t1_hours": record.get("time_to_t1_hours"),
        "stop_hit": record.get("stop_hit"),
        "closed_at": record.get("closed_at"),
    }


def _avg(values: list[float], digits: int = 4) -> float:
    return round(sum(values) / len(values), digits) if values else 0.0


def _performance_feedback_score(returns: list[float], winners: int, closed: int, stop_hits: int) -> float:
    if closed <= 0:
        return 0.0
    expectancy = _avg(returns)
    win_rate = float(winners) / closed
    stop_rate = float(stop_hits) / closed
    score = (expectancy / 5.0) + ((win_rate - 0.5) * 0.8) - (stop_rate * 0.35)
    if closed < 5:
        score *= 0.45
    elif closed < 12:
        score *= 0.7
    return round(max(min(score, 1.0), -1.0), 4)


def _performance_evidence_quality(closed: int) -> str:
    if closed >= 30:
        return "usable"
    if closed >= 12:
        return "thin"
    if closed > 0:
        return "anecdotal"
    return "none"


def _paper_exit_action(item: dict[str, Any], idea_details: dict[str, Any], follow_details: dict[str, Any]) -> dict[str, Any] | None:
    management = follow_details.get("exit_management") if isinstance(follow_details.get("exit_management"), dict) else {}
    done = {
        str(event.get("key") or "")
        for event in management.get("events", [])
        if isinstance(event, dict)
    }
    status = str(item.get("idea_status") or "").upper()
    lifecycle = str(idea_details.get("lifecycle_status") or "").lower()
    highest = str(idea_details.get("highest_target_hit") or "NONE").upper()
    latest = _optional_float(item.get("idea_latest_price") or item.get("latest_price")) or 0.0
    entry = _optional_float(item.get("entry_price") or item.get("idea_entry_price")) or 0.0
    stop = _optional_float(idea_details.get("stop_loss"))
    current_return = _return_pct(entry, latest)
    mark_state = follow_details.get("mark_state") if isinstance(follow_details.get("mark_state"), dict) else {}
    peak_return = _optional_float(mark_state.get("peak_return_pct"))
    if peak_return is None:
        peak_return = _optional_float(item.get("peak_return_pct"))
    if peak_return is None:
        peak_return = current_return
    worst_return = _optional_float(mark_state.get("worst_return_pct"))
    if worst_return is None:
        worst_return = current_return
    drawdown = idea_details.get("drawdown_status") if isinstance(idea_details.get("drawdown_status"), dict) else {}
    risk_used_pct = 0.0
    if entry > 0 and stop and entry > stop:
        risk_used_pct = max(min(((entry - latest) / (entry - stop)) * 100.0, 100.0), 0.0)

    if stop and latest > 0 and latest <= stop:
        return _exit_action("STOP_LOSS", "EXIT_FULL", 100, "Stop loss hit; exit the followed paper position.", full=True)
    if status == "STOP_HIT" or lifecycle == "stopped":
        return _exit_action("STOP_HIT", "EXIT_FULL", 100, "Signal lifecycle says stop was hit; exit the followed position.", full=True)
    if status == "EXIT_SIGNAL" or lifecycle == "exit_signal":
        return _exit_action("EXIT_SIGNAL", "EXIT_FULL", 100, "Engine generated an exit signal; close the followed position.", full=True)
    if status == "EXPIRED" or lifecycle == "expired":
        return _exit_action("EXPIRED", "EXIT_FULL", 100, "Idea expired without fresh confirmation; close the followed position.", full=True)
    if highest == "T3" or status == "TARGET_3_HIT" or lifecycle == "target_3_hit":
        return _exit_action("TARGET_3", "EXIT_FULL", 100, "Final target reached; close or trail no more than a token remainder.", full=True)

    t2_hit = highest in {"T2", "T3"} or status in {"TARGET_2_HIT", "TARGET_3_HIT"} or lifecycle in {"target_2_hit", "target_3_hit"} or _target_hit(idea_details, "T2")
    if t2_hit and "TARGET_2_REDUCE" not in done:
        return _exit_action(
            "TARGET_2_REDUCE",
            "REDUCE",
            _target_exit_pct(idea_details, "T2", 50.0),
            "T2 reached; book another tranche and trail the remaining quantity.",
        )

    t1_hit = highest in {"T1", "T2", "T3"} or status in {"TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT"} or lifecycle in {"target_1_hit", "target_2_hit", "target_3_hit"} or _target_hit(idea_details, "T1")
    if t1_hit and "TARGET_1_PARTIAL" not in done:
        return _exit_action(
            "TARGET_1_PARTIAL",
            "REDUCE",
            _target_exit_pct(idea_details, "T1", 35.0),
            "T1 reached; book partial profit and move the remaining trade to tighter risk.",
        )

    if "MFE_PROFIT_PROTECT" not in done:
        if peak_return >= 4.0 and current_return <= max(0.75, peak_return * 0.25):
            return _exit_action(
                "MFE_PROFIT_PROTECT",
                "EXIT_FULL",
                100,
                f"Trade gave back a {peak_return:.2f}% favorable move; protect capital instead of round-tripping.",
                full=True,
            )
        if peak_return >= 2.5 and current_return <= 0.0:
            return _exit_action(
                "MFE_PROFIT_PROTECT",
                "EXIT_FULL",
                100,
                f"Trade was up {peak_return:.2f}% but has returned to breakeven/loss; close before momentum failure deepens.",
                full=True,
            )

    if peak_return >= 2.0 and current_return <= 0.25 and "MFE_BREAKEVEN_REDUCE" not in done:
        return _exit_action(
            "MFE_BREAKEVEN_REDUCE",
            "REDUCE",
            50,
            f"Trade was up {peak_return:.2f}% but has faded near breakeven; reduce exposure and keep optionality.",
        )

    near_stop = bool(drawdown.get("near_stop")) or risk_used_pct >= 65.0
    before_t1 = not t1_hit
    if before_t1 and (near_stop or current_return <= -2.0 or worst_return <= -3.0):
        return _exit_action(
            "RISK_EXIT_BEFORE_T1",
            "EXIT_FULL",
            100,
            f"Adverse move before T1: return {current_return:.2f}%, worst {worst_return:.2f}%, risk used {risk_used_pct:.0f}%.",
            full=True,
        )
    return None


def _exit_action(key: str, action: str, exit_pct: float, reason: str, full: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "action": action,
        "label": "Exit" if full else "Reduce",
        "exit_pct": float(exit_pct),
        "reason": reason,
        "full": bool(full),
    }


def _target_hit(details: dict[str, Any], label: str) -> bool:
    target_label = str(label or "").upper()
    for target in details.get("target_status", []) or []:
        if not isinstance(target, dict):
            continue
        if str(target.get("label") or "").upper() == target_label and bool(target.get("hit")):
            return True
    return False


def _target_exit_pct(details: dict[str, Any], label: str, default: float) -> float:
    target_label = str(label or "").upper()
    for collection in (details.get("target_status", []), details.get("targets", [])):
        for target in collection or []:
            if not isinstance(target, dict):
                continue
            if str(target.get("label") or "").upper() != target_label:
                continue
            pct = _optional_float(target.get("suggested_exit_pct"))
            if pct and pct > 0:
                return max(min(pct, 100.0), 1.0)
    return default


def _short_reason(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _why_changed_payload(
    original_buy_reason: Any,
    latest_monitor_reason: Any,
    latest_action: str,
    continuity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_action = str(latest_action or "HOLD").upper()
    original = _short_reason(original_buy_reason, 260)
    latest = _short_reason(latest_monitor_reason, 320)
    continuity = continuity if isinstance(continuity, dict) else {}
    if continuity.get("duplicate_active_buy") or continuity.get("already_active_buy"):
        summary = "Already active. Repeated BUY is treated as position monitoring, not a new entry."
    elif latest_action == "BUY":
        summary = "Fresh BUY is currently confirmed by the latest engine cycle."
    elif original and latest:
        summary = f"BUY preserved. Latest engine action {latest_action} because {latest}"
    elif original:
        summary = f"BUY preserved. Latest engine action {latest_action}; no fresh add until a new BUY confirmation or exit."
    elif latest:
        summary = f"Latest engine action {latest_action} because {latest}"
    else:
        summary = f"Latest engine action {latest_action}; no fresh entry is active."
    return {
        "preserved": bool(continuity),
        "latest_engine_action": latest_action,
        "summary": summary,
        "original_buy_reason": original,
        "latest_monitor_reason": latest,
    }


def _drawdown_review_state(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    entry = _optional_float(item.get("entry_price"))
    latest = _optional_float(item.get("latest_price") or item.get("price"))
    stop = _optional_float(details.get("stop_loss"))
    current_return = _optional_float(item.get("current_return_pct")) or 0.0
    worst_return = _optional_float(item.get("worst_return_pct")) or current_return
    near_stop = bool((details.get("drawdown_status") or {}).get("near_stop")) if isinstance(details.get("drawdown_status"), dict) else False
    risk_used_pct = 0.0
    if entry and latest and stop and entry > stop:
        risk_used_pct = max(min(((entry - latest) / (entry - stop)) * 100.0, 100.0), 0.0)
    review = near_stop or risk_used_pct >= 55.0 or current_return <= -2.0 or worst_return <= -3.0
    return {
        "risk_review": bool(review),
        "risk_used_pct": round(risk_used_pct, 2),
        "reason": (
            f"Adverse move needs review: return {current_return:.2f}%, worst {worst_return:.2f}%, risk used {risk_used_pct:.0f}%."
            if review
            else ""
        ),
    }


def _signal_state_payload(item: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
    details = details if isinstance(details, dict) else {}
    signal_type = str(item.get("signal_type") or item.get("suggestion") or "").upper()
    status = str(item.get("status") or "").upper()
    lifecycle = str(details.get("lifecycle_status") or item.get("lifecycle_status") or status or "active").lower()
    latest_action = str(details.get("latest_system_action") or details.get("action") or signal_type or "HOLD").upper()
    continuity = details.get("signal_continuity") if isinstance(details.get("signal_continuity"), dict) else {}
    current_return = _optional_float(item.get("current_return_pct")) or 0.0
    latest_monitor_reason = details.get("latest_monitor_reason")
    original_buy_reason = details.get("original_buy_reason")
    why_changed = details.get("why_changed") if isinstance(details.get("why_changed"), dict) else {}
    if not why_changed:
        why_changed = _why_changed_payload(original_buy_reason, latest_monitor_reason, latest_action, continuity)
    drawdown_review = _drawdown_review_state(item, details)
    follow = item.get("user_follow") if isinstance(item.get("user_follow"), dict) else {}
    follow_status = str(follow.get("status") or item.get("follow_status") or "").upper()
    followed_active = follow_status in {"ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"} and _optional_int(follow.get("qty") if follow else item.get("qty")) not in {None, 0}
    follow_exited = follow_status in {"EXITED", "REJECTED", "CANCELLED", "CANCELED"}
    fresh_buy_recent = _recent_dt(item.get("last_seen_at"))

    if status == "STOP_HIT" or lifecycle == "stopped":
        display_signal = "Stopped"
        fresh_action = "EXITED"
        trade_state = "STOP_HIT"
        class_name = "negative"
        reason = "Idea invalidated by stop."
    elif status == "EXIT_SIGNAL" or lifecycle == "exit_signal" or signal_type == "EXIT":
        display_signal = "Exit"
        fresh_action = "EXIT"
        trade_state = "EXIT_SIGNAL"
        class_name = "negative"
        reason = "Exit signal is active for this idea."
    elif status == "EXPIRED" or lifecycle == "expired":
        display_signal = "Expired"
        fresh_action = "EXPIRED"
        trade_state = "EXPIRED"
        class_name = "warning"
        reason = "Idea timeline has expired."
    elif signal_type == "BUY" and status in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
        duplicate_active = bool(continuity.get("duplicate_active_buy") or continuity.get("already_active_buy"))
        follow_mode = str(follow.get("mode") or item.get("mode") or "").upper()
        readiness = trade_readiness_gate(
            {
                **item,
                "details": details,
                "action": details.get("action") or signal_type,
                "signal_type": signal_type,
                "status": status,
            }
        )
        if followed_active and follow_mode == "PAPER":
            display_signal = "Paper Entered"
            fresh_action = "NO_FRESH_ADD"
            reason = "Paper position is already entered; this idea is now being monitored."
        elif followed_active:
            display_signal = "Position Monitor"
            fresh_action = "NO_FRESH_ADD"
            reason = "Position is already active; this idea is now being monitored."
        elif follow_exited:
            display_signal = "No Fresh Add"
            fresh_action = "NO_FRESH_ADD"
            reason = "Your previous follow is closed; wait for a new fresh BUY before entering again."
        elif duplicate_active:
            display_signal = "Already Active"
            fresh_action = "NO_FRESH_ADD"
            reason = why_changed.get("summary") or "Already active; repeated BUY is monitor/no fresh add during cooldown."
        elif not readiness.get("passed"):
            display_signal = "Watch"
            fresh_action = "WATCH"
            reason = readiness.get("message") or "BUY thesis needs stronger quality/data before it is actionable."
        elif latest_action == "BUY" and not continuity and readiness.get("passed") and fresh_buy_recent:
            display_signal = "Actionable"
            fresh_action = "BUY_NOW"
            reason = "Fresh BUY passed the current entry and risk gates."
        elif latest_action == "BUY" and not continuity and readiness.get("passed"):
            display_signal = "No Fresh Add"
            fresh_action = "NO_FRESH_ADD"
            reason = "BUY is older than the fresh-entry window; keep monitoring unless a new BUY confirmation appears."
        elif drawdown_review["risk_review"]:
            display_signal = "Risk Review"
            fresh_action = "NO_FRESH_ADD"
            reason = drawdown_review["reason"] or "Active BUY is in adverse movement; review risk before adding."
        else:
            display_signal = "Position Monitor"
            fresh_action = "NO_FRESH_ADD"
            reason = why_changed.get("summary") or "Active BUY remains valid; latest cycle is monitor/no fresh add until a new BUY confirmation or exit."
        trade_state = {
            "Paper Entered": "PAPER_ENTERED",
            "Actionable": "ACTIONABLE",
            "No Fresh Add": "POSITION_MONITOR",
            "Already Active": "POSITION_MONITOR",
            "Position Monitor": "POSITION_MONITOR",
            "Watch": "WATCH",
        }.get(display_signal, "POSITION_MONITOR")
        class_name = "open"
        if display_signal == "Watch":
            class_name = "warning"
        if drawdown_review["risk_review"]:
            trade_state = "RISK_REVIEW"
            class_name = "warning"
        if current_return < 0:
            reason = f"{reason} Current return is {current_return:.2f}% from the original signal."
    elif signal_type == "WATCH" or status == "WATCH" or lifecycle == "watch":
        display_signal = "Watch"
        fresh_action = "WATCH"
        trade_state = "WATCH"
        class_name = "warning"
        reason = "Setup is being monitored but is not a fresh BUY."
    elif followed_active and drawdown_review["risk_review"]:
        display_signal = "Risk Review"
        fresh_action = "NO_FRESH_ADD"
        trade_state = "RISK_REVIEW"
        class_name = "warning"
        reason = drawdown_review["reason"] or "Followed position needs risk review before adding."
        if current_return < 0:
            reason = f"{reason} Current return is {current_return:.2f}% from the original signal."
    elif followed_active:
        display_signal = "Position Monitor"
        fresh_action = "NO_FRESH_ADD"
        trade_state = "POSITION_MONITOR"
        class_name = "open"
        reason = why_changed.get("summary") or "Followed position is active; this is position monitoring, not a fresh entry."
    else:
        display_signal = "Monitor"
        fresh_action = "NO_TRADE"
        trade_state = "MONITORING"
        class_name = "neutral"
        reason = "No fresh trade action is active."

    if continuity:
        reason = str(why_changed.get("summary") or continuity.get("reason") or reason)
    return {
        "display_signal": display_signal,
        "fresh_action": fresh_action,
        "fresh_action_label": {
            "BUY_NOW": "Actionable",
            "NO_FRESH_ADD": "No Fresh Add",
            "WATCH": "Watch",
            "EXIT": "Exit",
            "EXITED": "Exited",
            "EXPIRED": "Expired",
            "NO_TRADE": "No Trade",
        }.get(fresh_action, display_signal),
        "trade_state": trade_state,
        "trade_state_label": display_signal,
        "class_name": class_name,
        "latest_system_action": latest_action,
        "display_reason": reason,
        "original_buy_reason": original_buy_reason,
        "latest_monitor_reason": latest_monitor_reason,
        "why_changed": why_changed,
        "risk_review": drawdown_review,
    }


def _decorate_signal_idea_item(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    opportunity = details.get("opportunity_state") if isinstance(details.get("opportunity_state"), dict) else {}
    if not opportunity:
        opportunity = opportunity_state_from_signal_details(details)
    state = _signal_state_payload(item, details)
    execution = _execution_state_payload(item)
    setup_bucket = _setup_bucket_payload(item, details, state)
    item["signal_state"] = state
    item["display_signal"] = state["display_signal"]
    item["fresh_action"] = state["fresh_action"]
    item["fresh_action_label"] = state["fresh_action_label"]
    item["trade_state"] = state["trade_state"]
    item["latest_system_action"] = state["latest_system_action"]
    state_first = state.get("fresh_action") in {"NO_FRESH_ADD", "EXIT", "EXITED", "EXPIRED"} or state.get("trade_state") in {
        "PAPER_ENTERED",
        "POSITION_MONITOR",
        "RISK_REVIEW",
    }
    item["display_reason"] = state["display_reason"] if state_first else opportunity.get("summary") or state["display_reason"]
    item["execution_state"] = execution["state"]
    item["execution_state_label"] = execution["label"]
    item["execution_state_note"] = execution["note"]
    item["why_changed"] = state["why_changed"]
    item["risk_review"] = state["risk_review"]
    item["opportunity_state"] = opportunity.get("state")
    item["opportunity_label"] = opportunity.get("label")
    item["opportunity_summary"] = opportunity.get("summary")
    item["opportunity_next_step"] = opportunity.get("next_step")
    item["opportunity_reasons"] = opportunity.get("reasons") or []
    item["opportunity_terms"] = opportunity.get("term_explanations") or []
    item["setup_bucket"] = setup_bucket["bucket"]
    item["setup_bucket_label"] = setup_bucket["label"]
    item["setup_bucket_reason"] = setup_bucket["reason"]
    return item


def _setup_bucket_payload(item: dict[str, Any], details: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    status = str(item.get("status") or "").upper()
    signal_type = str(item.get("signal_type") or "").upper()
    opportunity = details.get("opportunity_state") if isinstance(details.get("opportunity_state"), dict) else {}
    risk_flags = details.get("risk_flags") if isinstance(details.get("risk_flags"), list) else []
    classification = str((details.get("classification") or {}).get("classification") or "").upper() if isinstance(details.get("classification"), dict) else ""
    cap = _optional_float(details.get("allocation_cap_multiplier"))
    readiness = trade_readiness_gate(
        {
            **item,
            "details": details,
            "action": details.get("action") or signal_type,
            "signal_type": signal_type,
            "status": status,
        }
    )
    if status in {"STOP_HIT", "EXIT_SIGNAL", "EXPIRED", "TARGET_3_HIT"} or signal_type == "EXIT":
        return {"bucket": "AVOID", "label": "Avoid", "reason": "Idea is closed, invalidated, or in exit mode."}
    if state.get("trade_state") == "RISK_REVIEW":
        return {"bucket": "RISK_REVIEW", "label": "Risk Review", "reason": "Adverse move is outside normal noise; do not add without review."}
    if opportunity and state.get("fresh_action") in {"WATCH", "NO_TRADE"}:
        return {
            "bucket": str(opportunity.get("state") or "WATCH"),
            "label": str(opportunity.get("label") or "Watch"),
            "reason": str(opportunity.get("summary") or opportunity.get("next_step") or "Setup is not actionable yet."),
        }
    if state.get("fresh_action") == "WATCH" or state.get("trade_state") == "WATCH":
        return {"bucket": "WATCH", "label": "Watch", "reason": "Setup is not actionable yet."}
    readiness_size = _optional_float(readiness.get("size_multiplier")) or 1.0
    readiness_warnings = readiness.get("risk_warnings") if isinstance(readiness.get("risk_warnings"), list) else []
    full_size_ready = readiness_size >= 0.75 and not readiness_warnings
    if signal_type == "BUY" and state.get("fresh_action") == "BUY_NOW" and readiness.get("passed") and full_size_ready and not risk_flags and classification != "SPECULATIVE":
        return {"bucket": "ACTIONABLE", "label": "Actionable", "reason": "Fresh BUY with strong score, confluence, and no active risk flags."}
    if signal_type == "BUY":
        if not readiness.get("passed"):
            return {
                "bucket": "WATCH",
                "label": "Watch",
                "reason": readiness.get("message") or "BUY thesis is present, but it is not trade-ready.",
            }
        if readiness.get("passed") and full_size_ready and classification != "SPECULATIVE" and not risk_flags and not (cap is not None and cap <= 0.3):
            return {"bucket": "ACTIONABLE", "label": "Actionable", "reason": "BUY thesis is active and risk checks are acceptable."}
        return {
            "bucket": "SMALL_SIZE_ONLY",
            "label": "Small Size Only",
            "reason": readiness.get("message") or "BUY thesis exists, but risk/data/quality profile requires reduced size.",
        }
    if signal_type == "WATCH" or status == "WATCH":
        return {"bucket": "WATCH", "label": "Watch", "reason": "Setup is not actionable yet."}
    return {"bucket": "AVOID", "label": "Avoid", "reason": "No active trade setup is available."}


def _execution_state_payload(item: dict[str, Any]) -> dict[str, str]:
    signal_type = str(item.get("signal_type") or item.get("suggestion") or "").upper()
    status_label = str(item.get("status") or "").upper()
    follow = item.get("user_follow") if isinstance(item.get("user_follow"), dict) else {}
    mode = str(follow.get("mode") or item.get("mode") or "").upper()
    status = str(follow.get("status") or item.get("follow_status") or "").upper()
    qty = _optional_int(follow.get("qty") if follow else item.get("qty")) or 0
    if not follow and not mode:
        return {"state": "SIGNAL_ONLY", "label": "Signal Only", "note": "Not tracked or auto-followed for this user."}
    if signal_type == "WATCH" or status_label == "WATCH":
        return {"state": "WATCH", "label": "Watch", "note": "Watch only; no paper or live entry is allowed."}
    if mode == "TRACK":
        return {"state": "TRACKED", "label": "Tracked", "note": "Tracking only; no paper or live order."}
    if status in {"REJECTED", "FAILED"}:
        return {"state": status, "label": "Rejected", "note": "Order/follow request was rejected or failed."}
    if status == "CANCELLED":
        return {"state": "CANCELLED", "label": "Cancelled", "note": "Order/follow request was cancelled."}
    if status in {"FILLED", "ACCEPTED", "PARTIAL"}:
        return {"state": status, "label": status.title(), "note": "Broker/order lifecycle has advanced beyond request state."}
    if mode == "PAPER" and status == "ACTIVE" and qty > 0:
        return {"state": "PAPER_ENTERED", "label": "Paper Entered", "note": "Paper position is active and marked to latest price."}
    if mode == "LIVE" and status == "LIVE_REQUESTED":
        return {"state": "LIVE_REQUESTED", "label": "Live Requested", "note": "Guarded live request was created; broker fill must be reconciled."}
    if mode == "LIVE" and status == "LIVE_EXIT_REQUESTED":
        return {"state": "LIVE_EXIT_REQUESTED", "label": "Live Exit Requested", "note": "Exit request was created; broker fill must be reconciled."}
    if mode == "LIVE" and status == "ACTIVE" and qty > 0:
        return {"state": "LIVE_ACTIVE", "label": "Live Active", "note": "Live-follow position is active in OpenStocks."}
    if status == "EXITED":
        return {"state": "EXITED", "label": "Exited", "note": "Followed position has been exited."}
    return {"state": status or "FOLLOW_PENDING", "label": "Follow Pending", "note": "Follow state is pending or missing quantity."}


def _signal_idea_from_decision(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        audit = json.loads(row.get("details_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        audit = {}
    context = audit.get("context") if isinstance(audit.get("context"), dict) else {}
    context_summary = audit.get("context_summary") if isinstance(audit.get("context_summary"), dict) else {}
    risk_gates = audit.get("risk_gates") if isinstance(audit.get("risk_gates"), dict) else {}
    full = _first_dict(context.get("full_spectrum_analysis"), context_summary.get("full_spectrum_summary"))
    confluence = full.get("confluence_score") if isinstance(full.get("confluence_score"), dict) else {}
    trade_plan = full.get("trade_plan") if isinstance(full.get("trade_plan"), dict) else {}
    signal_plan = full.get("signal_plan") if isinstance(full.get("signal_plan"), dict) else {}
    risk = full.get("risk_overrides") if isinstance(full.get("risk_overrides"), dict) else {}
    strategy_logic = full.get("strategy_logic_filters") if isinstance(full.get("strategy_logic_filters"), dict) else {}
    breakout = full.get("breakout_quality") if isinstance(full.get("breakout_quality"), dict) else {}
    entry_quality = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
    decision_gate = _first_dict(context.get("decision_gate_context"), risk_gates.get("decision_gate_context"))
    score_breakdown = audit.get("score_breakdown") if isinstance(audit.get("score_breakdown"), dict) else {}
    system_audit = audit.get("system_gate_audit") or context.get("system_gate_audit") or {}
    if not isinstance(system_audit, dict):
        system_audit = {}
    data_readiness = _first_dict(
        context.get("data_readiness"),
        audit.get("data_readiness"),
        system_audit.get("data_readiness"),
        risk_gates.get("data_readiness"),
        context_summary.get("data_readiness"),
    )
    macro_event_context = _first_dict(
        context.get("macro_event_context"),
        context_summary.get("macro_event_context"),
    )
    action = str(row.get("action") or "HOLD").upper()
    combined = float(score_breakdown.get("combined") or 0.0)
    confluence_total = float(confluence.get("total") or 0.0)
    hard_blocked = bool(system_audit.get("hard_blocked"))
    overall_score = system_audit.get("overall_score_pct")
    if overall_score in (None, ""):
        overall_score = audit.get("overall_score_pct")
    if overall_score in (None, ""):
        overall_score = score_breakdown.get("score_percent")
    overall_grade = system_audit.get("overall_grade") or audit.get("overall_grade")
    post_gate_score = _optional_float(overall_score)
    pre_gate_score = _optional_float(score_breakdown.get("score_percent"))
    data_not_trade_ready = bool(data_readiness) and data_readiness.get("trade_decision_ready") is not True
    display_score = post_gate_score
    display_grade = overall_grade
    if data_not_trade_ready and pre_gate_score is not None and pre_gate_score > float(post_gate_score or 0.0):
        display_score = pre_gate_score
        display_grade = _score_grade(pre_gate_score)
    price = _optional_float(row.get("price")) or 0.0
    targets = trade_plan.get("targets")
    if not isinstance(targets, list):
        targets = []
    opportunity_scan = context.get("opportunity_scan") if isinstance(context.get("opportunity_scan"), dict) else {}
    details = {
        "action": action,
        "tier": confluence.get("tier"),
        "decision_readiness": signal_plan.get("decision_readiness", "monitor_only"),
        "entry_zone": trade_plan.get("entry_zone"),
        "stop_loss": trade_plan.get("stop_loss"),
        "targets": targets,
        "latest_price": price,
        "risk_flags": risk.get("flags", []),
        "active_flags": system_audit.get("active_flags", []),
        "overall_score_pct": display_score,
        "overall_grade": display_grade,
        "tradeability_score_pct": post_gate_score,
        "tradeability_grade": overall_grade,
        "setup_score_pct": pre_gate_score,
        "setup_grade": _score_grade(pre_gate_score) if pre_gate_score is not None else None,
        "confluence": confluence_total,
        "hard_blocked": hard_blocked,
        "hard_blocks": system_audit.get("hard_blocks", []),
        "soft_flags": system_audit.get("soft_flags", []),
        "failed_gates": decision_gate.get("failed_gates", []),
        "data_readiness": data_readiness,
        "macro_event_context": macro_event_context,
        "quote": context.get("quote") if isinstance(context.get("quote"), dict) else {},
        "entry_quality": entry_quality,
        "breakout_quality": breakout,
        "opportunity_scan": opportunity_scan,
        "live_momentum_review": full.get("live_momentum_review") if isinstance(full.get("live_momentum_review"), dict) else {},
        "strategy_logic_filters": strategy_logic,
        "reason": audit.get("action_reason") or row.get("reason"),
        "classification": system_audit.get("classification"),
        "allocation_cap_multiplier": system_audit.get("allocation_cap_multiplier"),
    }
    if action == "BUY":
        _apply_top_gainers_playbook_signal_details(details, price)
        _apply_btst_signal_details(details, price)
        display_score = details.get("overall_score_pct", display_score)
        display_grade = details.get("overall_grade", display_grade)
    quality_gate = fresh_buy_quality_gate(
        {
            "action": action,
            "signal_type": "BUY" if action == "BUY" else action,
            "status": "ACTIVE" if action == "BUY" else action,
            "overall_score_pct": post_gate_score,
            "overall_grade": overall_grade,
            "confluence": confluence_total,
            "hard_blocked": hard_blocked,
            "details": details,
        }
    )
    details["quality_gate"] = quality_gate
    details["opportunity_state"] = opportunity_state_from_signal_details(details)
    signal_type = "NO_TRADE"
    status = "MONITORING"
    if action == "BUY" and not hard_blocked and quality_gate.get("passed"):
        signal_type = "BUY"
        status = "ACTIVE"
    elif action == "SELL":
        signal_type = "EXIT"
        status = "EXIT_SIGNAL"
    elif action == "BUY" and not hard_blocked:
        signal_type = "WATCH"
        status = "WATCH"
        details["decision_readiness"] = "monitor_only"
        details["quality_downgrade"] = {
            "from": "BUY",
            "to": "WATCH",
            "reason": quality_gate.get("reason"),
            "message": quality_gate.get("message"),
        }
    elif (
        (confluence_total >= 16 and combined >= 0.20 and float(display_score or 0.0) >= 55)
        or is_signal_candidate_state(details["opportunity_state"])
    ) and action != "SELL":
        signal_type = "WATCH"
        status = "WATCH"
    else:
        return None
    if signal_type == "BUY":
        details["original_buy_reason"] = audit.get("action_reason") or row.get("reason")
    plan_code = _strategy_plan_code(str(row.get("strategy") or ""), action, price, full, system_audit)
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "strategy": str(row.get("strategy") or "unknown"),
        "plan_code": plan_code,
        "signal_type": signal_type,
        "status": status,
        "latest_price": price,
        "confidence": float(row.get("confidence") or 0.0),
        "combined_score": combined,
        "confluence": confluence_total,
        "overall_score_pct": float(display_score or 0.0),
        "overall_grade": str(display_grade or ""),
        "reason": str(audit.get("action_reason") or row.get("reason") or "")[:1000],
        "details": details,
    }


def _apply_top_gainers_playbook_signal_details(details: dict[str, Any], price: float) -> None:
    scan = details.get("opportunity_scan") if isinstance(details.get("opportunity_scan"), dict) else {}
    playbook = scan.get("top_gainers_playbook") if isinstance(scan.get("top_gainers_playbook"), dict) else {}
    signal = str(playbook.get("final_signal") or "").upper()
    if signal not in {"STRONG BUY", "MODERATE BUY"}:
        return
    levels = playbook.get("levels") if isinstance(playbook.get("levels"), dict) else {}
    entry = _optional_float(levels.get("entry"))
    max_entry = _optional_float(levels.get("max_entry"))
    stop = _optional_float(levels.get("stop"))
    if entry and max_entry:
        details["entry_zone"] = [round(entry, 2), round(max_entry, 2)]
    if stop:
        details["stop_loss"] = round(stop, 2)
        details["stop_status"] = {
            "price": round(stop, 2),
            "source": "top_gainers_playbook",
            "rule": str(levels.get("stop_rule") or "7pct_below_entry"),
        }
    targets: list[dict[str, Any]] = []
    for label, key, probability in (
        ("T1", "target1", "likely"),
        ("T2", "target2", "stretch"),
        ("T3", "target3", "low_probability"),
    ):
        target = _optional_float(levels.get(key))
        if not target:
            continue
        distance = ((target - price) / price) * 100.0 if price else None
        targets.append(
            {
                "label": label,
                "price": round(target, 2),
                "distance_pct": round(distance, 2) if distance is not None else None,
                "probability_label": probability,
                "source": "top_gainers_playbook",
            }
        )
    if targets:
        details["targets"] = targets
        details["target_status"] = targets
    quant_score = _optional_float(playbook.get("quant_score"))
    if quant_score is not None:
        existing_setup = _optional_float(details.get("setup_score_pct")) or 0.0
        existing_tradeability = _optional_float(details.get("overall_score_pct")) or 0.0
        details["setup_score_pct"] = max(existing_setup, quant_score)
        if quant_score > existing_tradeability:
            details["overall_score_pct"] = quant_score
            details["overall_grade"] = _score_grade(quant_score)
    details["playbook_signal"] = {
        "source": "top_gainers_playbook",
        "final_signal": signal,
        "quant_score": quant_score,
        "setup_confidence": playbook.get("setup_confidence"),
        "catalyst_review": playbook.get("catalyst_review"),
        "levels": levels,
    }


def _apply_btst_signal_details(details: dict[str, Any], price: float) -> None:
    scan = details.get("opportunity_scan") if isinstance(details.get("opportunity_scan"), dict) else {}
    if str(scan.get("setup") or "").strip().lower() != "btst_buy_candidate":
        return
    btst = scan.get("btst") if isinstance(scan.get("btst"), dict) else {}
    if not btst.get("detected"):
        return
    entry_zone = btst.get("entry_zone") if isinstance(btst.get("entry_zone"), dict) else {}
    entry_low = _optional_float(entry_zone.get("low")) or price
    entry_high = _optional_float(entry_zone.get("high")) or _optional_float(btst.get("max_entry")) or (price * 1.012 if price else None)
    stop = _optional_float(btst.get("stop_loss"))
    target1 = _optional_float(btst.get("target1"))
    if entry_low and entry_high:
        details["entry_zone"] = [round(entry_low, 2), round(entry_high, 2)]
    if stop:
        details["stop_loss"] = round(stop, 2)
        details["stop_status"] = {
            "price": round(stop, 2),
            "source": "btst_buy_candidate",
            "rule": "overnight_risk_control",
        }
    if target1:
        distance = ((target1 - price) / price) * 100.0 if price else None
        details["targets"] = [
            {
                "label": "BTST-T1",
                "price": round(target1, 2),
                "distance_pct": round(distance, 2) if distance is not None else None,
                "probability_label": "next_day_follow_through",
                "source": "btst_buy_candidate",
            }
        ]
        details["target_status"] = details["targets"]
    score = _optional_float(btst.get("score"))
    if score is not None:
        score_pct = score * 100.0 if score <= 1.0 else score
        details["setup_score_pct"] = max(_optional_float(details.get("setup_score_pct")) or 0.0, score_pct)
        if score_pct > float(details.get("overall_score_pct") or 0.0):
            details["overall_score_pct"] = score_pct
            details["overall_grade"] = _score_grade(score_pct)
    details["holding_period"] = "BTST"
    details["days_to_expiry"] = 2
    details["timeline"] = {
        "entry": "today_before_close",
        "exit": "tomorrow_first_strength_or_first_15m_failure",
        "max_holding_days": 2,
    }
    details["btst_signal"] = {
        "source": "btst_buy_candidate",
        "score": btst.get("score"),
        "confidence": btst.get("confidence"),
        "next_day_bias": btst.get("next_day_bias"),
        "exit_plan": btst.get("exit_plan"),
        "checks": btst.get("checks"),
        "evidence": btst.get("evidence"),
    }


def _empty_candle_coverage() -> dict[str, Any]:
    return {
        "intraday": {"count": 0, "latest_ts": None},
        "daily": {"count": 0, "latest_ts": None},
        "weekly": {"count": 0, "latest_ts": None},
        "analysis": {"count": 0, "latest_ts": None},
        "sources": {},
    }


def _candle_source_bucket(source: str) -> str | None:
    normalized = str(source or "").lower()
    if not normalized:
        return None
    if normalized == "yahoo-delayed" or ":day" in normalized or ":1day" in normalized:
        return "daily"
    if ":week" in normalized or ":1week" in normalized:
        return "weekly"
    if (
        "minute" in normalized
        or normalized.endswith(":5m")
        or normalized.endswith(":15m")
        or normalized.endswith(":30m")
        or normalized.endswith(":60m")
        or normalized in {"upstox-live", "indstocks-live", "kite-live"}
        or normalized.startswith("nubra")
    ):
        return "intraday"
    return None


def _latest_ts(current: Any, candidate: Any) -> Any:
    if not current:
        return candidate
    if not candidate:
        return current
    return candidate if str(candidate) > str(current) else current


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), max(1, size)):
        yield items[index : index + size]


def _should_preserve_active_buy(existing: sqlite3.Row, idea: dict[str, Any], row: dict[str, Any]) -> bool:
    existing_signal = str(existing["signal_type"] or "").upper()
    existing_status = str(existing["status"] or "").upper()
    incoming_signal = str(idea.get("signal_type") or "").upper()
    incoming_status = str(idea.get("status") or "").upper()
    action = str(row.get("action") or "").upper()
    if existing_signal != "BUY":
        return False
    if existing_status not in {"ACTIVE", "MONITORING", "TARGET_1_HIT", "TARGET_2_HIT"}:
        return False
    if action in {"SELL", "EXIT"} or incoming_signal == "EXIT" or incoming_status == "EXIT_SIGNAL":
        return False
    quality_gate = (idea.get("details") or {}).get("quality_gate") if isinstance(idea.get("details"), dict) else {}
    if action == "BUY" and isinstance(quality_gate, dict) and quality_gate.get("passed") is False:
        return False
    return action == "HOLD" or incoming_signal in {"WATCH", "NO_TRADE"} or incoming_status in {"WATCH", "MONITORING"}


def _is_duplicate_active_buy_refresh(existing: sqlite3.Row, idea: dict[str, Any], row: dict[str, Any], now_iso: str) -> bool:
    if str(existing["signal_type"] or "").upper() != "BUY":
        return False
    if str(existing["status"] or "").upper() not in {"ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT"}:
        return False
    if str(row.get("action") or "").upper() != "BUY":
        return False
    if str(idea.get("signal_type") or "").upper() != "BUY":
        return False
    first_seen = _parse_dt(existing["first_seen_at"])
    now_dt = _parse_dt(now_iso)
    if not first_seen or not now_dt:
        return True
    age_hours = (now_dt - first_seen).total_seconds() / 3600
    return age_hours < DUPLICATE_BUY_COOLDOWN_HOURS


def _strategy_plan_code(
    strategy: str,
    action: str,
    price: float,
    full: dict[str, Any],
    system_audit: dict[str, Any],
) -> str:
    name = str(strategy or "").lower()
    classification = ((system_audit.get("classification") or {}) if isinstance(system_audit.get("classification"), dict) else {}).get("classification")
    if action == "SELL" or "exit" in name or "risk" in name:
        return "defensive_exit_manager"
    if "btst" in name or "buy_today_sell_tomorrow" in name:
        return "btst_next_day"
    if "aggressive_relative_strength" in name or "relative_strength_breakout" in name or "fifty_two_week_high_momentum" in name:
        return "aggressive_rs_breakout"
    if "breakout" in name or "darvas" in name or "vcp" in name:
        return "confirmed_breakout"
    if "pullback" in name or "ema" in name or "continuation" in name:
        return "pullback_to_strength"
    if str(classification or "").upper() == "SPECULATIVE" or (0 < float(price or 0) <= 250):
        return "smallcap_momentum"
    return "institutional_quality_swing"
