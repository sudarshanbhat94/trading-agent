from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, Response

from .config import Settings


SESSION_COOKIE = "openstocks_session"
PASSWORD_ITERATIONS = 260_000


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
    payload = _verify_token(token, settings)
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


def login_user(username: str, password: str, response: Response, settings: Settings, db: Any) -> dict[str, Any]:
    if not auth_available(db, settings):
        raise HTTPException(status_code=403, detail="Create an admin password before login.")
    normalized = normalize_username(username)
    user = db.user_by_username(normalized)
    if not user and settings.admin_password and hmac.compare_digest(normalized, normalize_username(settings.admin_username)):
        db.ensure_default_admin_user(normalized, hash_password(settings.admin_password))
        user = db.user_by_username(normalized)
    if not user or not user.get("active") or not verify_password(password, user.get("password_hash") or ""):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    db.mark_user_login(int(user["id"]))
    public_user = _public_user(db.user_by_id(int(user["id"])) or user)
    response.set_cookie(
        SESSION_COOKIE,
        _make_token(public_user, settings),
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        samesite="lax",
    )
    return {
        "authenticated": True,
        "admin": public_user["role"] == "admin",
        "admin_configured": True,
        "user": public_user,
        "session_hours": settings.admin_session_hours,
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
        "user": user,
    }


def _secret(settings: Settings) -> bytes:
    material = settings.auth_session_secret or settings.admin_password or "openstocks-local-session"
    return material.encode("utf-8")


def _make_token(user: dict[str, Any], settings: Settings) -> str:
    payload = {
        "uid": int(user["id"]),
        "role": user["role"],
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(12),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_token(token: str, settings: Settings) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).hexdigest()
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
    return {
        "id": int(user["id"]),
        "username": user["username"],
        "role": user.get("role") or "user",
        "active": bool(user.get("active")),
        "credit_balance": round(float(user.get("credit_balance") or 0.0), 6),
        "daily_credit_limit": round(float(user.get("daily_credit_limit") or 0.0), 6),
        "paper_cash_by_market": paper_cash_by_market,
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
