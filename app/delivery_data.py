from __future__ import annotations

import asyncio
import csv
import io
import time
import zipfile
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .models import utc_now


IST_OFFSET = timedelta(hours=5, minutes=30)


class DeliveryDataService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._cache: dict[str, tuple[float, Any]] = {}
        self._fetch_lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None

    def start_background_task(self) -> None:
        if str(self.settings.market_region or "IN").upper() == "US":
            return
        if self._background_task and not self._background_task.done():
            return
        self._background_task = asyncio.create_task(self._daily_loop())

    async def stop_background_task(self) -> None:
        if not self._background_task:
            return
        self._background_task.cancel()
        try:
            await self._background_task
        except asyncio.CancelledError:
            pass
        self._background_task = None

    async def _daily_loop(self) -> None:
        while True:
            try:
                await self.ensure_data_current()
            except Exception as exc:
                self._log("WARN", "delivery_background_error", "Delivery background refresh failed", {"error": _error(exc)})
            await asyncio.sleep(3600)

    async def ensure_data_current(self) -> dict[str, Any]:
        if str(self.settings.market_region or "IN").upper() == "US":
            return self._neutral_status("us_market_no_nse_delivery_data")
        if not self.settings.enable_delivery_data:
            return self._neutral_status("disabled")
        now_ist = _now_ist()
        if now_ist.time() < dt_time(16, 0):
            return self._neutral_status("waiting_until_after_1600_ist")
        last_fetch_date = self.db.get_state("delivery_data_last_fetch_date")
        if last_fetch_date == now_ist.date().isoformat():
            return self._neutral_status("already_fetched_today")
        if self._refresh_task and not self._refresh_task.done():
            return self._neutral_status("refresh_in_progress")
        self._refresh_task = asyncio.create_task(self._refresh_data(now_ist.date().isoformat()))
        return self._neutral_status("refresh_scheduled")

    async def _refresh_data(self, fetch_date: str) -> dict[str, Any]:
        async with self._fetch_lock:
            last_fetch_date = self.db.get_state("delivery_data_last_fetch_date")
            if last_fetch_date == fetch_date:
                return self._neutral_status("already_fetched_today")
            rows = await self._fetch_recent_bhavcopies()
            if rows:
                self.db.upsert_delivery_data(rows)
                self.db.set_state("delivery_data_last_fetch_date", fetch_date)
                self._cache.clear()
            status = {
                "enabled": True,
                "updated_at": utc_now(),
                "rows": len(rows),
                "trading_days_requested": self.settings.delivery_fetch_days,
                "source": "nse_cm_bhavcopy_delivery_best_effort",
            }
            self._log("INFO", "delivery_refresh", "NSE delivery data refreshed", status)
            return status

    def rolling_delivery_trend(self, symbol: str, days: int = 15) -> dict[str, Any]:
        cache_key = f"trend:{symbol}:{days}"
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.settings.delivery_cache_seconds:
            return cached[1]
        rows = self.db.delivery_rows(symbol, max(days + 1, 16))
        if len(rows) < 2:
            result = {
                "available": False,
                "data_gap": "delivery_data_unavailable",
                "avg_delivery_pct": None,
                "trend_direction": "neutral",
                "accumulation_days": 0,
                "distribution_days": 0,
                "net_bias": "neutral",
            }
            self._cache[cache_key] = (time.monotonic(), result)
            return result
        recent = rows[-days:]
        pct_values = [float(row["delivery_pct"]) for row in recent if row.get("delivery_pct") is not None]
        avg_delivery = sum(pct_values) / len(pct_values) if pct_values else 0.0
        accumulation_days = 0
        distribution_days = 0
        for previous, current in zip(rows[-days - 1 :], rows[-days:]):
            prev_close = _float(previous.get("close"))
            close = _float(current.get("close"))
            delivery_pct = _float(current.get("delivery_pct"))
            if prev_close is None or close is None or delivery_pct is None:
                continue
            if close > prev_close and delivery_pct > 60:
                accumulation_days += 1
            if close < prev_close and delivery_pct > 55:
                distribution_days += 1
        if avg_delivery > 55 and accumulation_days > distribution_days:
            trend = "accumulation"
        elif avg_delivery > 55 and distribution_days > accumulation_days:
            trend = "distribution"
        else:
            trend = "neutral"
        if accumulation_days >= 3 and accumulation_days > distribution_days:
            net_bias = "accumulation"
        elif distribution_days >= 3 and distribution_days > accumulation_days:
            net_bias = "distribution"
        else:
            net_bias = "neutral"
        result = {
            "available": True,
            "source": "nse_delivery_bhavcopy",
            "days": len(recent),
            "avg_delivery_pct": round(avg_delivery, 3),
            "trend_direction": trend,
            "accumulation_days": accumulation_days,
            "distribution_days": distribution_days,
            "net_bias": net_bias,
            "latest_delivery_pct": _float(recent[-1].get("delivery_pct")) if recent else None,
            "data_gap": None,
        }
        self._cache[cache_key] = (time.monotonic(), result)
        return result

    def institutional_accumulation_fingerprint(self, symbol: str) -> bool:
        rows = self.db.delivery_rows(symbol, 16)
        if len(rows) < 6:
            return False
        consecutive = 0
        for previous, current in zip(rows[:-1], rows[1:]):
            prev_close = _float(previous.get("close"))
            close = _float(current.get("close"))
            delivery_pct = _float(current.get("delivery_pct"))
            if prev_close is not None and close is not None and delivery_pct is not None and close > prev_close and delivery_pct > 60:
                consecutive += 1
                if consecutive >= 3:
                    return True
            else:
                consecutive = 0
        last_10 = rows[-10:]
        high_delivery_up_days = 0
        for previous, current in zip(rows[-11:-1], last_10):
            prev_close = _float(previous.get("close"))
            close = _float(current.get("close"))
            delivery_pct = _float(current.get("delivery_pct"))
            if prev_close is not None and close is not None and delivery_pct is not None and close > prev_close and delivery_pct > 58:
                high_delivery_up_days += 1
        if high_delivery_up_days >= 5 and _float(last_10[-1].get("close"), 0) > _float(last_10[0].get("close"), 0):
            return True
        if len(rows) >= 15:
            last_5 = [_float(row.get("delivery_pct")) for row in rows[-5:]]
            prior_10 = [_float(row.get("delivery_pct")) for row in rows[-15:-5]]
            last_5 = [value for value in last_5 if value is not None]
            prior_10 = [value for value in prior_10 if value is not None]
            if last_5 and prior_10 and (sum(last_5) / len(last_5)) > (sum(prior_10) / len(prior_10)) + 8:
                return True
        return False

    def delivery_score(self, symbol: str) -> float:
        return float(self.delivery_score_payload(symbol).get("score", 0.0))

    def delivery_score_payload(self, symbol: str) -> dict[str, Any]:
        rows = self.db.delivery_rows(symbol, 16)
        if not rows:
            return {"score": 0.0, "data_gap": "delivery_data_unavailable", "source": "delivery_data"}
        trend = self.rolling_delivery_trend(symbol, 15)
        score = 0.0
        distribution_streak = 0
        for previous, current in zip(rows[:-1], rows[1:]):
            prev_close = _float(previous.get("close"))
            close = _float(current.get("close"))
            delivery_pct = _float(current.get("delivery_pct"))
            if prev_close is not None and close is not None and delivery_pct is not None and close < prev_close and delivery_pct > 55:
                distribution_streak += 1
            else:
                distribution_streak = 0
        if distribution_streak >= 5:
            score = -0.8
        elif self.institutional_accumulation_fingerprint(symbol):
            score = 0.8
        elif trend.get("net_bias") == "accumulation" and int(trend.get("accumulation_days") or 0) >= 3:
            score = 0.4
        elif trend.get("net_bias") == "distribution" and int(trend.get("distribution_days") or 0) >= 3:
            score = -0.4
        return {
            "score": score,
            "data_gap": trend.get("data_gap"),
            "source": "nse_delivery_bhavcopy",
            "fingerprint": self.institutional_accumulation_fingerprint(symbol),
            "trend": trend,
            "distribution_streak": distribution_streak,
        }

    async def _fetch_recent_bhavcopies(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=20, headers=_nse_headers(), follow_redirects=True) as client:
            try:
                await client.get("https://www.nseindia.com")
            except Exception:
                pass
            for day in _recent_weekdays(self.settings.delivery_fetch_days):
                try:
                    output.extend(await self._fetch_bhavcopy_for_day(client, day))
                except Exception as exc:
                    self._log(
                        "WARN",
                        "delivery_day_fetch_failed",
                        "NSE delivery day fetch failed",
                        {"date": day.isoformat(), "error": _error(exc)},
                    )
        return output

    async def _fetch_bhavcopy_for_day(self, client: httpx.AsyncClient, day: date) -> list[dict[str, Any]]:
        report_names = [
            "CM-UDiFF Common Bhavcopy Final (zip)",
            "Full Bhavcopy and Security Deliverable data",
            "CM - Security-wise Delivery Positions",
            "CM - Bhavcopy(csv)",
        ]
        last_error: Exception | None = None
        for name in report_names:
            params = {
                "archives": f'[{{"name":"{name}","type":"archives","category":"capital-market","section":"equities"}}]',
                "date": day.strftime("%d-%b-%Y"),
                "type": "equities",
                "mode": "single",
            }
            try:
                response = await client.get("https://www.nseindia.com/api/reports", params=params)
                response.raise_for_status()
                rows = _parse_delivery_csv(_extract_text(response.content))
                if rows:
                    return rows
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []

    def _neutral_status(self, reason: str) -> dict[str, Any]:
        return {"enabled": self.settings.enable_delivery_data, "updated_at": utc_now(), "status": reason}

    def _log(self, level: str, event: str, message: str, details: Any | None = None) -> None:
        try:
            self.db.insert_agent_log(level, "delivery_data", event, message, details)
        except Exception:
            pass


def _nse_headers() -> dict[str, str]:
    return {
        "Accept": "application/json,text/csv,application/zip,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "User-Agent": "Mozilla/5.0 OpenTrade/1.0 (+delivery-data)",
    }


def _recent_weekdays(days: int) -> list[date]:
    output: list[date] = []
    cursor = _now_ist().date() - timedelta(days=1)
    while len(output) < max(days, 1):
        if cursor.weekday() < 5:
            output.append(cursor)
        cursor -= timedelta(days=1)
    return output


def _now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _extract_text(content: bytes) -> str:
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".csv"):
                    return archive.read(name).decode("utf-8", errors="ignore")
    return content.decode("utf-8", errors="ignore")


def _parse_delivery_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows: list[dict[str, Any]] = []
    for raw in reader:
        normalized = {_clean_key(key): value for key, value in raw.items() if key}
        symbol = str(normalized.get("symbol") or normalized.get("symbl") or normalized.get("tckrsymb") or "").strip().upper()
        series = str(normalized.get("series") or "").strip().upper()
        if not symbol or (series and series != "EQ"):
            continue
        date_value = _parse_date(normalized.get("date1") or normalized.get("date") or normalized.get("trad_dt") or normalized.get("traddt"))
        rows.append(
            {
                "symbol": symbol,
                "date": date_value,
                "close": _float(
                    normalized.get("close_price")
                    or normalized.get("close")
                    or normalized.get("cls_prc")
                    or normalized.get("clspric")
                    or normalized.get("last")
                ),
                "total_volume": _float(
                    normalized.get("ttl_trd_qnty")
                    or normalized.get("totaltradedquantity")
                    or normalized.get("tottrdqty")
                    or normalized.get("ttl_trd_qty")
                    or normalized.get("ttltradgvol")
                ),
                "delivery_volume": _float(normalized.get("deliv_qty") or normalized.get("delivery_qty") or normalized.get("dlvryqty")),
                "delivery_pct": _float(normalized.get("deliv_per") or normalized.get("delivery_per") or normalized.get("deliv_pct") or normalized.get("dlvrytotradedqtypct")),
            }
        )
    return [row for row in rows if row["delivery_pct"] is not None]


def _parse_date(value: Any) -> str:
    text = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10] or _now_ist().date().isoformat()


def _clean_key(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _error(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {str(exc)[:240]}"
