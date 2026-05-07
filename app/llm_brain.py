from __future__ import annotations

import copy
import json
import asyncio
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from .analysis_tools import deterministic_score_breakdown
from .config import Settings
from .models import Decision, utc_now


DEFAULT_NVIDIA_MODEL_CHAIN = [
    "deepseek-ai/deepseek-v4-pro",
    "moonshotai/kimi-k2.6",
    "deepseek-ai/deepseek-v4-flash",
    "z-ai/glm-5.1",
    "minimaxai/minimax-m2.7",
    "mistralai/mistral-medium-3.5-128b",
]


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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        if self.settings.llm_provider == "nvidia" and self.settings.llm_model_fallback_enabled:
            return bool(self.settings.nvidia_api_key or self.settings.groq_api_key)
        if self.settings.llm_provider == "groq":
            return bool(self.settings.groq_api_key)
        if self.settings.llm_provider == "nvidia":
            return bool(self.settings.nvidia_api_key)
        if self.settings.llm_provider == "openai_compatible":
            return bool(self.settings.llm_api_key)
        return False

    @property
    def model(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_model
        if self.settings.llm_provider == "nvidia":
            return self._nvidia_models()[0]
        return self.settings.llm_model

    @property
    def base_url(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_base_url
        if self.settings.llm_provider == "nvidia":
            return self.settings.nvidia_base_url
        return self.settings.llm_base_url

    @property
    def api_key(self) -> str:
        if self.settings.llm_provider == "groq":
            return self.settings.groq_api_key
        if self.settings.llm_provider == "nvidia":
            return self.settings.nvidia_api_key
        return self.settings.llm_api_key

    def chat_completions_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

    def _nvidia_models(self) -> list[str]:
        configured = _split_model_chain(self.settings.nvidia_model_chain)
        if configured:
            return configured
        if self.settings.nvidia_model:
            return [self.settings.nvidia_model]
        return list(DEFAULT_NVIDIA_MODEL_CHAIN)

    def _endpoint_candidates(self) -> list[LLMEndpoint]:
        endpoints: list[LLMEndpoint] = []
        if self.settings.llm_provider == "nvidia":
            if self.settings.nvidia_api_key:
                models = self._nvidia_models() if self.settings.llm_model_fallback_enabled else [self.settings.nvidia_model]
                for model in _unique([model for model in models if model]):
                    endpoints.append(
                        LLMEndpoint(
                            provider="nvidia",
                            model=model,
                            base_url=self.settings.nvidia_base_url,
                            api_key=self.settings.nvidia_api_key,
                        )
                    )
            if self.settings.llm_model_fallback_enabled and self.settings.groq_api_key:
                endpoints.append(
                    LLMEndpoint(
                        provider="groq",
                        model=self.settings.groq_model,
                        base_url=self.settings.groq_base_url,
                        api_key=self.settings.groq_api_key,
                    )
                )
            return endpoints
        if self.settings.llm_provider == "groq" and self.settings.groq_api_key:
            return [
                LLMEndpoint(
                    provider="groq",
                    model=self.settings.groq_model,
                    base_url=self.settings.groq_base_url,
                    api_key=self.settings.groq_api_key,
                )
            ]
        if self.settings.llm_provider == "openai_compatible" and self.settings.llm_api_key:
            return [
                LLMEndpoint(
                    provider="openai_compatible",
                    model=self.settings.llm_model,
                    base_url=self.settings.llm_base_url,
                    api_key=self.settings.llm_api_key,
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
        try:
            return await asyncio.wait_for(self._decide_inner(context), timeout=budget)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            return self._hold_from_context(
                context,
                f"LLM primary timed out after {budget}s; deterministic gates remain active",
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
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are OpenTrade Intelligence v2.0, an institutional-style dry-run analyst for Indian equities. "
                        "Use the supplied MCP-style tool context: quote, candles, exact math indicators, "
                        "candlestick facts, strategy_signals, sentiment, global market context, free institutional "
                        "feed context, universe scan rank, full_spectrum_analysis, position, and risk limits. "
                        "You must explicitly use stage_analysis, entry_quality.entry_grade, breakout_quality.two_day_rule_failed, "
                        "price_volume_divergence.climax_volume_top, timeframe_alignment.alignment_grade, "
                        "sector_rotation.sector_tailwind, sector_rotation.sector_headwind, market_breadth.breadth_regime, "
                        "and delivery_accumulation.institutional_fingerprint. BUY is permitted only in Stage2_Markup; "
                        "entry grade D, failed breakout two-day rule, climax volume top, D timeframe alignment, or "
                        "bear_confirmed breadth means HOLD. Evidence must state the value checked for each new gate. "
                        "risk_checks must say whether each new gate passed or failed. If you recommend BUY while any "
                        "new gate conflicts, acknowledge that conflict in reason. "
                        "Return strict JSON only with keys action, confidence, risk, strategy, reason, checklist, "
                        "evidence, risk_checks, invalidators, signal_plan, confluence_score, trade_plan, "
                        "monitoring_checklist, and data_gaps. "
                        "Your entire response must be one JSON object. The first character must be { and the last "
                        "character must be }. Do not include markdown, scratchpad, reasoning text, or commentary. "
                        "Keep it compact: no newline characters inside strings, reason <= 280 characters, "
                        "each list <= 5 short phrases, and trade_plan/signal_plan values must be short strings. "
                        "action must be BUY, SELL, or HOLD. confidence is 0..1. "
                        "strategy must be one of the supplied strategy_signals names or best_strategy.name. "
                        "risk must be LOW, MEDIUM, or HIGH. "
                        "reason must be concise; evidence must list the concrete inputs that support the action. "
                        "risk_checks must list the gates that passed or failed. "
                        "invalidators must list the exact conditions that would make the action wrong. "
                        "Respect confluence_score: below 10 means HOLD, 10-15 watchlist only, 16+ may trade, "
                        "18+ high conviction, 22+ maximum conviction. For new BUY decisions, also require "
                        "institutional_scorecard.buy_ready=true; hard vetoes or failed must-pass gates override your opinion. "
                        "If an existing long position is supplied, act as the exit/risk manager: SELL only when "
                        "the hard stop, target/invalidation, technical breakdown, news shock, or risk-off regime "
                        "justifies exit; otherwise HOLD with a concrete updated exit plan. "
                        "Be conservative: HOLD unless the candle structure, math, sentiment, global regime, and risk all support action. "
                        "Never recommend leverage, short-selling, futures, options, or ignoring risk gates."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_context, separators=(",", ":")),
                },
            ],
        }
        self._apply_model_options(payload, schema=DECISION_SCHEMA)
        try:
            parsed = await self._chat_json(payload, retry_payload=self._compact_decision_retry_payload(context))
            parsed.update(rolling_meta)
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
                reason=f"LLM primary {parsed.get('_llm_provider', self.settings.llm_provider)}/{parsed.get('_llm_model', self.model)} ({parsed.get('risk', 'UNKNOWN')}): {reason}"[:700],
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
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a dry-run equity trading risk reviewer. "
                        "Return strict JSON only with action BUY, SELL, or HOLD; "
                        "confidence from 0 to 1; reason; evidence; risk_checks; invalidators; "
                        "signal_plan; confluence_score; trade_plan; monitoring_checklist; and data_gaps. "
                        "Your entire response must be one JSON object. Do not include markdown, scratchpad, "
                        "reasoning text, or commentary. "
                        "Keep it compact: no newline characters inside strings, reason <= 280 characters, "
                        "each list <= 5 short phrases, and trade_plan/signal_plan values must be short strings. "
                        "For existing long positions, review whether to continue holding or exit based on stop, "
                        "target, invalidation, technical breakdown, news, and market regime. "
                        "Never recommend leverage, options, futures, or short-selling."
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
        try:
            parsed = await self._chat_json(payload)
            parsed.update(rolling_meta)
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
                reason=f"LLM review {parsed.get('_llm_provider', self.settings.llm_provider)}/{parsed.get('_llm_model', self.model)}: {reason}",
                asof=decision.asof,
                strategy=decision.strategy,
                details_json=self._llm_review_details_json(decision, context, parsed, action, confidence, policy_gates),
            )
            return reviewed
        except Exception as exc:
            return Decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=0.0,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=f"LLM review failed; held safely: {_error_summary(exc)}",
                asof=decision.asof,
                strategy=decision.strategy,
                details_json=_json_dumps(
                    {
                        "audit_version": 1,
                        "decision_path": "llm_review_failed",
                        "final_action": "HOLD",
                        "action_reason": f"LLM review failed safely: {_error_summary(exc)}",
                        "candidate_decision": _decision_summary(decision),
                        "context": _compact_context(context),
                        "error_type": exc.__class__.__name__,
                        "error": str(exc)[:500],
                        "raw_response": getattr(exc, "raw", None),
                    }
                ),
            )

    async def _decision_prompt_context(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
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
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are compressing one chunk of an Indian equity trading context for a later decision. "
                            "Return one strict JSON object only. Preserve numeric thresholds, risk vetoes, data gaps, "
                            "scorecard values, institutional flow, news, indicators, and trade implications. "
                            "Do not make the final BUY/SELL/HOLD decision."
                        ),
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
                    min(max(self.settings.llm_timeout_seconds, 10), 45),
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
                "tool_protocol": "opentrade-rolling-decision-context-v1",
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
        try:
            content, meta = await self._chat_content_with_fallback(
                payload,
                self.settings.llm_timeout_seconds,
                schema=DECISION_SCHEMA,
                require_json=True,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            synthetic = _synthetic_safe_decision_from_text("", exc)
            synthetic["_json_synthetic"] = True
            synthetic["_llm_timeout"] = True
            synthetic["_json_repaired"] = False
            return synthetic
        except LLMResponseError as exc:
            initial_attempts = _attempts_from_exception(exc)
            if retry_payload is not None:
                try:
                    retry_content, retry_meta = await self._chat_content_with_fallback(
                        retry_payload,
                        min(max(self.settings.llm_timeout_seconds, 10), 45),
                        schema=DECISION_SCHEMA,
                        require_json=True,
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
                        min(max(self.settings.llm_timeout_seconds, 10), 45),
                        schema=DECISION_SCHEMA,
                        require_json=True,
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
        if self.settings.llm_provider != "nvidia":
            return None
        payload = {
            "model": self.model,
            "temperature": 0,
            "top_p": 0.1,
            "max_tokens": max(500, min(self.settings.llm_max_tokens, 800)),
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
                    "content": json.dumps(_nvidia_retry_context(context), separators=(",", ":")),
                },
            ],
        }
        self._apply_model_options(payload, schema=DECISION_SCHEMA)
        return payload

    def _parse_json_content(self, content: Any) -> dict[str, Any]:
        stripped = self._strip_json(content)
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
            raise LLMResponseError(f"LLM returned non-JSON content: {str(exc)}", raw=str(content)[:3000]) from exc
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
            started = perf_counter()
            try:
                if self._should_stream(endpoint_payload, endpoint):
                    content = await asyncio.wait_for(
                        self._chat_content_stream(endpoint_payload, attempt_timeout, endpoint),
                        timeout=attempt_timeout,
                    )
                else:
                    content = await asyncio.wait_for(
                        self._chat_content_once(endpoint_payload, attempt_timeout, endpoint),
                        timeout=attempt_timeout,
                    )
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
                return content, {
                    "_llm_provider": endpoint.provider,
                    "_llm_model": endpoint.model,
                    "_llm_attempts": attempts,
                }
            except Exception as exc:
                attempts.append(
                    {
                        "provider": endpoint.provider,
                        "model": endpoint.model,
                        "status": "failed",
                        "attempt": index,
                        "latency_ms": round((perf_counter() - started) * 1000),
                        "error": _error_summary(exc),
                    }
                )
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
        for key in ("guided_json", "response_format", "chat_template_kwargs", "reasoning_effort", "reasoning_format"):
            endpoint_payload.pop(key, None)
        endpoint_payload["model"] = endpoint.model
        self._apply_model_options_for_endpoint(endpoint_payload, endpoint, schema=schema)
        return endpoint_payload

    async def _chat_content_once(self, payload: dict[str, Any], timeout_seconds: int, endpoint: LLMEndpoint) -> str:
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            response = await client.post(
                _chat_completions_url_for_endpoint(endpoint),
                json=payload,
            )
            if (
                endpoint.provider == "nvidia"
                and response.status_code in {400, 422}
                and "guided_json" in payload
            ):
                fallback_payload = dict(payload)
                fallback_payload.pop("guided_json", None)
                fallback_payload["response_format"] = {"type": "json_object"}
                response = await client.post(
                    _chat_completions_url_for_endpoint(endpoint),
                    json=fallback_payload,
                )
            response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError("LLM response had no choices", raw=data)
        message = choices[0].get("message") or {}
        content = _first_text(message.get("content"))
        if content:
            return content
        reasoning = _first_text(message.get("reasoning_content"), message.get("reasoning"))
        if reasoning and "{" in reasoning and "}" in reasoning:
            return reasoning
        if reasoning:
            raise LLMResponseError(
                "LLM returned reasoning but no final JSON content",
                raw={"reasoning_tail": reasoning[-1000:]},
            )
        raise LLMResponseError("LLM response had no text content", raw=message)

    async def _chat_content_stream(self, payload: dict[str, Any], timeout_seconds: int, endpoint: LLMEndpoint) -> str:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
        chunks: list[str] = []
        reasoning_chunks: list[str] = []
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
            return content
        reasoning = "".join(reasoning_chunks).strip()
        if reasoning and "{" in reasoning and "}" in reasoning:
            return reasoning
        raise LLMResponseError(
            "LLM stream finished without final content",
            raw={"reasoning_tail": reasoning[-1000:] if reasoning else ""},
        )

    def _prompt_context(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.settings.llm_provider == "nvidia":
            if "nemotron" in self.model.lower():
                return _llm_prompt_context(context, profile="compact")
            return _llm_prompt_context(context, profile="rich")
        return _llm_prompt_context(context, profile="compact")

    def _decision_max_tokens(self) -> int:
        if self.settings.llm_provider == "groq":
            return max(350, min(self.settings.llm_max_tokens, 700))
        if self.settings.llm_provider == "nvidia":
            return max(700, min(self.settings.llm_max_tokens, 2500))
        return max(350, min(self.settings.llm_max_tokens, 1400))

    def _review_max_tokens(self) -> int:
        if self.settings.llm_provider == "groq":
            return max(256, min(self.settings.llm_max_tokens, 700))
        if self.settings.llm_provider == "nvidia":
            return max(700, min(self.settings.llm_max_tokens, 1800))
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
        if provider == "groq":
            self._apply_groq_options(payload, schema=schema)
            return
        if provider == "nvidia":
            self._apply_nvidia_options(payload, model=model, schema=schema)
            return
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

    def _apply_nvidia_options(self, payload: dict[str, Any], model: str | None = None, schema: dict[str, Any] | None = None) -> None:
        model = model or self.model
        if self._supports_nvidia_thinking_model(model):
            chat_template_kwargs: dict[str, Any] = {"thinking": self.settings.llm_thinking_enabled}
            effort = self.settings.llm_reasoning_effort
            if self.settings.llm_thinking_enabled and effort in {"high", "max"} and self._is_nvidia_deepseek_v4_model(model):
                chat_template_kwargs["reasoning_effort"] = effort
            payload["chat_template_kwargs"] = chat_template_kwargs
        if schema is not None:
            payload["guided_json"] = _nvidia_guided_schema(schema)
        if not self.settings.llm_thinking_enabled:
            return

    def _apply_groq_options(self, payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        payload["response_format"] = {"type": "json_object"}
        effort = self.settings.groq_reasoning_effort
        if effort in {"none", "default"}:
            payload["reasoning_effort"] = effort
        reasoning_format = self.settings.groq_reasoning_format
        if reasoning_format in {"hidden", "parsed", "raw"}:
            payload["reasoning_format"] = reasoning_format
        self._fold_system_messages_into_user(payload)

    def _fold_system_messages_into_user(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        system_parts = [
            str(message.get("content", ""))
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        if not system_parts:
            return
        instruction = "\n\n".join(part for part in system_parts if part).strip()
        folded: list[dict[str, Any]] = []
        inserted = False
        for message in messages:
            if not isinstance(message, dict) or message.get("role") == "system":
                continue
            item = dict(message)
            if not inserted and item.get("role") == "user":
                item["content"] = f"{instruction}\n\nTask:\n{item.get('content', '')}"
                inserted = True
            folded.append(item)
        if folded:
            payload["messages"] = folded

    def _is_nvidia_deepseek_v4(self) -> bool:
        return self.settings.llm_provider == "nvidia" and self._is_nvidia_deepseek_v4_model(self.model)

    def _is_nvidia_deepseek_v4_model(self, model: str) -> bool:
        return model.startswith("deepseek-ai/deepseek-v4")

    def _supports_nvidia_thinking(self) -> bool:
        return self.settings.llm_provider == "nvidia" and self._supports_nvidia_thinking_model(self.model)

    def _supports_nvidia_thinking_model(self, model: str) -> bool:
        return model.startswith(("deepseek-ai/deepseek-v4", "moonshotai/kimi-"))

    def _should_stream(self, payload: dict[str, Any] | None = None, endpoint: LLMEndpoint | None = None) -> bool:
        if payload and ("guided_json" in payload or "response_format" in payload):
            return False
        provider = endpoint.provider if endpoint else self.settings.llm_provider
        return provider == "nvidia" and self.settings.llm_streaming_enabled

    def _service_name(self) -> str:
        if self.settings.llm_provider == "groq":
            return "groq"
        if self.settings.llm_provider == "nvidia":
            return "nvidia-nim"
        return "openai-compatible"

    def _test_max_tokens(self) -> int:
        if self.settings.llm_thinking_enabled:
            return max(512, min(self.settings.llm_max_tokens, 4096))
        return 128

    def _decision_budget_seconds(self) -> int:
        configured = int(self.settings.llm_timeout_seconds or 30)
        return max(8, min(configured, 30))

    def _endpoint_attempt_timeout_seconds(self, endpoint: LLMEndpoint, remaining_seconds: float) -> float:
        model = endpoint.model.lower()
        if endpoint.provider == "groq":
            preferred = 8.0
        elif "flash" in model or "mistral" in model or "glm" in model or "minimax" in model:
            preferred = 7.0
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
                "requested_action": requested_action,
                "final_action": final_action,
                "action_reason": parsed.get("reason", "no reason supplied"),
                "confidence": round(confidence, 4),
                "json_repaired": bool(parsed.get("_json_repaired")),
                "json_retry": bool(parsed.get("_json_retry")),
                "json_retry_reason": parsed.get("_json_retry_reason"),
                "json_synthetic": bool(parsed.get("_json_synthetic")),
                "llm_timeout": bool(parsed.get("_llm_timeout")),
                "confidence_gate": {
                    "minimum_required": self.settings.llm_primary_min_confidence,
                    "passed": confidence_gate_passed,
                    "effect": "requested action allowed" if confidence_gate_passed else "downgraded to HOLD",
                },
                "policy_gates": policy_gates,
                "score_breakdown": deterministic_score_breakdown(context),
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
        "sector_rotation": context.get("sector_rotation"),
        "delivery_data": context.get("delivery_data"),
        "full_spectrum_analysis": context.get("full_spectrum_analysis"),
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-5:],
    }


def _llm_prompt_context(context: dict[str, Any], profile: str = "compact") -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    institutional = context.get("institutional_context") or {}
    institutional_flow = full.get("institutional_flow") or {}
    symbol = str(context.get("symbol") or "").upper()
    recent_candles = context.get("recent_candles") or []
    rich = profile == "rich"
    return _prune_empty(
        {
            "tool_protocol": "opentrade-rich-decision-context-v1" if rich else "opentrade-compact-decision-context-v1",
            "symbol": context.get("symbol"),
            "company": context.get("company"),
            "sector": context.get("sector"),
            "exchange": context.get("exchange"),
            "quote": context.get("quote"),
            "position": context.get("position"),
            "technical_math": context.get("technical_math"),
            "candlestick_analysis": context.get("candlestick_analysis"),
            "strategy_signals": _top_strategy_signals(context.get("strategy_signals") or [], limit=12 if rich else 6),
            "best_strategy": context.get("best_strategy"),
            "sentiment": context.get("sentiment"),
            "global_market_context": _compact_global_context(context.get("global_market_context") or {}, limit=16 if rich else 8),
            "institutional_context": {
                "enabled": institutional.get("enabled"),
                "source_quality": institutional.get("source_quality"),
                "market_bias": institutional.get("market_bias"),
                "symbol_flags": (institutional.get("symbol_flags") or {}).get(symbol, {}),
                "data_gaps": _limit_list(institutional.get("data_gaps"), 16 if rich else 8),
            },
            "market_breadth_context": context.get("market_breadth_context"),
            "macro_event_context": context.get("macro_event_context"),
            "sector_rotation": context.get("sector_rotation"),
            "delivery_data": context.get("delivery_data"),
            "full_spectrum_analysis": {
                "requirement_coverage": _coverage_summary(full.get("requirement_coverage") or {}) if rich else None,
                "data_quality": full.get("data_quality"),
                "primary_filters": full.get("primary_filters"),
                "signal_plan": full.get("signal_plan"),
                "trend_context": full.get("trend_context"),
                "stage_analysis": full.get("stage_analysis"),
                "entry_quality": full.get("entry_quality"),
                "breakout_quality": full.get("breakout_quality"),
                "price_volume_divergence": full.get("price_volume_divergence"),
                "key_levels": full.get("key_levels"),
                "fibonacci": full.get("fibonacci") if rich else None,
                "indicator_suite": _compact_indicators(full.get("indicator_suite") or {}),
                "liquidity_profile": full.get("liquidity_profile"),
                "relative_strength": full.get("relative_strength"),
                "candlestick_v2": full.get("candlestick_v2"),
                "chart_patterns": full.get("chart_patterns"),
                "institutional_structure": full.get("institutional_structure"),
                "fundamental_quality": full.get("fundamental_quality"),
                "corporate_event_risk": full.get("corporate_event_risk"),
                "delivery_accumulation": full.get("delivery_accumulation"),
                "sector_rotation": full.get("sector_rotation"),
                "market_breadth": full.get("market_breadth"),
                "macro_event_context": full.get("macro_event_context"),
                "options_oi": full.get("options_oi"),
                "backtest_snapshot": full.get("backtest_snapshot"),
                "signal_conflicts": full.get("signal_conflicts"),
                "institutional_scorecard": full.get("institutional_scorecard"),
                "news_sentiment": full.get("news_sentiment") if rich else None,
                "institutional_flow": {
                    "available": institutional_flow.get("available"),
                    "source_quality": institutional_flow.get("source_quality"),
                    "symbol_flags": institutional_flow.get("symbol_flags"),
                    "market_bias": institutional_flow.get("market_bias"),
                    "option_chain_proxy": institutional_flow.get("option_chain_proxy"),
                    "delivery_proxy": institutional_flow.get("delivery_proxy"),
                    "data_gaps": _limit_list(institutional_flow.get("data_gaps"), 16 if rich else 8),
                },
                "confluence_score": full.get("confluence_score"),
                "risk_overrides": full.get("risk_overrides"),
                "trade_plan": full.get("trade_plan"),
                "monitoring_checklist": _limit_list(full.get("monitoring_checklist"), 12 if rich else 8),
                "data_gaps": _limit_list(full.get("data_gaps"), 16 if rich else 10),
            },
            "risk_limits": _compact_risk_limits(context.get("risk_limits") or {}),
            "universe_scan": context.get("universe_scan"),
            "recent_candles_tail": [_compact_candle(candle) for candle in recent_candles[-(24 if rich else 8):]],
        }
    )


def _nvidia_retry_context(context: dict[str, Any]) -> dict[str, Any]:
    full = context.get("full_spectrum_analysis") or {}
    confluence = full.get("confluence_score") or {}
    trade_plan = full.get("trade_plan") or {}
    return _prune_empty(
        {
            "symbol": context.get("symbol"),
            "quote": context.get("quote"),
            "position": context.get("position"),
            "technical_math": context.get("technical_math"),
            "candles": context.get("candlestick_analysis"),
            "best_strategy": context.get("best_strategy"),
            "sentiment": context.get("sentiment"),
            "global_regime": _compact_global_context(context.get("global_market_context") or {}, limit=5),
            "confluence_score": confluence,
            "indicator_suite": _compact_indicators(full.get("indicator_suite") or {}),
            "risk_overrides": full.get("risk_overrides"),
            "liquidity_profile": full.get("liquidity_profile"),
            "relative_strength": full.get("relative_strength"),
            "corporate_event_risk": full.get("corporate_event_risk"),
            "delivery_accumulation": full.get("delivery_accumulation"),
            "options_oi": full.get("options_oi"),
            "backtest_snapshot": full.get("backtest_snapshot"),
            "signal_conflicts": full.get("signal_conflicts"),
            "institutional_scorecard": full.get("institutional_scorecard"),
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
                    "gate": "stage_buy_permitted",
                    "passed": bool(stage.get("buy_permitted")),
                    "value": stage.get("stage"),
                    "required": "Stage2_Markup buy_permitted=true",
                },
                {
                    "gate": "entry_grade_gate",
                    "passed": entry.get("entry_grade") != "D",
                    "value": entry.get("entry_grade"),
                    "required": "entry grade not D",
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
                    "required": "alignment grade not D",
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


def _chat_completions_url_for_endpoint(endpoint: LLMEndpoint) -> str:
    base_url = endpoint.base_url.rstrip("/")
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


def _nvidia_guided_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # NVIDIA guided_json is strong at object shape/enums, but some deployments reject
    # stricter validation keywords. Keep payload guidance broadly compatible.
    unsupported = {"maxLength", "minLength", "maxItems", "minItems", "minimum", "maximum"}
    if not isinstance(schema, dict):
        return schema
    output: dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported:
            continue
        if isinstance(value, dict):
            output[key] = _nvidia_guided_schema(value)
        elif isinstance(value, list):
            output[key] = [_nvidia_guided_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            output[key] = value
    return output


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
    is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)) or "timeout" in exc.__class__.__name__.lower()
    if is_timeout:
        reason = (
            "LLM timed out before returning a strict decision JSON, so OpenTrade used the safe fallback HOLD. "
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
            "LLM returned malformed or non-JSON output, so OpenTrade used the safe fallback HOLD. "
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
