"""Which banner an account is shown, executed as real JavaScript.

The bug this exists for: the trial was checked BEFORE the subscription, so an
account that signed up and paid on day one was still told "6 days left of your
free trial". The app counted down a trial at somebody who had just handed over
money, and kept doing it for a week.

It survived because the decision was tangled up with innerHTML — there was
nothing a test could call. The choice is now a pure function, so the precedence
is pinned directly:

    pending payment > lapsed subscription > active subscription > trial

What has been PAID FOR always outranks the trial.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_WEB = REPO_ROOT / "app" / "v2_web.py"


def _function_source(source: str, name: str) -> str:
    active = source.rindex('SPA_HTML = r"""')
    start = source.index(f"function {name}(", active)
    end = source.index("\nfunction ", start)
    return source[start:end] + "\n"


def banner(me):
    """Run the shipped bannerFor() against a payload, return the decision."""
    src = V2_WEB.read_text(encoding="utf-8")
    js = _function_source(src, "bannerFor")
    script = js + "\nconsole.log(JSON.stringify(bannerFor(" + json.dumps(me) + ")));\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "t.js"
        path.write_text(script, encoding="utf-8")
        out = subprocess.run([shutil.which("node") or "node", str(path)],
                             capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout.strip())


def me(**kw):
    base = dict(plan_label="Pro", trial_tier="paper", renewal_warn_days=5,
                trial=dict(active=False, days_left=0, had_trial=False),
                subscription=dict(paid=False, active=False, expired=False, days_left=0),
                pending_request=None, tiers=[dict(key="paper", label="Pro")])
    base.update(kw)
    return base


@unittest.skipIf(shutil.which("node") is None, "node not available")
class BannerPrecedenceTest(unittest.TestCase):
    def test_a_paid_subscription_silences_the_trial_countdown(self) -> None:
        """THE BUG. Signed up today, paid today — the trial is still running,
        and telling them about it is the wrong thing to say."""
        out = banner(me(trial=dict(active=True, days_left=6, had_trial=True),
                        subscription=dict(paid=True, active=True, expired=False, days_left=30)))
        self.assertIsNone(out, "a paying subscriber must not see a trial countdown")

    def test_a_comfortable_subscription_shows_nothing_at_all(self) -> None:
        """A banner that never goes away stops being read, and then the one
        that matters is not read either."""
        self.assertIsNone(banner(me(subscription=dict(paid=True, active=True,
                                                      expired=False, days_left=22))))

    def test_it_speaks_up_near_renewal(self) -> None:
        out = banner(me(subscription=dict(paid=True, active=True, expired=False, days_left=3)))
        self.assertEqual(out["kind"], "renewal")
        self.assertIn("3 days", out["text"])
        self.assertEqual(out["cta"], "Renew now")

    def test_one_day_left_is_singular(self) -> None:
        out = banner(me(subscription=dict(paid=True, active=True, expired=False, days_left=1)))
        self.assertIn("1 day.", out["text"])
        self.assertNotIn("1 days", out["text"])

    def test_a_lapsed_subscription_beats_a_still_running_trial(self) -> None:
        out = banner(me(trial=dict(active=True, days_left=2, had_trial=True),
                        subscription=dict(paid=True, active=False, expired=True, days_left=0)))
        self.assertEqual(out["kind"], "lapsed")

    def test_a_pending_payment_outranks_everything(self) -> None:
        """They have already acted. Anything else is noise until it clears."""
        out = banner(me(pending_request=dict(id=1, plan="auto", amount=999),
                        trial=dict(active=True, days_left=4, had_trial=True),
                        subscription=dict(paid=True, active=True, expired=False, days_left=2)))
        self.assertEqual(out["kind"], "pending")

    def test_a_trial_user_who_has_not_paid_gets_the_countdown(self) -> None:
        out = banner(me(trial=dict(active=True, days_left=7, had_trial=True)))
        self.assertEqual(out["kind"], "trial")
        self.assertIn("7 days", out["text"])
        self.assertIn("Pro", out["text"])

    def test_a_finished_trial_prompts_an_upgrade(self) -> None:
        out = banner(me(plan_label="Starter",
                        trial=dict(active=False, days_left=0, had_trial=True)))
        self.assertEqual(out["kind"], "trial_over")
        self.assertIn("Starter", out["text"])

    def test_an_account_that_never_had_a_trial_sees_nothing(self) -> None:
        """The pre-existing accounts. A banner about a trial they never had
        would be a lie."""
        self.assertIsNone(banner(me()))

    def test_a_missing_payload_does_not_throw(self) -> None:
        self.assertIsNone(banner(None))

    def test_no_field_leaks_undefined_or_an_internal_key(self) -> None:
        for payload in (me(trial=dict(active=True, days_left=3, had_trial=True)),
                        me(subscription=dict(paid=True, active=True, expired=False, days_left=2)),
                        me(subscription=dict(paid=True, active=False, expired=True, days_left=0))):
            out = banner(payload)
            with self.subTest(kind=out["kind"]):
                self.assertNotIn("undefined", out["text"])
                self.assertNotIn("paper", out["text"], "tier keys must not reach the page")


if __name__ == "__main__":
    unittest.main()
