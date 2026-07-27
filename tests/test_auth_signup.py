"""Self-service registration.

The risk in adding signup to a system that already has an admin and real users
is not the happy path — it is that registration becomes a way to obtain
privileges. These tests pin the things that must stay true: signup is off
unless explicitly enabled, the payload cannot choose a role or grant itself
credits, and registration is throttled.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest

from fastapi import HTTPException

from app import auth
from app.config import Settings


class _FakeResponse:
    def __init__(self) -> None:
        self.cookies: dict[str, dict] = {}

    def set_cookie(self, key, value, **kwargs) -> None:
        self.cookies[key] = {"value": value, **kwargs}


class _FakeRequest:
    def __init__(self, headers=None, scheme="http", host="203.0.113.9"):
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()
        self.client = type("C", (), {"host": host})()


class _FakeDB:
    """Enough of db for the signup path, including the UNIQUE constraint."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self._next_id = 1
        self.runtime: dict[str, str] = {}
        self.logs: list[tuple] = []
        self.raise_integrity_error = False

    # -- used by _secret() ------------------------------------------------
    def runtime_settings(self) -> dict[str, str]:
        return dict(self.runtime)

    def update_runtime_settings(self, values: dict[str, str]) -> None:
        self.runtime.update(values)

    # -- users ------------------------------------------------------------
    def user_by_username(self, username: str):
        return self.users.get(username)

    def user_by_id(self, user_id: int):
        return next((u for u in self.users.values() if u["id"] == user_id), None)

    def create_user(self, username, password_hash, role="user", active=True, **kwargs):
        if self.raise_integrity_error or username in self.users:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: users.username")
        user = {
            "id": self._next_id,
            "username": username,
            "password_hash": password_hash,
            "role": role,
            "active": active,
            "account_plan": "standard",
            "paper_cash_in": None,
            "paper_cash_us": None,
            "monitor_symbols_json": "[]",
            "credit_balance": 0.0,
        }
        self._next_id += 1
        self.users[username] = user
        return user

    def insert_agent_log(self, *args) -> None:
        self.logs.append(args)


def _settings(**overrides) -> Settings:
    return dataclasses.replace(Settings(), **overrides)


ENABLED = {"signup_enabled": "true", "auth_session_secret": "test-secret"}


class SignupDisabledByDefaultTest(unittest.TestCase):
    def test_disabled_unless_explicitly_enabled(self) -> None:
        """Deploying this change must not silently open public registration."""
        self.assertFalse(auth.signup_enabled(Settings()))

    def test_signup_rejected_when_disabled(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            auth.signup_user("newbie", "password123", _FakeResponse(),
                             _settings(signup_enabled="false"), _FakeDB(), _FakeRequest())
        self.assertEqual(caught.exception.status_code, 403)

    def test_auth_status_advertises_the_flag(self) -> None:
        self.assertTrue(auth.signup_enabled(_settings(**ENABLED)))


class SignupPrivilegeTest(unittest.TestCase):
    def setUp(self) -> None:
        auth._LOGIN_ATTEMPTS.clear()
        auth._LOGIN_LOCKED_UNTIL.clear()
        self.db = _FakeDB()
        self.settings = _settings(**ENABLED)

    def test_creates_a_plain_user(self) -> None:
        result = auth.signup_user("newbie", "password123", _FakeResponse(),
                                  self.settings, self.db, _FakeRequest())
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["admin"])
        self.assertEqual(self.db.users["newbie"]["role"], "user")

    def test_payload_cannot_request_admin_role(self) -> None:
        """signup_user takes no role argument at all — the only way in is the
        username/password pair. This test fails loudly if someone later widens
        the signature to accept a role."""
        import inspect

        params = set(inspect.signature(auth.signup_user).parameters)
        self.assertNotIn("role", params)
        self.assertNotIn("payload", params)

    def test_new_account_has_no_credits_or_cash(self) -> None:
        auth.signup_user("newbie", "password123", _FakeResponse(),
                         self.settings, self.db, _FakeRequest())
        user = self.db.users["newbie"]
        self.assertEqual(user["credit_balance"], 0.0)
        self.assertIsNone(user["paper_cash_in"])
        self.assertEqual(user["account_plan"], "standard")

    def test_password_is_hashed_not_stored(self) -> None:
        auth.signup_user("newbie", "password123", _FakeResponse(),
                         self.settings, self.db, _FakeRequest())
        stored = self.db.users["newbie"]["password_hash"]
        self.assertNotIn("password123", stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.verify_password("password123", stored))

    def test_session_cookie_is_httponly(self) -> None:
        response = _FakeResponse()
        auth.signup_user("newbie", "password123", response,
                         self.settings, self.db, _FakeRequest())
        cookie = response.cookies[auth.SESSION_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "lax")


class SignupValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        auth._LOGIN_ATTEMPTS.clear()
        auth._LOGIN_LOCKED_UNTIL.clear()
        self.db = _FakeDB()
        self.settings = _settings(**ENABLED)

    def _signup(self, username, password):
        return auth.signup_user(username, password, _FakeResponse(),
                                self.settings, self.db, _FakeRequest())

    def test_rejects_short_password(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._signup("newbie", "short")
        self.assertEqual(caught.exception.status_code, 400)

    def test_rejects_invalid_username(self) -> None:
        for bad in ("ab", "sym!bol", "user@example.com", "x" * 41):
            with self.subTest(username=bad):
                with self.assertRaises(HTTPException) as caught:
                    self._signup(bad, "password123")
                self.assertEqual(caught.exception.status_code, 400)

    def test_internal_whitespace_is_stripped_not_rejected(self) -> None:
        """Pre-existing normalize_username() behaviour, shared with admin user
        creation: whitespace is removed rather than refused, so "has space"
        registers as "hasspace". Pinned here because it means two visually
        distinct names can collide onto one account.
        """
        self._signup("has space", "password123")
        self.assertIn("hasspace", self.db.users)
        with self.assertRaises(HTTPException) as caught:
            self._signup("hasspace", "password123")
        self.assertEqual(caught.exception.status_code, 409)

    def test_duplicate_username_is_409(self) -> None:
        self._signup("newbie", "password123")
        with self.assertRaises(HTTPException) as caught:
            self._signup("newbie", "password123")
        self.assertEqual(caught.exception.status_code, 409)

    def test_username_is_normalized(self) -> None:
        self._signup("  MixedCase  ", "password123")
        self.assertIn("mixedcase", self.db.users)

    def test_concurrent_duplicate_becomes_409_not_500(self) -> None:
        """Losing the race to the UNIQUE constraint must not surface as a 500."""
        self.db.raise_integrity_error = True
        with self.assertRaises(HTTPException) as caught:
            self._signup("racer", "password123")
        self.assertEqual(caught.exception.status_code, 409)

    def test_audit_log_failure_does_not_lose_the_account(self) -> None:
        def _boom(*args):
            raise RuntimeError("audit log down")

        self.db.insert_agent_log = _boom
        result = self._signup("newbie", "password123")
        self.assertTrue(result["authenticated"])
        self.assertIn("newbie", self.db.users)


class SignupThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        auth._LOGIN_ATTEMPTS.clear()
        auth._LOGIN_LOCKED_UNTIL.clear()

    tearDown = setUp

    def test_mass_registration_is_throttled(self) -> None:
        """Varying the username must not defeat the limit."""
        db = _FakeDB()
        settings = _settings(**ENABLED)
        request = _FakeRequest(host="198.51.100.22")

        for i in range(auth.LOGIN_MAX_ATTEMPTS):
            auth.signup_user(f"user{i}", "password123", _FakeResponse(), settings, db, request)

        with self.assertRaises(HTTPException) as caught:
            auth.signup_user("user99", "password123", _FakeResponse(), settings, db, request)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertNotIn("user99", db.users)

    def test_a_different_client_is_unaffected(self) -> None:
        db = _FakeDB()
        settings = _settings(**ENABLED)
        noisy = _FakeRequest(host="198.51.100.22")
        for i in range(auth.LOGIN_MAX_ATTEMPTS):
            auth.signup_user(f"user{i}", "password123", _FakeResponse(), settings, db, noisy)

        other = _FakeRequest(host="198.51.100.23")
        result = auth.signup_user("someone", "password123", _FakeResponse(), settings, db, other)
        self.assertTrue(result["authenticated"])


if __name__ == "__main__":
    unittest.main()
