from __future__ import annotations

import json
from time import perf_counter
from typing import Any

import httpx

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
                        "You are the primary dry-run analyst for Indian equities. "
                        "Use the supplied MCP-style tool context: quote, candles, exact math indicators, "
                        "candlestick facts, strategy_signals, sentiment, position, and risk limits. "
                        "Return strict JSON only with keys action, confidence, risk, strategy, reason, and checklist. "
                        "action must be BUY, SELL, or HOLD. confidence is 0..1. "
                        "strategy must be one of the supplied strategy_signals names or best_strategy.name. "
                        "risk must be LOW, MEDIUM, or HIGH. "
                        "Be conservative: HOLD unless the candle structure, math, sentiment, and risk all support action. "
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
            action = parsed.get("action", "HOLD")
            if action not in {"BUY", "SELL", "HOLD"}:
                action = "HOLD"
            confidence = max(min(float(parsed.get("confidence", 0)), 1.0), 0.0)
            if confidence < self.settings.llm_primary_min_confidence:
                action = "HOLD"
            checklist = parsed.get("checklist", [])
            strategy = str(parsed.get("strategy") or context.get("best_strategy", {}).get("name") or "llm_primary")
            reason = str(parsed.get("reason", "no reason supplied"))
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
            )
        except Exception as exc:
            return self._hold_from_context(context, f"LLM primary failed safely: {exc.__class__.__name__}")

    async def review(self, decision: Decision, context: dict[str, Any]) -> Decision:
        if not self.enabled:
            return decision
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
                        "confidence from 0 to 1; and a brief reason. "
                        "Never recommend leverage, options, futures, or short-selling."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "candidate_decision": decision.to_dict(),
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
            action = parsed.get("action", decision.action)
            if action not in {"BUY", "SELL", "HOLD"}:
                action = "HOLD"
            confidence = max(min(float(parsed.get("confidence", decision.confidence)), 1.0), 0.0)
            reason = str(parsed.get("reason", decision.reason))[:500]
            return Decision(
                symbol=decision.symbol,
                action=action,
                confidence=confidence,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=f"LLM: {reason}",
                asof=decision.asof,
                strategy=decision.strategy,
            )
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
            )

    async def _chat_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = await self._chat_content(payload, self.settings.llm_timeout_seconds)
        return json.loads(self._strip_json(content))

    async def _chat_content(self, payload: dict[str, Any], timeout_seconds: int) -> str:
        if self._should_stream():
            return await self._chat_content_stream(payload, timeout_seconds)
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
        )
