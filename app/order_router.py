from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .market_data import normalize_indstocks_access_token, normalize_upstox_access_token
from .models import Decision


LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"


class OrderRouter:
    def route(self, decision: Decision, qty: int) -> None:
        return None


class IndStocksOrderRouter(OrderRouter):
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.base_url = settings.indstocks_api_base_url.rstrip("/")
        self.access_token = normalize_indstocks_access_token(settings.indstocks_access_token)
        if not self.access_token:
            raise RuntimeError("INDSTOCKS_ACCESS_TOKEN is required for INDstocks order routing")
        if settings.live_trading_confirm != LIVE_TRADING_CONFIRMATION:
            raise RuntimeError(
                f"LIVE_TRADING_CONFIRM must equal {LIVE_TRADING_CONFIRMATION!r} for live order routing"
            )

    def route(self, decision: Decision, qty: int) -> None:
        row = self.db.universe_row(decision.symbol)
        security_id = self._security_id(row)
        exchange = str((row or {}).get("exchange") or "NSE").strip().upper()
        if not security_id:
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                "LIVE_FAILED",
                "missing INDstocks security_id",
                decision.strategy,
                self._route_details(decision, qty, {"error": "missing INDstocks security_id"}),
            )
            return

        payload: dict[str, Any] = {
            "txn_type": decision.action,
            "exchange": exchange if exchange in {"NSE", "BSE"} else "NSE",
            "segment": "EQUITY",
            "product": self.settings.indstocks_order_product,
            "order_type": self.settings.indstocks_order_type,
            "validity": self.settings.indstocks_order_validity,
            "security_id": security_id,
            "qty": int(qty),
            "is_amo": False,
            "algo_id": self._algo_id(exchange),
        }
        if self.settings.indstocks_order_type == "LIMIT":
            payload["limit_price"] = round(float(decision.price), 2)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self.access_token,
        }
        try:
            with httpx.Client(timeout=10, headers=headers) as client:
                response = client.post(f"{self.base_url}/order", json=payload)
                response.raise_for_status()
            data = response.json().get("data", {})
            order_id = data.get("order_id") or data.get("id") or "unknown"
            order_status = data.get("order_status") or data.get("status") or "submitted"
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                "LIVE_SUBMITTED",
                f"INDstocks order_id={order_id} status={order_status}",
                decision.strategy,
                self._route_details(decision, qty, {"order_id": order_id, "order_status": order_status, "payload": payload}),
            )
        except Exception as exc:
            self.db.insert_order(
                decision.symbol,
                decision.action,
                qty,
                decision.price,
                "LIVE_FAILED",
                f"{exc.__class__.__name__}: {exc}",
                decision.strategy,
                self._route_details(decision, qty, {"error_type": exc.__class__.__name__, "error": str(exc), "payload": payload}),
            )

    def _security_id(self, row: dict[str, Any] | None) -> str:
        if not row:
            return ""
        security_id = str(row.get("indstocks_security_id") or row.get("security_id") or "").strip()
        if security_id:
            return security_id
        scrip_code = str(row.get("indstocks_scrip_code") or row.get("scrip_code") or "").strip()
        if "_" in scrip_code:
            return scrip_code.rsplit("_", 1)[-1].strip()
        return ""

    def _algo_id(self, exchange: str) -> str:
        configured = str(self.settings.indstocks_algo_id or "").strip()
        if configured and not (exchange == "BSE" and configured == "99999"):
            return configured
        return "9999999999999999" if exchange == "BSE" else "99999"

    def _route_details(self, decision: Decision, qty: int, route: dict[str, Any]) -> str:
        decision_data = decision.to_dict()
        try:
            decision_data["details"] = json.loads(decision_data.pop("details_json", "{}") or "{}")
        except json.JSONDecodeError:
            pass
        return json.dumps(
            {
                "audit_version": 1,
                "router": "indstocks_live",
                "decision": decision_data,
                "qty": qty,
                "route": route,
            },
            default=str,
            separators=(",", ":"),
        )


class UpstoxOrderRouter(OrderRouter):
    def __init__(self, settings: Settings, db: Database, sandbox: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.base_url = settings.upstox_order_base_url
        self.sandbox = sandbox
        self.access_token = normalize_upstox_access_token(
            settings.upstox_sandbox_access_token if sandbox else settings.upstox_access_token
        )
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
                self._route_details(decision, qty, {"error": "missing upstox_instrument_key"}),
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
                self._route_details(decision, qty, {"order_id": order_id, "payload": payload}),
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
                self._route_details(decision, qty, {"error_type": exc.__class__.__name__, "error": str(exc), "payload": payload}),
            )

    def _route_details(self, decision: Decision, qty: int, route: dict[str, Any]) -> str:
        decision_data = decision.to_dict()
        try:
            decision_data["details"] = json.loads(decision_data.pop("details_json", "{}") or "{}")
        except json.JSONDecodeError:
            pass
        return json.dumps(
            {
                "audit_version": 1,
                "router": "upstox_sandbox" if self.sandbox else "upstox_live",
                "decision": decision_data,
                "qty": qty,
                "route": route,
            },
            default=str,
            separators=(",", ":"),
        )


def build_order_router(settings: Settings, db: Database) -> OrderRouter | None:
    if settings.execution_mode == "paper":
        return None
    if not settings.live_trading_enabled:
        return None
    if settings.execution_mode == "upstox_sandbox":
        return UpstoxOrderRouter(settings, db, sandbox=True)
    if settings.execution_mode == "upstox_live":
        return UpstoxOrderRouter(settings, db, sandbox=False)
    if settings.execution_mode != "indstocks_live":
        return None
    return IndStocksOrderRouter(settings, db)
