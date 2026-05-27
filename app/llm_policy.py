from __future__ import annotations

from dataclasses import replace
from typing import Any

from .config import Settings


LLM_HARD_DISABLED = True
LLM_DISABLED_REASON = "LLM is hard-disabled for all users; OpenStocks uses deterministic rules, market data, news feeds, and strategy math only."


def settings_without_llm(active_settings: Settings) -> Settings:
    return replace(
        active_settings,
        llm_provider="offline",
        llm_decision_mode="offline",
        enable_llm_sentiment=False,
        user_default_llm_provider="offline",
        user_default_llm_model="offline",
        deepseek_api_key="",
        groq_api_key="",
        llm_max_reviews_per_market_day=0,
    )


def runtime_overrides_without_llm(overrides: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(overrides)
    cleaned.update(
        {
            "llm_provider": "offline",
            "llm_decision_mode": "offline",
            "enable_llm_sentiment": False,
            "user_default_llm_provider": "offline",
            "user_default_llm_model": "offline",
            "deepseek_api_key": "",
            "groq_api_key": "",
            "llm_max_reviews_per_market_day": 0,
        }
    )
    return cleaned


def assigned_llm_from_payload(payload: dict[str, Any], settings: Settings) -> tuple[str, str]:
    if LLM_HARD_DISABLED:
        return "offline", "offline"
    provider = str(payload.get("assigned_llm_provider") or payload.get("llm_provider") or settings.user_default_llm_provider or "groq").strip().lower()
    if provider not in {"groq", "deepseek", "offline"}:
        provider = "groq"
    model = str(payload.get("assigned_llm_model") or payload.get("llm_model") or "").strip()
    if provider == "groq":
        return provider, model or settings.groq_model or "qwen/qwen3-32b"
    if provider == "deepseek":
        return provider, model if model in {"deepseek-v4-pro", "deepseek-v4-flash"} else settings.deepseek_model
    return "offline", "offline"
