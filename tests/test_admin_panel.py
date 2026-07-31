"""The admin panel: who may manage accounts, and what they may change.

`users.account_plan` existed since the schema was written and enforced nothing,
so free and paid were the same product. This is the layer that makes it mean
something — plus the panel to set it.

ROLE AND PLAN ARE SEPARATE, deliberately. An admin manages other people's
plans; it does not change what their own book does. Conflating them is how
"admin" quietly becomes a billing tier that cannot be revoked, and it is why
the operator can hold a real trading account and still administer.

The self-lockout guards are the ones worth having. Demoting or disabling your
own account is a ONE-WAY DOOR — the account cannot log back in to undo it, and
on the only admin it takes the panel away from everyone.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid

from app import plans


class TierTest(unittest.TestCase):
    def test_the_ladder_is_ordered(self) -> None:
        self.assertEqual(plans.TIERS, ("watch", "paper", "auto"))
        self.assertLess(plans.rank("watch"), plans.rank("paper"))
        self.assertLess(plans.rank("paper"), plans.rank("auto"))

    def test_a_higher_tier_grants_everything_below_it(self) -> None:
        for feature in plans.FEATURES:
            with self.subTest(feature=feature):
                self.assertTrue(plans.allows("auto", feature))

    def test_the_lowest_tier_does_not_reach_paid_features(self) -> None:
        for feature in ("market_internals", "option_chain", "paper_book",
                        "index_options", "broker_connect", "export"):
            with self.subTest(feature=feature):
                self.assertFalse(plans.allows("watch", feature))

    def test_the_free_tier_still_reaches_the_free_features(self) -> None:
        for feature in ("signals", "catalysts", "index_call"):
            self.assertTrue(plans.allows("watch", feature))

    def test_existing_accounts_are_not_demoted_by_the_migration(self) -> None:
        """The live book has ten accounts reading 'standard' — the operator's
        own people. Silently dropping them to the free tier on deploy would
        break working accounts to satisfy a naming change."""
        self.assertEqual(plans.normalize("standard"), "auto")

    def test_an_unknown_plan_falls_back_rather_than_raising(self) -> None:
        for bad in (None, "", "gold", "PAPER ", 7):
            self.assertIn(plans.normalize(bad), plans.TIERS)
        self.assertEqual(plans.normalize("PAPER "), "paper")

    def test_an_unknown_feature_is_denied(self) -> None:
        """Fail closed: a misspelled feature key at a call site would otherwise
        be free for everyone, which is the exact hole this module prevents."""
        self.assertFalse(plans.allows("auto", "nonexistent_feature"))


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
    from app import main as m, v2_web
    v2_web.V2_DB = v2
    return TestClient(m.app), m


class AdminApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.admin_name = "adm_" + uuid.uuid4().hex[:8]
        self.user_name = "usr_" + uuid.uuid4().hex[:8]
        self.admin = self.main.db.create_user(self.admin_name, hash_password(self.pw),
                                              role="admin", active=True)
        self.member = self.main.db.create_user(self.user_name, hash_password(self.pw),
                                               role="user", active=True)

    def login(self, name):
        self.client.cookies.clear()
        r = self.client.post("/api/auth/login", json={"username": name, "password": self.pw})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_normal_user_cannot_reach_the_admin_api(self) -> None:
        self.login(self.user_name)
        self.assertEqual(self.client.get("/v2/api/admin/users").status_code, 403)
        self.assertEqual(self.client.post(f"/v2/api/admin/users/{self.member['id']}",
                                          json={"plan": "auto"}).status_code, 403)

    def test_an_anonymous_request_cannot_either(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/v2/api/admin/users").status_code, 401)

    def test_an_admin_sees_every_account(self) -> None:
        self.login(self.admin_name)
        r = self.client.get("/v2/api/admin/users")
        self.assertEqual(r.status_code, 200)
        names = {u["username"] for u in r.json()["users"]}
        self.assertIn(self.admin_name, names)
        self.assertIn(self.user_name, names)

    def test_an_admin_can_change_a_plan(self) -> None:
        self.login(self.admin_name)
        r = self.client.post(f"/v2/api/admin/users/{self.member['id']}", json={"plan": "paper"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["plan"], "paper")
        fresh = self.main.db.user_by_id(self.member["id"])
        self.assertEqual(plans.normalize(fresh["account_plan"]), "paper")

    def test_an_unknown_plan_is_normalised_not_stored_raw(self) -> None:
        self.login(self.admin_name)
        r = self.client.post(f"/v2/api/admin/users/{self.member['id']}", json={"plan": "platinum"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.json()["plan"], plans.TIERS)

    def test_an_admin_cannot_remove_their_own_admin_role(self) -> None:
        """One-way door: the account cannot log back in to undo it, and on the
        only admin the panel becomes unreachable for everyone."""
        self.login(self.admin_name)
        r = self.client.post(f"/v2/api/admin/users/{self.admin['id']}", json={"role": "user"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.main.db.user_by_id(self.admin["id"])["role"], "admin")

    def test_an_admin_cannot_deactivate_themselves(self) -> None:
        self.login(self.admin_name)
        r = self.client.post(f"/v2/api/admin/users/{self.admin['id']}", json={"active": False})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.main.db.user_by_id(self.admin["id"])["active"])

    def test_an_admin_can_still_manage_someone_else(self) -> None:
        """The guard must be about SELF, not about admins generally."""
        self.login(self.admin_name)
        r = self.client.post(f"/v2/api/admin/users/{self.member['id']}", json={"active": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.main.db.user_by_id(self.member["id"])["active"])

    def test_updating_a_missing_user_is_a_404(self) -> None:
        self.login(self.admin_name)
        self.assertEqual(self.client.post("/v2/api/admin/users/999999",
                                          json={"plan": "auto"}).status_code, 404)


class WhoAmITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "me_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})

    def test_the_plan_is_resolved_server_side(self) -> None:
        """A plan computed in JavaScript is a suggestion, not a gate. The page
        and the API must never disagree about it."""
        r = self.client.get("/v2/api/me")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["plan"], plans.TIERS)
        self.assertEqual(body["username"], self.name)
        self.assertFalse(body["is_admin"])
        self.assertIn("features", body)

    def test_the_session_carries_the_plan_at_all(self) -> None:
        """_public_user did not expose account_plan, so every caller asking the
        session for a plan got None — which normalises to the LOWEST tier and
        would have quietly demoted every account the day gating went live."""
        self.main.db.update_user(self.user["id"], account_plan="paper")
        from app.auth import current_user
        # the stored value has to survive the round trip through the session
        fresh = self.main.db.user_by_id(self.user["id"])
        self.assertEqual(plans.normalize(fresh["account_plan"]), "paper")
        r = self.client.get("/v2/api/me")
        self.assertEqual(r.json()["plan"], "paper")

    def test_both_public_user_builders_carry_the_plan(self) -> None:
        """There are TWO of them — auth._public_user and db._public_user — and
        the session goes through auth's. Adding the field to only one left every
        session reporting no plan, which normalises to the lowest tier: silent
        demotion of every account. This caught it once; it should catch it
        again."""
        from app import auth as _auth, db as _db
        row = dict(self.main.db.user_by_id(self.user["id"]))
        row["account_plan"] = "paper"
        self.assertEqual(_auth._public_user(row).get("account_plan"), "paper")
        self.assertEqual(_db._public_user(row).get("account_plan"), "paper")

    def test_features_match_the_plan(self) -> None:
        self.main.db.update_user(self.user["id"], account_plan="watch")
        feats = self.client.get("/v2/api/me").json()["features"]
        self.assertTrue(feats["catalysts"])
        self.assertFalse(feats["broker_connect"])


if __name__ == "__main__":
    unittest.main()
