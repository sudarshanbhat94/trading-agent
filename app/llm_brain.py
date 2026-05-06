from __future__ import annotations

import json
import asyncio
from time import perf_counter
from typing import Any

import httpx

from .analysis_tools import deterministic_score_breakdown
from .config import Settings
from .models import Decision, utc_now


class LLMBrain:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        if self.settings.llm_provider == "nvidia":
            return bool(self.settings.nvidia_api_key)
        if self.settings.llm_provider == "openai_compatible":
            return bool(self.settings.llm_api_key)
        return False

    @property
    def model(self) -> str:
        if self.settings.llm_provider == "nvidia":
            return self.settings.nvidia_model
        return self.settings.llm_model

    @property
    def base_url(self) -> str:
        if self.settings.llm_provider == "nvidia":
            return self.settings.nvidia_base_url
        return self.settings.llm_base_url

    @property
    def api_key(self) -> str:
        if self.settings.llm_provider == "nvidia":
            return self.settings.nvidia_api_key
        return self.settings.llm_api_key

    def chat_completions_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

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
            "temperature": self.settings.llm_temperature,
            "top_p": self.settings.llm_top_p,
            "max_tokens": self._test_max_tokens(),
            "messages": [
                {
                    "role": "system",
                    "content": "Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": 'Return exactly this JSON shape: {"ok":true,"service":"nvidia-nim","note":"ready"}',
                },
            ],
        }
        self._apply_model_options(payload)
        started = perf_counter()
        url = self.chat_completions_url()
        timeout_seconds = min(max(self.settings.llm_timeout_seconds, 5), 180)
        try:
            content = await self._chat_content(payload, timeout_seconds)
            latency_ms = round((perf_counter() - started) * 1000)
            try:
                parsed = json.loads(self._strip_json(content))
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
                "model": self.model,
                "url": url,
                "latency_ms": latency_ms,
                "timeout_seconds": timeout_seconds,
                "reply": parsed,
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
                    "for trading-cycle use. Try Reasoning Effort=none, a faster NVIDIA model, or a higher "
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

        payload = {
            "model": self.model,
            "temperature": self.settings.llm_temperature,
            "top_p": self.settings.llm_top_p,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are OpenTrade Intelligence v2.0, an institutional-style dry-run analyst for Indian equities. "
                        "Use the supplied MCP-style tool context: quote, candles, exact math indicators, "
                        "candlestick facts, strategy_signals, sentiment, global market context, free institutional "
                        "feed context, universe scan rank, full_spectrum_analysis, position, and risk limits. "
                        "Return strict JSON only with keys action, confidence, risk, strategy, reason, checklist, "
                        "evidence, risk_checks, invalidators, signal_plan, confluence_score, trade_plan, "
                        "monitoring_checklist, and data_gaps. "
                        "action must be BUY, SELL, or HOLD. confidence is 0..1. "
                        "strategy must be one of the supplied strategy_signals names or best_strategy.name. "
                        "risk must be LOW, MEDIUM, or HIGH. "
                        "reason must be concise; evidence must list the concrete inputs that support the action. "
                        "risk_checks must list the gates that passed or failed. "
                        "invalidators must list the exact conditions that would make the action wrong. "
                        "Respect confluence_score: below 10 means HOLD, 10-13 watchlist only, 14+ may trade, "
                        "18+ high conviction, 22+ maximum conviction. "
                        "Be conservative: HOLD unless the candle structure, math, sentiment, global regime, and risk all support action. "
                        "Never recommend leverage, short-selling, futures, options, or ignoring risk gates."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, separators=(",", ":")),
                },
            ],
        }
        self._apply_model_options(payload)
        try:
            parsed = await self._chat_json(payload)
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
            return self._hold_from_context(context, f"LLM primary failed safely: {exc.__class__.__name__}")

    async def review(self, decision: Decision, context: dict[str, Any]) -> Decision:
        if not self.enabled:
            return decision
        candidate_decision = decision.to_dict()
        candidate_decision.pop("details_json", None)
        payload = {
            "model": self.model,
            "temperature": self.settings.llm_temperature,
            "top_p": self.settings.llm_top_p,
            "max_tokens": max(256, min(self.settings.llm_max_tokens, 900)),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a dry-run equity trading risk reviewer. "
                        "Return strict JSON only with action BUY, SELL, or HOLD; "
                        "confidence from 0 to 1; reason; evidence; risk_checks; invalidators; "
                        "signal_plan; confluence_score; trade_plan; monitoring_checklist; and data_gaps. "
                        "Never recommend leverage, options, futures, or short-selling."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "candidate_decision": candidate_decision,
                            "context": context,
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
        self._apply_model_options(payload)
        try:
            parsed = await self._chat_json(payload)
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
            return Decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=0.0,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=f"LLM review failed; held safely: {exc.__class__.__name__}",
                asof=decision.asof,
                strategy=decision.strategy,
                details_json=_json_dumps(
                    {
                        "audit_version": 1,
                        "decision_path": "llm_review_failed",
                        "final_action": "HOLD",
                        "action_reason": f"LLM review failed safely: {exc.__class__.__name__}",
                        "candidate_decision": _decision_summary(decision),
                        "context": _compact_context(context),
                        "error_type": exc.__class__.__name__,
                    }
                ),
            )

    async def _chat_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = await self._chat_content(payload, self.settings.llm_timeout_seconds)
        return json.loads(self._strip_json(content))

    async def _chat_content(self, payload: dict[str, Any], timeout_seconds: int) -> str:
        if self._should_stream():
            return await asyncio.wait_for(
                self._chat_content_stream(payload, timeout_seconds),
                timeout=timeout_seconds,
            )
        return await asyncio.wait_for(
            self._chat_content_once(payload, timeout_seconds),
            timeout=timeout_seconds,
        )

    async def _chat_content_once(self, payload: dict[str, Any], timeout_seconds: int) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            response = await client.post(
                self.chat_completions_url(),
                json=payload,
            )
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _chat_content_stream(self, payload: dict[str, Any], timeout_seconds: int) -> str:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        chunks: list[str] = []
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            async with client.stream("POST", self.chat_completions_url(), json=stream_payload) as response:
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
                    content = delta.get("content")
                    if content:
                        chunks.append(content)
        return "".join(chunks)

    def _apply_model_options(self, payload: dict[str, Any]) -> None:
        if self._supports_nvidia_thinking():
            chat_template_kwargs: dict[str, Any] = {"thinking": self.settings.llm_thinking_enabled}
            effort = self.settings.llm_reasoning_effort
            if self.settings.llm_thinking_enabled and effort in {"high", "max"} and self._is_nvidia_deepseek_v4():
                chat_template_kwargs["reasoning_effort"] = effort
            payload["chat_template_kwargs"] = chat_template_kwargs
        if not self.settings.llm_thinking_enabled:
            return

    def _is_nvidia_deepseek_v4(self) -> bool:
        return self.settings.llm_provider == "nvidia" and self.model.startswith("deepseek-ai/deepseek-v4")

    def _supports_nvidia_thinking(self) -> bool:
        if self.settings.llm_provider != "nvidia":
            return False
        return self.model.startswith(("deepseek-ai/deepseek-v4", "moonshotai/kimi-"))

    def _should_stream(self) -> bool:
        return self.settings.llm_provider == "nvidia" and self.settings.llm_streaming_enabled

    def _test_max_tokens(self) -> int:
        if self.settings.llm_thinking_enabled:
            return max(512, min(self.settings.llm_max_tokens, 4096))
        return 128

    def _strip_json(self, content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            return text[start : end + 1]
        return text

    def _hold_from_context(self, context: dict[str, Any], reason: str) -> Decision:
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
                "provider": self.settings.llm_provider,
                "model": self.model,
                "requested_action": requested_action,
                "final_action": final_action,
                "action_reason": parsed.get("reason", "no reason supplied"),
                "confidence": round(confidence, 4),
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
                "provider": self.settings.llm_provider,
                "model": self.model,
                "candidate_decision": _decision_summary(original),
                "final_action": final_action,
                "confidence": round(confidence, 4),
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
        "full_spectrum_analysis": context.get("full_spectrum_analysis"),
        "universe_scan": context.get("universe_scan"),
        "risk_limits": context.get("risk_limits"),
        "recent_candle_count": len(recent_candles),
        "recent_candles_tail": recent_candles[-5:],
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
                    "gate": "full_spectrum_confluence",
                    "passed": confluence_total >= 14,
                    "value": confluence_total,
                    "required": ">= 14/26",
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
