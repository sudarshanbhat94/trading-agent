from __future__ import annotations

from contextvars import ContextVar


current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_llm_usage_scope: ContextVar[str | None] = ContextVar("current_llm_usage_scope", default=None)
