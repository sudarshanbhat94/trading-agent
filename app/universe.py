from __future__ import annotations

import csv
import io
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .models import utc_now


class UniverseService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._last_status: dict[str, Any] = {}

    async def refresh_if_enabled(self, force: bool = False) -> dict[str, Any]:
        if not force and not (
            self.settings.universe_source == "nse_equity"
            and self.settings.nse_universe_refresh_on_start
        ):
            status = {
                "enabled": False,
                "source": self.settings.universe_source,
                "reason": "nse_universe_refresh_not_enabled",
                "updated_at": utc_now(),
            }
            self._last_status = status
            return status
        return await self.refresh_nse_equity()

    async def refresh_nse_equity(self) -> dict[str, Any]:
        try:
            rows = await self._fetch_nse_equity_rows()
            inserted = self.db.upsert_universe_rows(rows, disable_missing=False)
            status = {
                "enabled": True,
                "source": "nse_equity",
                "url": self.settings.nse_equity_list_url,
                "rows": inserted,
                "series": self._allowed_series(),
                "updated_at": utc_now(),
            }
            self.db.set_state("universe_refresh_status", status)
            self.db.insert_agent_log(
                "INFO",
                "universe",
                "nse_equity_refreshed",
                f"Refreshed {inserted} NSE equity symbols",
                status,
            )
            self._last_status = status
            return status
        except Exception as exc:
            status = {
                "enabled": True,
                "source": "nse_equity",
                "url": self.settings.nse_equity_list_url,
                "error": f"{exc.__class__.__name__}: {str(exc)[:300]}",
                "updated_at": utc_now(),
            }
            self.db.set_state("universe_refresh_status", status)
            self.db.insert_agent_log(
                "WARN",
                "universe",
                "nse_equity_refresh_failed",
                "NSE equity universe refresh failed; keeping existing universe",
                status,
            )
            self._last_status = status
            return status

    def status(self) -> dict[str, Any]:
        return self._last_status or self.db.get_state("universe_refresh_status", {})

    async def _fetch_nse_equity_rows(self) -> list[dict[str, Any]]:
        headers = {
            "Accept": "text/csv,*/*",
            "Referer": "https://www.nseindia.com/market-data/securities-available-for-trading",
            "User-Agent": "Mozilla/5.0 OpenTrade/1.0",
        }
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            response = await client.get(self.settings.nse_equity_list_url)
            response.raise_for_status()
        return self._parse_nse_equity_csv(response.text)

    def _parse_nse_equity_csv(self, text: str) -> list[dict[str, Any]]:
        allowed_series = self._allowed_series()
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        rows: list[dict[str, Any]] = []
        for raw in reader:
            normalized = {_clean_key(key): (value or "").strip() for key, value in raw.items() if key}
            symbol = normalized.get("SYMBOL", "").upper()
            series = normalized.get("SERIES", "").upper()
            isin = normalized.get("ISIN NUMBER") or normalized.get("ISIN")
            if not symbol or (allowed_series and series not in allowed_series):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": normalized.get("NAME OF COMPANY") or symbol,
                    "exchange": "NSE",
                    "yahoo_symbol": f"{symbol}.NS",
                    "kite_symbol": f"NSE:{symbol}",
                    "upstox_instrument_key": f"NSE_EQ|{isin}" if isin else "",
                    "nubra_symbol": symbol,
                    "sector": "",
                    "base_price": 100,
                    "enabled": 1,
                }
            )
        return rows

    def _allowed_series(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.settings.nse_universe_series.split(",")
            if item.strip()
        ]


def _clean_key(value: str) -> str:
    return " ".join(str(value or "").strip().upper().split())
