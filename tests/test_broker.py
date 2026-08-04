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
from unittest import mock
from datetime import datetime, timedelta

from app import broker as _broker_mod


UID = 7          # every test acts as ONE user; the id is now part of the path


def _fresh():
    """A broker module pointed at a throwaway per-user state directory."""
    tmp = tempfile.mkdtemp()
    os.environ["BROKER_STATE_DIR"] = os.path.join(tmp, "brokers")
    os.environ["BROKER_STATE_PATH"] = os.path.join(tmp, "legacy.json")
    return importlib.reload(_broker_mod)


class DefaultsAreClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()

    def test_a_fresh_install_is_not_connected_or_armed(self) -> None:
        s = self.b.state(UID)
        self.assertFalse(s["connected"])
        self.assertFalse(s["armed"])
        self.assertTrue(s["kill_switch"])
        self.assertFalse(s["live_ready"])

    def test_it_is_blocked_for_more_than_one_reason(self) -> None:
        """Defence in depth: fixing one switch by accident must not be enough."""
        s = self.b.state(UID)
        reasons = []
        for patch in (dict(kill_switch=False),
                      dict(kill_switch=False, armed=True),
                      dict(kill_switch=False, armed=True, owner_user_id=1)):
            ok, why = self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0), UID,
                                       st={**s, **patch})
            self.assertFalse(ok)
            reasons.append(why)
        self.assertEqual(len(set(reasons)), 3, reasons)

    def test_options_are_off_and_say_why_in_rupees(self) -> None:
        s = self.b.state(UID)
        self.assertFalse(s["allow_options"])
        self.assertIn("5,704", s["options_blocked_reason"])

    def test_options_cannot_be_enabled_under_the_minimum_budget(self) -> None:
        """A switch that silently does nothing is worse than one that says no."""
        s = self.b.configure(UID, budget=10000, allow_options=True)
        self.assertFalse(s["allow_options"])

    def test_options_can_be_enabled_once_the_sleeve_can_afford_a_lot(self) -> None:
        s = self.b.configure(UID, budget=60000, allow_options=True)
        self.assertTrue(s["allow_options"])


class PerUserStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()
        self.b.configure(UID, armed=True, kill_switch=False)
        self.b.save_token(UID, "t0k")

    def _try(self, uid):
        return self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0), uid)

    def test_the_owner_may_trade(self) -> None:
        self.assertTrue(self._try(7)[0])

    def test_another_user_has_their_own_empty_broker(self) -> None:
        """THE isolation property. User 9 does not inherit user 7's connection —
        they get their own default state, which is disarmed and tokenless."""
        other = self.b.state(9)
        self.assertFalse(other["connected"])
        self.assertFalse(other["armed"])
        self.assertFalse(self._try(9)[0])

    def test_one_users_token_is_not_visible_to_another(self) -> None:
        self.b.save_token(UID, "SEVENS_TOKEN")
        self.assertNotIn("SEVENS_TOKEN", repr(self.b.state(9)))
        self.assertEqual(self.b._token(9), "")
        self.assertEqual(self.b._token(UID), "SEVENS_TOKEN")

    def test_pairing_one_id_with_anothers_state_is_refused(self) -> None:
        """The consistency check on top of the path boundary: a caller that
        hands can_trade a mismatched pair is a bug, not a trade."""
        st = self.b.state(UID)
        ok, why = self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0), 9, st=st)
        self.assertFalse(ok)
        self.assertIn("does not belong", why)

    def test_an_anonymous_caller_may_not(self) -> None:
        st = self.b.state(UID)
        self.assertFalse(self.b.can_trade(
            dict(symbol="ITC", qty=1, price=100.0), None, st=st)[0])


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
        self.b.configure(UID, armed=True, kill_switch=False)
        self.b.save_token(UID, "t0k", now=now)
        later = now + timedelta(days=1)          # past the 03:30 cut
        st = self.b.state(UID, now=later)
        self.assertTrue(st["stale"])
        ok, why = self.b.can_trade(dict(symbol="ITC", qty=1, price=100.0), UID, st=st)
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_disconnect_also_disarms(self) -> None:
        """Replacing a token must never leave an armed sleeve behind it."""
        self.b.configure(UID, armed=True, kill_switch=False)
        self.b.save_token(UID, "t0k")
        s = self.b.disconnect(UID)
        self.assertFalse(s["armed"])
        self.assertTrue(s["kill_switch"])
        self.assertFalse(s["connected"])

    def test_the_token_is_never_returned_to_the_ui(self) -> None:
        self.b.save_token(UID, "SUPERSECRET")
        self.assertNotIn("SUPERSECRET", repr(self.b.state(UID)))
        self.assertNotIn("access_token", self.b.state(UID))

    def test_the_api_key_is_shown_only_as_a_fingerprint(self) -> None:
        self.b.configure(UID, api_key="8561a2f4-a4ae-425d-9c7b-abcdefabcdef")
        hint = self.b.state(UID)["api_key_hint"]
        self.assertNotIn("a4ae-425d", hint)
        self.assertTrue(hint.startswith("8561"))

    def test_the_state_file_is_not_world_readable(self) -> None:
        self.b.save_token(UID, "t0k")
        self.assertEqual(os.stat(self.b._path(UID)).st_mode & 0o077, 0)

    def test_an_empty_token_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.b.save_token(UID, "   ")


class CapsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.b = _fresh()
        self.b.configure(UID, armed=True, kill_switch=False, budget=10000)
        self.b.save_token(UID, "t0k")

    def _try(self, qty, price, **kw):
        return self.b.can_trade(dict(symbol="ITC", qty=qty, price=price), UID, **kw)

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
        ok, why = self.b.can_trade(dict(symbol="BANKNIFTY26AUG57400CE", qty=30, price=100.0), UID)
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
        self.assertTrue(self.b.can_trade(dict(symbol="RELIANCE", qty=1, price=1300.0), UID)[0])


class ExtractCodeTest(unittest.TestCase):
    """The operator copies out of an address bar on a page showing a 404.

    Making them isolate the `code=` value by hand is a needless way to burn a
    single-use credential that expires in minutes, so both forms are accepted.
    """

    def setUp(self) -> None:
        self.b = _fresh()

    def test_a_full_redirect_url(self) -> None:
        self.assertEqual(
            self.b.extract_code("https://openstocks.in/upstox/callback?code=AbC123xYz"),
            "AbC123xYz")

    def test_a_url_with_other_params_in_any_order(self) -> None:
        self.assertEqual(
            self.b.extract_code("https://x.in/cb?state=zz&code=AbC123&client_id=k"),
            "AbC123")

    def test_a_bare_code(self) -> None:
        self.assertEqual(self.b.extract_code("AbC123xYz"), "AbC123xYz")

    def test_a_key_value_fragment(self) -> None:
        self.assertEqual(self.b.extract_code("code=AbC123xYz"), "AbC123xYz")

    def test_surrounding_whitespace_and_quotes(self) -> None:
        self.assertEqual(self.b.extract_code('  "AbC123xYz" '), "AbC123xYz")

    def test_a_url_with_no_code_yields_nothing(self) -> None:
        """Better to say 'no code' than to send the hostname to Upstox as one."""
        self.assertEqual(self.b.extract_code("https://openstocks.in/upstox/callback"), "")

    def test_empty_input(self) -> None:
        for junk in ("", "   ", None):
            with self.subTest(v=junk):
                self.assertEqual(self.b.extract_code(junk), "")

    def test_an_error_redirect_is_not_mistaken_for_a_code(self) -> None:
        self.assertEqual(
            self.b.extract_code("https://x.in/cb?error=access_denied"), "")


class NoCredentialsInRepoTest(unittest.TestCase):
    def test_the_state_file_lives_outside_git(self) -> None:
        """var/ is gitignored. A committed token is a funded account handed to
        whoever reads the public repo first."""
        import subprocess
        b = _fresh()
        default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(b.__file__))),
                               "var", "brokers", "1.json")
        out = subprocess.run(["git", "check-ignore", default],
                             capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(b.__file__))))
        self.assertEqual(out.returncode, 0, "var/broker.json must be gitignored")


if __name__ == "__main__":
    unittest.main()


class TokenVerificationTest(unittest.TestCase):
    """The clock is not evidence. Upstox is.

    A token believed good for another twelve hours while every order 401s is
    this module's worst failure: it looks connected, reports success, and places
    nothing. It happened for real — changing the app's static-IP setting revoked
    the token, and the UI kept saying "connected, not stale" while every order
    came back UDAPI100050.
    """

    def setUp(self) -> None:
        self.b = _fresh()
        self.b.save_token(UID, "t0k")

    def test_a_rejected_token_reads_stale_whatever_the_clock_says(self) -> None:
        self.assertFalse(self.b.state(UID)["stale"])       # clock still happy
        self.b._verify_cache.clear()

        class _R:
            status_code = 401
        with mock.patch("httpx.get", return_value=_R()):
            self.assertFalse(self.b.verify(UID, force=True))
        self.assertTrue(self.b.state(UID)["stale"])
        self.assertFalse(self.b.state(UID)["live_ready"])

    def test_a_new_token_clears_the_invalid_flag(self) -> None:
        s = self.b._read(UID)
        s["token_invalid"] = True
        self.b._write(UID, s)
        self.b.save_token(UID, "fresh")
        self.assertFalse(self.b.state(UID)["stale"])

    def test_an_unreachable_broker_does_not_disconnect_a_working_account(self) -> None:
        """None means "could not find out". A network blip must not log the
        operator out of a broker that is fine."""
        self.b._verify_cache.clear()
        with mock.patch("httpx.get", side_effect=OSError("network down")):
            self.assertIsNone(self.b.verify(UID, force=True))
        self.assertFalse(self.b.state(UID)["stale"])

    def test_no_token_at_all_is_not_verifiable(self) -> None:
        b = _fresh()
        self.assertFalse(b.verify(999))

    def test_the_result_is_cached(self) -> None:
        self.b._verify_cache.clear()
        with mock.patch("httpx.get", side_effect=OSError("x")) as g:
            self.b.verify(UID, force=True)
            self.b.verify(UID)
            self.assertEqual(g.call_count, 1)
