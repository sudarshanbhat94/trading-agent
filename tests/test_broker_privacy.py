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
        os.environ["BROKER_STATE_PATH"] = os.path.join(self.tmp, "broker.json")
        import importlib
        from app import broker
        self.broker = importlib.reload(broker)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.owner = self._user("owner_")
        self.other = self._user("other_")
        self.broker.configure(owner_user_id=self.owner["id"])
        self.broker.save_token("t0k")

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
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 403)

    def test_the_broker_status_endpoint_is_owner_only(self) -> None:
        self._login(self.other)
        self.assertEqual(self.client.get("/v2/api/broker").status_code, 403)


class GateShapeTest(unittest.TestCase):
    """Pin the shape so this cannot regress into a role check."""

    def test_overview_compares_the_numeric_user_id(self) -> None:
        src = inspect.getsource(v2_web.api_overview)
        self.assertIn('int(owner) == int(user.get("id") or -1)', src)
        self.assertIn("is_owner and", src)

    def test_overview_takes_the_session_user(self) -> None:
        """It took no user at all, which is why it could not check one."""
        self.assertIn("user", inspect.signature(v2_web.api_overview).parameters)

    def test_the_broker_endpoint_prefers_ownership_over_role(self) -> None:
        src = inspect.getsource(v2_web.api_broker)
        self.assertIn("not the live-trading owner", src)
        self.assertIn('int(owner) != int(user.get("id") or -1)', src)


if __name__ == "__main__":
    unittest.main()
