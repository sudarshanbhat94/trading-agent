from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .signal_quality import FRESH_BUY_WINDOW_MINUTES, auto_follow_quality_gate


DEFAULT_ALERT_TYPES = ["fresh_buy", "paper_follow", "risk_exit"]


def normalize_whatsapp_phone(value: Any, *, default_country_code: str = "91") -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    country = re.sub(r"\D+", "", str(default_country_code or "91")) or "91"
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""
    if not has_plus and len(digits) == 10:
        digits = f"{country}{digits}"
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError("WhatsApp phone must be an international number with 8-15 digits.")
    return f"+{digits}"


def provider_phone_number(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def mask_whatsapp_phone(value: Any) -> str:
    digits = provider_phone_number(str(value or ""))
    if not digits:
        return ""
    tail = digits[-4:]
    prefix = f"+{digits[:2]}" if len(digits) > 6 else "+"
    return f"{prefix}****{tail}"


def normalize_alert_types(value: Any) -> list[str]:
    allowed = set(DEFAULT_ALERT_TYPES)
    if isinstance(value, str):
        raw = [item.strip() for item in re.split(r"[\s,;]+", value) if item.strip()]
    elif isinstance(value, Iterable):
        raw = [str(item or "").strip() for item in value]
    else:
        raw = DEFAULT_ALERT_TYPES
    output: list[str] = []
    for item in raw:
        key = item.lower()
        if key in allowed and key not in output:
            output.append(key)
    return output or list(DEFAULT_ALERT_TYPES)


@dataclass(frozen=True)
class WhatsAppSendResult:
    ok: bool
    status_code: int = 0
    provider_message_id: str = ""
    error: str = ""
    response: dict[str, Any] | None = None


class WhatsAppNotifier:
    def __init__(self, settings: Any):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            getattr(self.settings, "whatsapp_alerts_enabled", False)
            and getattr(self.settings, "whatsapp_access_token", "")
            and getattr(self.settings, "whatsapp_phone_number_id", "")
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.settings, "whatsapp_alerts_enabled", False)),
            "configured": self.configured,
            "provider": "meta_cloud_api",
            "phone_number_id_saved": bool(getattr(self.settings, "whatsapp_phone_number_id", "")),
            "access_token_saved": bool(getattr(self.settings, "whatsapp_access_token", "")),
            "cooldown_minutes": int(getattr(self.settings, "whatsapp_alert_cooldown_minutes", 30) or 30),
            "test_template_name": str(getattr(self.settings, "whatsapp_test_template_name", "hello_world") or ""),
            "alert_template_name": str(getattr(self.settings, "whatsapp_alert_template_name", "") or ""),
        }

    def send_text(self, to_phone: str, body: str) -> WhatsAppSendResult:
        if not self.configured:
            return WhatsAppSendResult(ok=False, error="whatsapp_provider_not_configured")
        to_number = provider_phone_number(to_phone)
        if not to_number:
            return WhatsAppSendResult(ok=False, error="whatsapp_phone_missing")
        message = str(body or "").strip()
        if not message:
            return WhatsAppSendResult(ok=False, error="whatsapp_message_empty")
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"preview_url": False, "body": message[:4000]},
        }
        return self._post_message(payload)

    def send_template(
        self,
        to_phone: str,
        template_name: str,
        *,
        language_code: str = "en_US",
        body_parameters: list[Any] | None = None,
    ) -> WhatsAppSendResult:
        if not self.configured:
            return WhatsAppSendResult(ok=False, error="whatsapp_provider_not_configured")
        to_number = provider_phone_number(to_phone)
        if not to_number:
            return WhatsAppSendResult(ok=False, error="whatsapp_phone_missing")
        name = str(template_name or "").strip()
        if not name:
            return WhatsAppSendResult(ok=False, error="whatsapp_template_missing")
        template: dict[str, Any] = {
            "name": name,
            "language": {"code": str(language_code or "en_US").strip() or "en_US"},
        }
        parameters = [
            {"type": "text", "text": str(value or "")[:1024]}
            for value in (body_parameters or [])
        ]
        if parameters:
            template["components"] = [{"type": "body", "parameters": parameters}]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": template,
        }
        return self._post_message(payload)

    def _post_message(self, payload: dict[str, Any]) -> WhatsAppSendResult:
        if not self.configured:
            return WhatsAppSendResult(ok=False, error="whatsapp_provider_not_configured")
        headers = {
            "Authorization": f"Bearer {getattr(self.settings, 'whatsapp_access_token', '')}",
            "Content-Type": "application/json",
        }
        base_url = str(getattr(self.settings, "whatsapp_api_base_url", "https://graph.facebook.com/v25.0")).rstrip("/")
        phone_number_id = str(getattr(self.settings, "whatsapp_phone_number_id", "")).strip()
        timeout = float(getattr(self.settings, "whatsapp_timeout_seconds", 10) or 10)
        url = f"{base_url}/{phone_number_id}/messages"
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            return WhatsAppSendResult(ok=False, error=f"{exc.__class__.__name__}: {exc}")
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text[:500]}
        provider_id = ""
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            provider_id = str(messages[0].get("id") or "")
        if 200 <= response.status_code < 300:
            return WhatsAppSendResult(ok=True, status_code=response.status_code, provider_message_id=provider_id, response=data)
        error_text = ""
        if isinstance(data, dict):
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            error_text = str(error.get("message") or data.get("message") or response.text[:500])
        return WhatsAppSendResult(ok=False, status_code=response.status_code, error=error_text or response.text[:500], response=data if isinstance(data, dict) else None)


def format_signal_alert(idea: dict[str, Any]) -> str:
    symbol = str(idea.get("symbol") or "").upper()
    market = str(idea.get("market_region") or "IN").upper()
    price = _fmt_number(idea.get("latest_price") or idea.get("entry_price"))
    stop = _fmt_number(idea.get("stop_loss"))
    target = _fmt_number(idea.get("target1"))
    score = _fmt_number(idea.get("overall_score_pct") or idea.get("combined_score"))
    reason = str(idea.get("reason") or idea.get("action_reason") or "Fresh OpenStocks BUY signal.").strip()
    lines = [
        f"OpenStocks BUY alert: {symbol} ({market})",
        f"Entry zone: {price}",
    ]
    if stop != "-":
        lines.append(f"Stop: {stop}")
    if target != "-":
        lines.append(f"Target 1: {target}")
    if score != "-":
        lines.append(f"Score: {score}")
    lines.append(f"Why: {reason[:240]}")
    lines.append("Action: verify liquidity and place only if price is still inside the entry zone.")
    return "\n".join(lines)


def alert_template_parameters(idea: dict[str, Any]) -> list[str]:
    return [
        str(idea.get("symbol") or "").upper(),
        str(idea.get("market_region") or "IN").upper(),
        _fmt_number(idea.get("latest_price") or idea.get("entry_price")),
        _fmt_number(idea.get("stop_loss")),
        _fmt_number(idea.get("target1")),
        str(idea.get("reason") or idea.get("action_reason") or "Fresh OpenStocks BUY signal.")[:180],
    ]


def dispatch_fresh_buy_alerts(
    *,
    db: Any,
    settings: Any,
    notifier: WhatsAppNotifier,
    decision_buy_symbols: set[str] | None = None,
    source: str = "agent_cycle",
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "enabled": bool(getattr(settings, "whatsapp_alerts_enabled", False)),
        "configured": notifier.configured,
        "source": source,
        "users_checked": 0,
        "candidates_checked": 0,
        "sent": 0,
        "skipped": [],
    }
    if not summary["enabled"]:
        summary["reason"] = "whatsapp_alerts_disabled"
        return summary
    if not notifier.configured:
        summary["reason"] = "whatsapp_provider_not_configured"
        return summary
    fresh_symbols = {str(symbol or "").upper() for symbol in (decision_buy_symbols or set()) if str(symbol or "").strip()}
    users = db.subscribed_whatsapp_users("fresh_buy")
    summary["users_checked"] = len(users)
    cooldown_minutes = max(int(getattr(settings, "whatsapp_alert_cooldown_minutes", 30) or 30), 1)
    since = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    max_per_cycle = max(int(getattr(settings, "whatsapp_max_alerts_per_cycle", 5) or 5), 1)
    for user in users:
        user_id = int(user["id"])
        monitor_symbols = db.user_monitor_symbols(user_id)
        ideas = db.latest_signal_ideas(120, user_id=user_id, symbols=monitor_symbols or None)
        sent_for_user = 0
        for idea in ideas:
            symbol = str(idea.get("symbol") or "").upper()
            if sent_for_user >= max_per_cycle:
                break
            if not _fresh_buy_alert_candidate(idea, fresh_symbols):
                continue
            summary["candidates_checked"] += 1
            if db.recent_whatsapp_alert(user_id, "fresh_buy", symbol, since.isoformat()):
                summary["skipped"].append({"user_id": user_id, "symbol": symbol, "reason": "cooldown"})
                continue
            alert_template = str(getattr(settings, "whatsapp_alert_template_name", "") or "").strip()
            if alert_template:
                result = notifier.send_template(
                    str(user.get("whatsapp_phone") or ""),
                    alert_template,
                    language_code=str(getattr(settings, "whatsapp_alert_template_language_code", "en_US") or "en_US"),
                    body_parameters=alert_template_parameters(idea),
                )
                message_mode = "template"
            else:
                result = notifier.send_text(str(user.get("whatsapp_phone") or ""), format_signal_alert(idea))
                message_mode = "text"
            db.record_whatsapp_alert(
                user_id=user_id,
                phone=str(user.get("whatsapp_phone") or ""),
                alert_type="fresh_buy",
                symbol=symbol,
                status="SENT" if result.ok else "FAILED",
                reason="" if result.ok else result.error,
                provider_message_id=result.provider_message_id,
                details={
                    "source": source,
                    "message_mode": message_mode,
                    "template_name": alert_template,
                    "idea_id": idea.get("id"),
                    "status_code": result.status_code,
                    "response": result.response,
                },
            )
            if result.ok:
                summary["sent"] += 1
                sent_for_user += 1
            else:
                summary["skipped"].append({"user_id": user_id, "symbol": symbol, "reason": result.error})
    return summary


def _fresh_buy_alert_candidate(idea: dict[str, Any], fresh_symbols: set[str]) -> bool:
    symbol = str(idea.get("symbol") or "").upper()
    if not symbol:
        return False
    if str(idea.get("signal_type") or "").upper() != "BUY":
        return False
    if str(idea.get("status") or "").upper() != "ACTIVE":
        return False
    if str(idea.get("fresh_action") or "").upper() != "BUY_NOW":
        return False
    quality_gate = auto_follow_quality_gate(idea)
    if not quality_gate.get("passed"):
        return False
    if symbol in fresh_symbols:
        return True
    return _seen_recently(idea)


def _seen_recently(idea: dict[str, Any]) -> bool:
    raw = idea.get("last_seen_at") or idea.get("updated_at") or idea.get("first_seen_at")
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed.astimezone(timezone.utc) <= timedelta(minutes=FRESH_BUY_WINDOW_MINUTES)


def _fmt_number(value: Any) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "-"
    if parsed <= 0:
        return "-"
    return f"{parsed:.2f}"
