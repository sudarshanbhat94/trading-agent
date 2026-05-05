from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, Response

from .config import Settings


ADMIN_COOKIE = "trading_agent_admin"


def admin_available(settings: Settings) -> bool:
    return bool(settings.admin_password)


def is_admin_request(request: Request, settings: Settings) -> bool:
    token = request.cookies.get(ADMIN_COOKIE)
    if not token or not admin_available(settings):
        return False
    return _verify_token(token, settings)


def require_admin(request: Request, settings: Settings) -> None:
    if not admin_available(settings):
        raise HTTPException(status_code=403, detail="Set ADMIN_PASSWORD before using admin controls")
    if not is_admin_request(request, settings):
        raise HTTPException(status_code=401, detail="Admin login required")


def login_admin(username: str, password: str, response: Response, settings: Settings) -> dict[str, Any]:
    if not admin_available(settings):
        raise HTTPException(status_code=403, detail="Set ADMIN_PASSWORD before admin login")
    if not hmac.compare_digest(username, settings.admin_username):
        raise HTTPException(status_code=401, detail="Invalid admin username or password")
    if not hmac.compare_digest(password, settings.admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin username or password")
    token = _make_token(settings)
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        samesite="lax",
    )
    return {"admin": True, "admin_configured": True, "session_hours": settings.admin_session_hours}


def logout_admin(response: Response) -> dict[str, bool]:
    response.delete_cookie(ADMIN_COOKIE)
    return {"admin": False}


def _secret(settings: Settings) -> bytes:
    material = settings.auth_session_secret or settings.admin_password
    return material.encode("utf-8")


def _make_token(settings: Settings) -> str:
    payload = {
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(12),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_token(token: str, settings: Settings) -> bool:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
        age = time.time() - int(payload["iat"])
        return 0 <= age <= settings.admin_session_hours * 3600
    except Exception:
        return False
