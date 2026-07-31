"""Every /v2 endpoint requires a logged-in user.

There was NO authentication on the v2 router. Not a weak check — none at all:
no dependency, no user lookup, nothing, while nginx proxies /v2 straight to the
internet with no auth_basic in front. Verified from outside the host with no
cookie:

    GET https://openstocks.in/v2/api/positions  ->  200, the full book

Read access was the mild half. These were open to anyone who knew the URL:

    POST /v2/api/reset                  wipes the entire book
    POST /v2/api/buy, /api/sell         trade it
    POST /v2/api/positions/{id}/exit    close any position
    POST /v2/api/index-settings         change auto-trade, budget, instruments

The gate is attached to the ROUTER, not to each route, and that is what these
tests pin hardest. Thirty-five endpoints and growing: a per-route decorator is
one forgotten line from a hole, and this codebase has already produced exactly
that shape of failure — four separate index gates, each silently disabling a
whole lane, each individually plausible. A router-level dependency cannot be
forgotten on a new route because nobody has to remember it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest


def _client(tmp):
    os.environ["OPENSTOCKS_DISABLE_ENGINE"] = "1"
    os.environ["DATABASE_PATH"] = os.path.join(tmp, "auth.db")
    v2 = os.path.join(tmp, "v2.db")
    os.environ["V2_PAPER_DB"] = v2
    con = sqlite3.connect(v2)
    from app import v2_live
    v2_live.ensure_schema(con)
    con.commit(); con.close()
    from fastapi.testclient import TestClient
    from app import main as m
    from app import v2_web
    v2_web.V2_DB = v2
    return TestClient(m.app), m


class AnonymousAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, _ = _client(self.tmp)

    def test_reads_are_refused(self) -> None:
        for path in ("/v2/api/positions", "/v2/api/overview", "/v2/api/orders",
                     "/v2/api/trades", "/v2/api/index-settings", "/v2/api/index-call",
                     "/v2/api/watchlist", "/v2/api/movers"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_the_destructive_endpoints_are_refused(self) -> None:
        """The ones that would have cost real money if anyone had found them."""
        self.assertEqual(self.client.post("/v2/api/reset").status_code, 401)
        self.assertEqual(self.client.post("/v2/api/buy", json={}).status_code, 401)
        self.assertEqual(self.client.post("/v2/api/sell", json={}).status_code, 401)
        self.assertEqual(self.client.post("/v2/api/positions/1/exit").status_code, 401)
        self.assertEqual(
            self.client.post("/v2/api/index-settings", json={"auto_trade": True}).status_code, 401)

    def test_a_forged_cookie_is_refused(self) -> None:
        self.client.cookies.set("session", "not-a-real-token")
        self.assertEqual(self.client.get("/v2/api/positions").status_code, 401)


class AuthenticatedAccessTest(unittest.TestCase):
    """The gate must not lock out the operator — the failure that would matter
    just as much, and the one a 401-only test would miss entirely."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        from app.auth import hash_password
        # app.main is imported once, so db is shared across tests in the run —
        # a fixed username collides on the second test in this class.
        import uuid
        name = "planuser_" + uuid.uuid4().hex[:8]
        user = self.main.db.create_user(name, hash_password("Str0ngPassw0rd!x"),
                                        role="user", active=True)
        # A paid plan, so these assert AUTHENTICATION and not the subscription
        # tier — a free account is correctly refused with 402, which would make
        # this test pass or fail for the wrong reason.
        self.main.db.update_user(user["id"], account_plan="auto")
        r = self.client.post("/api/auth/login",
                             json={"username": name, "password": "Str0ngPassw0rd!x"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_logged_in_user_gets_through(self) -> None:
        for path in ("/v2/api/positions", "/v2/api/orders", "/v2/api/trades"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_logging_out_closes_the_door_again(self) -> None:
        self.assertEqual(self.client.get("/v2/api/positions").status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/v2/api/positions").status_code, 401)


class GateShapeTest(unittest.TestCase):
    """Pin the SHAPE, not just today's behaviour. A future route added without
    the dependency is the whole risk being defended against."""

    def test_the_dependency_is_on_the_router_not_per_route(self) -> None:
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web)
        head = src[:src.index('SPA_HTML = r"""')]
        self.assertIn('APIRouter(prefix="/v2", dependencies=[Depends(require_session)])', head)

    def test_every_route_inherits_it(self) -> None:
        """Enumerated from the live router, so a new endpoint is covered the
        moment it is registered."""
        from app.v2_web import router, require_session
        checked = 0
        for route in router.routes:
            deps = getattr(getattr(route, "dependant", None), "dependencies", [])
            calls = [d.call for d in deps]
            self.assertIn(require_session, calls, getattr(route, "path", route))
            checked += 1
        self.assertGreater(checked, 30, "expected the full v2 surface")

    def test_a_broken_auth_path_fails_closed(self) -> None:
        """If the user lookup throws, returning the book is the same outcome as
        having no auth at all. It must refuse instead."""
        import inspect
        from fastapi import HTTPException
        from app import v2_web
        src = inspect.getsource(v2_web.require_session)
        self.assertIn("status_code=503", src)
        orig = v2_web._auth_bits
        v2_web._auth_bits = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        try:
            with self.assertRaises(HTTPException) as caught:
                v2_web.require_session(object())
            self.assertEqual(caught.exception.status_code, 503)
        finally:
            v2_web._auth_bits = orig


if __name__ == "__main__":
    unittest.main()
