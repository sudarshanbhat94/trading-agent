"""The macro analyst and the calendar flags that feed it.

Distinct from the risk analyst: risk reads the current tape, macro reads the
diary — events known in advance that change a session's character regardless of
what price is doing.

Calendar expectations are derived from the actual 2026 calendar, not from the
implementation. July 2026: the 30th is a Thursday and July's last, so it is
both weekly and monthly expiry.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import analysts
from app.v2_web import _macro_flags


class MacroFlagTest(unittest.TestCase):
    def test_monthly_expiry_is_the_last_thursday(self) -> None:
        self.assertEqual(date(2026, 7, 30).strftime("%a"), "Thu")
        self.assertEqual(date(2026, 7, 31).strftime("%a"), "Fri")   # so the 30th is the last
        flags = _macro_flags(date(2026, 7, 30))
        self.assertTrue(flags["is_expiry_day"])
        self.assertTrue(flags["is_monthly_expiry_day"])

    def test_tuesday_of_expiry_week(self) -> None:
        flags = _macro_flags(date(2026, 7, 28))
        self.assertFalse(flags["is_expiry_day"])
        self.assertTrue(flags["is_expiry_week"])

    def test_friday_after_expiry_is_not_expiry_week(self) -> None:
        """The next Thursday is six days out, past the four-day window."""
        self.assertFalse(_macro_flags(date(2026, 7, 31))["is_expiry_week"])

    def test_budget_and_rbi_weeks(self) -> None:
        flags = _macro_flags(date(2026, 2, 3))
        self.assertTrue(flags["is_budget_week"])
        self.assertTrue(flags["is_rbi_week"])

    def test_ordinary_month_is_not_an_rbi_week(self) -> None:
        self.assertFalse(_macro_flags(date(2026, 7, 3))["is_rbi_week"])

    def test_returns_a_dict_for_any_date(self) -> None:
        for day in (date(2026, 1, 1), date(2026, 12, 31), date(2026, 2, 28)):
            self.assertIsInstance(_macro_flags(day), dict)


class MacroAnalystTest(unittest.TestCase):
    def test_abstains_without_context(self) -> None:
        opinion = analysts.macro_analyst({})
        self.assertTrue(opinion["abstained"])
        self.assertEqual(opinion["confidence"], 0.0)

    def test_clear_calendar_is_neutral_but_participating(self) -> None:
        """A clear diary is a real finding, not an abstention."""
        opinion = analysts.macro_analyst({"macro": {"is_expiry_day": False}})
        self.assertFalse(opinion["abstained"])
        self.assertEqual(opinion["stance"], 0.0)
        self.assertIn("No scheduled event risk", opinion["rationale"])

    def test_expiry_day_is_a_headwind(self) -> None:
        opinion = analysts.macro_analyst({"macro": {"is_expiry_day": True}})
        self.assertLess(opinion["stance"], 0.0)
        self.assertIn("expiry", opinion["rationale"])

    def test_expiry_day_outweighs_expiry_week(self) -> None:
        day = analysts.macro_analyst({"macro": {"is_expiry_day": True, "is_expiry_week": True}})
        week = analysts.macro_analyst({"macro": {"is_expiry_week": True}})
        self.assertLess(day["stance"], week["stance"])

    def test_earnings_inside_the_block_window(self) -> None:
        """Mirrors EARNINGS_BLOCK_DAYS = 3 in v2_live, which already refuses
        new entries this close to a result."""
        self.assertEqual(analysts.__dict__.get("_num")(3), 3.0)
        inside = analysts.macro_analyst({"macro": {"earnings_days_away": 2}})
        outside = analysts.macro_analyst({"macro": {"earnings_days_away": 9}})
        self.assertLess(inside["stance"], 0.0)
        self.assertEqual(outside["stance"], 0.0)
        self.assertIn("earnings", inside["rationale"])

    def test_never_argues_to_buy(self) -> None:
        """Like risk, macro is non-positive: a clear calendar is the absence of
        a headwind, not a reason to buy."""
        for macro in ({"is_expiry_day": True}, {"is_rbi_week": True},
                      {"is_budget_week": True}, {"earnings_days_away": 1},
                      {"is_expiry_day": False}):
            with self.subTest(macro=macro):
                self.assertLessEqual(analysts.macro_analyst({"macro": macro})["stance"], 0.0)

    def test_multiple_events_compound(self) -> None:
        single = analysts.macro_analyst({"macro": {"is_rbi_week": True}})
        stacked = analysts.macro_analyst(
            {"macro": {"is_rbi_week": True, "is_budget_week": True, "is_expiry_day": True}})
        self.assertLess(stacked["stance"], single["stance"])
        self.assertGreaterEqual(stacked["stance"], -1.0)

    def test_evidence_cites_the_calendar(self) -> None:
        opinion = analysts.macro_analyst({"macro": {"is_expiry_day": True}})
        self.assertTrue(opinion["evidence"])
        self.assertEqual(opinion["evidence"][0]["source"], "macro_calendar")

    def test_garbage_context_does_not_raise(self) -> None:
        self.assertTrue(analysts.macro_analyst({"macro": "nonsense"})["abstained"])
        self.assertIsInstance(
            analysts.macro_analyst({"macro": {"earnings_days_away": "abc"}}), dict)


class PanelIntegrationTest(unittest.TestCase):
    def test_macro_joins_the_panel(self) -> None:
        names = [a.__name__ for a in analysts.ANALYSTS]
        self.assertIn("macro_analyst", names)
        self.assertEqual(len(analysts.ANALYSTS), 5)

    def test_macro_appears_in_analyse_output(self) -> None:
        result = analysts.analyse({"macro": {"is_expiry_day": True}})
        agents = [o["agent"] for o in result["opinions"]]
        self.assertIn("macro", agents)

    def test_macro_can_create_dissent_against_a_bullish_chart(self) -> None:
        facts = {
            "price": 105.0, "close": 105.0, "sma20": 100.0, "sma50": 95.0,
            "regime_on": True, "atr_pct": 0.02, "rvol": 1.4,
            "technicals": {"supertrend": {"direction": "up", "value": 98.0},
                           "ichimoku": {"kijun": 100.0}, "stale": False},
            "macro": {"is_expiry_day": True, "is_rbi_week": True, "is_budget_week": True},
        }
        cio = analysts.analyse(facts)["cio"]
        self.assertTrue(cio["dissent"])
        self.assertTrue(any("macro" in d for d in cio["dissent"]))


if __name__ == "__main__":
    unittest.main()
