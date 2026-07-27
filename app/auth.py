from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
from typing import Any

from fastapi import HTTPException, Request, Response

from .config import Settings
from .whatsapp import DEFAULT_ALERT_TYPES, mask_whatsapp_phone, normalize_alert_types


SESSION_COOKIE = "openstocks_session"
PASSWORD_ITERATIONS = 260_000

_LOGGER = logging.getLogger("openstocks.auth")

# runtime_settings key holding the auto-generated session secret.
_GENERATED_SECRET_KEY = "auth_session_secret_generated"
_SECRET_CACHE: dict[str, bytes] = {}

# Login throttling. In-process only: this app runs as a single uvicorn worker,
# so a shared store is unnecessary today. Scaling to multiple workers or hosts
# means moving this to Redis or the database, otherwise the limit multiplies by
# the worker count.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_LOCKOUT_SECONDS = 900
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCKED_UNTIL: dict[str, float] = {}


def _client_key(request: Request | None, username: str) -> str:
    """Throttle per (client IP, username) so one attacker cannot lock out
    every account, and one account cannot be sprayed from a single host."""
    host = ""
    if request is not None:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        host = forwarded or (request.client.host if request.client else "")
    return f"{host}|{username}"


def _login_blocked_for(key: str, now: float) -> int:
    """Seconds remaining on a lockout, or 0 when the caller may try again."""
    until = _LOGIN_LOCKED_UNTIL.get(key, 0.0)
    if until > now:
        return int(until - now) + 1
    if until:
        _LOGIN_LOCKED_UNTIL.pop(key, None)
        _LOGIN_ATTEMPTS.pop(key, None)
    return 0


def _record_failed_login(key: str, now: float) -> None:
    recent = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    recent.append(now)
    _LOGIN_ATTEMPTS[key] = recent
    if len(recent) >= LOGIN_MAX_ATTEMPTS:
        _LOGIN_LOCKED_UNTIL[key] = now + LOGIN_LOCKOUT_SECONDS
        _LOGGER.warning(
            "Login locked for %ss after %s failed attempts (%s)",
            LOGIN_LOCKOUT_SECONDS, len(recent), key,
        )


def _clear_login_attempts(key: str) -> None:
    _LOGIN_ATTEMPTS.pop(key, None)
    _LOGIN_LOCKED_UNTIL.pop(key, None)


def _cookie_is_secure(request: Request | None, settings: Settings) -> bool:
    """Whether to set the Secure flag on the session cookie.

    Default is adaptive: on when the request arrived over HTTPS. nginx
    terminates TLS and proxies to plain HTTP, so trust X-Forwarded-Proto. An
    explicit SESSION_COOKIE_SECURE of true/false overrides, which keeps plain
    HTTP local development working.
    """
    configured = (getattr(settings, "session_cookie_secure", "auto") or "auto").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    if request is None:
        return False
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return (forwarded or request.url.scheme or "").lower() == "https"


def normalize_username(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


def validate_username(value: str) -> str:
    username = normalize_username(value)
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise HTTPException(status_code=400, detail="Username must be 3-40 characters: letters, numbers, dot, dash, underscore.")
    return username


def validate_password(value: str) -> str:
    password = value or ""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    return password


def normalize_role(value: str) -> str:
    role = (value or "user").strip().lower()
    return role if role in {"admin", "user"} else "user"


def _normalize_monitor_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = re.split(r"[\s,;]+", value)
        except json.JSONDecodeError:
            value = re.split(r"[\s,;]+", value)
    raw_items = value if isinstance(value, list) else []
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        token = str(raw or "").strip().upper()
        if not token:
            continue
        if ":" in token:
            token = token.rsplit(":", 1)[-1]
        for suffix in (".NS", ".BO", ".NSE", ".BSE"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        token = "".join(char for char in token if char.isalnum() or char in {"&", "-", "_"})
        if token and token not in seen:
            seen.add(token)
            symbols.append(token[:32])
    return symbols


def _json_load(value: Any) -> Any:
    try:
        return json.loads(value or "null")
    except (TypeError, json.JSONDecodeError):
        return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def auth_available(db: Any, settings: Settings) -> bool:
    return bool(db.has_active_users() or settings.admin_password)


def current_user(request: Request, settings: Settings, db: Any) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = _verify_token(token, settings, db)
    if not payload:
        return None
    user = db.user_by_id(int(payload.get("uid") or 0))
    if not user or not user.get("active"):
        return None
    return _public_user(user)


def is_authenticated_request(request: Request, settings: Settings, db: Any) -> bool:
    return current_user(request, settings, db) is not None


def is_admin_request(request: Request, settings: Settings, db: Any) -> bool:
    user = current_user(request, settings, db)
    return bool(user and user.get("role") == "admin")


def require_user(request: Request, settings: Settings, db: Any) -> dict[str, Any]:
    if not auth_available(db, settings):
        raise HTTPException(status_code=403, detail="Create an admin password before using OpenStocks.")
    user = current_user(request, settings, db)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_admin(request: Request, settings: Settings, db: Any) -> dict[str, Any]:
    user = require_user(request, settings, db)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def login_user(
    username: str,
    password: str,
    response: Response,
    settings: Settings,
    db: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    if not auth_available(db, settings):
        raise HTTPException(status_code=403, detail="Create an admin password before login.")
    normalized = normalize_username(username)

    # Throttle before touching the password hash: PBKDF2 at 260k iterations is
    # deliberately expensive, so unthrottled attempts are also a CPU DoS.
    now = time.time()
    throttle_key = _client_key(request, normalized)
    blocked_for = _login_blocked_for(throttle_key, now)
    if blocked_for:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(blocked_for)},
        )

    user = db.user_by_username(normalized)
    if not user and settings.admin_password and hmac.compare_digest(normalized, normalize_username(settings.admin_username)):
        db.ensure_default_admin_user(normalized, hash_password(settings.admin_password))
        user = db.user_by_username(normalized)
    if not user or not user.get("active") or not verify_password(password, user.get("password_hash") or ""):
        _record_failed_login(throttle_key, now)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _clear_login_attempts(throttle_key)
    db.mark_user_login(int(user["id"]))
    public_user = _public_user(db.user_by_id(int(user["id"])) or user)
    return _issue_session(public_user, response, settings, db, request)


def _issue_session(
    public_user: dict[str, Any],
    response: Response,
    settings: Settings,
    db: Any,
    request: Request | None,
) -> dict[str, Any]:
    """Set the session cookie and build the auth payload.

    Shared by login and signup so the two cannot drift apart on cookie flags.
    """
    response.set_cookie(
        SESSION_COOKIE,
        _make_token(public_user, settings, db),
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(request, settings),
    )
    return {
        "authenticated": True,
        "admin": public_user["role"] == "admin",
        "admin_configured": True,
        "user": public_user,
        "session_hours": settings.admin_session_hours,
    }


def signup_user(
    username: str,
    password: str,
    response: Response,
    settings: Settings,
    db: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    """Self-service registration.

    Disabled unless SIGNUP_ENABLED is set. This deployment already has users and
    a single admin; silently opening public registration on the next deploy
    would be a security regression, so it is opt-in.

    The new account is always role "user" and active, with no credits, paper
    cash or LLM assignment. Those are admin grants and are deliberately not
    readable from the request payload — accepting a "role" field here would be
    a privilege-escalation hole.
    """
    if not signup_enabled(settings):
        raise HTTPException(status_code=403, detail="Self-registration is disabled.")

    normalized = validate_username(username)
    validated_password = validate_password(password)

    # Throttle per IP: without this, registration is an open door to mass
    # account creation. The username is not part of the key here — the account
    # does not exist yet, so keying on it would let an attacker create
    # unlimited accounts by varying the name.
    now = time.time()
    throttle_key = _client_key(request, "<signup>")
    blocked_for = _login_blocked_for(throttle_key, now)
    if blocked_for:
        raise HTTPException(
            status_code=429,
            detail="Too many sign-up attempts. Try again later.",
            headers={"Retry-After": str(blocked_for)},
        )
    _record_failed_login(throttle_key, now)

    if db.user_by_username(normalized):
        raise HTTPException(status_code=409, detail="Username already exists")

    try:
        created = db.create_user(normalized, hash_password(validated_password), role="user", active=True)
    except sqlite3.IntegrityError:
        # Lost the race against a concurrent signup for the same name. The
        # UNIQUE constraint on users.username is what actually guarantees
        # uniqueness; the check above is only a friendlier fast path.
        raise HTTPException(status_code=409, detail="Username already exists") from None

    user_id = int(created["id"])
    _LOGGER.info("New account registered: id=%s username=%s", user_id, normalized)
    try:
        db.insert_agent_log(
            "info", "auth", "signup", f"New account registered: {normalized}",
            {"user_id": user_id},
        )
    except Exception:
        # An audit-log failure must not lose the account that was just created.
        _LOGGER.exception("Could not write the signup audit log")

    public_user = _public_user(db.user_by_id(user_id) or created)
    return _issue_session(public_user, response, settings, db, request)


def signup_enabled(settings: Settings) -> bool:
    return str(getattr(settings, "signup_enabled", "") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def logout_user(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"authenticated": False, "admin": False}


def auth_status(request: Request, settings: Settings, db: Any) -> dict[str, Any]:
    user = current_user(request, settings, db)
    return {
        "authenticated": bool(user),
        "admin": bool(user and user.get("role") == "admin"),
        "admin_configured": auth_available(db, settings),
        "signup_enabled": signup_enabled(settings),
        "user": user,
    }


def _secret(settings: Settings, db: Any = None) -> bytes:
    """Return the HMAC key used to sign session cookies.

    Precedence matters for safe migration:

    1. ``AUTH_SESSION_SECRET`` when configured. Deployments that already set it
       keep the same key, so existing sessions stay valid across this change.
    2. Otherwise a random 32-byte secret generated once and persisted in
       ``runtime_settings``, so a fresh install is secure by default and the
       key survives restarts.

    This deliberately no longer falls back to ``admin_password`` (a weak,
    guessable HMAC key) or to a hardcoded literal. The previous literal was
    published in this public repository, which meant any deployment that set
    neither value would accept a session cookie forged by anyone who had read
    the source — including ``{"uid": 1, "role": "admin"}``.
    """
    configured = (settings.auth_session_secret or "").strip()
    if configured:
        return configured.encode("utf-8")

    if db is None:
        raise RuntimeError(
            "No AUTH_SESSION_SECRET is configured and no database is available "
            "to load a generated one. Refusing to sign sessions with a "
            "predictable key — set AUTH_SESSION_SECRET."
        )

    cached = _SECRET_CACHE.get("value")
    if cached:
        return cached

    stored = ""
    try:
        stored = str((db.runtime_settings() or {}).get(_GENERATED_SECRET_KEY) or "").strip()
    except Exception:
        _LOGGER.exception("Could not read the generated session secret")

    if not stored:
        stored = secrets.token_urlsafe(32)
        try:
            db.update_runtime_settings({_GENERATED_SECRET_KEY: stored})
            _LOGGER.warning(
                "AUTH_SESSION_SECRET is not set. Generated and stored a random "
                "session secret. Set AUTH_SESSION_SECRET explicitly to keep "
                "sessions valid across database resets."
            )
        except Exception:
            # Still better than a predictable key: sessions will not survive a
            # restart, but they cannot be forged.
            _LOGGER.exception("Could not persist the generated session secret")

    material = stored.encode("utf-8")
    _SECRET_CACHE["value"] = material
    return material


def _make_token(user: dict[str, Any], settings: Settings, db: Any = None) -> str:
    payload = {
        "uid": int(user["id"]),
        "role": user["role"],
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(12),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(_secret(settings, db), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_token(token: str, settings: Settings, db: Any = None) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(settings, db), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
        age = time.time() - int(payload["iat"])
        if not 0 <= age <= settings.admin_session_hours * 3600:
            return None
        return payload if payload.get("uid") else None
    except Exception:
        return None


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    paper_cash_by_market = {
        "IN": round(float(user["paper_cash_in"]), 2) if user.get("paper_cash_in") is not None else None,
        "US": round(float(user["paper_cash_us"]), 2) if user.get("paper_cash_us") is not None else None,
    }
    monitor_symbols = _normalize_monitor_symbols(user.get("monitor_symbols_json") or [])
    whatsapp_alert_types = normalize_alert_types(_json_load(user.get("whatsapp_alert_types_json")) or DEFAULT_ALERT_TYPES)
    return {
        "id": int(user["id"]),
        "username": user["username"],
        "role": user.get("role") or "user",
        "active": bool(user.get("active")),
        "signal_execution_mode": str(user.get("signal_execution_mode") or "SIGNAL_ONLY").strip().upper(),
        "credit_balance": round(float(user.get("credit_balance") or 0.0), 6),
        "daily_credit_limit": round(float(user.get("daily_credit_limit") or 0.0), 6),
        "paper_cash_by_market": paper_cash_by_market,
        "monitor_symbols": monitor_symbols,
        "monitor_symbols_count": len(monitor_symbols),
        "monitor_scope": "CUSTOM" if monitor_symbols else "DYNAMIC_OPPORTUNITY",
        "whatsapp": {
            "subscribed": bool(user.get("whatsapp_alerts_enabled") and user.get("whatsapp_phone")),
            "phone_masked": mask_whatsapp_phone(user.get("whatsapp_phone")),
            "phone_saved": bool(user.get("whatsapp_phone")),
            "alert_types": whatsapp_alert_types,
            "verified": bool(user.get("whatsapp_verified_at")),
            "verified_at": user.get("whatsapp_verified_at"),
            "updated_at": user.get("whatsapp_updated_at"),
        },
        "broker_accounts": {
            "indstocks": {
                "connected": bool(user.get("indstocks_access_token")),
                "access_token_saved": bool(user.get("indstocks_access_token")),
                "base_url": user.get("indstocks_api_base_url") or "",
            },
            "upstox": {
                "connected": bool(user.get("upstox_access_token")),
                "api_key_saved": bool(user.get("upstox_api_key")),
                "api_secret_saved": bool(user.get("upstox_api_secret")),
                "access_token_saved": bool(user.get("upstox_access_token")),
                "redirect_uri_saved": bool(user.get("upstox_redirect_uri")),
                "scope": user.get("upstox_token_scope") or "",
            },
            "kite": {
                "connected": bool(user.get("kite_access_token")),
                "api_key_saved": bool(user.get("kite_api_key")),
                "access_token_saved": bool(user.get("kite_access_token")),
                "scope": user.get("kite_token_scope") or "",
            },
        },
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
    }
