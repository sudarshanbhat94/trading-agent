"""Reset destroys the SHARED book, so it cannot be self-serve.

v2_book, v2_positions, v2_trades and v2_equity have no user column. Every
subscriber sees the same paper account — the one the engine actually trades. So
`/api/reset` is not "clear my cash", it is "delete the engine's open positions
and the entire trade history, for everybody".

It sat behind the `manual_trade` feature, i.e. every Pro and Elite subscriber
could call it. The book was wiped this way once already (2026-07-28), taking
the live record with it.

This is the stop-gap. Per-user books are the real fix and are a schema change
across the engine.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import unittest
import uuid

from app import v2_live, v2_web


class BookIsSharedTest(unittest.TestCase):
    """State the premise as an assertion, so the day it stops being true this
    test fails and the guard can be revisited."""

    def test_no_book_table_has_a_user_column(self) -> None:
        con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(con)
        for table in ("v2_book", "v2_positions", "v2_trades", "v2_equity"):
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
            with self.subTest(table=table):
                self.assertNotIn("user_id", cols)

    def test_reset_is_now_scoped_to_the_caller(self) -> None:
        """Superseded the stop-gap: reset used to run unqualified DELETEs on
        the engine's tables. It now clears the caller's own book (books.reset),
        and the unqualified version survives only as an operator-only helper."""
        src = inspect.getsource(v2_web.api_reset)
        self.assertIn("books.reset(v2, uid)", src)
        self.assertNotIn('"DELETE FROM %s" % t', src)
        house = inspect.getsource(v2_web._reset_house_book)
        self.assertIn('"DELETE FROM %s" % t', house)


class ResetIsOperatorOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        os.environ["OPENSTOCKS_DISABLE_ENGINE"] = "1"
        os.environ["DATABASE_PATH"] = os.path.join(tmp, "auth.db")
        v2 = os.path.join(tmp, "v2.db")
        os.environ["V2_PAPER_DB"] = v2
        con = sqlite3.connect(v2)
        v2_live.ensure_schema(con)
        con.commit(); con.close()
        main_db = os.path.join(tmp, "main.db")
        m = sqlite3.connect(main_db)
        m.execute("CREATE TABLE IF NOT EXISTS latest_quotes(symbol TEXT, source TEXT,"
                  " ts TEXT, price REAL, open REAL, high REAL, low REAL, close REAL,"
                  " volume REAL)")
        m.commit(); m.close()
        from fastapi.testclient import TestClient
        from app import main as mn
        v2_web.V2_DB = v2
        v2_web.MAIN_DB = main_db
        self.client, self.main = TestClient(mn.app), mn
        self.pw = "Str0ngPassw0rd!x"

    def _login_as(self, role):
        from app.auth import hash_password
        name = f"{role}_" + uuid.uuid4().hex[:8]
        u = self.main.db.create_user(name, hash_password(self.pw), role=role, active=True)
        self.main.db.update_user(u["id"], account_plan="auto")
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": name, "password": self.pw})
        return u

    def test_a_subscriber_resets_only_their_own_book(self) -> None:
        """They may reset — it is their book now — and the engine's must be
        untouched by it."""
        import sqlite3
        from app import v2_live as vl
        u = self._login_as("user")
        con = sqlite3.connect(v2_web.V2_DB)
        vl.record_entry(con, "IN", "swing_meanrev", "RELIANCE", "2026-08-04",
                        1300.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
        con.commit(); con.close()
        r = self.client.post("/v2/api/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scope"], "your book")
        con = sqlite3.connect(v2_web.V2_DB)
        left = con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0]
        con.close()
        self.assertEqual(left, 1, "a subscriber reset must not touch the engine")

    def test_the_operator_can_also_reset_the_house_book(self) -> None:
        self._login_as("admin")
        self.assertEqual(self.client.post("/v2/api/reset").status_code, 200)

    def test_it_is_still_behind_authentication(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/v2/api/reset").status_code, 401)


if __name__ == "__main__":
    unittest.main()
