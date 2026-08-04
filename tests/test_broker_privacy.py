"""The broker account is ONE person's. Nobody else may see it.

There is a single real Upstox account on this deployment. OpenStocks is a
multi-user product being sold to subscribers, so every endpoint that can expose
that account's balance, holdings or order history must be gated on the OWNER's
numeric user id.

The first version of the home-page tile checked only "is a broker connected",
which showed the operator's real cash and positions to EVERY logged-in
subscriber. /api/broker had the same shape of hole: it checked the admin ROLE,
so any second admin promoted for support would have inherited the view.

A role is a set that grows. Ownership is an id.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import unittest
import uuid

from app import v2_web


def _client(tmp):
    os.environ["OPENSTOCKS_DISABLE_ENGINE"] = "1"
    os.environ["DATABASE_PATH"] = os.path.join(tmp, "auth.db")
    v2 = os.path.join(tmp, "v2.db")
    os.environ["V2_PAPER_DB"] = v2
    con = sqlite3.connect(v2)
    from app import v2_live
    v2_live.ensure_schema(con)
    con.commit(); con.close()
    main_db = os.path.join(tmp, "main.db")
    m = sqlite3.connect(main_db)
    m.execute("CREATE TABLE IF NOT EXISTS latest_quotes(symbol TEXT, source TEXT, ts TEXT,"
              " price REAL, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    m.execute("CREATE TABLE IF NOT EXISTS candles(symbol TEXT, source TEXT, ts TEXT,"
              " open REAL, high REAL, low REAL, close REAL, volume REAL)")
    m.commit(); m.close()
    from fastapi.testclient import TestClient
    from app import main as mn
    v2_web.V2_DB = v2
    v2_web.MAIN_DB = main_db
    return TestClient(mn.app), mn


class OverviewLeakTest(unittest.TestCase):
    """THE leak: the home page returned the real account to everyone."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        os.environ["BROKER_STATE_DIR"] = os.path.join(self.tmp, "brokers")
        os.environ["BROKER_STATE_PATH"] = os.path.join(self.tmp, "legacy.json")
        import importlib
        from app import broker
        self.broker = importlib.reload(broker)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.owner = self._user("owner_")
        self.other = self._user("other_")
        self.broker.configure(self.owner["id"])
        self.broker.save_token(self.owner["id"], "t0k")

    def _user(self, prefix, role="user"):
        from app.auth import hash_password
        name = prefix + uuid.uuid4().hex[:8]
        u = self.main.db.create_user(name, hash_password(self.pw), role=role, active=True)
        self.main.db.update_user(u["id"], account_plan="auto")
        u["username"] = name
        return u

    def _login(self, u):
        self.client.cookies.clear()
        r = self.client.post("/api/auth/login",
                             json={"username": u["username"], "password": self.pw})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_subscriber_never_sees_the_real_account(self) -> None:
        self._login(self.other)
        r = self.client.get("/v2/api/overview")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json().get("real"),
                          "another user must not see the operator's broker balance")

    def test_a_second_admin_does_not_inherit_it(self) -> None:
        """Promoting someone to admin for support must not hand them a view of
        real money."""
        admin2 = self._user("admin2_", role="admin")
        self._login(admin2)
        self.assertIsNone(self.client.get("/v2/api/overview").json().get("real"))
        # they see their OWN broker panel, which is empty — not the operator's
        own = self.client.get("/v2/api/broker")
        self.assertEqual(own.status_code, 200)
        self.assertFalse(own.json()["connected"])

    def test_the_broker_endpoint_shows_the_callers_own_empty_broker(self) -> None:
        """Every user may LINK their own Upstox account, and must never see
        anyone else's. Refusing outright would block them from connecting."""
        self._login(self.other)
        r = self.client.get("/v2/api/broker")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["connected"])
        self.assertEqual(r.json()["orders_today"], 0)


class GateShapeTest(unittest.TestCase):
    """Pin the shape so this cannot regress into a role check."""

    def test_overview_reads_the_callers_own_broker_state(self) -> None:
        """No owner comparison any more: state is per-user, so the id passed in
        IS the boundary and there is nothing to compare against."""
        src = inspect.getsource(v2_web.api_overview)
        self.assertIn("_bk.state(uid)", src)
        self.assertIn("_bk.account_snapshot(uid)", src)
        self.assertNotIn("owner_user_id", src)

    def test_overview_takes_the_session_user(self) -> None:
        """It took no user at all, which is why it could not check one."""
        self.assertIn("user", inspect.signature(v2_web.api_overview).parameters)

    def test_the_broker_endpoint_reads_only_the_callers_state(self) -> None:
        src = inspect.getsource(v2_web.api_broker)
        self.assertIn("broker.state(_uid(user))", src)
        self.assertIn("user_id=?", src)      # the ledger is scoped too



class EliteOnlyTest(unittest.TestCase):
    """Connecting a real brokerage account is an Elite feature.

    The routes already carried an OWNER check, but that answers "whose money is
    this sleeve", not "may this account link a broker at all". Ungated, every
    Starter subscriber could have pointed the product at a live trading account.

    Both gates apply: tier first (the shared /v2 dependency), ownership second.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        os.environ["BROKER_STATE_DIR"] = os.path.join(self.tmp, "brokers")
        os.environ["BROKER_STATE_PATH"] = os.path.join(self.tmp, "legacy.json")
        import importlib
        from app import broker
        self.broker = importlib.reload(broker)
        self.pw = "Str0ngPassw0rd!x"

    def _as(self, plan, role="admin"):
        from app.auth import hash_password
        name = f"{plan}_" + uuid.uuid4().hex[:8]
        u = self.main.db.create_user(name, hash_password(self.pw), role=role, active=True)
        self.main.db.update_user(u["id"], account_plan=plan)
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": name, "password": self.pw})
        return u

    def test_starter_cannot_reach_the_broker_at_all(self) -> None:
        self._as("watch")
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 402)

    def test_pro_cannot_either(self) -> None:
        """Pro buys a paper book, not a live brokerage link."""
        self._as("paper")
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 402)

    def test_elite_can(self) -> None:
        u = self._as("auto")
        self.broker.configure(u["id"])
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 200)

    def test_every_broker_route_is_gated_not_just_the_read(self) -> None:
        """Six endpoints; gating five would be the same hole."""
        from app import plans
        for path in ("/v2/api/broker", "/v2/api/broker/config",
                     "/v2/api/broker/auth-url", "/v2/api/broker/connect",
                     "/v2/api/broker/arm", "/v2/api/broker/disconnect"):
            with self.subTest(path=path):
                self.assertEqual(plans.ROUTE_FEATURES.get(path), "broker_connect")

    def test_the_arming_endpoint_refuses_a_lower_tier(self) -> None:
        """The one that spends money."""
        self._as("paper")
        r = self.client.post("/v2/api/broker/arm",
                             json={"armed": True, "confirm": "TRADE REAL MONEY"})
        self.assertEqual(r.status_code, 402)

    def test_tier_is_checked_even_for_their_own_broker(self) -> None:
        """A Pro user has their own broker file and still may not use it."""
        u = self._as("paper")
        self.broker.configure(u["id"])
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 402)
if __name__ == "__main__":
    unittest.main()
