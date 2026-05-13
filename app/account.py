from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .db import Database
from .market_regions import market_region_for_row, normalize_market_region

MARKET_REGIONS = ("IN", "US")


class AccountService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    async def snapshot(self) -> dict[str, Any]:
        paper = self._paper_account()
        indstocks = self._indstocks_account()
        return {"paper": paper, "indstocks": indstocks}

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
            "connected": bool(self.settings.indstocks_access_token),
            "base_url": self.settings.indstocks_api_base_url,
            "provider": "indstocks",
        }

    async def _upstox_account(self) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.settings.upstox_access_token}"}
        output: dict[str, Any] = {"connected": True, "errors": []}
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            await self._fetch_account_part(client, output, "profile", f"{self.settings.upstox_api_base_url}/user/profile")
            await self._fetch_account_part(
                client,
                output,
                "funds",
                f"{self.settings.upstox_api_base_url.replace('/v2', '/v3')}/user/get-funds-and-margin",
                headers={"Api-Version": "3.0"},
            )
            await self._fetch_account_part(
                client, output, "positions", f"{self.settings.upstox_api_base_url}/portfolio/short-term-positions"
            )
            await self._fetch_account_part(
                client, output, "holdings", f"{self.settings.upstox_api_base_url}/portfolio/long-term-holdings"
            )
        return output

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
            output[key] = self._mask_profile(response.json().get("data", response.json()))
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
