from __future__ import annotations

import unittest

from app.config import Settings
from app.llm_brain import LLMBrain
from app.llm_policy import assigned_llm_from_payload, runtime_overrides_without_llm, settings_without_llm
from app.sentiment import SentimentService


class LLMHardDisableTests(unittest.TestCase):
    def test_settings_are_forced_offline_even_with_keys(self) -> None:
        settings = Settings(
            llm_provider="deepseek",
            llm_decision_mode="primary",
            enable_llm_sentiment=True,
            user_default_llm_provider="groq",
            user_default_llm_model="qwen/qwen3-32b",
            deepseek_api_key="deepseek-secret",
            groq_api_key="groq-secret",
            llm_max_reviews_per_market_day=20,
        )

        locked = settings_without_llm(settings)

        self.assertEqual(locked.llm_provider, "offline")
        self.assertEqual(locked.llm_decision_mode, "offline")
        self.assertFalse(locked.enable_llm_sentiment)
        self.assertEqual(locked.user_default_llm_provider, "offline")
        self.assertEqual(locked.user_default_llm_model, "offline")
        self.assertEqual(locked.deepseek_api_key, "")
        self.assertEqual(locked.groq_api_key, "")
        self.assertEqual(locked.llm_max_reviews_per_market_day, 0)

    def test_runtime_overrides_cannot_reenable_llm(self) -> None:
        overrides = runtime_overrides_without_llm(
            {
                "llm_provider": "groq",
                "llm_decision_mode": "review",
                "enable_llm_sentiment": True,
                "user_default_llm_provider": "deepseek",
                "user_default_llm_model": "deepseek-v4-pro",
                "deepseek_api_key": "deepseek-secret",
                "groq_api_key": "groq-secret",
                "llm_max_reviews_per_market_day": 12,
            }
        )

        self.assertEqual(overrides["llm_provider"], "offline")
        self.assertEqual(overrides["llm_decision_mode"], "offline")
        self.assertFalse(overrides["enable_llm_sentiment"])
        self.assertEqual(overrides["user_default_llm_provider"], "offline")
        self.assertEqual(overrides["user_default_llm_model"], "offline")
        self.assertEqual(overrides["deepseek_api_key"], "")
        self.assertEqual(overrides["groq_api_key"], "")
        self.assertEqual(overrides["llm_max_reviews_per_market_day"], 0)

    def test_user_assignment_payload_is_forced_offline(self) -> None:
        provider, model = assigned_llm_from_payload(
            {"assigned_llm_provider": "groq", "assigned_llm_model": "qwen/qwen3-32b"},
            Settings(user_default_llm_provider="groq", groq_api_key="secret"),
        )

        self.assertEqual((provider, model), ("offline", "offline"))

    def test_llm_and_sentiment_services_are_disabled_at_low_level(self) -> None:
        settings = Settings(
            llm_provider="groq",
            llm_decision_mode="review",
            enable_llm_sentiment=True,
            groq_api_key="secret",
        )

        self.assertFalse(LLMBrain(settings).enabled)
        self.assertEqual(LLMBrain(settings)._endpoint_candidates(), [])
        self.assertFalse(SentimentService(settings, _NoopDb())._llm_sentiment_enabled())


class _NoopDb:
    def insert_agent_log(self, *args, **kwargs) -> None:
        return None
