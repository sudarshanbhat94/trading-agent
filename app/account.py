from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .db import Database


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
        return {
            "mode": self.settings.execution_mode,
            "cash": self.db.get_state("cash", self.settings.initial_cash_inr),
            "portfolio": portfolio,
            "positions": self.db.positions(),
        }

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
