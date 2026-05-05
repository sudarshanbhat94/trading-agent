from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .db import Database
from .models import Decision


LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"


class OrderRouter:
    def route(self, decision: Decision, qty: int) -> None:
        return None


class UpstoxOrderRouter(OrderRouter):
    def __init__(self, settings: Settings, db: Database, sandbox: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.base_url = settings.upstox_order_base_url
        self.sandbox = sandbox
        self.access_token = settings.upstox_sandbox_access_token if sandbox else settings.upstox_access_token
        if not self.access_token:
            name = "UPSTOX_SANDBOX_ACCESS_TOKEN" if sandbox else "UPSTOX_ACCESS_TOKEN"
            raise RuntimeError(f"{name} is required for Upstox order routing")
        if not sandbox and settings.live_trading_confirm != LIVE_TRADING_CONFIRMATION:
            raise RuntimeError(
                f"LIVE_TRADING_CONFIRM must equal {LIVE_TRADING_CONFIRMATION!r} for live order routing"
            )

    def route(self, decision: Decision, qty: int) -> None:
        row = self.db.universe_row(decision.symbol)
        instrument = row.get("upstox_instrument_key") if row else None
        if not instrument:
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                "LIVE_FAILED",
                "missing upstox_instrument_key",
                decision.strategy,
            )
            return

        payload: dict[str, Any] = {
            "quantity": qty,
            "product": self.settings.upstox_order_product,
            "validity": self.settings.upstox_order_validity,
            "price": 0,
            "tag": "llm-agent",
            "instrument_token": instrument,
            "order_type": self.settings.upstox_order_type,
            "transaction_type": decision.action,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "market_protection": -1,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        try:
            with httpx.Client(timeout=10, headers=headers) as client:
                response = client.post(f"{self.base_url}/order/place", json=payload)
                response.raise_for_status()
            order_id = response.json().get("data", {}).get("order_id", "unknown")
            status = "SANDBOX_SUBMITTED" if self.sandbox else "LIVE_SUBMITTED"
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                status,
                f"Upstox order_id={order_id}",
                decision.strategy,
            )
        except Exception as exc:
            status = "SANDBOX_FAILED" if self.sandbox else "LIVE_FAILED"
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                status,
                f"{exc.__class__.__name__}: {exc}",
                decision.strategy,
            )


def build_order_router(settings: Settings, db: Database) -> OrderRouter | None:
    if settings.execution_mode == "upstox_sandbox":
        return UpstoxOrderRouter(settings, db, sandbox=True)
    if settings.execution_mode != "upstox_live":
        return None
    if not settings.live_trading_enabled:
        return None
    return UpstoxOrderRouter(settings, db)
