"""User investment profile: risk tolerance and investment style.

Two decisions are load-bearing and both are tested:

- The vocabularies are closed. Free text would make the values unusable by any
  consumer, which is the whole point of storing them.
- The WRITE path rejects an unknown value, while the READ path coerces. Those
  look inconsistent but are not: rejecting on write tells the user their input
  was wrong, whereas a row written before these columns existed must still read
  as a usable default rather than None.
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest

from app.db import (INVESTMENT_STYLES, RISK_TOLERANCES, Database,
                    normalize_investment_style, normalize_risk_tolerance)


class VocabularyTest(unittest.TestCase):
    def test_risk_scale_is_ordered_and_closed(self) -> None:
        self.assertEqual(RISK_TOLERANCES, ("conservative", "balanced", "aggressive"))

    def test_styles_include_a_neutral_default(self) -> None:
        self.assertIn("balanced", INVESTMENT_STYLES)
        for style in ("value", "growth", "momentum", "income"):
            self.assertIn(style, INVESTMENT_STYLES)

    def test_default_is_a_member_of_each_vocabulary(self) -> None:
        self.assertIn(normalize_risk_tolerance(None), RISK_TOLERANCES)
        self.assertIn(normalize_investment_style(None), INVESTMENT_STYLES)


class NormalisationTest(unittest.TestCase):
    def test_case_and_whitespace(self) -> None:
        self.assertEqual(normalize_risk_tolerance("  AGGRESSIVE "), "aggressive")
        self.assertEqual(normalize_investment_style("Growth"), "growth")

    def test_unknown_falls_back_to_balanced(self) -> None:
        for value in ("reckless", "", None, 5, object()):
            with self.subTest(value=value):
                self.assertEqual(normalize_risk_tolerance(value), "balanced")
                self.assertEqual(normalize_investment_style(value), "balanced")

    def test_every_valid_value_round_trips(self) -> None:
        for value in RISK_TOLERANCES:
            self.assertEqual(normalize_risk_tolerance(value), value)
        for value in INVESTMENT_STYLES:
            self.assertEqual(normalize_investment_style(value), value)


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "agent.db"
        self.db = Database(self.path)
        self.db.init()
        self.db.create_user("tester", "pbkdf2_sha256$1$abc$def")
        self.user_id = int(self.db.user_by_username("tester")["id"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_user_defaults_to_balanced(self) -> None:
        profile = self.db.user_profile(self.user_id)
        self.assertEqual(profile["risk_tolerance"], "balanced")
        self.assertEqual(profile["investment_style"], "balanced")

    def test_update_persists(self) -> None:
        self.db.update_user_profile(self.user_id, risk_tolerance="aggressive",
                                    investment_style="momentum")
        profile = self.db.user_profile(self.user_id)
        self.assertEqual(profile["risk_tolerance"], "aggressive")
        self.assertEqual(profile["investment_style"], "momentum")

    def test_partial_update_leaves_the_other_field(self) -> None:
        self.db.update_user_profile(self.user_id, risk_tolerance="conservative",
                                    investment_style="value")
        self.db.update_user_profile(self.user_id, risk_tolerance="aggressive")
        profile = self.db.user_profile(self.user_id)
        self.assertEqual(profile["risk_tolerance"], "aggressive")
        self.assertEqual(profile["investment_style"], "value")

    def test_no_op_update_returns_the_user(self) -> None:
        self.assertIsNotNone(self.db.update_user_profile(self.user_id))

    def test_unknown_value_is_stored_as_the_default(self) -> None:
        """The DB layer coerces; the HTTP layer is what rejects. This stops a
        bad internal caller writing an unreadable value."""
        self.db.update_user_profile(self.user_id, risk_tolerance="reckless")
        self.assertEqual(self.db.user_profile(self.user_id)["risk_tolerance"], "balanced")

    def test_schema_forbids_a_null_profile(self) -> None:
        """Stronger than coercion: the columns are NOT NULL DEFAULT 'balanced',
        so a row created before they existed was backfilled, and nothing can
        write NULL afterwards. The reader's fallback is belt-and-braces."""
        with self.assertRaises(sqlite3.IntegrityError):
            with sqlite3.connect(str(self.path)) as con:
                con.execute("UPDATE users SET risk_tolerance=NULL WHERE id=?",
                            (self.user_id,))
        self.assertEqual(self.db.user_profile(self.user_id)["risk_tolerance"], "balanced")

    def test_reader_coerces_an_unexpected_stored_value(self) -> None:
        """Defence in depth for a value written by some other path."""
        with sqlite3.connect(str(self.path)) as con:
            con.execute("UPDATE users SET risk_tolerance='reckless' WHERE id=?",
                        (self.user_id,))
        self.assertEqual(self.db.user_profile(self.user_id)["risk_tolerance"], "balanced")

    def test_unknown_user_still_returns_defaults(self) -> None:
        profile = self.db.user_profile(999999)
        self.assertEqual(profile["risk_tolerance"], "balanced")

    def test_columns_exist_after_schema_setup(self) -> None:
        with sqlite3.connect(str(self.path)) as con:
            columns = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        self.assertIn("risk_tolerance", columns)
        self.assertIn("investment_style", columns)

    def test_migration_is_idempotent(self) -> None:
        """Re-running schema setup on an existing DB must not raise."""
        Database(self.path).init()
        self.assertEqual(self.db.user_profile(self.user_id)["risk_tolerance"], "balanced")

    def test_profiles_are_per_user(self) -> None:
        self.db.create_user("other", "pbkdf2_sha256$1$abc$def")
        other_id = int(self.db.user_by_username("other")["id"])
        self.db.update_user_profile(self.user_id, risk_tolerance="aggressive")
        self.assertEqual(self.db.user_profile(other_id)["risk_tolerance"], "balanced")


if __name__ == "__main__":
    unittest.main()
