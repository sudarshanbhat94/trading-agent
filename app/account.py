from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .db import Database
from .market_data import normalize_upstox_access_token
from .market_regions import market_region_for_row, normalize_market_region
from .models import utc_now

MARKET_REGIONS = ("IN", "US")


class AccountService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    async def snapshot(self, user: dict[str, Any] | None = None) -> dict[str, Any]:
        paper = self._paper_account()
        indstocks = self._indstocks_account()
        upstox = self._upstox_feed_account()
        broker_sync = await self._broker_sync(user)
        return {"paper": paper, "indstocks": indstocks, "upstox": upstox, "broker_sync": broker_sync}

    def _paper_account(self) -> dict[str, Any]:
        portfolio = self.db.latest_portfolio()
        positions = self.db.positions()
        portfolio_by_market = self._portfolio_by_market(positions)
        return {
            "mode": self.settings.execution_mode,
            "cash": round(sum(float(row["cash"]) for row in portfolio_by_market.values()), 2),
            "cash_by_market": self._cash_by_market(),
            "portfolio": portfolio,
            "portfolio_by_market": portfolio_by_market,
            "positions": positions,
        }

    def _cash_by_market(self) -> dict[str, float]:
        raw = self.db.get_state("cash_by_market", None)
        if not isinstance(raw, dict):
            raw = {}
        output: dict[str, float] = {}
        for market in MARKET_REGIONS:
            try:
                output[market] = float(raw.get(market, self.settings.initial_cash_inr))
            except (TypeError, ValueError):
                output[market] = float(self.settings.initial_cash_inr)
        return output

    def _portfolio_by_market(self, positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        cash_by_market = self._cash_by_market()
        output: dict[str, dict[str, Any]] = {}
        for market in MARKET_REGIONS:
            market_positions = [row for row in positions if _row_market(row) == market]
            cash = cash_by_market.get(market, float(self.settings.initial_cash_inr))
            invested = sum(float(row["qty"]) * float(row["avg_price"]) for row in market_positions)
            market_value = sum(float(row["qty"]) * float(row["market_price"]) for row in market_positions)
            realized = sum(float(row["realized_pnl"]) for row in market_positions)
            unrealized = market_value - invested
            output[market] = {
                "market_region": market,
                "currency": "USD" if market == "US" else "INR",
                "cash": round(cash, 2),
                "invested": round(invested, 2),
                "market_value": round(market_value, 2),
                "equity": round(cash + market_value, 2),
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
            }
        return output

    def _indstocks_account(self) -> dict[str, Any]:
        return {
            "connected": False,
            "base_url": self.settings.indstocks_api_base_url,
            "provider": "indstocks",
            "disabled": True,
        }

    def _upstox_feed_account(self) -> dict[str, Any]:
        return {
            "connected": bool(self.settings.upstox_access_token),
            "base_url": self.settings.upstox_api_base_url,
            "provider": "upstox",
            "scope": "shared_analytics",
            "default_analytics": str(self.settings.market_data_provider or "").startswith("upstox"),
        }

    async def _broker_sync(self, user: dict[str, Any] | None) -> dict[str, Any]:
        if not user or user.get("role") == "admin":
            return self._broker_sync_status(
                "ADMIN_SHARED_ANALYTICS",
                "Shared runtime account is analytics-only. Live reconciliation is shown for user accounts with personal broker tokens.",
            )
        stored = self.db.user_by_id(int(user["id"])) or {}
        if stored.get("upstox_access_token") and stored.get("upstox_token_scope") == "user":
            account = await self._upstox_account(
                access_token=str(stored.get("upstox_access_token") or ""),
                base_url=str(stored.get("upstox_api_base_url") or self.settings.upstox_api_base_url).rstrip("/"),
            )
            return self._upstox_broker_sync_payload(int(user["id"]), account)
        if stored.get("kite_access_token") and stored.get("kite_token_scope") == "user":
            return self._broker_sync_status(
                "KITE_CONNECTED_SYNC_PENDING",
                "Kite is saved for this user, but automated position/fill reconciliation is not enabled yet.",
                provider="kite",
                connected=True,
                can_live_trade=True,
            )
        if self.settings.upstox_access_token:
            return self._broker_sync_status(
                "PERSONAL_BROKER_NOT_CONNECTED",
                "Shared Upstox analytics is connected, but live order fill/reject sync needs this user's own Upstox or Kite token.",
                provider="upstox",
            )
        return self._broker_sync_status(
            "PERSONAL_BROKER_NOT_CONNECTED",
            "No personal broker token is connected. Signals can be tracked or paper-followed, but live fills cannot be confirmed.",
        )

    def _broker_sync_status(
        self,
        status: str,
        note: str,
        provider: str = "none",
        connected: bool = False,
        can_live_trade: bool = False,
        errors: list[str] | None = None,
        reconciliation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "connected": connected,
            "status": status,
            "status_label": status.replace("_", " ").title(),
            "can_live_trade": bool(can_live_trade),
            "last_sync_at": utc_now(),
            "errors": errors or [],
            "note": note,
            "reconciliation": reconciliation or {
                "openstocks_live_requests": 0,
                "broker_position_symbols": 0,
                "matched_symbols": [],
                "unmatched_live_requests": [],
                "external_broker_positions": [],
                "basis": "not_connected",
            },
        }

    async def _upstox_account(self, access_token: str | None = None, base_url: str | None = None) -> dict[str, Any]:
        token = normalize_upstox_access_token(access_token or self.settings.upstox_access_token)
        api_base = str(base_url or self.settings.upstox_api_base_url).rstrip("/")
        if not token:
            return {"connected": False, "errors": ["missing_upstox_access_token"]}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        output: dict[str, Any] = {"connected": True, "errors": []}
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            await self._fetch_account_part(client, output, "profile", f"{api_base}/user/profile")
            await self._fetch_account_part(
                client,
                output,
                "funds",
                f"{api_base.replace('/v2', '/v3')}/user/get-funds-and-margin",
                headers={"Api-Version": "3.0"},
            )
            await self._fetch_account_part(
                client, output, "positions", f"{api_base}/portfolio/short-term-positions"
            )
            await self._fetch_account_part(
                client, output, "holdings", f"{api_base}/portfolio/long-term-holdings"
            )
        return output

    def _upstox_broker_sync_payload(self, user_id: int, account: dict[str, Any]) -> dict[str, Any]:
        errors = [str(item) for item in account.get("errors") or []]
        broker_positions = self._broker_positions_from_upstox(account)
        reconciliation = self._reconcile_live_follows(user_id, broker_positions)
        status = "SYNC_DEGRADED" if errors else "SYNCED"
        note = (
            "Upstox account synced. OpenStocks can compare live requests against broker positions, but only broker order ids can prove fills/rejections."
            if not errors
            else "Upstox account partly synced. Resolve the listed broker API errors before trusting live reconciliation."
        )
        payload = self._broker_sync_status(
            status,
            note,
            provider="upstox",
            connected=bool(account.get("connected")),
            can_live_trade=bool(account.get("connected")),
            errors=errors,
            reconciliation=reconciliation,
        )
        payload["profile"] = account.get("profile")
        payload["funds"] = account.get("funds")
        payload["positions_count"] = len(self._as_list(account.get("positions")))
        payload["holdings_count"] = len(self._as_list(account.get("holdings")))
        return payload

    def _reconcile_live_follows(self, user_id: int, broker_positions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        live_rows = [
            row
            for row in self.db.user_followed_signal_ideas(user_id, 200)
            if str(row.get("mode") or "").upper() == "LIVE"
        ]
        requested_symbols = {str(row.get("symbol") or "").upper() for row in live_rows}
        matched = sorted(symbol for symbol in requested_symbols if float(broker_positions.get(symbol, {}).get("qty") or 0) > 0)
        unmatched = [
            {
                "symbol": str(row.get("symbol") or "").upper(),
                "status": row.get("follow_status"),
                "qty": row.get("qty"),
                "execution_state": row.get("execution_state"),
            }
            for row in live_rows
            if str(row.get("symbol") or "").upper() not in matched
        ]
        external = [
            {"symbol": symbol, "qty": data.get("qty"), "source": data.get("source")}
            for symbol, data in sorted(broker_positions.items())
            if symbol not in requested_symbols and float(data.get("qty") or 0) > 0
        ][:20]
        return {
            "openstocks_live_requests": len(live_rows),
            "broker_position_symbols": len([row for row in broker_positions.values() if float(row.get("qty") or 0) > 0]),
            "matched_symbols": matched,
            "unmatched_live_requests": unmatched,
            "external_broker_positions": external,
            "basis": "broker_position_match_without_order_id",
            "note": "Position matching is not the same as order fill confirmation; rejected/partial/fill status needs broker order ids.",
        }

    def _broker_positions_from_upstox(self, account: dict[str, Any]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for source, rows in (("positions", self._as_list(account.get("positions"))), ("holdings", self._as_list(account.get("holdings")))):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = self._broker_symbol(row)
                if not symbol:
                    continue
                qty = self._broker_qty(row)
                current = output.setdefault(symbol, {"symbol": symbol, "qty": 0.0, "source": source})
                current["qty"] = round(float(current.get("qty") or 0.0) + qty, 6)
                current["source"] = f"{current.get('source')},{source}" if current.get("source") != source else source
        return output

    def _broker_symbol(self, row: dict[str, Any]) -> str:
        for key in ("trading_symbol", "tradingsymbol", "symbol", "tradingsymbol_display"):
            value = str(row.get(key) or "").strip().upper()
            if value:
                return value.split(" ")[0]
        return ""

    def _broker_qty(self, row: dict[str, Any]) -> float:
        for key in ("quantity", "net_quantity", "qty", "holding_quantity", "available_quantity"):
            try:
                value = float(row.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value:
                return value
        return 0.0

    def _as_list(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("data", "positions", "holdings"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return nested
        return []

    async def _fetch_account_part(
        self,
        client: httpx.AsyncClient,
        output: dict[str, Any],
        key: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            output[key] = self._mask_profile(payload.get("data", payload))
        except Exception as exc:
            output[key] = None
            output["errors"].append(f"{key}: {exc.__class__.__name__}")

    def _mask_profile(self, value: Any) -> Any:
        if isinstance(value, dict):
            masked = {}
            for key, item in value.items():
                lower = key.lower()
                if "email" in lower:
                    masked[key] = self._mask_email(str(item))
                elif "mobile" in lower or "phone" in lower:
                    masked[key] = self._mask_phone(str(item))
                else:
                    masked[key] = self._mask_profile(item)
            return masked
        if isinstance(value, list):
            return [self._mask_profile(item) for item in value]
        return value

    def _mask_email(self, value: str) -> str:
        if "@" not in value:
            return "***"
        name, domain = value.split("@", 1)
        return f"{name[:2]}***@{domain}"

    def _mask_phone(self, value: str) -> str:
        if len(value) <= 4:
            return "***"
        return f"***{value[-4:]}"


def _row_market(row: dict[str, Any]) -> str:
    explicit = row.get("market_region")
    if explicit:
        return normalize_market_region(explicit, default="IN")
    return normalize_market_region(market_region_for_row(row), default="IN")
