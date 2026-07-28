"""Shareholding reader and the fundamental analyst it feeds.

The table does not exist on the live box yet — the ingester is undeployed — so
the first requirement is that everything degrades to "unknown" rather than
raising or, worse, reading a missing stake as zero.

Stances are hand-computed from the documented scale: change/5.0, capped at
±0.6, so a +2.5pp quarter maps to +0.5.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import analysts
from app import v2_web


class ReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE shareholding(symbol TEXT, as_of TEXT, promoter_pct REAL, "
                    "public_pct REAL, employee_trust_pct REAL, company TEXT, "
                    "submitted_at TEXT, ingested_at TEXT, PRIMARY KEY(symbol, as_of))")
        con.commit()
        con.close()
        self._db = v2_web.MAIN_DB
        v2_web.MAIN_DB = self.path

    def tearDown(self) -> None:
        v2_web.MAIN_DB = self._db
        os.unlink(self.path)

    def _add(self, symbol, as_of, promoter, public=None):
        con = sqlite3.connect(self.path)
        con.execute("INSERT OR REPLACE INTO shareholding(symbol,as_of,promoter_pct,public_pct) "
                    "VALUES(?,?,?,?)", (symbol, as_of, promoter, public))
        con.commit()
        con.close()

    def test_missing_table_returns_none(self) -> None:
        """The live state today. None means unknown, never zero."""
        v2_web.MAIN_DB = "/nonexistent/path.db"
        self.assertIsNone(v2_web.shareholding_trend("ABC"))

    def test_unknown_symbol_returns_none(self) -> None:
        self.assertIsNone(v2_web.shareholding_trend("NOPE"))

    def test_history_is_oldest_first(self) -> None:
        self._add("ABC", "2025-12-31", 72.0)
        self._add("ABC", "2026-03-31", 70.0)
        self._add("ABC", "2026-06-30", 68.44)
        trend = v2_web.shareholding_trend("ABC")
        self.assertEqual([h["as_of"] for h in trend["history"]],
                         ["2025-12-31", "2026-03-31", "2026-06-30"])
        self.assertEqual(trend["latest"]["promoter_pct"], 68.44)

    def test_change_is_newest_minus_oldest(self) -> None:
        self._add("ABC", "2025-12-31", 72.0)
        self._add("ABC", "2026-06-30", 68.0)
        self.assertEqual(v2_web.shareholding_trend("ABC")["promoter_change_pp"], -4.0)

    def test_single_quarter_has_no_change(self) -> None:
        self._add("ABC", "2026-06-30", 68.44)
        trend = v2_web.shareholding_trend("ABC")
        self.assertIsNone(trend["promoter_change_pp"])
        self.assertEqual(trend["quarters"], 1)

    def test_quarter_limit_is_respected(self) -> None:
        for i, date in enumerate(("2025-03-31", "2025-06-30", "2025-09-30",
                                  "2025-12-31", "2026-03-31", "2026-06-30")):
            self._add("ABC", date, 70.0 - i)
        self.assertEqual(v2_web.shareholding_trend("ABC", quarters=3)["quarters"], 3)

    def test_rows_without_a_promoter_figure_are_skipped(self) -> None:
        self._add("ABC", "2026-06-30", None, 100.0)
        self.assertIsNone(v2_web.shareholding_trend("ABC"))

    def test_symbol_is_upper_cased(self) -> None:
        self._add("ABC", "2026-06-30", 68.44)
        self.assertIsNotNone(v2_web.shareholding_trend("abc"))


def _trend(promoter, change=None, quarters=1, as_of="2026-06-30"):
    return {"latest": {"as_of": as_of, "promoter_pct": promoter},
            "history": [], "quarters": quarters, "promoter_change_pp": change}


class FundamentalAnalystTest(unittest.TestCase):
    def test_abstains_without_data(self) -> None:
        """The live state until the ingester ships."""
        opinion = analysts.fundamental_analyst({})
        self.assertTrue(opinion["abstained"])
        self.assertIn("No shareholding", opinion["rationale"])

    def test_rising_promoter_stake_is_bullish(self) -> None:
        opinion = analysts.fundamental_analyst({"shareholding": _trend(70.0, 2.5, 4)})
        self.assertAlmostEqual(opinion["stance"], 0.5)      # 2.5 / 5.0
        self.assertIn("up", opinion["rationale"])

    def test_falling_promoter_stake_is_bearish(self) -> None:
        """-4.0pp / 5.0 = -0.8, which the ±0.6 cap clips to -0.6."""
        opinion = analysts.fundamental_analyst({"shareholding": _trend(68.0, -4.0, 3)})
        self.assertAlmostEqual(opinion["stance"], -0.6)
        self.assertIn("down", opinion["rationale"])

    def test_uncapped_range_maps_linearly(self) -> None:
        """Inside the cap the scale is change/5.0: -1.5pp -> -0.3."""
        opinion = analysts.fundamental_analyst({"shareholding": _trend(68.0, -1.5, 3)})
        self.assertAlmostEqual(opinion["stance"], -0.3)

    def test_stance_is_capped_both_ways(self) -> None:
        """A 20pp swing is usually a corporate action, not a signal — the cap
        stops it dominating the panel."""
        up = analysts.fundamental_analyst({"shareholding": _trend(90.0, 20.0, 4)})
        down = analysts.fundamental_analyst({"shareholding": _trend(30.0, -20.0, 4)})
        self.assertEqual(up["stance"], 0.6)
        self.assertEqual(down["stance"], -0.6)

    def test_steady_stake_is_neutral(self) -> None:
        opinion = analysts.fundamental_analyst({"shareholding": _trend(68.0, 0.1, 4)})
        self.assertIn("steady", opinion["rationale"])
        self.assertLess(abs(opinion["stance"]), 0.05)

    def test_single_quarter_gives_level_only(self) -> None:
        """One quarter tells you the level, nothing about direction."""
        opinion = analysts.fundamental_analyst({"shareholding": _trend(68.0)})
        self.assertIn("no trend yet", opinion["rationale"])
        self.assertLessEqual(opinion["confidence"], 0.3)

    def test_more_quarters_means_more_confidence(self) -> None:
        few = analysts.fundamental_analyst({"shareholding": _trend(70.0, 1.0, 2)})
        many = analysts.fundamental_analyst({"shareholding": _trend(70.0, 1.0, 4)})
        self.assertGreater(many["confidence"], few["confidence"])

    def test_confidence_stays_modest(self) -> None:
        """Ownership is one input, not a thesis."""
        opinion = analysts.fundamental_analyst({"shareholding": _trend(70.0, 3.0, 8)})
        self.assertLessEqual(opinion["confidence"], 0.75)

    def test_evidence_cites_the_quarter(self) -> None:
        opinion = analysts.fundamental_analyst({"shareholding": _trend(68.44, -1.5, 3)})
        self.assertIn("2026-06-30", opinion["evidence"][0]["source"])
        self.assertEqual(opinion["evidence"][0]["value"], 68.44)

    def test_garbage_input_abstains(self) -> None:
        for value in ("nonsense", 5, [], {"latest": "bad"}):
            with self.subTest(value=value):
                self.assertTrue(
                    analysts.fundamental_analyst({"shareholding": value})["abstained"])


class PanelIntegrationTest(unittest.TestCase):
    def test_fundamental_joins_the_panel(self) -> None:
        names = [a.__name__ for a in analysts.ANALYSTS]
        self.assertIn("fundamental_analyst", names)
        self.assertEqual(len(names), len(set(names)), "an analyst is registered twice")

    def test_panel_abstains_cleanly_with_no_shareholding(self) -> None:
        result = analysts.analyse({"price": 100.0})
        fundamental = next(o for o in result["opinions"] if o["agent"] == "fundamental")
        self.assertTrue(fundamental["abstained"])
        self.assertIn("fundamental", result["cio"]["abstained"])

    def test_fundamental_can_dissent_against_the_chart(self) -> None:
        facts = {
            "price": 105.0, "close": 105.0, "sma20": 100.0, "sma50": 95.0,
            "regime_on": True, "atr_pct": 0.02, "rvol": 1.4,
            "technicals": {"supertrend": {"direction": "up", "value": 98.0},
                           "ichimoku": {"kijun": 100.0}, "stale": False},
            "shareholding": _trend(45.0, -6.0, 4),      # promoters selling hard
        }
        cio = analysts.analyse(facts)["cio"]
        self.assertTrue(cio["dissent"])
        self.assertTrue(any("fundamental" in d for d in cio["dissent"]))


if __name__ == "__main__":
    unittest.main()
