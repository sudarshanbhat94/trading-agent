"""Live broker locks. Real money, so every refusal is tested explicitly.

The default posture is the one being asserted hardest: a fresh install must be
unable to place an order for several independent reasons at once, so that no
single mistake — a stray click, a copied config, a forgotten switch — is enough
to start spending.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app import broker as _broker_mod


def _fresh():
    """A broker module pointed at a throwaway state file."""
    tmp = tempfile.mkdtemp()
    os.environ["BROKER_STATE_PATH"] = os.path.join(tmp, "broker.json")
    return importlib.reload(_broker_mod)


class DefaultsAreClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()

    def test_a_fresh_install_is_not_connected_or_armed(self) -> None:
        s = self.b.state()
        self.assertFalse(s["connected"])
        self.assertFalse(s["armed"])
        self.assertTrue(s["kill_switch"])
        self.assertFalse(s["live_ready"])

    def test_it_is_blocked_for_more_than_one_reason(self) -> None:
        """Defence in depth: fixing one switch by accident must not be enough."""
        s = self.b.state()
        reasons = []
        for patch in (dict(kill_switch=False),
                      dict(kill_switch=False, armed=True),
                      dict(kill_switch=False, armed=True, owner_user_id=1)):
            ok, why = self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0),
                                       st={**s, **patch}, user_id=1)
            self.assertFalse(ok)
            reasons.append(why)
        self.assertEqual(len(set(reasons)), 3, reasons)

    def test_options_are_off_and_say_why_in_rupees(self) -> None:
        s = self.b.state()
        self.assertFalse(s["allow_options"])
        self.assertIn("5,704", s["options_blocked_reason"])

    def test_options_cannot_be_enabled_under_the_minimum_budget(self) -> None:
        """A switch that silently does nothing is worse than one that says no."""
        s = self.b.configure(budget=10000, allow_options=True)
        self.assertFalse(s["allow_options"])

    def test_options_can_be_enabled_once_the_sleeve_can_afford_a_lot(self) -> None:
        s = self.b.configure(budget=60000, allow_options=True)
        self.assertTrue(s["allow_options"])


class OwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()
        self.b.configure(owner_user_id=7, armed=True, kill_switch=False)
        self.b.save_token("t0k")

    def _try(self, uid):
        return self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0), user_id=uid)

    def test_the_owner_may_trade(self) -> None:
        self.assertTrue(self._try(7)[0])

    def test_another_admin_may_not(self) -> None:
        """Ownership is a numeric id, not the admin role: a role is a set that
        grows, and promoting a second admin must not hand them real money."""
        ok, why = self._try(9)
        self.assertFalse(ok)
        self.assertIn("owner", why)

    def test_an_anonymous_caller_may_not(self) -> None:
        self.assertFalse(self._try(None)[0])


class TokenLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()

    def test_expiry_is_the_next_0330_ist_not_a_duration(self) -> None:
        """Upstox kills tokens at a wall-clock time. A token issued at 23:00 is
        good for four hours, not twenty-four."""
        late = datetime(2026, 8, 4, 23, 0, tzinfo=self.b.IST)
        self.assertEqual(self.b.token_expiry(now=late),
                         datetime(2026, 8, 5, 3, 30, tzinfo=self.b.IST))
        early = datetime(2026, 8, 4, 1, 0, tzinfo=self.b.IST)
        self.assertEqual(self.b.token_expiry(now=early),
                         datetime(2026, 8, 4, 3, 30, tzinfo=self.b.IST))

    def test_an_expired_token_reads_stale_and_blocks_trading(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=self.b.IST)
        self.b.configure(owner_user_id=1, armed=True, kill_switch=False)
        self.b.save_token("t0k", now=now)
        later = now + timedelta(days=1)          # past the 03:30 cut
        st = self.b.state(now=later)
        self.assertTrue(st["stale"])
        ok, why = self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0),
                                   st=st, user_id=1)
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_disconnect_also_disarms(self) -> None:
        """Replacing a token must never leave an armed sleeve behind it."""
        self.b.configure(owner_user_id=1, armed=True, kill_switch=False)
        self.b.save_token("t0k")
        s = self.b.disconnect()
        self.assertFalse(s["armed"])
        self.assertTrue(s["kill_switch"])
        self.assertFalse(s["connected"])

    def test_the_token_is_never_returned_to_the_ui(self) -> None:
        self.b.save_token("SUPERSECRET")
        self.assertNotIn("SUPERSECRET", repr(self.b.state()))
        self.assertNotIn("access_token", self.b.state())

    def test_the_api_key_is_shown_only_as_a_fingerprint(self) -> None:
        self.b.configure(api_key="8561a2f4-a4ae-425d-9c7b-abcdefabcdef")
        hint = self.b.state()["api_key_hint"]
        self.assertNotIn("a4ae-425d", hint)
        self.assertTrue(hint.startswith("8561"))

    def test_the_state_file_is_not_world_readable(self) -> None:
        self.b.save_token("t0k")
        self.assertEqual(os.stat(self.b.STATE_PATH).st_mode & 0o077, 0)

    def test_an_empty_token_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.b.save_token("   ")


class CapsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()
        self.b.configure(owner_user_id=1, armed=True, kill_switch=False, budget=10000)
        self.b.save_token("t0k")

    def _try(self, qty, price, **kw):
        return self.b.can_trade(dict(symbol="ITC", qty=qty, price=price), user_id=1, **kw)

    def test_a_normal_order_passes(self) -> None:
        self.assertTrue(self._try(10, 300.0)[0])

    def test_an_order_over_the_per_order_cap_is_refused(self) -> None:
        ok, why = self._try(10, 400.0)          # Rs 4,000 > 35% of Rs 10,000
        self.assertFalse(ok)
        self.assertIn("per-order cap", why)

    def test_the_daily_notional_cap_is_enforced(self) -> None:
        ok, why = self._try(10, 300.0, day_notional=9000.0)
        self.assertFalse(ok)
        self.assertIn("daily notional", why)

    def test_too_many_open_positions_is_refused(self) -> None:
        ok, why = self._try(1, 100.0, open_positions=3)
        self.assertFalse(ok)
        self.assertIn("max 3", why)

    def test_a_closed_market_is_refused(self) -> None:
        ok, why = self._try(1, 100.0, market_is_open=False)
        self.assertFalse(ok)
        self.assertIn("closed", why)

    def test_a_zero_value_order_is_refused(self) -> None:
        self.assertFalse(self._try(0, 100.0)[0])

    def test_an_option_is_refused_while_options_are_off(self) -> None:
        ok, why = self.b.can_trade(dict(symbol="BANKNIFTY26AUG57400CE", qty=30, price=100.0),
                                   user_id=1)
        self.assertFalse(ok)
        self.assertIn("lot costs", why)

    def test_option_detection_covers_both_sides(self) -> None:
        self.assertTrue(self.b.is_option("NIFTY2680424300CE"))
        self.assertTrue(self.b.is_option("NIFTY2680424300PE"))
        self.assertTrue(self.b.is_option("BANKNIFTY26AUG57400CE"))

    def test_an_equity_ending_in_CE_is_not_an_option(self) -> None:
        """RELIANCE ends in "CE". A suffix test blocks the most traded stock on
        the exchange while options are off — and would size it as a lot if the
        switch were ever inverted."""
        for equity in ("RELIANCE", "JUSTDIAL", "ONGC", "NHPC"):
            with self.subTest(symbol=equity):
                self.assertFalse(self.b.is_option(equity))

    def test_an_equity_order_is_allowed_while_options_are_off(self) -> None:
        self.assertTrue(self.b.can_trade(dict(symbol="RELIANCE", qty=1, price=1300.0),
                                         user_id=1)[0])


class NoCredentialsInRepoTest(unittest.TestCase):
    def test_the_state_file_lives_outside_git(self) -> None:
        """var/ is gitignored. A committed token is a funded account handed to
        whoever reads the public repo first."""
        import subprocess
        b = _fresh()
        default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(b.__file__))),
                               "var", "broker.json")
        out = subprocess.run(["git", "check-ignore", default],
                             capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(b.__file__))))
        self.assertEqual(out.returncode, 0, "var/broker.json must be gitignored")


if __name__ == "__main__":
    unittest.main()
