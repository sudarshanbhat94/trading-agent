"""Session-authentication hardening.

Covers three defects found in app/auth.py:

1. `_secret()` fell back to the literal "openstocks-local-session" when neither
   AUTH_SESSION_SECRET nor ADMIN_PASSWORD was set. That literal is published in
   this public repository, so anyone could sign a cookie claiming uid 1 / role
   admin and be logged in as the first user. `current_user` reads the role from
   the database, so the role claim itself was not the escalation — impersonating
   uid 1 was.
2. The session cookie was set without the Secure flag, so it was sent over
   plain HTTP.
3. Login had no throttling, allowing unlimited password guessing against a
   deliberately expensive PBKDF2 hash.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import time
import unittest

from fastapi import HTTPException

from app import auth
from app.config import Settings

RETIRED_LITERAL = b"openstocks-local-session"


class _FakeDB:
    """Minimal stand-in for the pieces of db that auth touches."""

    def __init__(self) -> None:
        self._runtime: dict[str, str] = {}

    def runtime_settings(self) -> dict[str, str]:
        return dict(self._runtime)

    def update_runtime_settings(self, values: dict[str, str]) -> None:
        self._runtime.update(values)


def _settings(**overrides) -> Settings:
    return dataclasses.replace(Settings(), **overrides)


def _forge(secret: bytes, uid: int = 1, role: str = "admin") -> str:
    payload = {"uid": uid, "role": role, "iat": int(time.time()), "nonce": "forged"}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


class SessionSecretTest(unittest.TestCase):
    def setUp(self) -> None:
        auth._SECRET_CACHE.clear()

    def test_published_literal_is_no_longer_a_valid_signing_key(self) -> None:
        """The original exploit, kept as a regression test."""
        settings = _settings(auth_session_secret="", admin_password="")
        forged = _forge(RETIRED_LITERAL)
        self.assertIsNone(auth._verify_token(forged, settings, _FakeDB()))

    def test_admin_password_is_not_used_as_a_signing_key(self) -> None:
        """A user-chosen password is guessable; it must not be the HMAC key."""
        settings = _settings(auth_session_secret="", admin_password="hunter2000")
        forged = _forge(b"hunter2000")
        self.assertIsNone(auth._verify_token(forged, settings, _FakeDB()))

    def test_configured_secret_takes_precedence(self) -> None:
        """Deployments that set AUTH_SESSION_SECRET keep their existing key, so
        this change does not invalidate their live sessions."""
        settings = _settings(auth_session_secret="a-real-configured-secret")
        self.assertEqual(auth._secret(settings, _FakeDB()), b"a-real-configured-secret")

    def test_generated_secret_is_random_and_persisted(self) -> None:
        db = _FakeDB()
        settings = _settings(auth_session_secret="", admin_password="")

        first = auth._secret(settings, db)
        self.assertNotEqual(first, RETIRED_LITERAL)
        self.assertGreaterEqual(len(first), 32)
        self.assertIn(auth._GENERATED_SECRET_KEY, db.runtime_settings())

        # Survives a process restart by coming back from the database.
        auth._SECRET_CACHE.clear()
        self.assertEqual(auth._secret(settings, db), first)

    def test_two_installs_do_not_share_a_secret(self) -> None:
        settings = _settings(auth_session_secret="", admin_password="")
        first = auth._secret(settings, _FakeDB())
        auth._SECRET_CACHE.clear()
        second = auth._secret(settings, _FakeDB())
        self.assertNotEqual(first, second)

    def test_refuses_to_sign_without_a_database(self) -> None:
        settings = _settings(auth_session_secret="", admin_password="")
        with self.assertRaises(RuntimeError):
            auth._secret(settings, None)

    def test_token_signed_with_generated_secret_round_trips(self) -> None:
        db = _FakeDB()
        settings = _settings(auth_session_secret="", admin_password="")
        token = auth._make_token({"id": 7, "role": "user"}, settings, db)
        payload = auth._verify_token(token, settings, db)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["uid"], 7)


class _FakeRequest:
    def __init__(self, headers=None, scheme="http", host="203.0.113.9"):
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()
        self.client = type("C", (), {"host": host})()


class CookieSecureFlagTest(unittest.TestCase):
    def test_secure_when_behind_https_proxy(self) -> None:
        request = _FakeRequest(headers={"x-forwarded-proto": "https"})
        self.assertTrue(auth._cookie_is_secure(request, _settings()))

    def test_not_secure_on_plain_http(self) -> None:
        self.assertFalse(auth._cookie_is_secure(_FakeRequest(), _settings()))

    def test_explicit_override_wins(self) -> None:
        request = _FakeRequest(headers={"x-forwarded-proto": "https"})
        self.assertFalse(
            auth._cookie_is_secure(request, _settings(session_cookie_secure="false"))
        )
        self.assertTrue(
            auth._cookie_is_secure(_FakeRequest(), _settings(session_cookie_secure="true"))
        )


class LoginThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        auth._LOGIN_ATTEMPTS.clear()
        auth._LOGIN_LOCKED_UNTIL.clear()

    tearDown = setUp

    def test_lockout_after_max_attempts(self) -> None:
        key = auth._client_key(_FakeRequest(), "victim")
        now = time.time()
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(auth._login_blocked_for(key, now), 0)
            auth._record_failed_login(key, now)
        self.assertGreater(auth._login_blocked_for(key, now), 0)

    def test_lockout_expires(self) -> None:
        key = auth._client_key(_FakeRequest(), "victim")
        now = time.time()
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            auth._record_failed_login(key, now)
        later = now + auth.LOGIN_LOCKOUT_SECONDS + 1
        self.assertEqual(auth._login_blocked_for(key, later), 0)

    def test_success_clears_attempts(self) -> None:
        key = auth._client_key(_FakeRequest(), "victim")
        now = time.time()
        for _ in range(auth.LOGIN_MAX_ATTEMPTS - 1):
            auth._record_failed_login(key, now)
        auth._clear_login_attempts(key)
        for _ in range(auth.LOGIN_MAX_ATTEMPTS - 1):
            auth._record_failed_login(key, now)
        self.assertEqual(auth._login_blocked_for(key, now), 0)

    def test_one_attacker_cannot_lock_out_every_account(self) -> None:
        """Throttling is per (IP, username), so hammering one account must not
        deny service to another user from the same address."""
        now = time.time()
        attacker = _FakeRequest(host="198.51.100.7")
        victim_key = auth._client_key(attacker, "victim")
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            auth._record_failed_login(victim_key, now)
        other_key = auth._client_key(attacker, "someone-else")
        self.assertEqual(auth._login_blocked_for(other_key, now), 0)

    def test_login_raises_429_when_locked(self) -> None:
        request = _FakeRequest()
        key = auth._client_key(request, "victim")
        now = time.time()
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            auth._record_failed_login(key, now)

        class _DB:
            def has_active_users(self) -> bool:
                return True

            def user_by_username(self, _name):  # pragma: no cover - not reached
                raise AssertionError("throttle must short-circuit before any DB lookup")

        with self.assertRaises(HTTPException) as caught:
            auth.login_user("victim", "guess", object(), _settings(), _DB(), request)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("Retry-After", caught.exception.headers or {})


if __name__ == "__main__":
    unittest.main()
