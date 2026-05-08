from __future__ import annotations

import json
import math
from typing import Any

from .models import utc_now


TOKEN_PER_ENGLISH_CHAR = 0.3


DEEPSEEK_PRICING_PER_1M_USD: dict[str, dict[str, float | str]] = {
    "deepseek-v4-flash": {
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
        "note": "User supplied DeepSeek V4 Flash pricing.",
    },
    "deepseek-v4-pro": {
        "input_cache_hit": 0.003625,
        "input_cache_miss": 0.435,
        "output": 0.87,
        "note": "User supplied DeepSeek V4 Pro 75% discount pricing, valid until DeepSeek changes it.",
    },
}


def build_llm_usage_event(
    *,
    component: str,
    purpose: str,
    provider: str,
    model: str,
    payload: dict[str, Any],
    response_data: dict[str, Any] | None,
    output_text: str,
    latency_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_chars = _char_count(payload)
    output_chars = len(output_text or "")
    usage = response_data.get("usage") if isinstance(response_data, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    estimated_input_tokens = _estimate_tokens(input_chars)
    estimated_output_tokens = _estimate_tokens(output_chars)
    prompt_tokens = _optional_int(
        usage.get("prompt_tokens"),
        usage.get("input_tokens"),
        usage.get("total_input_tokens"),
    )
    completion_tokens = _optional_int(
        usage.get("completion_tokens"),
        usage.get("output_tokens"),
        usage.get("total_output_tokens"),
    )
    cache_hit_tokens = _optional_int(
        usage.get("prompt_cache_hit_tokens"),
        usage.get("input_cache_hit_tokens"),
        usage.get("cache_hit_tokens"),
        _nested(usage, "prompt_tokens_details", "cached_tokens"),
        _nested(usage, "input_tokens_details", "cached_tokens"),
    )
    cache_miss_tokens = _optional_int(
        usage.get("prompt_cache_miss_tokens"),
        usage.get("input_cache_miss_tokens"),
        usage.get("cache_miss_tokens"),
    )

    estimated = not usage
    if prompt_tokens is None:
        prompt_tokens = estimated_input_tokens
        estimated = True
    if completion_tokens is None:
        completion_tokens = estimated_output_tokens
        estimated = True
    if cache_hit_tokens is None:
        cache_hit_tokens = 0
    if cache_miss_tokens is None:
        if prompt_tokens and cache_hit_tokens:
            cache_miss_tokens = max(prompt_tokens - cache_hit_tokens, 0)
        else:
            cache_miss_tokens = prompt_tokens
    total_tokens = _optional_int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
    cost = estimate_deepseek_cost_usd(
        model=model,
        input_cache_hit_tokens=cache_hit_tokens,
        input_cache_miss_tokens=cache_miss_tokens,
        output_tokens=completion_tokens,
    )

    return {
        "ts": utc_now(),
        "component": component,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cache_hit_tokens": int(cache_hit_tokens),
        "cache_miss_tokens": int(cache_miss_tokens),
        "estimated_tokens": bool(estimated),
        "input_chars": input_chars,
        "output_chars": output_chars,
        "cost_usd": round(cost, 10),
        "latency_ms": int(latency_ms or 0),
        "details": {
            "token_rule": "fallback estimate uses tokens = english_characters * 0.3",
            "raw_usage": usage,
            "pricing_per_1m_usd": pricing_for_model(model),
            **(details or {}),
        },
    }


def pricing_for_model(model: str) -> dict[str, Any]:
    key = str(model or "").strip().lower()
    pricing = DEEPSEEK_PRICING_PER_1M_USD.get(key) or DEEPSEEK_PRICING_PER_1M_USD["deepseek-v4-pro"]
    return dict(pricing)


def estimate_deepseek_cost_usd(
    *,
    model: str,
    input_cache_hit_tokens: int,
    input_cache_miss_tokens: int,
    output_tokens: int,
) -> float:
    pricing = pricing_for_model(model)
    return (
        (max(input_cache_hit_tokens, 0) / 1_000_000) * float(pricing["input_cache_hit"])
        + (max(input_cache_miss_tokens, 0) / 1_000_000) * float(pricing["input_cache_miss"])
        + (max(output_tokens, 0) / 1_000_000) * float(pricing["output"])
    )


def _char_count(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return len(str(value or ""))


def _estimate_tokens(chars: int) -> int:
    return max(int(math.ceil(max(chars, 0) * TOKEN_PER_ENGLISH_CHAR)), 0)


def _optional_int(*values: Any) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
