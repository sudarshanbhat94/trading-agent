"""Tiers that actually stop something.

Until now `plans.allows()` existed, was tested, and nothing called it — the
tiers were settable and meant nothing. This is the enforcement.

It runs in the SINGLE dependency every route already inherits, not as a
decorator on each of the 37 routes, for the same reason authentication does: a
per-route annotation is one forgotten line from a hole, and this codebase has
already produced that exact failure four times over with the index gates —
each individually plausible, each silently disabling a whole lane.

The unmapped-route policy is the interesting decision. A path missing from
ROUTE_FEATURES is treated as FREE rather than denied, because denying it would
mean a newly added route silently breaks for every paying user until someone
notices — a self-inflicted outage. Instead the test below enumerates the live
router and fails when a path is missing. Fail open in production, catch it in
CI.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid

from app import plans


def _client(tmp):
    os.environ["OPENSTOCKS_DISABLE_ENGINE"] = "1"
    os.environ["DATABASE_PATH"] = os.path.join(tmp, "auth.db")
    v2 = os.path.join(tmp, "v2.db")
    os.environ["V2_PAPER_DB"] = v2
    con = sqlite3.connect(v2)
    from app import v2_live
    v2_live.ensure_schema(con)
    con.commit(); con.close()
    # A real (empty) main DB. Without one the routes that read quotes raise
    # rather than returning a status, and the tier assertion cannot be made at
    # all — the fixture has to be complete enough for the gate to be the thing
    # under test.
    main_db = os.path.join(tmp, "main.db")
    mcon = sqlite3.connect(main_db)
    mcon.execute("CREATE TABLE IF NOT EXISTS latest_quotes(symbol TEXT, source TEXT, ts TEXT,"
                 " price REAL, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    mcon.execute("CREATE TABLE IF NOT EXISTS candles(symbol TEXT, source TEXT, ts TEXT,"
                 " open REAL, high REAL, low REAL, close REAL, volume REAL)")
    mcon.commit(); mcon.close()
    from fastapi.testclient import TestClient
    from app import main as m, v2_web
    v2_web.V2_DB = v2
    v2_web.MAIN_DB = main_db
    return TestClient(m.app), m


class RouteCoverageTest(unittest.TestCase):
    """The guard that makes the fail-open policy safe."""

    def test_every_live_route_is_in_the_table(self) -> None:
        from app.v2_web import router
        missing = [r.path for r in router.routes
                   if r.path not in plans.ROUTE_FEATURES]
        self.assertEqual(missing, [],
                         "add these to plans.ROUTE_FEATURES — an unmapped route is FREE")

    def test_the_table_has_no_stale_entries(self) -> None:
        """A path left behind after a route is renamed is dead config that
        reads like protection."""
        from app.v2_web import router
        live = {r.path for r in router.routes}
        stale = [p for p in plans.ROUTE_FEATURES if p not in live]
        self.assertEqual(stale, [], "these paths no longer exist")

    def test_every_mapped_feature_is_a_real_feature(self) -> None:
        for path, feature in plans.ROUTE_FEATURES.items():
            if feature is not None:
                with self.subTest(path=path):
                    self.assertIn(feature, plans.FEATURES)

    def test_the_money_endpoints_are_not_free(self) -> None:
        """Whatever else moves, these must never become free by accident."""
        for path in ("/v2/api/buy", "/v2/api/sell", "/v2/api/reset",
                     "/v2/api/positions/{pid}/exit", "/v2/api/index-settings"):
            with self.subTest(path=path):
                self.assertIsNotNone(plans.ROUTE_FEATURES.get(path))


class EnforcementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "tier_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)

    def as_plan(self, plan):
        self.main.db.update_user(self.user["id"], account_plan=plan)
        self.client.cookies.clear()
        r = self.client.post("/api/auth/login",
                             json={"username": self.name, "password": self.pw})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_free_tier_is_refused_the_paid_endpoints(self) -> None:
        self.as_plan("watch")
        for path in ("/v2/api/positions", "/v2/api/orders", "/v2/api/index-candles"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 402)

    def test_the_free_tier_still_gets_the_free_ones(self) -> None:
        """Being on the bottom tier must not read as being logged out."""
        self.as_plan("watch")
        self.assertEqual(self.client.get("/v2/api/me").status_code, 200)
        # /health reads the main quote DB, which this fixture has no copy of —
        # so assert only what this test is about: the TIER did not block it.
        self.assertNotIn(self.client.get("/v2/api/health").status_code, (401, 402))

    def test_the_middle_tier_reaches_the_book_but_not_options(self) -> None:
        self.as_plan("paper")
        self.assertEqual(self.client.get("/v2/api/positions").status_code, 200)
        self.assertEqual(self.client.get("/v2/api/index-settings").status_code, 402)

    def test_the_top_tier_reaches_everything(self) -> None:
        self.as_plan("auto")
        for path in ("/v2/api/positions", "/v2/api/orders", "/v2/api/index-settings"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_refusal_says_why(self) -> None:
        """402 with the reason, not a bare 403. The user has to know what to
        upgrade to, and the UI needs it to render an offer."""
        self.as_plan("watch")
        body = self.client.get("/v2/api/positions").json()["detail"]
        self.assertEqual(body["plan"], "watch")
        self.assertEqual(body["feature"], "paper_book")
        self.assertEqual(body["needs"], "paper")

    def test_a_low_tier_cannot_trade_the_book(self) -> None:
        """The endpoints that move money are the point of the whole exercise."""
        self.as_plan("watch")
        self.assertEqual(self.client.post("/v2/api/reset").status_code, 402)
        self.assertEqual(self.client.post("/v2/api/buy", json={}).status_code, 402)

    def test_the_existing_accounts_are_unaffected(self) -> None:
        """Ten live accounts read 'standard'. They must keep working exactly as
        before — a deploy that demotes real users to satisfy a rename is a
        worse outcome than no tiers at all."""
        self.as_plan("standard")
        for path in ("/v2/api/positions", "/v2/api/orders", "/v2/api/index-settings"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_authentication_still_comes_first(self) -> None:
        """A tier check must not accidentally become a way in."""
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/v2/api/positions").status_code, 401)


class GateShapeTest(unittest.TestCase):
    def test_enforcement_lives_in_the_shared_dependency(self) -> None:
        import inspect
        from app import v2_web
        self.assertIn("_check_plan(request, user)",
                      inspect.getsource(v2_web.require_session))

    def test_it_matches_on_the_route_pattern_not_the_raw_url(self) -> None:
        """/api/stock/{symbol} must be one entry, not one per symbol."""
        import inspect
        from app import v2_web
        self.assertIn('request.scope.get("route")',
                      inspect.getsource(v2_web._check_plan))


if __name__ == "__main__":
    unittest.main()


class MainAppFeatureGateTest(unittest.TestCase):
    """Routes on the MAIN app inherit nothing from the /v2 router.

    The /v2 gate is one shared dependency, which is what makes it safe. These
    endpoints hang off `app` instead, so Telegram alerts were reachable on
    every tier — including free — while being sold as a Pro feature.
    """

    def setUp(self) -> None:
        import uuid
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "tg_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)

    def as_plan(self, plan):
        self.main.db.update_user(self.user["id"], account_plan=plan)
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})

    def test_starter_cannot_reach_telegram(self) -> None:
        self.as_plan("watch")
        self.assertEqual(self.client.get("/api/me/telegram").status_code, 402)

    def test_free_cannot_either(self) -> None:
        self.as_plan("free")
        self.assertEqual(self.client.get("/api/me/telegram").status_code, 402)

    def test_pro_can(self) -> None:
        self.as_plan("paper")
        self.assertEqual(self.client.get("/api/me/telegram").status_code, 200)

    def test_elite_can(self) -> None:
        self.as_plan("auto")
        self.assertEqual(self.client.get("/api/me/telegram").status_code, 200)

    def test_every_telegram_route_is_gated(self) -> None:
        """Six endpoints; gating five of them would be the same hole."""
        import inspect
        from app import main as m
        src = inspect.getsource(m)
        start = src.index('@app.get("/api/me/telegram")')
        # end at the unlink handler's own body, not the next route's
        end = src.index("return {\"ok\": True}",
                        src.index('@app.post("/api/me/telegram/unlink")'))
        block = src[start:end]
        self.assertNotIn("require_user(request, settings, db)", block)
        self.assertEqual(block.count('require_feature(request, "telegram_alerts")'), 6)

    def test_writes_are_gated_not_just_the_read(self) -> None:
        self.as_plan("watch")
        for path in ("/api/me/telegram/token", "/api/me/telegram/verify",
                     "/api/me/telegram/test", "/api/me/telegram/prefs",
                     "/api/me/telegram/unlink"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 402)
