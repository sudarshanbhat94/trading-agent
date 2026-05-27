from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from .config import Settings
from .models import utc_now


class FreeInstitutionalFeedsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def context_for_cycle(self, universe: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.settings.enable_free_institutional_feeds:
            return {
                "enabled": False,
                "source_quality": "disabled",
                "updated_at": utc_now(),
                "symbol_flags": {},
                "feeds": {},
                "nubra_placeholders": self._nubra_placeholders(),
            }

        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.settings.free_feed_cache_seconds:
            return self._cache[1]

        symbols = {str(row["symbol"]).strip().upper() for row in universe}
        async with httpx.AsyncClient(
            timeout=self.settings.free_feed_timeout_seconds,
            headers=self._headers(),
            follow_redirects=True,
        ) as client:
            await self._bootstrap_nse(client)
            (
                fii_dii,
                indices,
                option_pcr,
                asm,
                gsm,
                announcements,
                bulk_deals,
            ) = await asyncio.gather(
                self._fetch_fii_dii(client),
                self._fetch_indices(client),
                self._fetch_option_pcr(client),
                self._fetch_csv_symbols(client, "asm", "https://www.nseindia.com/api/reportASM?csv=true"),
                self._fetch_csv_symbols(client, "gsm", "https://www.nseindia.com/api/reportGSM?csv=true"),
                self._fetch_corporate_announcements(client),
                self._fetch_bulk_deals(client),
            )

        symbol_flags = self._symbol_flags(
            symbols=symbols,
            asm_symbols=set(asm.get("symbols", [])),
            gsm_symbols=set(gsm.get("symbols", [])),
            announcements=announcements,
            bulk_deals=bulk_deals,
        )
        context = {
            "enabled": True,
            "source_quality": "free_public_eod_best_effort",
            "updated_at": utc_now(),
            "feeds": {
                "fii_dii": fii_dii,
                "indices": indices,
                "option_pcr": option_pcr,
                "asm": asm,
                "gsm": gsm,
                "corporate_announcements": self._summary_feed(announcements),
                "bulk_deals": self._summary_feed(bulk_deals),
                "fo_ban": {
                    "status": "not_connected",
                    "source_quality": "free_public_eod",
                    "note": "NSE F&O ban feed endpoint/file is left as a pluggable adapter because public paths vary.",
                },
                "delivery_pct": {
                    "status": "not_connected",
                    "source_quality": "free_eod",
                    "note": "NSE/BSE bhavcopy delivery integration pending.",
                },
            },
            "symbol_flags": symbol_flags,
            "market_bias": self._market_bias(fii_dii, indices, option_pcr),
            "nubra_placeholders": self._nubra_placeholders(),
        }
        self._cache = (now, context)
        return context

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json,text/csv,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
            "User-Agent": "Mozilla/5.0 OpenStocks/1.0 (+free-institutional-feeds)",
        }

    async def _bootstrap_nse(self, client: httpx.AsyncClient) -> None:
        try:
            await client.get("https://www.nseindia.com")
        except Exception:
            return

    async def indices_now(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.settings.free_feed_timeout_seconds,
            headers=self._headers(),
            follow_redirects=True,
        ) as client:
            await self._bootstrap_nse(client)
            return await self._fetch_indices(client)

    async def _fetch_fii_dii(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            response = await client.get("https://www.nseindia.com/api/fiidiiTradeReact")
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("unexpected FII/DII payload")
        except Exception as exc:
            return _feed_error("fii_dii", exc, "free_public_eod")

        parsed = []
        by_category: dict[str, float] = {}
        for row in rows:
            category = str(row.get("category", "")).strip().upper()
            net = _float_or_none(row.get("netValue"))
            parsed.append(
                {
                    "category": category,
                    "date": row.get("date"),
                    "buy_value_cr": _float_or_none(row.get("buyValue")),
                    "sell_value_cr": _float_or_none(row.get("sellValue")),
                    "net_value_cr": net,
                }
            )
            if category and net is not None:
                by_category[category] = net
        return {
            "status": "ok",
            "source_quality": "free_public_eod",
            "updated_at": utc_now(),
            "rows": parsed,
            "net_by_category_cr": by_category,
        }

    async def _fetch_indices(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            response = await client.get("https://www.nseindia.com/api/allIndices")
            response.raise_for_status()
            rows = response.json().get("data", [])
            if not isinstance(rows, list):
                raise ValueError("unexpected index payload")
        except Exception as exc:
            return _feed_error("indices", exc, "free_public")

        interesting = {}
        for row in rows:
            index = str(row.get("index") or row.get("indexSymbol") or "").strip().upper()
            if index in {"NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY MIDCAP 100"}:
                interesting[index] = {
                    "last": _float_or_none(row.get("last")),
                    "change": _float_or_none(row.get("variation") or row.get("change")),
                    "change_pct": _float_or_none(row.get("percentChange")),
                    "asof": row.get("lastUpdateTime") or row.get("timestamp") or utc_now(),
                }
        return {
            "status": "ok",
            "source_quality": "free_public_delayed_or_live_page",
            "updated_at": utc_now(),
            "items": interesting,
        }

    async def _fetch_option_pcr(self, client: httpx.AsyncClient) -> dict[str, Any]:
        symbols = [
            symbol.strip().upper()
            for symbol in self.settings.free_feed_option_chain_symbols.split(",")
            if symbol.strip()
        ]
        results: dict[str, Any] = {}
        for symbol in symbols[:4]:
            results[symbol] = await self._option_pcr_for_symbol(client, symbol)
        return {
            "status": "ok" if any(item.get("status") == "ok" for item in results.values()) else "partial_or_empty",
            "source_quality": "free_public_page_best_effort",
            "updated_at": utc_now(),
            "items": results,
        }

    async def _option_pcr_for_symbol(self, client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
        try:
            response = await client.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}")
            response.raise_for_status()
            rows = response.json().get("records", {}).get("data", [])
            if not rows:
                raise ValueError("empty option-chain payload")
        except Exception as exc:
            return _feed_error(f"option_chain_{symbol}", exc, "free_public_page_best_effort")

        ce_oi = 0.0
        pe_oi = 0.0
        for row in rows:
            ce_oi += float((row.get("CE") or {}).get("openInterest") or 0)
            pe_oi += float((row.get("PE") or {}).get("openInterest") or 0)
        return {
            "status": "ok",
            "ce_open_interest": int(ce_oi),
            "pe_open_interest": int(pe_oi),
            "pcr_oi": round(pe_oi / ce_oi, 3) if ce_oi else None,
        }

    async def _fetch_csv_symbols(self, client: httpx.AsyncClient, feed: str, url: str) -> dict[str, Any]:
        try:
            response = await client.get(url)
            response.raise_for_status()
            text = response.text.lstrip("\ufeff")
            reader = csv.DictReader(io.StringIO(text))
            rows = []
            symbols = []
            for raw in reader:
                normalized = {_clean_key(key): str(value).strip() for key, value in raw.items() if key}
                symbol = normalized.get("symbol")
                if not symbol or symbol.lower() == "symbol":
                    continue
                symbols.append(symbol.upper())
                rows.append(normalized)
        except Exception as exc:
            return _feed_error(feed, exc, "free_public_eod")
        return {
            "status": "ok",
            "source_quality": "free_public_eod",
            "updated_at": utc_now(),
            "count": len(symbols),
            "symbols": sorted(set(symbols)),
            "sample_rows": rows[:20],
        }

    async def _fetch_corporate_announcements(self, client: httpx.AsyncClient) -> dict[str, list[dict[str, Any]]]:
        end = datetime.now()
        start = end - timedelta(days=self.settings.free_feed_corporate_lookback_days)
        url = "https://www.nseindia.com/api/corporate-announcements"
        params = {
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("unexpected announcements payload")
        except Exception:
            return {}

        output: dict[str, list[dict[str, Any]]] = {}
        for row in rows[:500]:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            output.setdefault(symbol, []).append(
                {
                    "desc": row.get("desc"),
                    "subject": row.get("sm_name") or row.get("attchmntText"),
                    "timestamp": row.get("dt"),
                    "attachment": row.get("attchmntFile"),
                }
            )
        return output

    async def _fetch_bulk_deals(self, client: httpx.AsyncClient) -> dict[str, list[dict[str, Any]]]:
        end = datetime.now()
        start = end - timedelta(days=self.settings.free_feed_corporate_lookback_days)
        url = "https://www.nseindia.com/api/historical/bulk-deals"
        params = {"from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y")}
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", payload if isinstance(payload, list) else [])
            if not isinstance(rows, list):
                raise ValueError("unexpected bulk-deals payload")
        except Exception:
            return {}

        output: dict[str, list[dict[str, Any]]] = {}
        for row in rows[:500]:
            symbol = str(row.get("symbol", row.get("SYMBOL", ""))).strip().upper()
            if not symbol:
                continue
            output.setdefault(symbol, []).append(row)
        return output

    def _symbol_flags(
        self,
        symbols: set[str],
        asm_symbols: set[str],
        gsm_symbols: set[str],
        announcements: dict[str, list[dict[str, Any]]],
        bulk_deals: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        flags: dict[str, Any] = {}
        for symbol in symbols:
            symbol_announcements = announcements.get(symbol, [])
            symbol_bulk_deals = bulk_deals.get(symbol, [])
            flags[symbol] = {
                "asm": symbol in asm_symbols,
                "gsm": symbol in gsm_symbols,
                "fno_ban": None,
                "delivery_pct": None,
                "official_announcements_count": len(symbol_announcements),
                "recent_announcements": symbol_announcements[:5],
                "bulk_deals_count": len(symbol_bulk_deals),
                "recent_bulk_deals": symbol_bulk_deals[:5],
                "source_quality": "free_public_eod_best_effort",
            }
        return flags

    def _market_bias(
        self,
        fii_dii: dict[str, Any],
        indices: dict[str, Any],
        option_pcr: dict[str, Any],
    ) -> dict[str, Any]:
        score = 0.0
        reasons = []
        fii = _first_category_value(fii_dii, ("FII", "FPI", "FII/FPI"))
        dii = _first_category_value(fii_dii, ("DII",))
        if fii is not None:
            score += 0.18 if fii > 0 else -0.18 if fii < 0 else 0
            reasons.append(f"FII/FPI net {fii:+.2f} Cr")
        if dii is not None:
            score += 0.08 if dii > 0 else -0.08 if dii < 0 else 0
            reasons.append(f"DII net {dii:+.2f} Cr")
        india_vix = (indices.get("items") or {}).get("INDIA VIX", {})
        vix_last = india_vix.get("last")
        if vix_last is not None:
            score += -0.15 if float(vix_last) >= 20 else 0.08 if float(vix_last) <= 15 else 0
            reasons.append(f"India VIX {vix_last}")
        nifty_pcr = (option_pcr.get("items") or {}).get("NIFTY", {}).get("pcr_oi")
        if nifty_pcr is not None:
            score += 0.06 if 0.8 <= float(nifty_pcr) <= 1.25 else -0.06
            reasons.append(f"NIFTY PCR {nifty_pcr}")
        return {
            "score": round(max(min(score, 1.0), -1.0), 3),
            "rationale": reasons,
        }

    def _summary_feed(self, by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "status": "ok" if by_symbol else "empty_or_unavailable",
            "source_quality": "free_public_eod_best_effort",
            "symbols_with_items": len(by_symbol),
            "total_items": sum(len(items) for items in by_symbol.values()),
        }

    def _nubra_placeholders(self) -> dict[str, Any]:
        return {
            "enabled_when_market_data_provider_is_nubra": self.settings.market_data_provider == "nubra",
            "api_base_url": self.settings.nubra_api_base_url,
            "has_session_token": bool(self.settings.nubra_session_token),
            "has_device_id": bool(self.settings.nubra_device_id),
            "option_chain_endpoint": self.settings.nubra_option_chain_endpoint,
            "market_depth_endpoint": self.settings.nubra_market_depth_endpoint,
            "delivery_endpoint": self.settings.nubra_delivery_endpoint,
            "oi_endpoint": self.settings.nubra_oi_endpoint,
            "status": "ready_for_endpoint_mapping"
            if self.settings.nubra_session_token and self.settings.nubra_device_id
            else "needs_nubra_session_token_and_device_id",
        }


def _clean_key(value: str) -> str:
    return " ".join(value.replace("\ufeff", "").strip().lower().split()).replace(" ", "_")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _feed_error(feed: str, exc: Exception, source_quality: str) -> dict[str, Any]:
    return {
        "status": "error",
        "source_quality": source_quality,
        "updated_at": utc_now(),
        "feed": feed,
        "error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
    }


def _first_category_value(feed: dict[str, Any], categories: tuple[str, ...]) -> float | None:
    values = feed.get("net_by_category_cr") or {}
    for category in categories:
        if category in values:
            return _float_or_none(values[category])
    for key, value in values.items():
        if any(category in key for category in categories):
            return _float_or_none(value)
    return None
