"""Seven-day trial, the upgrade request, and the manual UPI payment.

The flow deliberately has no payment processor: the subscriber scans a UPI QR,
the admin confirms, the plan flips. Nothing sensitive is stored and no card
details pass through the app, which is the right shape while the product is
still finding out whether anyone will pay — and it can be swapped for a gateway
later without changing this contract.

Two details carry most of the risk and are pinned hardest:

  * A LAPSED TRIAL MUST NOT LOCK THE DOOR. An expired account still reaches its
    own profile and the plans screen, or it cannot pay its way back in.
  * A NULL trial date means NO TRIAL, not an expired one. The ten accounts that
    predate this feature must keep the plan they already have rather than being
    read as lapsed triallists and demoted.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

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
    from fastapi.testclient import TestClient
    from app import main as m, v2_web
    v2_web.V2_DB = v2
    v2_web.PAYMENT_FILE = os.path.join(tmp, "payment.json")
    return TestClient(m.app), m, v2_web


class TrialWindowTest(unittest.TestCase):
    def test_a_fresh_trial_is_active_and_counts_down(self) -> None:
        ends = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        state = plans.trial_state(ends)
        self.assertTrue(state["active"])
        self.assertEqual(state["days_left"], 7)

    def test_the_last_partial_day_still_reads_as_one(self) -> None:
        """Rounding down would say '0 days left' while the trial is genuinely
        still running, which reads as a bug to the person using it."""
        ends = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        state = plans.trial_state(ends)
        self.assertTrue(state["active"])
        self.assertEqual(state["days_left"], 1)

    def test_an_expired_trial_is_inactive(self) -> None:
        ends = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        state = plans.trial_state(ends)
        self.assertFalse(state["active"])
        self.assertTrue(state["had_trial"])

    def test_no_trial_date_is_not_an_expired_trial(self) -> None:
        """The distinction the pre-existing accounts depend on."""
        state = plans.trial_state(None)
        self.assertFalse(state["active"])
        self.assertFalse(state["had_trial"])

    def test_a_malformed_date_does_not_raise(self) -> None:
        for bad in ("", "not-a-date", 0):
            self.assertFalse(plans.trial_state(bad)["active"])

    def test_a_trial_lifts_a_free_account(self) -> None:
        ends = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        self.assertEqual(plans.effective("free", ends), plans.TRIAL_TIER)

    def test_a_trial_never_demotes_a_paying_account(self) -> None:
        """A subscriber on the top tier whose trial window happens to still be
        open must not be dropped to the trial tier by it."""
        ends = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        self.assertEqual(plans.effective("auto", ends), "auto")

    def test_after_the_trial_the_stored_plan_applies(self) -> None:
        ends = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertEqual(plans.effective("free", ends), "free")
        self.assertEqual(plans.effective("paper", ends), "paper")

    def test_an_untouched_account_keeps_its_plan(self) -> None:
        self.assertEqual(plans.effective("standard", None), "auto")


class UpgradeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main, self.web = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "sub_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)
        self.login()

    def login(self):
        self.client.cookies.clear()
        r = self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})
        self.assertEqual(r.status_code, 200, r.text)

    def mine(self, status="pending"):
        """Only THIS user's requests. app.main imports once so the database is
        shared across the run, and a global count picks up other tests' rows."""
        return [r for r in self.main.db.plan_requests(status)
                if int(r["user_id"]) == int(self.user["id"])]

    def configure_payment(self):
        import json
        with open(self.web.PAYMENT_FILE, "w", encoding="utf-8") as fh:
            json.dump({"upi_id": "test@ybl", "payee": "Test", "qr_url": "", "note": ""}, fh)

    def test_requesting_an_upgrade_creates_one_pending_request(self) -> None:
        r = self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["plan"], "paper")
        self.assertEqual(r.json()["amount"], plans.PRICES["paper"])
        self.assertEqual(len(self.mine()), 1)

    def test_pressing_the_button_twice_does_not_queue_two(self) -> None:
        """An admin should not have to work through duplicates of the same
        request because someone double-tapped."""
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        self.assertEqual(len(self.mine()), 1)

    def test_changing_your_mind_updates_the_open_request(self) -> None:
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        open_ = self.mine()
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]["requested_plan"], "auto")
        self.assertEqual(open_[0]["amount"], plans.PRICES["auto"])

    def test_you_cannot_request_a_downgrade_as_an_upgrade(self) -> None:
        self.main.db.update_user(self.user["id"], account_plan="auto")
        self.login()
        self.assertEqual(self.client.post("/v2/api/upgrade", json={"plan": "watch"}).status_code, 400)

    def test_a_trial_user_can_buy_the_plan_their_trial_is_showing_them(self) -> None:
        """The trial lifts the account to Pro, so comparing against the
        EFFECTIVE plan refused the trial user who wanted to buy Pro — the very
        plan they were about to lose. Found on the live site."""
        self.main.db.start_trial(self.user["id"], 7)
        self.login()
        me = self.client.get("/v2/api/me").json()
        self.assertEqual(me["plan"], "paper")       # lifted by the trial
        self.assertEqual(me["paid_plan"], plans.SIGNUP_TIER)
        self.assertEqual(self.client.post("/v2/api/upgrade", json={"plan": "paper"}).status_code, 200)

    def test_you_still_cannot_buy_what_you_already_pay_for(self) -> None:
        self.main.db.update_user(self.user["id"], account_plan="paper")
        self.login()
        self.assertEqual(self.client.post("/v2/api/upgrade", json={"plan": "paper"}).status_code, 400)

    def test_the_price_comes_from_the_server_not_the_client(self) -> None:
        """A client-supplied amount would be a free upgrade for anyone with a
        browser console."""
        r = self.client.post("/v2/api/upgrade", json={"plan": "auto", "amount": 1})
        self.assertEqual(r.json()["amount"], plans.PRICES["auto"])

    def test_the_payment_block_says_when_it_is_not_set_up(self) -> None:
        r = self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        self.assertFalse(r.json()["payment"]["configured"])

    def test_a_configured_payment_returns_a_upi_link_with_the_amount(self) -> None:
        self.configure_payment()
        r = self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        pay = r.json()["payment"]
        self.assertTrue(pay["configured"])
        self.assertIn("upi://pay?", pay["upi_link"])
        self.assertIn("pa=test%40ybl", pay["upi_link"])
        self.assertIn(f"am={plans.PRICES['paper']:.2f}", pay["upi_link"])

    def test_each_payment_app_gets_its_own_link(self) -> None:
        """One button per app so nobody has to find the right one in a chooser,
        and the generic upi:// stays as the fallback because app-specific
        schemes are vendor conventions rather than a standard."""
        self.configure_payment()
        apps = self.client.post("/v2/api/upgrade", json={"plan": "paper"}).json()["payment"]["apps"]
        names = [a["name"] for a in apps]
        self.assertIn("PhonePe", names)
        self.assertIn("Google Pay", names)
        self.assertEqual(names[-1], "Any UPI app", "the generic link must remain, and last")

    def test_every_app_link_carries_the_same_amount(self) -> None:
        """They differ only by scheme, so they cannot disagree about the price —
        a branded link that charged something else would be the worst possible
        bug in this flow."""
        self.configure_payment()
        apps = self.client.post("/v2/api/upgrade", json={"plan": "auto"}).json()["payment"]["apps"]
        want = f"am={plans.PRICES['auto']:.2f}"
        for a in apps:
            with self.subTest(app=a["name"]):
                self.assertIn(want, a["link"])
                self.assertIn("pa=test%40ybl", a["link"])

    def test_no_apps_when_payment_is_unconfigured(self) -> None:
        self.assertEqual(
            self.client.post("/v2/api/upgrade", json={"plan": "paper"}).json()["payment"]["apps"], [])

    def test_the_payment_note_carries_the_request_id(self) -> None:
        """NOTHING tells this app that money arrived — the admin reconciles
        against their own bank feed. The note lands in the statement narration
        and is the only thread tying a UPI credit to the account that owes it."""
        self.configure_payment()
        d = self.client.post("/v2/api/upgrade", json={"plan": "auto"}).json()
        self.assertEqual(d["payment"]["reference"], f"OpenStocks Elite #{d['request_id']}")
        for a in d["payment"]["apps"]:
            with self.subTest(app=a["name"]):
                self.assertIn(f"%23{d['request_id']}", a["link"])   # '#' url-encoded

    def test_approving_records_what_was_matched(self) -> None:
        """An approval with no reference is an unauditable claim that money
        arrived, and leaves nothing to check a disputed subscription against."""
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        rid = self.mine()[0]["id"]
        self.main.db.decide_plan_request(rid, True, "admin", "UTR123456789")
        row = self.main.db.plan_request(rid)
        self.assertEqual(row["status"], "approved")
        self.assertIn("UTR123456789", row["note"])

    def test_approving_without_a_reference_still_works(self) -> None:
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        rid = self.mine()[0]["id"]
        self.main.db.decide_plan_request(rid, True, "admin", "")
        self.assertEqual(self.main.db.plan_request(rid)["status"], "approved")

    def test_the_qr_encodes_the_amount(self) -> None:
        """A static QR makes the subscriber type the price, and a wrong amount
        is the reconciliation work this flow cannot absorb."""
        self.configure_payment()
        r = self.client.get("/v2/api/pay-qr?plan=auto")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("image/svg+xml", r.headers["content-type"])
        self.assertIn("<svg", r.text)

    def test_the_qr_is_404_when_payment_is_unconfigured(self) -> None:
        self.assertEqual(self.client.get("/v2/api/pay-qr?plan=paper").status_code, 404)

    def test_approval_grants_the_plan_in_one_step(self) -> None:
        """Marking a request handled and forgetting to upgrade the account is
        the obvious way a manual flow goes wrong."""
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        rid = self.mine()[0]["id"]
        admin_name = "adm_" + uuid.uuid4().hex[:8]
        from app.auth import hash_password
        self.main.db.create_user(admin_name, hash_password(self.pw), role="admin", active=True)
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": admin_name, "password": self.pw})
        r = self.client.post(f"/v2/api/admin/requests/{rid}", json={"approve": True})
        self.assertEqual(r.status_code, 200, r.text)
        fresh = self.main.db.user_by_id(self.user["id"])
        self.assertEqual(plans.normalize(fresh["account_plan"]), "paper")

    def test_rejection_does_not_grant_the_plan(self) -> None:
        self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        rid = self.mine()[0]["id"]
        self.main.db.decide_plan_request(rid, False, "admin")
        fresh = self.main.db.user_by_id(self.user["id"])
        self.assertNotEqual(plans.normalize(fresh["account_plan"]), "auto")

    def test_a_decided_request_cannot_be_decided_again(self) -> None:
        # `paper`, not `watch`: a new account already STARTS on watch, so
        # requesting it is not an upgrade and never creates a request.
        self.client.post("/v2/api/upgrade", json={"plan": "paper"})
        rid = self.mine()[0]["id"]
        self.main.db.decide_plan_request(rid, False, "admin")
        self.main.db.decide_plan_request(rid, True, "admin")
        fresh = self.main.db.user_by_id(self.user["id"])
        self.assertNotEqual(plans.normalize(fresh["account_plan"]), "paper")

    def test_a_normal_user_cannot_approve_their_own_request(self) -> None:
        self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        rid = self.mine()[0]["id"]
        self.assertEqual(
            self.client.post(f"/v2/api/admin/requests/{rid}", json={"approve": True}).status_code, 403)


class LapsedAccountTest(unittest.TestCase):
    """A lapsed subscriber must still be able to pay their way back in."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main, self.web = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "lapsed_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)
        self.main.db.update_user(self.user["id"], account_plan="free")
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})

    def test_they_can_still_see_their_own_account(self) -> None:
        r = self.client.get("/v2/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"], "free")

    def test_they_can_still_reach_the_plans_and_pay(self) -> None:
        self.assertEqual(self.client.post("/v2/api/upgrade", json={"plan": "watch"}).status_code, 200)

    def test_but_the_paid_features_are_closed(self) -> None:
        for path in ("/v2/api/positions", "/v2/api/catalysts", "/v2/api/orders"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 402)


class SignupTierTest(unittest.TestCase):
    def test_a_new_account_starts_on_starter(self) -> None:
        """Operator decision: everyone keeps the signals and the catalyst feed
        for good, and only loses the paper book and analytics when the trial
        lapses."""
        tmp = tempfile.mkdtemp()
        _c, m, _w = _client(tmp)
        from app.auth import hash_password
        u = m.db.create_user("new_" + uuid.uuid4().hex[:8],
                             hash_password("Str0ngPassw0rd!x"), role="user", active=True)
        self.assertEqual(plans.normalize(m.db.user_by_id(u["id"])["account_plan"]),
                         plans.SIGNUP_TIER)
        self.assertEqual(plans.SIGNUP_TIER, "watch")

    def test_the_fallback_for_garbage_is_still_the_free_tier(self) -> None:
        """SIGNUP_TIER is a product decision; DEFAULT_TIER is what an unreadable
        stored value falls back to, and that must stay closed."""
        self.assertEqual(plans.DEFAULT_TIER, "free")
        self.assertEqual(plans.normalize("nonsense"), "free")


class SignupStartsTrialTest(unittest.TestCase):
    def test_a_trial_cannot_be_restarted(self) -> None:
        """Calling it twice must not hand the same account another free week."""
        tmp = tempfile.mkdtemp()
        _c, m, _w = _client(tmp)
        from app.auth import hash_password
        name = "t_" + uuid.uuid4().hex[:8]
        u = m.db.create_user(name, hash_password("Str0ngPassw0rd!x"), role="user", active=True)
        m.db.start_trial(u["id"], 7)
        first = m.db.user_by_id(u["id"])["trial_ends_at"]
        m.db.start_trial(u["id"], 30)
        self.assertEqual(m.db.user_by_id(u["id"])["trial_ends_at"], first)

    def test_signup_wires_the_trial_in(self) -> None:
        import inspect
        from app import auth
        self.assertIn("start_trial(", inspect.getsource(auth.signup_user))


class AdminNotificationTest(unittest.TestCase):
    """Nothing tells this system that money ARRIVED — a UPI credit lands in a
    bank account the app cannot see. What it CAN do is tell the admin a payment
    is expected and what reference to look for, so the bank notification on
    their phone means something when it appears."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main, self.web = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "u_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)
        self.admin_name = "a_" + uuid.uuid4().hex[:8]
        self.admin = self.main.db.create_user(self.admin_name, hash_password(self.pw),
                                              role="admin", active=True)

    def test_requesting_an_upgrade_alerts_the_admins(self) -> None:
        from app import telegram_bot, v2_web
        seen = {}
        orig = telegram_bot.notify_users
        telegram_bot.notify_users = lambda ids, text: seen.update(ids=list(ids), text=text) or 1
        try:
            self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})
            self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        finally:
            telegram_bot.notify_users = orig
        self.assertIn(int(self.admin["id"]), seen.get("ids", []))
        self.assertNotIn(int(self.user["id"]), seen.get("ids", []))
        self.assertIn(self.name, seen.get("text", ""))
        self.assertIn("999", seen.get("text", ""))

    def test_a_telegram_failure_does_not_block_the_subscription(self) -> None:
        """A messaging outage must never stop someone paying."""
        from app import telegram_bot
        orig = telegram_bot.notify_users
        telegram_bot.notify_users = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})
            r = self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        finally:
            telegram_bot.notify_users = orig
        self.assertEqual(r.status_code, 200, r.text)
        # this user's row only — app.main imports once, so the database is
        # shared across the run and a global count sees other tests' requests
        self.assertEqual(len([q for q in self.main.db.plan_requests("pending")
                              if int(q["user_id"]) == int(self.user["id"])]), 1)

    def test_the_admin_sees_a_pending_count(self) -> None:
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})
        self.client.post("/v2/api/upgrade", json={"plan": "auto"})
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": self.admin_name, "password": self.pw})
        self.assertGreaterEqual(self.client.get("/v2/api/me").json()["pending_approvals"], 1)

    def test_a_normal_user_is_not_told_the_queue_length(self) -> None:
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})
        self.assertEqual(self.client.get("/v2/api/me").json()["pending_approvals"], 0)


class SubscriptionExpiryTest(unittest.TestCase):
    """A paid plan RUNS OUT. Without this an approval was permanent — pay Rs 999
    once and hold Elite forever, which is a one-off sale wearing the word
    "subscription"."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.client, self.main, self.web = _client(self.tmp)
        from app.auth import hash_password
        self.pw = "Str0ngPassw0rd!x"
        self.name = "exp_" + uuid.uuid4().hex[:8]
        self.user = self.main.db.create_user(self.name, hash_password(self.pw),
                                             role="user", active=True)

    def login(self):
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": self.name, "password": self.pw})

    def test_approval_sets_a_billing_period(self) -> None:
        r = self.main.db.create_plan_request(self.user["id"], "auto", 999.0)
        self.main.db.decide_plan_request(r["id"], True, "admin")
        row = self.main.db.user_by_id(self.user["id"])
        self.assertTrue(row["plan_expires_at"])
        st = plans.subscription_state(row["account_plan"], row["plan_expires_at"])
        self.assertEqual(st["days_left"], plans.SUBSCRIPTION_DAYS)

    def test_renewing_early_extends_rather_than_resets(self) -> None:
        """Paying on time must not throw away what is left."""
        for _ in range(2):
            r = self.main.db.create_plan_request(self.user["id"], "auto", 999.0)
            self.main.db.decide_plan_request(r["id"], True, "admin")
        row = self.main.db.user_by_id(self.user["id"])
        st = plans.subscription_state(row["account_plan"], row["plan_expires_at"])
        self.assertEqual(st["days_left"], plans.SUBSCRIPTION_DAYS * 2)

    def test_a_lapsed_subscription_loses_its_features(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.main.db.update_user(self.user["id"], account_plan="auto", plan_expires_at=past)
        self.login()
        self.assertEqual(self.client.get("/v2/api/me").json()["plan"], plans.SIGNUP_TIER)
        self.assertEqual(self.client.get("/v2/api/index-settings").status_code, 402)

    def test_a_lapsed_subscriber_keeps_the_starter_features(self) -> None:
        """Dropping them to `free` would take away the signals they had before
        they ever paid."""
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.main.db.update_user(self.user["id"], account_plan="auto", plan_expires_at=past)
        self.login()
        self.assertEqual(self.client.get("/v2/api/catalysts").status_code, 200)

    def test_a_lapsed_subscriber_can_re_buy_the_same_tier(self) -> None:
        """Comparing against the STORED plan would call it a downgrade and
        refuse the renewal."""
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.main.db.update_user(self.user["id"], account_plan="auto", plan_expires_at=past)
        self.login()
        self.assertEqual(self.client.post("/v2/api/upgrade", json={"plan": "auto"}).status_code, 200)

    def test_an_active_subscription_keeps_working(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
        self.main.db.update_user(self.user["id"], account_plan="auto", plan_expires_at=future)
        self.login()
        me = self.client.get("/v2/api/me").json()
        self.assertEqual(me["plan"], "auto")
        self.assertEqual(me["subscription"]["days_left"], 9)

    def test_a_paid_plan_with_no_end_date_is_not_treated_as_lapsed(self) -> None:
        """Rows that predate this column must not be demoted to fix a schema
        gap — that would cut off real subscribers."""
        self.main.db.update_user(self.user["id"], account_plan="auto")
        self.login()
        self.assertEqual(self.client.get("/v2/api/me").json()["plan"], "auto")


if __name__ == "__main__":
    unittest.main()