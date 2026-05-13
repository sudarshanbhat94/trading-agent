from __future__ import annotations

import copy
import hashlib
import json
import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from .analysis_tools import deterministic_score_breakdown
from .config import Settings
from .llm_usage import build_llm_usage_event
from .models import Decision, utc_now


_PROVIDER_RATE_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class LLMEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str


HEALTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "service": {"type": "string", "maxLength": 40},
        "note": {"type": "string", "maxLength": 80},
    },
    "required": ["ok", "service", "note"],
}


SHORT_TEXT: dict[str, Any] = {"type": "string", "maxLength": 180}
MEDIUM_TEXT: dict[str, Any] = {"type": "string", "maxLength": 280}
SHORT_LIST: dict[str, Any] = {"type": "array", "maxItems": 4, "items": SHORT_TEXT}
GAP_LIST: dict[str, Any] = {"type": "array", "maxItems": 6, "items": SHORT_TEXT}


DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "strategy": {"type": "string", "maxLength": 80},
        "reason": MEDIUM_TEXT,
        "checklist": SHORT_LIST,
        "evidence": {"type": "array", "maxItems": 5, "items": SHORT_TEXT},
        "risk_checks": {"type": "array", "maxItems": 5, "items": SHORT_TEXT},
        "invalidators": SHORT_LIST,
        "signal_plan": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "bias": SHORT_TEXT,
                "entry_trigger": SHORT_TEXT,
                "exit_trigger": SHORT_TEXT,
                "timeframe": SHORT_TEXT,
            },
        },
        "confluence_score": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "total": {"type": "number"},
                "max": {"type": "number"},
                "rating": SHORT_TEXT,
                "why": SHORT_TEXT,
            },
        },
        "trade_plan": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entry": SHORT_TEXT,
                "stop_loss": SHORT_TEXT,
                "target": SHORT_TEXT,
                "position_size": SHORT_TEXT,
                "exit_rule": SHORT_TEXT,
                "time_stop": SHORT_TEXT,
            },
        },
        "monitoring_checklist": {"type": "array", "maxItems": 5, "items": SHORT_TEXT},
        "data_gaps": GAP_LIST,
    },
    "required": [
        "action",
        "confidence",
        "risk",
        "strategy",
        "reason",
        "checklist",
        "evidence",
        "risk_checks",
        "invalidators",
        "signal_plan",
        "confluence_score",
        "trade_plan",
        "monitoring_checklist",
        "data_gaps",
    ],
}


ROLLING_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "chunk_index": {"type": "number"},
        "key_evidence": {"type": "array", "items": SHORT_TEXT},
        "bullish_factors": {"type": "array", "items": SHORT_TEXT},
        "bearish_factors": {"type": "array", "items": SHORT_TEXT},
        "risk_flags": {"type": "array", "items": SHORT_TEXT},
        "missing_data": {"type": "array", "items": SHORT_TEXT},
        "trade_implication": MEDIUM_TEXT,
    },
}


class LLMResponseError(RuntimeError):
    def __init__(self, message: str, raw: Any | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class LLMBrain:
    def __init__(self, settings: Settings, db: Any | None = None) -> None:
        self.settings = settings
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.settings.llm_provider in {"deepseek", "groq"}

    @property
    def model(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_model
        return self.settings.deepseek_model

    @property
    def base_url(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_base_url
        return self.settings.deepseek_base_url

    @property
    def api_key(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_api_key
        return self.settings.deepseek_api_key

    def chat_completions_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if self.settings.llm_provider == "deepseek":
            return f"{base_url}/chat/completions"
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

    def _has_multiple_fallback_endpoints(self) -> bool:
        return False

    def _endpoint_candidates(self) -> list[LLMEndpoint]:
        if self.settings.llm_provider == "deepseek" and self.settings.deepseek_api_key:
            return [
                LLMEndpoint(
                    provider="deepseek",
                    model=self.settings.deepseek_model,
                    base_url=self.settings.deepseek_base_url,
                    api_key=self.settings.deepseek_api_key,
                )
            ]
        if self.settings.llm_provider == "groq" and self.settings.groq_api_key:
            return [
                LLMEndpoint(
                    provider="groq",
                    model=self.settings.groq_model,
                    base_url=self.settings.groq_base_url,
                    api_key=self.settings.groq_api_key,
                )
            ]
        return []

    async def test_connection(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "provider": self.settings.llm_provider,
                "model": self.model,
                "reason": "LLM is not enabled or the API key is not saved.",
            }

        payload = {
            "model": self.model,
            "temperature": 0,
            "top_p": 0.1,
            "max_tokens": self._test_max_tokens(),
            "_openstocks_usage_component": "llm_brain",
            "_openstocks_usage_purpose": "health_check",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return one strict JSON object only. The first character must be { and the last "
                        "character must be }. Do not include reasoning, markdown, or commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": f'Return exactly this JSON shape: {{"ok":true,"service":"{self._service_name()}","note":"ready"}}',
                },
            ],
        }
        self._apply_model_options(payload, schema=HEALTH_SCHEMA)
        started = perf_counter()
        url = self.chat_completions_url()
        timeout_seconds = min(max(self.settings.llm_timeout_seconds, 5), 180)
        meta: dict[str, Any] = {}
        try:
            content, meta = await self._chat_content_with_fallback(
                payload,
                timeout_seconds,
                schema=HEALTH_SCHEMA,
                require_json=True,
            )
            latency_ms = round((perf_counter() - started) * 1000)
            json_repaired = False
            try:
                parsed = self._parse_json_content(content)
            except LLMResponseError as parse_exc:
                repaired = await self._repair_json(
                    content,
                    (
                        'Convert the model response into exactly this JSON object if it indicates readiness: '
                        f'{{"ok":true,"service":"{self._service_name()}","note":"ready"}}. '
                        f'If it clearly indicates failure, return {{"ok":false,"service":"{self._service_name()}","note":"not ready"}}. '
                        "Return one JSON object only. Do not explain."
                    ),
                    schema=HEALTH_SCHEMA,
                )
                try:
                    parsed = self._parse_json_content(repaired)
                    json_repaired = True
                except LLMResponseError as repair_exc:
                    return {
                        "ok": True,
                        "provider": self.settings.llm_provider,
                        "model": meta.get("_llm_model", self.model),
                        "actual_provider": meta.get("_llm_provider", self.settings.llm_provider),
                        "attempts": meta.get("_llm_attempts", []),
                        "url": url,
                        "latency_ms": latency_ms,
                        "timeout_seconds": timeout_seconds,
                        "json_strict": False,
                        "json_repaired": False,
                        "warning": (
                            "Model endpoint responded, but did not obey strict JSON even after repair. "
                            "Trading decisions will use safe HOLD fallback on malformed output."
                        ),
                        "reason": f"non_strict_json; original_parse={parse_exc}; repair_failed={repair_exc}",
                        "raw_reply": str(content)[:1000],
                        "repair_reply": str(repaired)[:1000],
                    }
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "provider": self.settings.llm_provider,
                    "model": self.model,
                    "url": url,
                    "latency_ms": latency_ms,
                    "timeout_seconds": timeout_seconds,
                    "reason": "Model responded, but not with parseable JSON for the health check.",
                    "raw_reply": content[:1000],
                }
            return {
                "ok": bool(parsed.get("ok")),
                "provider": self.settings.llm_provider,
                "model": meta.get("_llm_model", self.model),
                "actual_provider": meta.get("_llm_provider", self.settings.llm_provider),
                "configured_model_chain": [endpoint.model for endpoint in self._endpoint_candidates()],
                "url": url,
                "latency_ms": latency_ms,
                "timeout_seconds": timeout_seconds,
                "json_repaired": json_repaired,
                "attempts": meta.get("_llm_attempts", []),
                "reply": parsed,
            }
        except LLMResponseError as exc:
            latency_ms = round((perf_counter() - started) * 1000)
            return {
                "ok": False,
                "provider": self.settings.llm_provider,
                "model": self.model,
                "url": url,
                "latency_ms": latency_ms,
                "timeout_seconds": timeout_seconds,
                "reason": _error_summary(exc),
                "configured_model_chain": [endpoint.model for endpoint in self._endpoint_candidates()],
                "attempts": _attempts_from_exception(exc),
                "raw": getattr(exc, "raw", None),
            }
        except httpx.HTTPStatusError as exc:
            try:
                reason = exc.response.text[:500]
            except httpx.ResponseNotRead:
                reason = f"HTTP {exc.response.status_code}"
            return {
                "ok": False,
                "provider": self.settings.llm_provider,
                "model": self.model,
                "url": url,
                "status_code": exc.response.status_code,
                "reason": reason,
            }
        except httpx.TimeoutException:
            latency_ms = round((perf_counter() - started) * 1000)
            return {
                "ok": False,
                "provider": self.settings.llm_provider,
                "model": self.model,
                "url": url,
                "latency_ms": latency_ms,
                "timeout_seconds": timeout_seconds,
                "configured_model_chain": [endpoint.model for endpoint in self._endpoint_candidates()],
                "reason": (
                    f"Timed out after {timeout_seconds}s. This model/endpoint is too slow or unavailable "
                    "for trading-cycle use. Try Reasoning Effort=none, a faster model, or a higher "
                    "timeout only for manual testing."
                ),
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": self.settings.llm_provider,
                "model": self.model,
                "url": url,
                "reason": f"{exc.__class__.__name__}: {exc}",
            }

    async def decide(self, context: dict[str, Any]) -> Decision:
        if not self.enabled:
            return self._hold_from_context(context, "LLM disabled")
        budget = self._decision_budget_seconds()
        retry_budget = min(max(budget // 2, 15), 60) if self._has_multiple_fallback_endpoints() else 0
        outer_budget = budget + retry_budget + 10 if self._has_multiple_fallback_endpoints() else budget
        try:
            return await asyncio.wait_for(self._decide_inner(context), timeout=outer_budget)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            return self._hold_from_context(
                context,
                f"LLM primary timed out after {outer_budget}s; deterministic gates remain active",
                exc,
            )

    async def _decide_inner(self, context: dict[str, Any]) -> Decision:
        if not self.enabled:
            return self._hold_from_context(context, "LLM disabled")

        prompt_context, rolling_meta = await self._decision_prompt_context(context)
        payload = {
            "model": self.model,
            "temperature": min(self.settings.llm_temperature, 0.2),
            "top_p": min(self.settings.llm_top_p, 0.7),
            "max_tokens": self._decision_max_tokens(),
            "_openstocks_usage_component": "llm_brain",
            "_openstocks_usage_purpose": "decision",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        _budget_decision_system_prompt(prompt_context)
                        if self.settings.llm_provider == "groq"
                        else _decision_system_prompt(prompt_context)
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_context, separators=(",", ":")),
                },
            ],
        }
        self._apply_model_options(payload, schema=DECISION_SCHEMA)
        prompt_audit = _llm_payload_audit(prompt_context, payload["messages"][0]["content"], payload)
        try:
            parsed = await self._chat_json(payload, retry_payload=self._compact_decision_retry_payload(context))
            parsed.update(rolling_meta)
            parsed["_llm_prompt_audit"] = prompt_audit
            requested_action = str(parsed.get("action", "HOLD")).upper()
            confidence = max(min(float(parsed.get("confidence", 0)), 1.0), 0.0)
            confidence_gate_passed = confidence >= self.settings.llm_primary_min_confidence
            action, policy_gates = _policy_gate_action(
                context=context,
                requested_action=requested_action,
                confidence=confidence,
                min_confidence=self.settings.llm_primary_min_confidence,
            )
            checklist = parsed.get("checklist", [])
            strategy = str(parsed.get("strategy") or context.get("best_strategy", {}).get("name") or "llm_primary")
            reason = str(parsed.get("reason", "no reason supplied"))
            failed_policy = _failed_gate_summary(policy_gates)
            if action != requested_action and failed_policy:
                reason = f"{reason} | policy_gate={failed_policy}"
            if checklist:
                reason = f"{reason} | checklist={checklist}"
            return Decision(
                symbol=context["symbol"],
                action=action,
                confidence=round(confidence, 3),
                price=float(context["quote"]["price"]),
                technical_score=float(context["technical_math"]["score"]),
                sentiment_score=float(context["sentiment"]["score"]),
                reason=f"LLM primary ({parsed.get('risk', 'UNKNOWN')}): {reason}"[:700],
                asof=utc_now(),
                strategy=strategy[:80],
                details_json=self._llm_primary_details_json(
                    context=context,
                    parsed=parsed,
                    requested_action=requested_action,
                    final_action=action,
                    confidence=confidence,
                    confidence_gate_passed=confidence_gate_passed,
                    policy_gates=policy_gates,
                ),
            )
        except Exception as exc:
            return self._hold_from_context(
                context,
                f"LLM primary failed safely: {_error_summary(exc)}",
                exc,
            )

    async def review(self, decision: Decision, context: dict[str, Any]) -> Decision:
        if not self.enabled:
            return decision
        candidate_decision = decision.to_dict()
        candidate_decision.pop("details_json", None)
        prompt_context, rolling_meta = await self._decision_prompt_context(context)
        payload = {
            "model": self.model,
            "temperature": min(self.settings.llm_temperature, 0.2),
            "top_p": min(self.settings.llm_top_p, 0.7),
            "max_tokens": self._review_max_tokens(),
            "_openstocks_usage_component": "llm_brain",
            "_openstocks_usage_purpose": "review",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        _review_system_prompt(prompt_context)
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "candidate_decision": candidate_decision,
                            "context": prompt_context,
                            "constraints": {
                                "dry_money_only": True,
                                "long_only": True,
                                "risk_layer_may_veto": True,
                            },
                        }
                    ),
                },
            ],
        }
        self._apply_model_options(payload, schema=DECISION_SCHEMA)
        prompt_audit = _llm_payload_audit(prompt_context, payload["messages"][0]["content"], payload)
        try:
            parsed = await self._chat_json(payload)
            parsed.update(rolling_meta)
            parsed["_llm_prompt_audit"] = prompt_audit
            action = str(parsed.get("action", decision.action)).upper()
            confidence = max(min(float(parsed.get("confidence", decision.confidence)), 1.0), 0.0)
            action, policy_gates = _policy_gate_action(
                context=context,
                requested_action=action,
                confidence=confidence,
                min_confidence=self.settings.llm_primary_min_confidence,
            )
            reason = str(parsed.get("reason", decision.reason))[:500]
            failed_policy = _failed_gate_summary(policy_gates)
            if action != str(parsed.get("action", decision.action)).upper() and failed_policy:
                reason = f"{reason} | policy_gate={failed_policy}"[:500]
            reviewed = Decision(
                symbol=decision.symbol,
                action=action,
                confidence=confidence,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=f"LLM review: {reason}",
                asof=decision.asof,
                strategy=decision.strategy,
                details_json=self._llm_review_details_json(decision, context, parsed, action, confidence, policy_gates),
            )
            return reviewed
        except Exception as exc:
            original = _decision_summary(decision)
            original["llm_review_error"] = _error_summary(exc)
            return Decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=0.0,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=f"LLM review failed, so OpenStocks forced HOLD and did not preserve the deterministic trade: {_error_summary(exc)}"[:700],
                asof=decision.asof,
                strategy=decision.strategy,
                details_json=_json_dumps(
                    {
                        "audit_version": 1,
                        "decision_path": "llm_review_failed",
                        "final_action": "HOLD",
                        "action_reason": "LLM review failed, so OpenStocks forced HOLD.",
                        "candidate_decision": original,
                        "context": _compact_context(context),
                        "sizing_grade": context.get("sizing_grade"),
                        "risk_gates": {
                            "llm_review_failed": True,
                            "deterministic_action_preserved": False,
                            "deterministic_action_blocked": True,
                            "sizing_grade": context.get("sizing_grade"),
                        },
                        "error_type": exc.__class__.__name__,
                        "error": str(exc)[:500],
                        "raw_response": getattr(exc, "raw", None),
                        "score_breakdown": deterministic_score_breakdown(context),
                        "llm_error": {
                            "error_type": exc.__class__.__name__,
                            "error": str(exc)[:500],
                            "raw_response": getattr(exc, "raw", None),
                        },
                    }
                ),
            )

    async def _decision_prompt_context(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.settings.llm_provider == "groq":
            return _groq_budget_context(context), {"_llm_analysis_mode": "groq_budget_context"}

        cycle_safe_context = _llm_prompt_context(context, profile="compact")
        cycle_safe_json = json.dumps(cycle_safe_context, default=str, separators=(",", ":"))
        if (
            not self.settings.llm_rolling_context_enabled
            or len(cycle_safe_json) <= self.settings.llm_rolling_context_threshold_chars
        ):
            return cycle_safe_context, {"_llm_analysis_mode": "single_context"}

        rich_context = _llm_prompt_context(context, profile="rich")
        rich_json = json.dumps(rich_context, default=str, separators=(",", ":"))
        chunks = _chunk_text(
            rich_json,
            max(int(self.settings.llm_rolling_context_chunk_chars or 7000), 1000),
        )
        max_chunks = int(self.settings.llm_rolling_context_max_chunks or 0)
        selected_chunks = chunks[: min(len(chunks), 2)] if max_chunks <= 0 else chunks[:max(max_chunks, 1)]
        summaries: list[dict[str, Any]] = []
        summary_attempts: list[dict[str, Any]] = []
        for index, chunk in enumerate(selected_chunks, start=1):
            payload = {
                "model": self.model,
                "temperature": 0,
                "top_p": 0.1,
                "max_tokens": max(600, min(self.settings.llm_max_tokens, 1200)),
                "_openstocks_usage_component": "llm_brain",
                "_openstocks_usage_purpose": "rolling_summary",
                "messages": [
	                    {
	                        "role": "system",
	                        "content": _rolling_summary_system_prompt(context),
	                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "chunk_index": index,
                                "chunk_count": len(chunks),
                                "symbol": context.get("symbol"),
                                "context_chunk": chunk,
                            },
                            separators=(",", ":"),
                        ),
                    },
                ],
            }
            self._apply_model_options(payload, schema=ROLLING_CONTEXT_SCHEMA)
            try:
                content, meta = await self._chat_content_with_fallback(
                    payload,
                    self._rolling_summary_timeout_seconds(len(selected_chunks)),
                    schema=ROLLING_CONTEXT_SCHEMA,
                    require_json=True,
                )
                summary = self._parse_json_content(content)
                summary["chunk_index"] = index
                summary["source_chars"] = len(chunk)
                summary["model"] = meta.get("_llm_model")
                summary["provider"] = meta.get("_llm_provider")
                summaries.append(_compact_rolling_summary(summary))
                summary_attempts.extend(meta.get("_llm_attempts", []))
            except Exception as exc:
                summaries.append(
                    {
                        "chunk_index": index,
                        "source_chars": len(chunk),
                        "summary_error": _error_summary(exc),
                        "fallback_excerpt": chunk[:800],
                    }
                )
                summary_attempts.extend(_attempts_from_exception(exc))

        rolling_context = _prune_empty(
            {
                "tool_protocol": "openstocks-rolling-decision-context-v1",
                "core_decision_context": _llm_prompt_context(context, profile="compact"),
                "rolling_context_coverage": {
                    "full_context_chars": len(rich_json),
                    "chunk_count": len(chunks),
                    "summarized_chunks": len(summaries),
                    "truncated_chunks": max(len(chunks) - len(selected_chunks), 0),
                    "chunk_chars": self.settings.llm_rolling_context_chunk_chars,
                    "method": "map_summarize_chunks_then_final_decision",
                },
                "rolling_evidence_summaries": summaries,
            }
        )
        return rolling_context, {
            "_llm_analysis_mode": "rolling_context",
            "_rolling_context": {
                "full_context_chars": len(rich_json),
                "chunk_count": len(chunks),
                "summarized_chunks": len(summaries),
                "truncated_chunks": max(len(chunks) - len(selected_chunks), 0),
                "summary_attempts": summary_attempts[-20:],
            },
        }

    async def _chat_json(
        self,
        payload: dict[str, Any],
        retry_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        decision_timeout = self._decision_budget_seconds()
        try:
            content, meta = await self._chat_content_with_fallback(
                payload,
                decision_timeout,
                schema=DECISION_SCHEMA,
                require_json=False,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            synthetic = _synthetic_safe_decision_from_text("", exc)
            synthetic["_json_synthetic"] = True
            synthetic["_llm_timeout"] = True
            synthetic["_json_repaired"] = False
            return synthetic
        except LLMResponseError as exc:
            initial_attempts = _attempts_from_exception(exc)
            should_try_compact_retry = (
                retry_payload is not None
                and (
                    not _all_endpoint_attempts_failed(exc)
                    or _attempts_have_timeout(initial_attempts)
                    or _attempts_have_payload_limit(initial_attempts)
                )
            )
            if should_try_compact_retry:
                try:
                    retry_content, retry_meta = await self._chat_content_with_fallback(
                        retry_payload,
                        min(max(decision_timeout // 2, 15), 60),
                        schema=DECISION_SCHEMA,
                        require_json=False,
                    )
                    parsed = _normalize_decision_payload(self._parse_json_content(retry_content))
                    parsed.update(retry_meta)
                    parsed["_llm_attempts"] = initial_attempts + retry_meta.get("_llm_attempts", [])
                    parsed["_json_retry"] = True
                    parsed["_json_retry_reason"] = _error_summary(exc)
                    return parsed
                except Exception as retry_exc:
                    retry_attempts = _attempts_from_exception(retry_exc)
                    synthetic = _synthetic_safe_decision_from_text(getattr(exc, "raw", ""), exc)
                    synthetic["_json_synthetic"] = True
                    synthetic["_json_repaired"] = False
                    synthetic["_json_retry_error"] = _error_summary(retry_exc)
                    synthetic["_llm_attempts"] = initial_attempts + retry_attempts
                    return synthetic
            synthetic = _synthetic_safe_decision_from_text(getattr(exc, "raw", ""), exc)
            synthetic["_json_synthetic"] = True
            synthetic["_json_repaired"] = False
            synthetic["_llm_attempts"] = initial_attempts
            return synthetic
        try:
            parsed = _normalize_decision_payload(self._parse_json_content(content))
            parsed.update(meta)
            return parsed
        except LLMResponseError as exc:
            repaired = ""
            synthetic = _synthetic_safe_decision_from_text(content, exc)
            synthetic.update(meta)
            if retry_payload is not None:
                try:
                    retry_content, retry_meta = await self._chat_content_with_fallback(
                        retry_payload,
                        min(max(decision_timeout // 2, 15), 60),
                        schema=DECISION_SCHEMA,
                        require_json=False,
                    )
                    parsed = _normalize_decision_payload(self._parse_json_content(retry_content))
                    parsed.update(retry_meta)
                    parsed["_llm_attempts"] = meta.get("_llm_attempts", []) + retry_meta.get("_llm_attempts", [])
                    parsed["_json_retry"] = True
                    parsed["_json_retry_reason"] = _error_summary(exc)
                    return parsed
                except Exception as retry_exc:
                    synthetic["_json_retry_error"] = _error_summary(retry_exc)
                    synthetic["_llm_attempts"] = meta.get("_llm_attempts", []) + _attempts_from_exception(retry_exc)
            try:
                repaired = await self._repair_json(content, schema=DECISION_SCHEMA)
            except Exception as repair_call_exc:
                synthetic["_json_repaired"] = False
                synthetic["_json_synthetic"] = True
                synthetic["_json_repair_error"] = _error_summary(repair_call_exc)
                synthetic["_json_repair_raw"] = ""
                return synthetic
            try:
                parsed = self._parse_json_content(repaired)
            except LLMResponseError as repair_exc:
                synthetic["_json_repaired"] = False
                synthetic["_json_synthetic"] = True
                synthetic["_json_repair_error"] = str(repair_exc)[:500]
                synthetic["_json_repair_raw"] = str(repaired)[:1000]
                return synthetic
            parsed["_json_repaired"] = True
            normalized = _normalize_decision_payload(parsed)
            normalized.update(meta)
            return normalized

    def _compact_decision_retry_payload(self, context: dict[str, Any]) -> dict[str, Any] | None:
        if self.settings.llm_provider not in {"deepseek", "groq"}:
            return None
        payload = {
            "model": self.model,
            "temperature": 0,
            "top_p": 0.1,
            "max_tokens": max(500, min(self.settings.llm_max_tokens, 800)),
            "_openstocks_usage_component": "llm_brain",
            "_openstocks_usage_purpose": "decision_retry",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only a minified JSON object. No markdown. No prose. No analysis text before JSON. "
                        "If uncertain, use HOLD. Required shape: "
                        '{"action":"HOLD","confidence":0.0,"risk":"HIGH","strategy":"name",'
                        '"reason":"short reason","checklist":[],"evidence":[],"risk_checks":[],'
                        '"invalidators":[],"signal_plan":{},"confluence_score":{},"trade_plan":{},'
                        '"monitoring_checklist":[],"data_gaps":[]}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        _groq_budget_context(context) if self.settings.llm_provider == "groq" else _compact_retry_context(context),
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        self._apply_model_options(payload, schema=DECISION_SCHEMA)
        return payload

    def _parse_json_content(self, content: Any) -> dict[str, Any]:
        raw_text = "" if content is None else str(content).strip()
        candidates = _json_object_candidates(raw_text)
        stripped = self._strip_json(content)
        if stripped and stripped not in candidates:
            candidates.insert(0, stripped)
        if raw_text and raw_text not in candidates:
            candidates.insert(0, raw_text)
        fallback_dict: dict[str, Any] | None = None
        last_error: json.JSONDecodeError | None = None
        for candidate in candidates:
            try:
                parsed_candidate = json.loads(candidate)
            except json.JSONDecodeError as exc:
                repaired_candidate = _escape_json_string_newlines(candidate)
                if repaired_candidate != candidate:
                    try:
                        parsed_candidate = json.loads(repaired_candidate)
                    except json.JSONDecodeError as repair_exc:
                        last_error = repair_exc
                        continue
                else:
                    last_error = exc
                    continue
            if not isinstance(parsed_candidate, dict):
                continue
            if _looks_like_llm_response_object(parsed_candidate):
                return parsed_candidate
            if fallback_dict is None:
                fallback_dict = parsed_candidate
        if fallback_dict is not None:
            return fallback_dict
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            repaired = _escape_json_string_newlines(stripped)
            if repaired != stripped:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, dict):
                        parsed["_json_repaired"] = True
                        return parsed
                except json.JSONDecodeError:
                    pass
            raise LLMResponseError(f"LLM returned non-JSON content: {str(last_error or exc)}", raw=str(content)[:3000]) from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError("LLM JSON response was not an object", raw=parsed)
        return parsed

    async def _repair_json(
        self,
        content: Any,
        system_content: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> str:
        if system_content is None:
            system_content = (
                "Convert the analyst text into one strict JSON object only. Do not explain. "
                "If the text is ambiguous or conflicted, set action to HOLD and confidence to 0.0. "
                "Required keys: action, confidence, risk, strategy, reason, checklist, evidence, "
                "risk_checks, invalidators, signal_plan, confluence_score, trade_plan, "
                "monitoring_checklist, data_gaps. Keep arrays to 5 short phrases, use no newline "
                "characters inside strings, and keep reason under 280 characters."
            )
        payload = {
            "model": self.model,
            "temperature": 0,
            "top_p": 0.1,
            "max_tokens": max(500, min(self.settings.llm_max_tokens, 900)),
            "_openstocks_usage_component": "llm_brain",
            "_openstocks_usage_purpose": "json_repair",
            "messages": [
                {
                    "role": "system",
                    "content": system_content,
                },
                {
                    "role": "user",
                    "content": str(content)[:6000],
                },
            ],
        }
        self._apply_model_options(payload, schema=schema)
        return await self._chat_content(
            payload,
            min(max(self.settings.llm_timeout_seconds, 10), 45),
            schema=schema,
            require_json=True,
        )

    async def _chat_content(
        self,
        payload: dict[str, Any],
        timeout_seconds: int,
        schema: dict[str, Any] | None = None,
        require_json: bool = False,
    ) -> str:
        content, _ = await self._chat_content_with_fallback(
            payload,
            timeout_seconds,
            schema=schema,
            require_json=require_json,
        )
        return content

    async def _chat_content_with_fallback(
        self,
        payload: dict[str, Any],
        timeout_seconds: int,
        schema: dict[str, Any] | None = None,
        require_json: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        endpoints = self._endpoint_candidates()
        if not endpoints:
            raise LLMResponseError("No configured LLM endpoint is available")
        attempts: list[dict[str, Any]] = []
        deadline = perf_counter() + max(float(timeout_seconds), 1.0)
        for index, endpoint in enumerate(endpoints, start=1):
            remaining = deadline - perf_counter()
            if remaining <= 0:
                attempts.append(
                    {
                        "provider": endpoint.provider,
                        "model": endpoint.model,
                        "status": "skipped",
                        "attempt": index,
                        "latency_ms": 0,
                        "error": f"LLM decision budget exhausted after {timeout_seconds}s",
                    }
                )
                break
            endpoint_payload = self._payload_for_endpoint(payload, endpoint, schema=schema)
            attempt_timeout = self._endpoint_attempt_timeout_seconds(endpoint, remaining)
            endpoints_left = len(endpoints) - index + 1
            if endpoints_left > 1:
                attempt_timeout = min(attempt_timeout, max(2.0, remaining / endpoints_left))
            started = perf_counter()
            try:
                if self._should_stream(endpoint_payload, endpoint):
                    content = await asyncio.wait_for(
                        self._chat_content_stream(endpoint_payload, attempt_timeout, endpoint),
                        timeout=attempt_timeout,
                    )
                else:
                    content = await self._chat_content_once(endpoint_payload, attempt_timeout, endpoint)
                if require_json:
                    self._parse_json_content(content)
                attempts.append(
                    {
                        "provider": endpoint.provider,
                        "model": endpoint.model,
                        "status": "ok",
                        "attempt": index,
                        "latency_ms": round((perf_counter() - started) * 1000),
                    }
                )
                self._log_llm_attempt(endpoint, payload, "ok", attempts[-1]["latency_ms"], attempt=index)
                return content, {
                    "_llm_provider": endpoint.provider,
                    "_llm_model": endpoint.model,
                    "_llm_attempts": attempts,
                }
            except Exception as exc:
                latency_ms = round((perf_counter() - started) * 1000)
                attempts.append(
                    {
                        "provider": endpoint.provider,
                        "model": endpoint.model,
                        "status": "failed",
                        "attempt": index,
                        "latency_ms": latency_ms,
                        "error": _error_summary(exc),
                    }
                )
                self._log_llm_attempt(endpoint, payload, "failed", latency_ms, error=_error_summary(exc), attempt=index)
                continue
        raise LLMResponseError("All configured LLM models failed", raw={"attempts": attempts})

    def _payload_for_endpoint(
        self,
        payload: dict[str, Any],
        endpoint: LLMEndpoint,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint_payload = copy.deepcopy(payload)
        if "max_completion_tokens" in endpoint_payload and "max_tokens" not in endpoint_payload:
            endpoint_payload["max_tokens"] = endpoint_payload.pop("max_completion_tokens")
        for key in ("guided_json", "response_format", "chat_template_kwargs", "reasoning_effort", "reasoning_format", "thinking"):
            endpoint_payload.pop(key, None)
        endpoint_payload["model"] = endpoint.model
        self._apply_model_options_for_endpoint(endpoint_payload, endpoint, schema=schema)
        return endpoint_payload

    async def _chat_content_once(self, payload: dict[str, Any], timeout_seconds: int, endpoint: LLMEndpoint) -> str:
        api_payload = _api_payload(payload)
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        started = perf_counter()
        lock = _provider_rate_lock(endpoint.provider) if endpoint.provider == "groq" else None
        if lock:
            await asyncio.to_thread(lock.acquire)
        try:
            await self._respect_provider_rate_limit(endpoint, api_payload)
            async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
                response = await client.post(
                    _chat_completions_url_for_endpoint(endpoint),
                    json=api_payload,
                )
                response.raise_for_status()
            data = response.json()
            latency_ms = round((perf_counter() - started) * 1000)
            choices = data.get("choices") or []
            if not choices:
                raise LLMResponseError("LLM response had no choices", raw=data)
            message = choices[0].get("message") or {}
            content = _first_text(message.get("content"))
            if content:
                self._record_usage(api_payload, data, content, endpoint, payload, latency_ms)
                return content
            reasoning = _first_text(message.get("reasoning_content"), message.get("reasoning"))
            if reasoning and "{" in reasoning and "}" in reasoning:
                self._record_usage(api_payload, data, reasoning, endpoint, payload, latency_ms)
                return reasoning
            if reasoning:
                raise LLMResponseError(
                    "LLM returned reasoning but no final JSON content",
                    raw={"reasoning_tail": reasoning[-1000:]},
                )
            raise LLMResponseError("LLM response had no text content", raw=message)
        finally:
            if lock:
                lock.release()

    async def _chat_content_stream(self, payload: dict[str, Any], timeout_seconds: int, endpoint: LLMEndpoint) -> str:
        stream_payload = _api_payload(payload)
        stream_payload["stream"] = True
        headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
        chunks: list[str] = []
        reasoning_chunks: list[str] = []
        started = perf_counter()
        await self._respect_provider_rate_limit(endpoint, stream_payload)
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            async with client.stream("POST", _chat_completions_url_for_endpoint(endpoint), json=stream_payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    parsed = json.loads(data)
                    choices = parsed.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = _first_text(delta.get("content"))
                    if content:
                        chunks.append(content)
                    reasoning = _first_text(delta.get("reasoning_content"), delta.get("reasoning"))
                    if reasoning:
                        reasoning_chunks.append(reasoning)
        content = "".join(chunks).strip()
        if content:
            self._record_usage(stream_payload, None, content, endpoint, payload, round((perf_counter() - started) * 1000))
            return content
        reasoning = "".join(reasoning_chunks).strip()
        if reasoning and "{" in reasoning and "}" in reasoning:
            self._record_usage(stream_payload, None, reasoning, endpoint, payload, round((perf_counter() - started) * 1000))
            return reasoning
        raise LLMResponseError(
            "LLM stream finished without final content",
            raw={"reasoning_tail": reasoning[-1000:] if reasoning else ""},
        )

    def _prompt_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return _llm_prompt_context(context, profile="compact")

    def _decision_max_tokens(self) -> int:
        if self.settings.llm_provider == "deepseek":
            return max(900, min(self.settings.llm_max_tokens, 4096))
        if self.settings.llm_provider == "groq":
            # Groq on-demand Qwen commonly enforces a tight TPM budget. Keep
            # the response compact so the decision call does not fail before
            # a usable JSON answer is returned.
            return max(550, min(self.settings.llm_max_tokens, 750))
        return max(350, min(self.settings.llm_max_tokens, 1400))

    def _review_max_tokens(self) -> int:
        if self.settings.llm_provider == "deepseek":
            return max(700, min(self.settings.llm_max_tokens, 3000))
        if self.settings.llm_provider == "groq":
            return max(900, min(self.settings.llm_max_tokens, 1600))
        return max(256, min(self.settings.llm_max_tokens, 1200))

    def _apply_model_options(self, payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
        self._apply_model_options_for_provider(payload, self.settings.llm_provider, self.model, schema=schema)

    def _apply_model_options_for_endpoint(
        self,
        payload: dict[str, Any],
        endpoint: LLMEndpoint,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self._apply_model_options_for_provider(payload, endpoint.provider, endpoint.model, schema=schema)

    def _apply_model_options_for_provider(
        self,
        payload: dict[str, Any],
        provider: str,
        model: str,
        schema: dict[str, Any] | None = None,
    ) -> None:
        if provider == "deepseek":
            self._apply_deepseek_options(payload, schema=schema)
            return
        if provider == "groq":
            self._apply_groq_options(payload, schema=schema)
            return
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

    def _apply_deepseek_options(self, payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        effort = self.settings.llm_reasoning_effort
        if effort in {"high", "max"}:
            payload["reasoning_effort"] = effort
        if self.settings.llm_thinking_enabled:
            payload["thinking"] = {"type": "enabled"}

    def _apply_groq_options(self, payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
        # Groq's JSON response_format can reject Qwen generations with
        # json_validate_failed before any text is returned. Let OpenStocks parse
        # and repair the model text instead so a provider-side formatting miss
        # does not stop a trading cycle.
        payload.pop("response_format", None)
        payload["reasoning_effort"] = "default"
        payload["reasoning_format"] = "hidden"

    def _record_usage(
        self,
        api_payload: dict[str, Any],
        response_data: dict[str, Any] | None,
        output_text: str,
        endpoint: LLMEndpoint,
        local_payload: dict[str, Any],
        latency_ms: int,
    ) -> None:
        if self.db is None:
            return
        try:
            event = build_llm_usage_event(
                component=str(local_payload.get("_openstocks_usage_component") or "llm_brain"),
                purpose=str(local_payload.get("_openstocks_usage_purpose") or "chat"),
                provider=endpoint.provider,
                model=endpoint.model,
                payload=api_payload,
                response_data=response_data,
                output_text=output_text,
                latency_ms=latency_ms,
                details={
                    "response_id": response_data.get("id") if isinstance(response_data, dict) else None,
                    "api_usage_present": bool(isinstance(response_data, dict) and response_data.get("usage")),
                },
            )
            try:
                from .request_context import current_llm_usage_scope, current_user_id

                event["user_id"] = current_user_id.get()
                event["scope_id"] = current_llm_usage_scope.get() or ""
            except Exception:
                event["user_id"] = None
                event["scope_id"] = ""
            self.db.insert_llm_usage(event)
        except Exception:
            return

    async def _respect_provider_rate_limit(self, endpoint: LLMEndpoint, api_payload: dict[str, Any]) -> None:
        if endpoint.provider != "groq" or self.db is None:
            return
        wait_seconds = await asyncio.to_thread(_provider_rate_wait_seconds, self.db, endpoint.provider, api_payload)
        if wait_seconds <= 0:
            return
        purpose = str(api_payload.get("_openstocks_usage_purpose") or "chat")
        try:
            self.db.insert_agent_log(
                "INFO",
                "llm",
                "llm_rate_limit_wait",
                "OpenStocks Brain is waiting for provider token capacity before retrying.",
                {
                    "provider": endpoint.provider,
                    "model": endpoint.model,
                    "purpose": purpose,
                    "wait_seconds": round(wait_seconds, 2),
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_seconds)

    def _log_llm_attempt(
        self,
        endpoint: LLMEndpoint,
        local_payload: dict[str, Any],
        status: str,
        latency_ms: int,
        *,
        error: str | None = None,
        attempt: int = 1,
    ) -> None:
        if self.db is None:
            return
        purpose = str(local_payload.get("_openstocks_usage_purpose") or "chat")
        if purpose not in {"decision", "decision_retry", "llm_review", "rolling_summary", "json_repair"}:
            return
        try:
            api_payload = _api_payload(local_payload)
            try:
                from .request_context import current_user_id

                user_id = current_user_id.get()
            except Exception:
                user_id = None
            self.db.insert_agent_log(
                "INFO" if status == "ok" else "WARNING",
                "llm",
                "llm_attempt",
                f"OpenStocks Brain {purpose} attempt {status}",
                {
                    "purpose": purpose,
                    "status": status,
                    "attempt": attempt,
                    "provider": endpoint.provider,
                    "model": endpoint.model,
                    "latency_ms": latency_ms,
                    "user_id": user_id,
                    "input_chars": len(json.dumps(api_payload, default=str, separators=(",", ":"))),
                    "max_tokens": api_payload.get("max_tokens") or api_payload.get("max_completion_tokens"),
                    "error": error,
                    "billable_tokens_recorded": status == "ok",
                },
            )
        except Exception:
            return

    def _should_stream(self, payload: dict[str, Any] | None = None, endpoint: LLMEndpoint | None = None) -> bool:
        if payload and ("guided_json" in payload or "response_format" in payload):
            return False
        provider = endpoint.provider if endpoint else self.settings.llm_provider
        return provider == "deepseek" and self.settings.llm_streaming_enabled

    def _service_name(self) -> str:
        if self.settings.llm_provider == "groq":
            return "groq-qwen"
        return "deepseek"

    def _test_max_tokens(self) -> int:
        if self.settings.llm_provider == "groq":
            return max(128, min(self.settings.llm_max_tokens, 512))
        if self.settings.llm_thinking_enabled:
            return max(512, min(self.settings.llm_max_tokens, 4096))
        return 128

    def _decision_budget_seconds(self) -> int:
        configured = int(self.settings.llm_timeout_seconds or 30)
        if self.settings.llm_rolling_context_enabled:
            return max(35, min(max(configured, 60), 120))
        return max(20, min(max(configured, 45), 120))

    def _rolling_summary_timeout_seconds(self, chunk_count: int) -> int:
        configured = int(self.settings.llm_timeout_seconds or 30)
        if chunk_count <= 0:
            return max(10, min(configured, 20))
        return max(15, min(max(configured // max(chunk_count + 1, 1), 15), 30))

    def _endpoint_attempt_timeout_seconds(self, endpoint: LLMEndpoint, remaining_seconds: float) -> float:
        if endpoint.provider == "deepseek":
            preferred = float(max(int(self.settings.llm_timeout_seconds or 60), 45))
        else:
            preferred = 10.0
        return max(2.0, min(preferred, remaining_seconds))

    def _strip_json(self, content: Any) -> str:
        text = "" if content is None else str(content).strip()
        if not text:
            raise LLMResponseError("LLM returned empty content")
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            return text[start : end + 1]
        return text

    def _hold_from_context(self, context: dict[str, Any], reason: str, exc: Exception | None = None) -> Decision:
        error_details = None
        if exc is not None:
            error_details = {
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:500],
                "raw_response": getattr(exc, "raw", None),
            }
        return Decision(
            symbol=context["symbol"],
            action="HOLD",
            confidence=0.0,
            price=float(context["quote"]["price"]),
            technical_score=float(context["technical_math"]["score"]),
            sentiment_score=float(context["sentiment"]["score"]),
            reason=reason,
            asof=utc_now(),
            strategy=str(context.get("best_strategy", {}).get("name") or "llm_primary")[:80],
            details_json=_json_dumps(
                {
                    "audit_version": 1,
                    "decision_path": "safe_hold",
                    "final_action": "HOLD",
                    "action_reason": reason,
                    "score_breakdown": deterministic_score_breakdown(context),
                    "context": _compact_context(context),
                    "llm_error": error_details,
                }
            ),
        )

    def _llm_primary_details_json(
        self,
        context: dict[str, Any],
        parsed: dict[str, Any],
        requested_action: str,
        final_action: str,
        confidence: float,
        confidence_gate_passed: bool,
        policy_gates: list[dict[str, Any]],
    ) -> str:
        llm_error = None
        synthetic_safe_hold = bool(parsed.get("_json_synthetic")) and final_action == "HOLD" and not parsed.get("_llm_timeout")
        if (
            not synthetic_safe_hold
            and (parsed.get("_json_synthetic") or parsed.get("_llm_timeout") or parsed.get("_json_repair_error") or parsed.get("_json_retry_error"))
        ):
            llm_error = {
                "error_type": "llm_primary_safe_fallback",
                "reason": parsed.get("reason"),
                "json_synthetic": bool(parsed.get("_json_synthetic")),
                "llm_timeout": bool(parsed.get("_llm_timeout")),
                "json_repair_error": parsed.get("_json_repair_error"),
                "json_retry_error": parsed.get("_json_retry_error"),
                "model_attempts": parsed.get("_llm_attempts", []),
            }
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": "llm_primary",
                "provider": parsed.get("_llm_provider", self.settings.llm_provider),
                "model": parsed.get("_llm_model", self.model),
                "configured_provider": self.settings.llm_provider,
                "configured_model": self.model,
                "model_attempts": parsed.get("_llm_attempts", []),
	                "analysis_mode": parsed.get("_llm_analysis_mode", "single_context"),
	                "rolling_context": parsed.get("_rolling_context"),
	                "llm_prompt_audit": parsed.get("_llm_prompt_audit"),
	                "requested_action": requested_action,
                "final_action": final_action,
                "action_reason": parsed.get("reason", "no reason supplied"),
                "confidence": round(confidence, 4),
                "json_repaired": bool(parsed.get("_json_repaired")),
                "json_retry": bool(parsed.get("_json_retry")),
                "json_retry_reason": parsed.get("_json_retry_reason"),
                "json_synthetic": bool(parsed.get("_json_synthetic")),
                "llm_timeout": bool(parsed.get("_llm_timeout")),
                "llm_error": llm_error,
                "confidence_gate": {
                    "minimum_required": self.settings.llm_primary_min_confidence,
                    "passed": confidence_gate_passed,
                    "effect": "requested action allowed" if confidence_gate_passed else "downgraded to HOLD",
                },
                "policy_gates": policy_gates,
                "score_breakdown": deterministic_score_breakdown(context),
                "sizing_grade": context.get("sizing_grade"),
                "llm_output": {
                    "risk": parsed.get("risk"),
                    "strategy": parsed.get("strategy"),
                    "reason": parsed.get("reason"),
                    "checklist": parsed.get("checklist", []),
                    "evidence": parsed.get("evidence", []),
                    "risk_checks": parsed.get("risk_checks", []),
                    "invalidators": parsed.get("invalidators", []),
                    "signal_plan": parsed.get("signal_plan", {}),
                    "confluence_score": parsed.get("confluence_score", {}),
                    "trade_plan": parsed.get("trade_plan", {}),
                    "monitoring_checklist": parsed.get("monitoring_checklist", []),
                    "data_gaps": parsed.get("data_gaps", []),
                    "json_repaired": bool(parsed.get("_json_repaired")),
                    "json_retry": bool(parsed.get("_json_retry")),
                    "json_retry_reason": parsed.get("_json_retry_reason"),
                    "json_synthetic": bool(parsed.get("_json_synthetic")),
                    "llm_timeout": bool(parsed.get("_llm_timeout")),
                    "provider": parsed.get("_llm_provider", self.settings.llm_provider),
                    "model": parsed.get("_llm_model", self.model),
                    "analysis_mode": parsed.get("_llm_analysis_mode", "single_context"),
                    "json_repair_error": parsed.get("_json_repair_error"),
                },
                "risk_gates": {
                    "dry_run": True,
                    "long_only": True,
                    "no_leverage": True,
                    "llm_policy_gates_passed": all(gate.get("passed", False) for gate in policy_gates),
                    "sizing_grade": context.get("sizing_grade"),
                    "broker_checks_after_decision": [
                        "daily_loss_limit",
                        "max_positions",
                        "max_position_pct",
                        "max_order_value_pct",
                        "available_cash",
                    ],
                },
                "context": _compact_context(context),
            }
        )

    def _llm_review_details_json(
        self,
        original: Decision,
        context: dict[str, Any],
        parsed: dict[str, Any],
        final_action: str,
        confidence: float,
        policy_gates: list[dict[str, Any]],
    ) -> str:
        return _json_dumps(
            {
                "audit_version": 1,
                "decision_path": "llm_review",
                "provider": parsed.get("_llm_provider", self.settings.llm_provider),
                "model": parsed.get("_llm_model", self.model),
                "configured_provider": self.settings.llm_provider,
                "configured_model": self.model,
                "model_attempts": parsed.get("_llm_attempts", []),
	                "analysis_mode": parsed.get("_llm_analysis_mode", "single_context"),
	                "rolling_context": parsed.get("_rolling_context"),
	                "llm_prompt_audit": parsed.get("_llm_prompt_audit"),
	                "candidate_decision": _decision_summary(original),
                "final_action": final_action,
                "confidence": round(confidence, 4),
                "json_repaired": bool(parsed.get("_json_repaired")),
                "json_retry": bool(parsed.get("_json_retry")),
                "json_retry_reason": parsed.get("_json_retry_reason"),
                "json_synthetic": bool(parsed.get("_json_synthetic")),
                "llm_timeout": bool(parsed.get("_llm_timeout")),
                "action_reason": parsed.get("reason", original.reason),
                "policy_gates": policy_gates,
                "score_breakdown": deterministic_score_breakdown(context),
                "sizing_grade": context.get("sizing_grade"),
                "risk_gates": {
                    "llm_policy_gates_passed": all(gate.get("passed", False) for gate in policy_gates),
                    "sizing_grade": context.get("sizing_grade"),
                },
                "llm_output": {
                    "risk": parsed.get("risk"),
                    "reason": parsed.get("reason"),
                    "evidence": parsed.get("evidence", []),
                    "risk_checks": parsed.get("risk_checks", []),
                    "invalidators": parsed.get("invalidators", []),
                    "signal_plan": parsed.get("signal_plan", {}),
                    "confluence_score": parsed.get("confluence_score", {}),
                    "trade_plan": parsed.get("trade_plan", {}),
                    "monitoring_checklist": parsed.get("monitoring_checklist", []),
                    "data_gaps": parsed.get("data_gaps", []),
                    "json_repaired": bool(parsed.get("_json_repaired")),
                    "json_retry": bool(parsed.get("_json_retry")),
                    "json_retry_reason": parsed.get("_json_retry_reason"),
                    "json_synthetic": bool(parsed.get("_json_synthetic")),
                    "llm_timeout": bool(parsed.get("_llm_timeout")),
                    "provider": parsed.get("_llm_provider", self.settings.llm_provider),
                    "model": parsed.get("_llm_model", self.model),
                    "analysis_mode": parsed.get("_llm_analysis_mode", "single_context"),
                    "json_repair_error": parsed.get("_json_repair_error"),
                },
                "context": _compact_context(context),
            }
        )


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    recent_candles = context.get("recent_candles", [])
    return {
        "symbol": context.get("symbol"),
        "company": context.get("company"),
        "market_region": context.get("market_region"),
        "currency": context.get("currency"),
        "sector": context.get("sector"),
        "exchange": context.get("exchange"),
        "quote": context.get("quote"),
        "position": context.get("position"),
        "technical_math": context.get("technical_math"),
        "candlestick_analysis": context.get("candlestick_analysis"),
        "best_strategy": context.get("best_strategy"),
        "strategy_signals": context.get("strategy_signals"),
        "sentiment": context.get("sentiment"),
        "global_market_context": context.get("global_market_context"),
        "institutional_context": context.get("institutional_context"),
        "market_breadth_context": context.get("market_breadth_context"),
        "macro_event_context": context.get("macro_event_context"),
        "timeframe_data": context.get("timeframe_data"),
        "sector_rotation": context.get("sector_rotation"),
        "delivery_data": context.get("delivery_data"),
        "system_gate_audit": context.get("system_gate_audit"),
        "full_spectrum_analysis": context.get("full_spectrum_analysis"),
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-5:],
    }


def _market_region_from_context(context: dict[str, Any]) -> str:
    region = str(context.get("market_region") or context.get("market") or "").upper()
    if region in {"US", "IN"}:
        return region
    exchange = str(context.get("exchange") or "").upper()
    if exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"}:
        return "US"
    quote = context.get("quote") or {}
    source = str(quote.get("source") if isinstance(quote, dict) else "").lower()
    if "yahoo" in source and exchange not in {"NSE", "BSE"}:
        return "US"
    return "IN"


def _decision_system_prompt(prompt_context: dict[str, Any]) -> str:
    market_region = _market_region_from_context(prompt_context)
    currency = "USD" if market_region == "US" else "INR"
    market_scope = "US equities" if market_region == "US" else "Indian equities"
    market_specific = (
        "For US equities, do not use or infer NSE delivery data, FII/DII flows, F&O stock option-chain gates, expiry-day rules, or rupee-based assumptions unless the supplied context explicitly marks an equivalent US feed as available. Treat omitted market-specific feeds as data gaps, not bearish signals. "
        if market_region == "US"
        else "For Indian equities, use supplied NSE delivery, sector rotation, options/OI, expiry, and institutional-feed context only when the feed says it is available. "
    )
    return (
        f"You are OpenStocks Intelligence v2.0, an institutional-style dry-run analyst for {market_scope}. "
        f"All price and plan values are in {currency}. Use only the supplied MCP-style tool context for this exact symbol and market; ignore unrelated universe noise. "
        "Use quote, candles, exact math indicators, candlestick facts, strategy_signals, sentiment/news, global market context, universe scan rank, full_spectrum_analysis, position, and risk limits. "
        f"{market_specific}"
        "You must explicitly use stage_analysis, entry_quality.entry_grade, breakout_quality.two_day_rule_failed, price_volume_divergence.climax_volume_top, timeframe_alignment.alignment_grade, sector_rotation.sector_tailwind, sector_rotation.sector_headwind, market_breadth.breadth_regime, delivery_accumulation.institutional_fingerprint when applicable, and options_oi.max_pain_distance_pct when applicable. "
        "BUY is permitted only in Stage2_Markup; entry grade D, failed breakout two-day rule, climax volume top, D timeframe alignment, or bear_confirmed breadth means HOLD. If options_oi.buy_suppressed is true because stock-level max pain is 8% or more below current price, HOLD. Evidence must state the value checked for each new gate. "
        "system_gate_audit is mandatory and absolute: if hard_blocked is true or overall_score_pct is below 55, your action must be HOLD for new entries. Sentiment score 0.0 means DATA_MISSING, not neutral. Use system_gate_audit.classification exactly as FUNDAMENTAL, MOMENTUM, or SPECULATIVE and respect its allocation cap. "
        "Never call a setup institutional quality unless system_gate_audit.institutional_quality_allowed is true. risk_checks must say whether each new gate passed or failed. If you recommend BUY while any new gate conflicts, acknowledge that conflict in reason. "
        "Return strict JSON only with keys action, confidence, risk, strategy, reason, checklist, evidence, risk_checks, invalidators, signal_plan, confluence_score, trade_plan, monitoring_checklist, and data_gaps. "
        "Your entire response must be one JSON object. The first character must be { and the last character must be }. Do not include markdown, scratchpad, reasoning text, or commentary. "
        "Keep it compact: no newline characters inside strings, reason <= 280 characters, each list <= 5 short phrases, and trade_plan/signal_plan values must be short strings. action must be BUY, SELL, or HOLD. confidence is 0..1. strategy must be one of the supplied strategy_signals names or best_strategy.name. risk must be LOW, MEDIUM, or HIGH. "
        "Respect confluence_score: below 10 means HOLD, 10-15 watchlist only, 16+ may trade, 18+ high conviction, 22+ maximum conviction. For new BUY decisions, also require institutional_scorecard.buy_ready=true; hard vetoes or failed must-pass gates override your opinion. "
        "If an existing long position is supplied, act as the exit/risk manager: SELL only when the hard stop, target/invalidation, technical breakdown, news shock, or risk-off regime justifies exit; otherwise HOLD with a concrete updated exit plan. "
        "Never recommend leverage, short-selling, futures, options, or ignoring risk gates."
    )


def _budget_decision_system_prompt(prompt_context: dict[str, Any]) -> str:
    market_region = _market_region_from_context(prompt_context)
    currency = "USD" if market_region == "US" else "INR"
    return (
        f"You are OpenStocks Brain for {market_region} equities. Prices are in {currency}. "
        "Return one strict minified JSON object only, with no markdown or reasoning text. "
        "Required keys: action, confidence, risk, strategy, reason, checklist, evidence, risk_checks, invalidators, signal_plan, confluence_score, trade_plan, monitoring_checklist, data_gaps. "
        "Use only supplied data. HARD rules: hard_blocked=true, Stage not Stage2_Markup, entry grade WATCH/D, failed two-day breakout, climax top, D alignment, bear_confirmed breadth, or buy_suppressed options means HOLD for new entries. "
        "Sentiment 0 means DATA_MISSING. Below confluence 16 is watch/HOLD unless already managing an open position. Keep reason under 180 chars and every list under 4 short items."
    )


def _review_system_prompt(prompt_context: dict[str, Any]) -> str:
    market_region = _market_region_from_context(prompt_context)
    currency = "USD" if market_region == "US" else "INR"
    return (
        f"You are an OpenStocks dry-run equity trading risk reviewer for {market_region} market positions. All prices are in {currency}. "
        "Return strict JSON only with action BUY, SELL, or HOLD; confidence from 0 to 1; reason; evidence; risk_checks; invalidators; signal_plan; confluence_score; trade_plan; monitoring_checklist; and data_gaps. "
        "Your entire response must be one JSON object. Do not include markdown, scratchpad, reasoning text, or commentary. Keep it compact: no newline characters inside strings, reason <= 280 characters, each list <= 5 short phrases, and trade_plan/signal_plan values must be short strings. "
        "For existing long positions, review whether to continue holding or exit based on stop, target, invalidation, technical breakdown, news, and market regime. Do not use India-only NSE delivery/options/expiry signals for US symbols unless the context explicitly says such data is available. Never recommend leverage, options, futures, or short-selling."
    )


def _rolling_summary_system_prompt(context: dict[str, Any]) -> str:
    market_region = _market_region_from_context(context)
    return (
        f"You are compressing one chunk of an OpenStocks {market_region} equity trading context for a later decision. "
        "Return one strict JSON object only. Preserve numeric thresholds, risk vetoes, data gaps, scorecard values, market-specific feed availability, news, indicators, and trade implications. Do not make the final BUY/SELL/HOLD decision."
    )


def _llm_payload_audit(prompt_context: dict[str, Any], system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    context_json = json.dumps(prompt_context, default=str, separators=(",", ":"))
    system_chars = len(system_prompt)
    context_chars = len(context_json)
    payload_chars = system_chars + context_chars
    return {
        "market_region": _market_region_from_context(prompt_context),
        "currency": prompt_context.get("currency"),
        "model": payload.get("model"),
        "mode": "exact_context_sent_to_llm",
        "system_prompt_chars": system_chars,
        "context_chars": context_chars,
        "estimated_input_tokens": round(payload_chars * 0.3, 1),
        "included_sections": sorted(prompt_context.keys()),
        "context_sha256": hashlib.sha256(context_json.encode("utf-8")).hexdigest(),
        "system_prompt": system_prompt,
        "user_context": prompt_context,
    }


def _llm_prompt_context(context: dict[str, Any], profile: str = "compact") -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    institutional = context.get("institutional_context") or {}
    institutional_flow = full.get("institutional_flow") or {}
    symbol = str(context.get("symbol") or "").upper()
    recent_candles = context.get("recent_candles") or []
    rich = profile == "rich"
    market_region = _market_region_from_context(context)
    currency = "USD" if market_region == "US" else "INR"
    delivery_data = context.get("delivery_data") if market_region == "IN" else {
        "available": False,
        "data_gap": "NSE delivery bhavcopy is not applicable to US equities.",
    }
    institutional_prompt = (
        {
            "enabled": institutional.get("enabled"),
            "source_quality": institutional.get("source_quality"),
            "market_bias": institutional.get("market_bias"),
            "symbol_flags": (institutional.get("symbol_flags") or {}).get(symbol, {}),
            "data_gaps": _limit_list(institutional.get("data_gaps"), 16 if rich else 8),
        }
        if market_region == "IN"
        else {
            "enabled": False,
            "source_quality": "not_applicable_to_us_market",
            "data_gaps": ["NSE/FII/DII delivery feeds are omitted for US equities."],
        }
    )
    return _prune_empty(
        {
            "tool_protocol": "openstocks-rich-decision-context-v1" if rich else "openstocks-compact-decision-context-v1",
            "symbol": context.get("symbol"),
            "company": context.get("company"),
            "market_region": market_region,
            "currency": currency,
            "sector": context.get("sector"),
            "exchange": context.get("exchange"),
            "quote": context.get("quote"),
            "position": context.get("position"),
            "technical_math": context.get("technical_math"),
            "candlestick_analysis": context.get("candlestick_analysis"),
            "strategy_signals": _top_strategy_signals(context.get("strategy_signals") or [], limit=8 if rich else 4),
            "best_strategy": context.get("best_strategy"),
            "sentiment": context.get("sentiment"),
            "global_market_context": _compact_global_context(context.get("global_market_context") or {}, limit=16 if rich else 8),
            "institutional_context": institutional_prompt,
            "market_breadth_context": context.get("market_breadth_context"),
            "macro_event_context": context.get("macro_event_context"),
            "timeframe_data": context.get("timeframe_data"),
            "sector_rotation": context.get("sector_rotation"),
            "delivery_data": delivery_data,
            "system_gate_audit": context.get("system_gate_audit"),
            "full_spectrum_analysis": _compact_full_spectrum_for_llm(
                full,
                institutional_flow if market_region == "IN" else {},
                rich=rich,
                market_region=market_region,
            ),
            "risk_limits": _compact_risk_limits(context.get("risk_limits") or {}),
            "universe_scan": context.get("universe_scan"),
            "recent_candles_tail": [_compact_candle(candle) for candle in recent_candles[-(16 if rich else 5):]],
        }
    )


def _compact_retry_context(context: dict[str, Any]) -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    trend = full.get("trend_context") or {}
    indicators = full.get("indicator_suite") or {}
    institutional_flow = full.get("institutional_flow") or {}
    confluence = full.get("confluence_score") or {}
    trade_plan = full.get("trade_plan") or {}
    stage = full.get("stage_analysis") or {}
    entry = full.get("entry_quality") or {}
    breakout = full.get("breakout_quality") or {}
    divergence = full.get("price_volume_divergence") or {}
    sector = full.get("sector_rotation") or context.get("sector_rotation") or {}
    delivery = full.get("delivery_accumulation") or context.get("delivery_data") or {}
    breadth = full.get("market_breadth") or context.get("market_breadth_context") or {}
    macro_event = full.get("macro_event_context") or context.get("macro_event_context") or {}
    scorecard = full.get("institutional_scorecard") or {}
    market_region = _market_region_from_context(context)
    currency = "USD" if market_region == "US" else "INR"
    if market_region == "US":
        delivery = {
            "available": False,
            "data_gap": "NSE delivery bhavcopy is not applicable to US equities.",
        }
        institutional_flow = {}
    return _prune_empty(
        {
            "symbol": context.get("symbol"),
            "market_region": market_region,
            "currency": currency,
            "quote": context.get("quote"),
            "position": context.get("position"),
            "technical_math": context.get("technical_math"),
            "candles": context.get("candlestick_analysis"),
            "top_strategies": _top_strategy_signals(context.get("strategy_signals") or [], limit=3),
            "best_strategy": _short_object(context.get("best_strategy") or {}, ["name", "score", "direction", "confidence"], 80),
            "sentiment": context.get("sentiment"),
            "global_regime": _compact_global_context(context.get("global_market_context") or {}, limit=5),
            "system_gate_audit": _compact_system_gate_audit(context.get("system_gate_audit") or {}),
            "decision_gates": _prune_empty(
                {
                    "stage": stage.get("stage"),
                    "stage_buy_permitted": stage.get("buy_permitted"),
                    "entry_grade": entry.get("entry_grade"),
                    "breakout_quality": breakout.get("breakout_quality"),
                    "two_day_rule_failed": breakout.get("two_day_rule_failed"),
                    "climax_volume_top": divergence.get("climax_volume_top"),
                    "alignment_grade": (trend.get("timeframe_alignment") or {}).get("alignment_grade"),
                    "sector_tier": sector.get("sector_tier"),
                    "sector_stage": sector.get("sector_stage"),
                    "sector_tailwind": sector.get("sector_tailwind"),
                    "sector_headwind": sector.get("sector_headwind"),
                    "breadth_regime": breadth.get("breadth_regime"),
                    "delivery_bias": delivery.get("bias") or delivery.get("net_bias"),
                    "delivery_fingerprint": delivery.get("institutional_fingerprint") or delivery.get("fingerprint"),
                    "options_buy_suppressed": (full.get("options_oi") or {}).get("buy_suppressed"),
                    "expiry_day": macro_event.get("is_expiry_day"),
                    "earnings_days_away": macro_event.get("earnings_days_away"),
                }
            ),
            "scores": _prune_empty(
                {
                    "combined": (context.get("score_breakdown") or {}).get("combined"),
                    "confluence_total": confluence.get("total"),
                    "confluence_tier": confluence.get("tier"),
                    "institutional_total": scorecard.get("total_score"),
                    "institutional_buy_ready": scorecard.get("buy_ready"),
                    "delivery_score": delivery.get("delivery_score"),
                    "sector_rotation_score": sector.get("sector_rotation_score"),
                    "divergence_score": divergence.get("divergence_score"),
                    "entry_quality_score": entry.get("quality_score"),
                }
            ),
            "technical_state": _compact_technical_state(trend, indicators, full),
            "entry_and_levels": _compact_entry_levels(entry, breakout, full.get("key_levels") or {}),
            "risk_and_events": _compact_risk_events(full.get("risk_overrides") or {}, full, macro_event),
            "institutional_context": _compact_institutional_for_llm(
                scorecard,
                full.get("institutional_structure") or {},
                delivery,
                institutional_flow,
                rich=False,
            ),
            "market_context": _compact_market_context_for_llm(
                breadth,
                sector,
                full.get("liquidity_profile") or {},
                full.get("backtest_snapshot") or {},
            ),
            "trade_plan": {
                "direction": trade_plan.get("direction"),
                "entry_zone": trade_plan.get("entry_zone"),
                "stop_loss": trade_plan.get("stop_loss"),
                "targets": _limit_list(trade_plan.get("targets"), 3),
                "invalidation": trade_plan.get("invalidation"),
            },
            "universe_scan": context.get("universe_scan"),
            "risk_limits": _compact_risk_limits(context.get("risk_limits") or {}),
        }
    )


def _groq_budget_context(context: dict[str, Any]) -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    trend = full.get("trend_context") or {}
    indicators = full.get("indicator_suite") or {}
    confluence = full.get("confluence_score") or {}
    trade_plan = full.get("trade_plan") or {}
    stage = full.get("stage_analysis") or {}
    entry = full.get("entry_quality") or {}
    breakout = full.get("breakout_quality") or {}
    divergence = full.get("price_volume_divergence") or {}
    sector = full.get("sector_rotation") or context.get("sector_rotation") or {}
    delivery = full.get("delivery_accumulation") or context.get("delivery_data") or {}
    breadth = full.get("market_breadth") or context.get("market_breadth_context") or {}
    macro_event = full.get("macro_event_context") or context.get("macro_event_context") or {}
    scorecard = full.get("institutional_scorecard") or {}
    try:
        score_breakdown = context.get("score_breakdown") or deterministic_score_breakdown(context)
    except Exception:
        score_breakdown = {}
    market_region = _market_region_from_context(context)
    if market_region == "US":
        delivery = {"available": False, "data_gap": "NSE delivery not applicable to US equities."}
    quote = context.get("quote") or {}
    technical = context.get("technical_math") or {}
    sentiment = context.get("sentiment") or {}
    risk_overrides = full.get("risk_overrides") or {}
    options_oi = full.get("options_oi") or {}
    recent_candles = context.get("recent_candles") or []
    return _prune_empty(
        {
            "symbol": context.get("symbol"),
            "company": context.get("company"),
            "market_region": market_region,
            "currency": "USD" if market_region == "US" else "INR",
            "quote": _short_object(quote, ["price", "close", "volume", "source", "ts"], 80),
            "position": _short_object(context.get("position") or {}, ["qty", "avg_price", "market_price", "unrealized_pnl"], 80),
            "technical": _prune_empty(
                {
                    "score": technical.get("score"),
                    "trend": technical.get("trend"),
                    "rsi": technical.get("rsi"),
                    "sma_fast": technical.get("sma_fast"),
                    "sma_slow": technical.get("sma_slow"),
                    "momentum_pct": technical.get("momentum_pct"),
                    "atr_pct": indicators.get("atr_pct"),
                    "adx": indicators.get("adx"),
                    "volume_ratio_20": indicators.get("volume_ratio_20"),
                }
            ),
            "strategies": _top_strategy_signals(context.get("strategy_signals") or [], limit=3),
            "best_strategy": _short_object(context.get("best_strategy") or {}, ["name", "score", "direction", "confidence"], 80),
            "sentiment": _short_object(sentiment, ["score", "confidence", "headline_count", "data_source", "label"], 120),
            "must_pass_gates": _prune_empty(
                {
                    "hard_blocked": (context.get("system_gate_audit") or {}).get("hard_blocked"),
                    "overall_score_pct": (context.get("system_gate_audit") or {}).get("overall_score_pct"),
                    "classification": ((context.get("system_gate_audit") or {}).get("classification") or {}).get("classification"),
                    "active_flags": _limit_list((context.get("system_gate_audit") or {}).get("active_flags"), 5),
                    "stage": stage.get("stage"),
                    "stage_buy_permitted": stage.get("buy_permitted"),
                    "entry_grade": entry.get("entry_grade"),
                    "breakout_quality": breakout.get("breakout_quality"),
                    "two_day_rule_failed": breakout.get("two_day_rule_failed"),
                    "climax_volume_top": divergence.get("climax_volume_top"),
                    "alignment_grade": (trend.get("timeframe_alignment") or {}).get("alignment_grade"),
                    "breadth_regime": breadth.get("breadth_regime"),
                    "delivery_bias": delivery.get("bias") or delivery.get("net_bias"),
                    "delivery_fingerprint": delivery.get("institutional_fingerprint") or delivery.get("fingerprint"),
                    "options_buy_suppressed": options_oi.get("buy_suppressed"),
                    "earnings_days_away": macro_event.get("earnings_days_away"),
                    "expiry_day": macro_event.get("is_expiry_day"),
                    "no_new_longs": risk_overrides.get("no_new_longs"),
                    "risk_flags": _limit_list(risk_overrides.get("flags"), 6),
                }
            ),
            "scores": _prune_empty(
                {
                    "combined": score_breakdown.get("combined"),
                    "confluence_total": confluence.get("total"),
                    "confluence_tier": confluence.get("tier"),
                    "institutional_score": scorecard.get("total_score"),
                    "institutional_buy_ready": scorecard.get("buy_ready"),
                    "delivery_score": delivery.get("delivery_score"),
                    "sector_rotation_score": sector.get("sector_rotation_score"),
                    "divergence_score": divergence.get("divergence_score"),
                    "entry_quality_score": entry.get("quality_score"),
                }
            ),
            "market_context": _prune_empty(
                {
                    "sector": context.get("sector"),
                    "sector_tier": sector.get("sector_tier"),
                    "sector_stage": sector.get("sector_stage"),
                    "sector_tailwind": sector.get("sector_tailwind"),
                    "sector_headwind": sector.get("sector_headwind"),
                    "pct_above_50dma": breadth.get("pct_above_50dma"),
                    "advance_decline_ratio": breadth.get("advance_decline_ratio"),
                }
            ),
            "trade_plan": _prune_empty(
                {
                    "entry_zone": trade_plan.get("entry_zone"),
                    "stop_loss": trade_plan.get("stop_loss"),
                    "targets": _limit_list(trade_plan.get("targets"), 3),
                    "invalidation": trade_plan.get("invalidation"),
                }
            ),
            "data_quality": _short_object(full.get("data_quality") or {}, ["coverage", "analysis_candle_count", "daily_candle_count", "weekly_candle_count"], 80),
            "data_gaps": _limit_list(full.get("data_gaps"), 4),
            "recent_closes": [
                _short_object(candle, ["ts", "close", "volume"], 80)
                for candle in recent_candles[-3:]
                if isinstance(candle, dict)
            ],
        }
    )


def _compact_system_gate_audit(audit: dict[str, Any]) -> dict[str, Any]:
    classification = audit.get("classification") or {}
    return _prune_empty(
        {
            "hard_blocked": audit.get("hard_blocked"),
            "active_flags": _limit_list(audit.get("active_flags"), 8),
            "hard_blocks": [
                _prune_empty(
                    {
                        "flag": block.get("flag"),
                        "reason": block.get("reason"),
                    }
                )
                for block in (audit.get("hard_blocks") or [])[:5]
                if isinstance(block, dict)
            ],
            "overall_score_pct": audit.get("overall_score_pct"),
            "overall_grade": audit.get("overall_grade"),
            "institutional_quality_allowed": audit.get("institutional_quality_allowed"),
            "classification": _prune_empty(
                {
                    "classification": classification.get("classification"),
                    "max_allocation_multiplier": classification.get("max_allocation_multiplier"),
                    "reason": classification.get("reason"),
                }
            ),
            "sentiment": audit.get("sentiment"),
            "entry": audit.get("entry"),
            "mtf": audit.get("mtf"),
            "delivery": audit.get("delivery"),
        }
    )


def _compact_global_context(global_context: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    return _prune_empty(
        {
            "enabled": global_context.get("enabled"),
            "risk_score": global_context.get("risk_score"),
            "confidence": global_context.get("confidence"),
            "regime": global_context.get("regime"),
            "signals": _limit_list(global_context.get("signals"), limit),
            "data_gaps": _limit_list(global_context.get("data_gaps"), limit),
        }
    )


def _compact_full_spectrum_for_llm(
    full: dict[str, Any],
    institutional_flow: dict[str, Any],
    rich: bool = False,
    market_region: str = "IN",
) -> dict[str, Any]:
    trend = full.get("trend_context") or {}
    scorecard = full.get("institutional_scorecard") or {}
    confluence = full.get("confluence_score") or {}
    trade_plan = full.get("trade_plan") or {}
    risk_overrides = full.get("risk_overrides") or {}
    stage = full.get("stage_analysis") or {}
    entry = full.get("entry_quality") or {}
    breakout = full.get("breakout_quality") or {}
    divergence = full.get("price_volume_divergence") or {}
    sector = full.get("sector_rotation") or {}
    breadth = full.get("market_breadth") or {}
    delivery = full.get("delivery_accumulation") or {}
    if market_region == "US":
        delivery = {
            "available": False,
            "data_gap": "NSE delivery bhavcopy is not applicable to US equities.",
        }
    macro_event = full.get("macro_event_context") or {}
    indicators = full.get("indicator_suite") or {}
    return _prune_empty(
        {
            "decision_gates": _prune_empty(
                {
                    "stage": stage.get("stage"),
                    "stage_confidence": stage.get("stage_confidence"),
                    "stage_buy_permitted": stage.get("buy_permitted"),
                    "entry_grade": entry.get("entry_grade"),
                    "breakout_quality": breakout.get("breakout_quality"),
                    "two_day_rule_failed": breakout.get("two_day_rule_failed"),
                    "climax_volume_top": divergence.get("climax_volume_top"),
                    "timeframe_alignment_grade": (trend.get("timeframe_alignment") or {}).get("alignment_grade"),
                    "sector_tailwind": sector.get("sector_tailwind"),
                    "sector_headwind": sector.get("sector_headwind"),
                    "breadth_regime": breadth.get("breadth_regime"),
                    "delivery_fingerprint": delivery.get("institutional_fingerprint") or delivery.get("fingerprint"),
                    "market_region": market_region,
                    "expiry_day": macro_event.get("is_expiry_day"),
                    "earnings_days_away": macro_event.get("earnings_days_away"),
                    "no_new_longs": risk_overrides.get("no_new_longs"),
                    "risk_flags": _limit_list(risk_overrides.get("flags"), 10),
                }
            ),
            "scores": _prune_empty(
                {
                    "confluence_total": confluence.get("total"),
                    "confluence_max": confluence.get("max"),
                    "confluence_tier": confluence.get("tier"),
                    "institutional_total": scorecard.get("total_score"),
                    "institutional_normalized": scorecard.get("normalized_score"),
                    "institutional_buy_ready": scorecard.get("buy_ready"),
                    "institutional_verdict": scorecard.get("verdict"),
                    "delivery_score": delivery.get("delivery_score"),
                    "sector_rotation_score": sector.get("sector_rotation_score"),
                    "breadth_score": breadth.get("breadth_score"),
                    "divergence_score": divergence.get("divergence_score"),
                    "entry_quality_score": entry.get("quality_score"),
                }
            ),
            "technical_state": _compact_technical_state(trend, indicators, full),
            "entry_and_levels": _compact_entry_levels(entry, breakout, full.get("key_levels") or {}),
            "risk_and_events": _compact_risk_events(risk_overrides, full, macro_event),
            "institutional_context": _compact_institutional_for_llm(
                scorecard,
                full.get("institutional_structure") or {},
                delivery,
                institutional_flow,
                rich=rich,
            ),
            "market_context": _compact_market_context_for_llm(
                breadth,
                sector,
                full.get("liquidity_profile") or {},
                full.get("backtest_snapshot") or {},
            ),
            "trade_plan": _prune_empty(
                {
                    "direction": trade_plan.get("direction"),
                    "horizon": trade_plan.get("horizon"),
                    "entry_zone": trade_plan.get("entry_zone"),
                    "stop_loss": trade_plan.get("stop_loss"),
                    "targets": _limit_list(trade_plan.get("targets"), 3),
                    "invalidation": trade_plan.get("invalidation"),
                    "exit_plan": trade_plan.get("exit_plan"),
                }
            ),
            "data_quality": full.get("data_quality"),
            "monitoring_checklist": _limit_list(full.get("monitoring_checklist"), 5 if rich else 3),
            "data_gaps": _limit_list(full.get("data_gaps"), 6 if rich else 3),
            "requirement_coverage": _coverage_summary(full.get("requirement_coverage") or {}) if rich else None,
        }
    )


def _compact_technical_state(
    trend: dict[str, Any],
    indicators: dict[str, Any],
    full: dict[str, Any],
) -> dict[str, Any]:
    alignment = trend.get("timeframe_alignment") or {}
    timeframes = alignment.get("timeframes") or {}
    return _prune_empty(
        {
            "trend": trend.get("daily") or trend.get("structure"),
            "alignment_grade": alignment.get("alignment_grade"),
            "timeframes": {
                key: _prune_empty(
                    {
                        "direction": value.get("direction"),
                        "strength": value.get("strength"),
                        "price_vs_20sma": value.get("price_vs_20sma"),
                    }
                )
                for key, value in timeframes.items()
                if isinstance(value, dict)
            },
            "rsi_14": indicators.get("rsi_14"),
            "adx": indicators.get("adx"),
            "atr_pct": indicators.get("atr_pct"),
            "macd_bias": (indicators.get("macd") or {}).get("bias"),
            "volume_ratio_20": indicators.get("volume_ratio_20"),
            "obv_slope": indicators.get("obv_slope"),
            "cmf_20": indicators.get("cmf_20"),
            "candles": _prune_empty(
                {
                    "patterns": (full.get("candlestick_v2") or {}).get("patterns"),
                    "score": (full.get("candlestick_v2") or {}).get("score"),
                    "confirmation": (full.get("candlestick_v2") or {}).get("confirmation"),
                }
            ),
            "relative_strength_bias": (full.get("relative_strength") or {}).get("bias"),
            "relative_strength_pct": (full.get("relative_strength") or {}).get("relative_strength_pct"),
        }
    )


def _compact_entry_levels(entry: dict[str, Any], breakout: dict[str, Any], levels: dict[str, Any]) -> dict[str, Any]:
    return _prune_empty(
        {
            "entry_grade": entry.get("entry_grade"),
            "distance_from_pivot_pct": entry.get("distance_from_pivot_pct"),
            "volume_confirmation": entry.get("volume_confirmation"),
            "close_position": entry.get("last_close_position_in_range"),
            "breakout_quality": breakout.get("breakout_quality"),
            "two_day_rule_failed": breakout.get("two_day_rule_failed"),
            "prior_resistance": breakout.get("prior_resistance"),
            "false_breakout_risk_score": breakout.get("false_breakout_risk_score"),
            "nearest_support": levels.get("nearest_support"),
            "nearest_resistance": levels.get("nearest_resistance"),
            "distance_to_support_pct": levels.get("distance_to_support_pct"),
            "distance_to_resistance_pct": levels.get("distance_to_resistance_pct"),
            "risk_reward_from_current": levels.get("risk_reward_from_current"),
        }
    )


def _compact_risk_events(
    risk_overrides: dict[str, Any],
    full: dict[str, Any],
    macro_event: dict[str, Any],
) -> dict[str, Any]:
    conflicts = full.get("signal_conflicts") or {}
    event_risk = full.get("corporate_event_risk") or {}
    options_oi = full.get("options_oi") or {}
    return _prune_empty(
        {
            "no_new_longs": risk_overrides.get("no_new_longs"),
            "risk_flags": _limit_list(risk_overrides.get("flags"), 10),
            "conflict_severity": conflicts.get("severity"),
            "conflicts": _limit_list(conflicts.get("conflicts"), 5),
            "expiry_day": macro_event.get("is_expiry_day"),
            "event_risk_score": macro_event.get("event_risk_score"),
            "recommended_action": macro_event.get("recommended_action"),
            "corporate_event_high_impact": event_risk.get("high_impact_risk"),
            "options_bias": options_oi.get("bias"),
            "fno_ban": options_oi.get("fno_ban"),
        }
    )


def _compact_institutional_for_llm(
    scorecard: dict[str, Any],
    structure: dict[str, Any],
    delivery: dict[str, Any],
    institutional_flow: dict[str, Any],
    rich: bool = False,
) -> dict[str, Any]:
    return _prune_empty(
        {
            "scorecard_reasons": _limit_list(scorecard.get("reasons"), 5 if rich else 3),
            "wyckoff_phase": structure.get("wyckoff_phase"),
            "market_structure": structure.get("market_structure"),
            "liquidity_sweep": structure.get("liquidity_sweep"),
            "smart_money_bias": structure.get("smart_money_bias"),
            "delivery_bias": delivery.get("bias"),
            "delivery_score": delivery.get("delivery_score"),
            "delivery_fingerprint": delivery.get("institutional_fingerprint") or delivery.get("fingerprint"),
            "flow_quality": institutional_flow.get("source_quality"),
            "market_bias": institutional_flow.get("market_bias"),
            "symbol_flags": institutional_flow.get("symbol_flags"),
            "data_gaps": _limit_list(institutional_flow.get("data_gaps"), 5 if rich else 3),
        }
    )


def _compact_market_context_for_llm(
    breadth: dict[str, Any],
    sector: dict[str, Any],
    liquidity: dict[str, Any],
    backtest: dict[str, Any],
) -> dict[str, Any]:
    return _prune_empty(
        {
            "breadth_regime": breadth.get("breadth_regime"),
            "pct_above_50dma": breadth.get("pct_above_50dma"),
            "advance_decline_ratio": breadth.get("advance_decline_ratio"),
            "breadth_thrust": breadth.get("breadth_thrust"),
            "sector_rank": sector.get("sector_rank"),
            "sector_tier": sector.get("sector_tier"),
            "sector_stage": sector.get("sector_stage"),
            "sector_tailwind": sector.get("sector_tailwind"),
            "sector_headwind": sector.get("sector_headwind"),
            "liquidity_bucket": liquidity.get("liquidity_bucket"),
            "volume_ratio_20": liquidity.get("volume_ratio_20"),
            "avg_traded_value_20": liquidity.get("avg_traded_value_20"),
            "backtest_win_rate": backtest.get("win_rate"),
            "backtest_expectancy": backtest.get("expectancy"),
        }
    )


def _compact_indicators(indicators: dict[str, Any]) -> dict[str, Any]:
    return _prune_empty(
        {
            "atr": indicators.get("atr"),
            "atr_pct": indicators.get("atr_pct"),
            "adx": indicators.get("adx"),
            "rsi_14": indicators.get("rsi_14"),
            "stochastic": indicators.get("stochastic"),
            "cci_20": indicators.get("cci_20"),
            "macd": indicators.get("macd"),
            "bollinger": indicators.get("bollinger"),
            "ichimoku": indicators.get("ichimoku"),
            "moving_averages": indicators.get("moving_averages"),
            "volume_ratio_20": indicators.get("volume_ratio_20"),
            "volume_profile_proxy": indicators.get("volume_profile_proxy"),
            "divergence_proxy": indicators.get("divergence_proxy"),
            "obv_slope": indicators.get("obv_slope"),
            "cmf_20": indicators.get("cmf_20"),
        }
    )


def _compact_risk_limits(risk_limits: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "max_positions",
        "max_position_pct",
        "max_order_value_pct",
        "stop_loss_pct",
        "take_profit_pct",
        "daily_loss_limit_pct",
        "min_llm_confidence",
        "global_risk_weight",
        "institutional_risk_weight",
    }
    return {key: risk_limits.get(key) for key in keep if key in risk_limits}


def _top_strategy_signals(signals: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    ranked = sorted(signals, key=lambda item: abs(float(item.get("score", 0.0) or 0.0)), reverse=True)
    output: list[dict[str, Any]] = []
    for item in ranked[:limit]:
        output.append(
            _prune_empty(
                {
                    "name": item.get("name"),
                    "score": item.get("score"),
                    "direction": item.get("direction"),
                    "confidence": item.get("confidence"),
                    "notes": _limit_list(item.get("notes"), 6 if limit > 6 else 4),
                }
            )
        )
    return output


def _coverage_summary(coverage: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in coverage.items():
        if isinstance(value, dict):
            summary[key] = _prune_empty(
                {
                    "status": value.get("status"),
                    "gap": value.get("gap"),
                }
            )
        else:
            summary[key] = value
    return summary


def _compact_candle(candle: Any) -> dict[str, Any]:
    if not isinstance(candle, dict):
        return {}
    return _prune_empty(
        {
            "ts": candle.get("ts"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
            "source": candle.get("source"),
        }
    )


def _limit_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def _prune_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != {} and item != []
    }


def _policy_gate_action(
    context: dict[str, Any],
    requested_action: str,
    confidence: float,
    min_confidence: float,
) -> tuple[str, list[dict[str, Any]]]:
    action = requested_action if requested_action in {"BUY", "SELL", "HOLD"} else "HOLD"
    position = context.get("position") or {}
    has_position = float(position.get("qty") or 0) > 0
    full_spectrum = context.get("full_spectrum_analysis") or {}
    confluence = full_spectrum.get("confluence_score") or {}
    risk_overrides = full_spectrum.get("risk_overrides") or {}
    scorecard = full_spectrum.get("institutional_scorecard") or {}
    stage = full_spectrum.get("stage_analysis") or {}
    entry = full_spectrum.get("entry_quality") or {}
    breakout = full_spectrum.get("breakout_quality") or {}
    divergence = full_spectrum.get("price_volume_divergence") or {}
    alignment = ((full_spectrum.get("trend_context") or {}).get("timeframe_alignment") or {})
    options_oi = full_spectrum.get("options_oi") or {}
    system_gate_audit = context.get("system_gate_audit") or {}
    confluence_total = int(confluence.get("total", 0) or 0)
    gates: list[dict[str, Any]] = [
        {
            "gate": "valid_action",
            "passed": requested_action in {"BUY", "SELL", "HOLD"},
            "value": requested_action,
            "required": "BUY, SELL, or HOLD",
        },
        {
            "gate": "min_llm_confidence",
            "passed": action == "HOLD" or confidence >= min_confidence,
            "value": round(confidence, 4),
            "required": min_confidence,
        },
        {
            "gate": "dry_money_long_only",
            "passed": action != "SELL" or has_position,
            "value": action,
            "required": "SELL only closes an existing long; no short-selling",
        },
    ]
    if action == "BUY":
        gates.extend(
            [
                {
                    "gate": "system_hard_blocks",
                    "passed": not system_gate_audit.get("hard_blocked"),
                    "value": system_gate_audit.get("hard_blocks", []),
                    "required": "no hard blocks from data, entry, earnings, delivery, MTF, or capital rules",
                },
                {
                    "gate": "stage_buy_permitted",
                    "passed": bool(stage.get("buy_permitted")),
                    "value": stage.get("stage"),
                    "required": "Stage2_Markup buy_permitted=true",
                },
                {
                    "gate": "entry_grade_gate",
                    "passed": (system_gate_audit.get("entry") or {}).get("effective_entry_grade") in {"A", "B", "C"},
                    "value": {
                        "entry_grade": entry.get("entry_grade"),
                        "effective_entry_grade": (system_gate_audit.get("entry") or {}).get("effective_entry_grade"),
                    },
                    "required": "effective entry grade A, B, or C; WATCH/D/missing blocked",
                },
                {
                    "gate": "overall_quality_gate",
                    "passed": float(system_gate_audit.get("overall_score_pct") or 0.0) >= 55.0,
                    "value": {
                        "overall_score_pct": system_gate_audit.get("overall_score_pct"),
                        "overall_grade": system_gate_audit.get("overall_grade"),
                    },
                    "required": "overall production-readiness score >= 55%",
                },
                {
                    "gate": "breakout_quality_gate",
                    "passed": not breakout.get("two_day_rule_failed"),
                    "value": breakout.get("two_day_rule_failed"),
                    "required": "two_day_rule_failed=false",
                },
                {
                    "gate": "climax_volume_gate",
                    "passed": not divergence.get("climax_volume_top"),
                    "value": divergence.get("climax_volume_top"),
                    "required": "climax_volume_top=false",
                },
                {
                    "gate": "timeframe_alignment_gate",
                    "passed": alignment.get("alignment_grade") != "D",
                    "value": alignment.get("alignment_grade"),
                    "required": "alignment grade B+ for standard entries; C only at speculative size; D blocked",
                },
                {
                    "gate": "options_max_pain_gate",
                    "passed": not options_oi.get("buy_suppressed"),
                    "value": {
                        "source": options_oi.get("audit_label") or options_oi.get("source"),
                        "max_pain": options_oi.get("max_pain"),
                        "max_pain_distance_pct": options_oi.get("max_pain_distance_pct"),
                    },
                    "required": "max pain not 8% or more below current price",
                },
                {
                    "gate": "full_spectrum_confluence",
                    "passed": confluence_total >= 16,
                    "value": confluence_total,
                    "required": ">= 16/26",
                },
                {
                    "gate": "institutional_scorecard_buy_ready",
                    "passed": bool(scorecard.get("buy_ready")),
                    "value": {
                        "score": scorecard.get("total_score"),
                        "grade": scorecard.get("grade"),
                        "failed": scorecard.get("must_pass_failed", []),
                        "hard_veto": (scorecard.get("hard_veto") or {}).get("failed", []),
                    },
                    "required": "buy_ready=true, score >=75/100, hard veto clear, must-pass gates clear",
                },
                {
                    "gate": "no_new_longs_override",
                    "passed": not risk_overrides.get("no_new_longs"),
                    "value": risk_overrides.get("flags", []),
                    "required": "no active no-new-longs flag",
                },
                {
                    "gate": "no_existing_long_position",
                    "passed": not has_position,
                    "value": position.get("qty", 0),
                    "required": "0",
                },
            ]
        )
    if action in {"BUY", "SELL"} and not all(gate["passed"] for gate in gates):
        return "HOLD", gates
    return action, gates


def _failed_gate_summary(gates: list[dict[str, Any]]) -> str:
    failed = [str(gate.get("gate")) for gate in gates if not gate.get("passed")]
    return ", ".join(failed)


def _decision_summary(decision: Decision) -> dict[str, Any]:
    data = decision.to_dict()
    data.pop("details_json", None)
    return data


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _api_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if not str(key).startswith(("_openstocks_", "_opentrade_"))
    }


def _json_object_candidates(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(raw[start : index + 1])
                start = None
    return list(reversed(candidates))


def _looks_like_llm_response_object(value: dict[str, Any]) -> bool:
    keys = set(value.keys())
    if {"action", "confidence"} <= keys:
        return True
    if {"reason", "risk", "strategy"} & keys and {"evidence", "risk_checks", "trade_plan"} & keys:
        return True
    if {"ok", "service", "note"} <= keys:
        return True
    return False


def _provider_rate_lock(provider: str) -> threading.Lock:
    lock = _PROVIDER_RATE_LOCKS.get(provider)
    if lock is None:
        lock = threading.Lock()
        _PROVIDER_RATE_LOCKS[provider] = lock
    return lock


def _provider_rate_wait_seconds(db: Any, provider: str, api_payload: dict[str, Any]) -> float:
    if provider != "groq":
        return 0.0
    estimated_tokens = _estimate_payload_tokens(api_payload)
    if estimated_tokens <= 0:
        return 0.0
    window_seconds = 65
    soft_limit = 5600
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    try:
        with db.connect() as conn:
            row = conn.execute(
                """
                select coalesce(sum(total_tokens), 0) as tokens, max(ts) as latest_ts
                from llm_usage_events
                where provider = ? and ts >= ?
                """,
                (provider, cutoff.isoformat()),
            ).fetchone()
    except Exception:
        return 0.0
    recent_tokens = int((row["tokens"] if hasattr(row, "keys") else row[0]) or 0)
    latest_ts = (row["latest_ts"] if hasattr(row, "keys") else row[1]) if row else None
    if recent_tokens + estimated_tokens <= soft_limit:
        return 0.0
    latest = _parse_iso_datetime(latest_ts)
    if latest is None:
        return float(window_seconds)
    age = max((datetime.now(timezone.utc) - latest).total_seconds(), 0.0)
    return max(float(window_seconds) - age + 1.0, 0.0)


def _estimate_payload_tokens(api_payload: dict[str, Any]) -> int:
    prompt_chars = 0
    for message in api_payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        prompt_chars += len(content if isinstance(content, str) else json.dumps(content, default=str))
    output_budget = int(api_payload.get("max_tokens") or api_payload.get("max_completion_tokens") or 0)
    return int((prompt_chars * 0.3) + output_budget)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _chat_completions_url_for_endpoint(endpoint: LLMEndpoint) -> str:
    base_url = endpoint.base_url.rstrip("/")
    if endpoint.provider == "deepseek":
        return f"{base_url}/chat/completions"
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _split_model_chain(value: str) -> list[str]:
    models = [item.strip() for item in str(value or "").replace("\n", ",").split(",")]
    return _unique([model for model in models if model])


def _attempts_from_exception(exc: Exception) -> list[dict[str, Any]]:
    raw = getattr(exc, "raw", None)
    if isinstance(raw, dict) and isinstance(raw.get("attempts"), list):
        return raw["attempts"]
    return []


def _all_endpoint_attempts_failed(exc: Exception) -> bool:
    return isinstance(exc, LLMResponseError) and str(exc) == "All configured LLM models failed"


def _rate_limited_attempts(attempts: list[dict[str, Any]]) -> bool:
    errors = " ".join(str(item.get("error") or "") for item in attempts).lower()
    return "429" in errors or "too many requests" in errors or "rate limit" in errors


def _attempts_have_payload_limit(attempts: list[dict[str, Any]]) -> bool:
    errors = " ".join(str(item.get("error") or "") for item in attempts).lower()
    return (
        "413" in errors
        or "payload too large" in errors
        or "request too large" in errors
        or "tokens per minute" in errors
        or "tpm" in errors
    )


def _attempts_have_timeout(attempts: list[dict[str, Any]]) -> bool:
    errors = " ".join(str(item.get("error") or "") for item in attempts).lower()
    return "timeout" in errors or any(int(item.get("latency_ms") or 0) >= 7500 for item in attempts)


def _chunk_text(text: str, chunk_chars: int) -> list[str]:
    if chunk_chars <= 0:
        return [text]
    return [text[index : index + chunk_chars] for index in range(0, len(text), chunk_chars)] or [""]


def _compact_rolling_summary(summary: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "chunk_index",
        "source_chars",
        "provider",
        "model",
        "key_evidence",
        "bullish_factors",
        "bearish_factors",
        "risk_flags",
        "missing_data",
        "trade_implication",
    }
    output = {key: summary.get(key) for key in keep if key in summary}
    for key in ("key_evidence", "bullish_factors", "bearish_factors", "risk_flags", "missing_data"):
        output[key] = _short_list(output.get(key), limit=6, length=180)
    if "trade_implication" in output:
        output["trade_implication"] = _short_scalar(output["trade_implication"], 280)
    return _prune_empty(output)


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            joined = "".join(parts).strip()
            if joined:
                return joined
    return ""


def _normalize_decision_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    action = str(parsed.get("action") or "HOLD").upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        action = "HOLD"
    try:
        confidence = max(min(float(parsed.get("confidence", 0.0) or 0.0), 1.0), 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    risk = str(parsed.get("risk") or "MEDIUM").upper()
    if risk not in {"LOW", "MEDIUM", "HIGH"}:
        risk = "MEDIUM"

    normalized: dict[str, Any] = {
        "action": action,
        "confidence": confidence,
        "risk": risk,
        "strategy": _short_scalar(parsed.get("strategy") or "llm_primary", 80),
        "reason": _short_scalar(parsed.get("reason") or "no reason supplied", 280),
        "checklist": _short_list(parsed.get("checklist"), limit=4),
        "evidence": _short_list(parsed.get("evidence"), limit=5),
        "risk_checks": _short_list(parsed.get("risk_checks"), limit=5),
        "invalidators": _short_list(parsed.get("invalidators"), limit=4),
        "signal_plan": _short_object(
            parsed.get("signal_plan"),
            ["bias", "entry_trigger", "exit_trigger", "timeframe"],
        ),
        "confluence_score": _short_score_object(parsed.get("confluence_score")),
        "trade_plan": _short_object(
            parsed.get("trade_plan"),
            ["entry", "stop_loss", "target", "position_size", "exit_rule", "time_stop"],
        ),
        "monitoring_checklist": _short_list(parsed.get("monitoring_checklist"), limit=5),
        "data_gaps": _short_list(parsed.get("data_gaps"), limit=6),
    }
    for key in (
        "_json_repaired",
        "_json_retry",
        "_json_retry_reason",
        "_json_retry_error",
        "_json_synthetic",
        "_llm_timeout",
        "_llm_provider",
        "_llm_model",
        "_llm_attempts",
        "_llm_analysis_mode",
        "_rolling_context",
        "_json_repair_error",
        "_json_repair_raw",
    ):
        if key in parsed:
            normalized[key] = parsed[key]
    return normalized


def _short_list(value: Any, limit: int, length: int = 180) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in items[:limit]:
        text = _short_scalar(item, length)
        if text:
            output.append(text)
    return output


def _short_object(value: Any, keys: list[str], length: int = 120) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, str] = {}
    for key in keys:
        if key in value:
            output[key] = _short_scalar(value.get(key), length)
    if output:
        return output
    for key, item in list(value.items())[:6]:
        short_key = _short_scalar(key, 40)
        if short_key:
            output[short_key] = _short_scalar(item, length)
    return output


def _short_score_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key in ("total", "max"):
        if key not in value:
            continue
        try:
            output[key] = float(value[key])
        except (TypeError, ValueError):
            output[key] = _short_scalar(value[key], 80)
    for key in ("rating", "why"):
        if key in value:
            output[key] = _short_scalar(value[key], 120)
    return output


def _short_scalar(value: Any, length: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= length:
        return text
    return text[: max(length - 3, 0)].rstrip() + "..."


def _escape_json_string_newlines(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\":
            output.append(char)
            escaped = True
            continue
        if char == '"':
            output.append(char)
            in_string = False
            continue
        if char == "\n":
            output.append("\\n")
            continue
        if char == "\r":
            output.append("\\n")
            continue
        if char == "\t":
            output.append("\\t")
            continue
        output.append(char)
    return "".join(output)


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.text[:180]
        except Exception:
            detail = ""
        base = f"HTTPStatusError: HTTP {exc.response.status_code}"
        return f"{base} {detail}".strip()
    message = str(exc).strip()
    if message:
        return f"{exc.__class__.__name__}: {message[:240]}"
    return exc.__class__.__name__


def _synthetic_safe_decision_from_text(content: Any, exc: Exception) -> dict[str, Any]:
    text = str(content or "")
    explicit = re.search(r"""["']?action["']?\s*[:=]\s*["']?(BUY|SELL|HOLD)["']?""", text, re.IGNORECASE)
    action = explicit.group(1).upper() if explicit else "HOLD"
    if action in {"BUY", "SELL"}:
        action = "HOLD"
    snippet = " ".join(text.split())[:500]
    attempts = _attempts_from_exception(exc)
    is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)) or "timeout" in exc.__class__.__name__.lower()
    if _all_endpoint_attempts_failed(exc):
        rate_limited = _rate_limited_attempts(attempts)
        reason = (
            "LLM provider unavailable/rate-limited, so OpenStocks used the safe fallback HOLD. "
            f"error={_error_summary(exc)[:180]}"
        )
        checklist = [
            "llm_endpoint_failed",
            "rate_limited" if rate_limited else "endpoint_timeout_or_unavailable",
            "safe_hold_fallback_used",
        ]
        evidence = [
            "All configured model endpoints failed before valid JSON was returned.",
            "No BUY/SELL is allowed without a completed LLM primary decision.",
        ]
        risk_checks = ["LLM brain unavailable; deterministic scanner cannot execute new trades in primary mode."]
        data_gaps = ["llm_provider_rate_limited" if rate_limited else "llm_provider_unavailable"]
        if _attempts_have_payload_limit(attempts):
            reason = (
                "LLM request exceeded the provider token budget, so OpenStocks retried compactly or used safe HOLD. "
                f"error={_error_summary(exc)[:180]}"
            )
            checklist = ["llm_payload_too_large", "compact_retry_needed", "safe_hold_fallback_used"]
            evidence = [
                "The provider rejected the first request before billable token usage was returned.",
                "No BUY/SELL is allowed without a completed LLM primary decision.",
            ]
            risk_checks = ["Oversized LLM request cannot override deterministic risk gates."]
            data_gaps = ["llm_payload_too_large"]
    elif is_timeout:
        reason = (
            "LLM timed out before returning a strict decision JSON, so OpenStocks used the safe fallback HOLD. "
            f"timeout={_error_summary(exc)[:180]}"
        )
        checklist = ["llm_timeout", "safe_hold_fallback_used", "deterministic_gates_preserved"]
        evidence = [
            "The model endpoint did not finish inside the trading-cycle LLM budget.",
            "No BUY/SELL is allowed from an incomplete LLM response.",
        ]
        risk_checks = ["Timed-out LLM output cannot override deterministic risk gates."]
        data_gaps = ["llm_timeout_no_final_json"]
    else:
        reason = (
            "LLM returned malformed or non-JSON output, so OpenStocks used the safe fallback HOLD. "
            f"parse_error={_error_summary(exc)[:180]}"
        )
        checklist = ["strict_json_failed", "json_repair_attempted", "safe_hold_fallback_used"]
        evidence = [
            "The model endpoint responded, but the response was not a valid decision JSON object.",
            f"raw_excerpt={snippet}",
        ]
        risk_checks = ["No BUY/SELL is allowed from malformed free-form model text."]
        data_gaps = ["llm_response_not_strict_json"]
    return {
        "action": action,
        "confidence": 0.0,
        "risk": "HIGH",
        "strategy": "",
        "reason": reason,
        "checklist": checklist,
        "evidence": evidence,
        "risk_checks": risk_checks,
        "invalidators": ["A future cycle returns valid strict JSON with passed policy gates."],
        "signal_plan": {"action": "HOLD", "source": "synthetic_safe_fallback"},
        "confluence_score": {},
        "trade_plan": {},
        "monitoring_checklist": ["Retry next cycle with fresh market data and strict JSON repair."],
        "data_gaps": data_gaps,
    }
