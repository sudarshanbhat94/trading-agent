"""Portfolio analytics.

Every expected number here is hand-computed from the definition of the metric,
not read back from the implementation. Drawdown in particular is easy to write
in a way that quietly measures the wrong thing — largest single drop rather
than peak-to-trough — so the fixtures include a curve that distinguishes them.
"""

from __future__ import annotations

import json
import unittest

from app import portfolio


class AllocationTest(unittest.TestCase):
    def test_values_and_weights(self) -> None:
        # 10 x 100 = 1000 and 5 x 200 = 1000 against equity 10000 -> 10% each
        allocs = portfolio.allocations(
            [("AAA", "swing_meanrev", 10, 90.0, 100.0), ("BBB", "btst", 5, 210.0, 200.0)],
            10_000.0,
        )
        self.assertEqual(len(allocs), 2)
        self.assertEqual(allocs[0]["value"], 1000.0)
        self.assertEqual(allocs[0]["pct_of_equity"], 10.0)

    def test_unrealised_uses_entry_versus_mark(self) -> None:
        allocs = portfolio.allocations([("AAA", "swing_meanrev", 10, 90.0, 100.0)], 10_000.0)
        self.assertAlmostEqual(allocs[0]["unrealised_pct"], 11.11, places=2)  # 100/90 - 1

    def test_missing_live_price_marks_at_entry(self) -> None:
        allocs = portfolio.allocations([("AAA", "swing_meanrev", 10, 90.0, None)], 10_000.0)
        self.assertEqual(allocs[0]["value"], 900.0)
        self.assertEqual(allocs[0]["unrealised_pct"], 0.0)

    def test_sorted_largest_first(self) -> None:
        allocs = portfolio.allocations(
            [("SMALL", "x", 1, 10.0, 10.0), ("BIG", "x", 100, 10.0, 10.0)], 10_000.0
        )
        self.assertEqual([a["symbol"] for a in allocs], ["BIG", "SMALL"])

    def test_malformed_rows_are_skipped(self) -> None:
        allocs = portfolio.allocations(
            [("AAA", "x", 10, 100.0, 100.0), ("bad",), None, ("ZZZ", "x", 0, 0.0, 0.0)],
            10_000.0,
        )
        self.assertEqual(len(allocs), 1)


class ConcentrationTest(unittest.TestCase):
    def test_hand_computed_hhi(self) -> None:
        """Four equal 10% positions: HHI = 4 x 0.10^2 = 0.04."""
        allocs = [{"value": 1000.0} for _ in range(4)]
        result = portfolio.concentration(allocs, 10_000.0)
        self.assertEqual(result["hhi"], 0.04)
        self.assertEqual(result["largest_pct"], 10.0)
        self.assertEqual(result["top3_pct"], 30.0)
        self.assertEqual(result["deployed_pct"], 40.0)

    def test_single_position_book(self) -> None:
        """Everything in one name at full equity: HHI = 1."""
        result = portfolio.concentration([{"value": 10_000.0}], 10_000.0)
        self.assertEqual(result["hhi"], 1.0)
        self.assertEqual(result["largest_pct"], 100.0)

    def test_idle_cash_counts_as_diversification(self) -> None:
        """Two 10% positions with 80% cash must score far lower than two 50%
        positions — weights are of equity, not of the deployed slice."""
        light = portfolio.concentration([{"value": 1000.0}, {"value": 1000.0}], 10_000.0)
        heavy = portfolio.concentration([{"value": 5000.0}, {"value": 5000.0}], 10_000.0)
        self.assertEqual(light["hhi"], 0.02)     # 2 x 0.1^2
        self.assertEqual(heavy["hhi"], 0.50)     # 2 x 0.5^2
        self.assertLess(light["hhi"], heavy["hhi"])

    def test_empty_book(self) -> None:
        result = portfolio.concentration([], 10_000.0)
        self.assertEqual(result["n_positions"], 0)
        self.assertEqual(result["hhi"], 0.0)

    def test_zero_equity_does_not_divide_by_zero(self) -> None:
        self.assertEqual(portfolio.concentration([{"value": 100.0}], 0)["hhi"], 0.0)


class DrawdownTest(unittest.TestCase):
    def test_peak_to_trough_not_largest_single_drop(self) -> None:
        """100 -> 120 -> 110 -> 90 -> 130.
        Largest single step down is 110->90 (-18.2%), but the true drawdown is
        peak 120 to trough 90 = -25%."""
        curve = [("d1", 100.0), ("d2", 120.0), ("d3", 110.0), ("d4", 90.0), ("d5", 130.0)]
        result = portfolio.drawdown(curve)
        self.assertEqual(result["max_drawdown_pct"], -25.0)
        self.assertEqual(result["peak"], 120.0)
        self.assertEqual(result["trough"], 90.0)
        self.assertEqual(result["peak_date"], "d2")
        self.assertEqual(result["trough_date"], "d4")

    def test_current_drawdown_measures_from_the_high_water_mark(self) -> None:
        # High water 130, last 117 -> -10%
        curve = [("d1", 100.0), ("d2", 130.0), ("d3", 117.0)]
        self.assertEqual(portfolio.drawdown(curve)["current_drawdown_pct"], -10.0)

    def test_at_a_new_high_current_drawdown_is_zero(self) -> None:
        curve = [("d1", 100.0), ("d2", 90.0), ("d3", 150.0)]
        result = portfolio.drawdown(curve)
        self.assertEqual(result["current_drawdown_pct"], 0.0)
        self.assertEqual(result["max_drawdown_pct"], -10.0)   # the earlier dip still counts

    def test_monotonic_rise_has_no_drawdown(self) -> None:
        curve = [("d1", 100.0), ("d2", 110.0), ("d3", 120.0)]
        self.assertEqual(portfolio.drawdown(curve)["max_drawdown_pct"], 0.0)

    def test_empty_and_malformed_curves(self) -> None:
        for curve in ([], None, [("d1", None), ("bad",), ("d2", "abc")]):
            with self.subTest(curve=curve):
                self.assertEqual(portfolio.drawdown(curve)["max_drawdown_pct"], 0.0)


class LaneCurveTest(unittest.TestCase):
    def test_cumulative_pnl_in_exit_order(self) -> None:
        trades = [("btst", "2026-07-28", 100.0), ("btst", "2026-07-29", -30.0),
                  ("btst", "2026-07-30", 50.0)]
        curve = portfolio.lane_curves(trades)["btst"]
        self.assertEqual([p["cum_pnl"] for p in curve], [100.0, 70.0, 120.0])

    def test_out_of_order_rows_are_sorted_by_exit_date(self) -> None:
        trades = [("btst", "2026-07-30", 50.0), ("btst", "2026-07-28", 100.0)]
        curve = portfolio.lane_curves(trades)["btst"]
        self.assertEqual([p["date"] for p in curve], ["2026-07-28", "2026-07-30"])
        self.assertEqual([p["cum_pnl"] for p in curve], [100.0, 150.0])

    def test_lanes_are_kept_separate(self) -> None:
        trades = [("btst", "2026-07-28", 100.0), ("swing_meanrev", "2026-07-28", -20.0)]
        curves = portfolio.lane_curves(trades)
        self.assertEqual(set(curves), {"btst", "swing_meanrev"})
        self.assertEqual(curves["swing_meanrev"][0]["cum_pnl"], -20.0)

    def test_empty_trades(self) -> None:
        self.assertEqual(portfolio.lane_curves([]), {})


class BuildTest(unittest.TestCase):
    def _payload(self):
        return portfolio.build(
            positions=[("AAA", "swing_meanrev", 10, 90.0, 100.0),
                       ("BBB", "btst", 5, 210.0, 200.0)],
            equity_curve=[("d1", 100_000.0), ("d2", 105_000.0), ("d3", 99_000.0)],
            trades=[("btst", "2026-07-28", 500.0), ("swing_meanrev", "2026-07-27", -200.0)],
            budget=100_000.0,
        )

    def test_payload_has_every_section(self) -> None:
        payload = self._payload()
        for key in ("equity", "cash", "deployed", "allocations", "concentration",
                    "drawdown", "lane_curves", "sector_exposure", "sector_note"):
            self.assertIn(key, payload)

    def test_deployed_matches_position_values(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["deployed"], 2000.0)      # 1000 + 1000
        self.assertAlmostEqual(payload["cash"] + payload["deployed"], payload["equity"], places=2)

    def test_sector_exposure_is_absent_and_explained(self) -> None:
        """Better to return nothing with a reason than a chart reading 100%
        'NSE Listed Equity'."""
        payload = self._payload()
        self.assertIsNone(payload["sector_exposure"])
        self.assertIn("catch-all", payload["sector_note"])

    def test_drawdown_is_computed_from_the_curve(self) -> None:
        # 105000 -> 99000 = -5.71%
        self.assertAlmostEqual(self._payload()["drawdown"]["max_drawdown_pct"], -5.71, places=2)

    def test_payload_is_json_serialisable(self) -> None:
        json.dumps(self._payload())

    def test_empty_book_does_not_raise(self) -> None:
        payload = portfolio.build([], [], [], 100_000.0)
        self.assertEqual(payload["allocations"], [])
        self.assertEqual(payload["concentration"]["n_positions"], 0)

    def test_garbage_inputs_do_not_raise(self) -> None:
        payload = portfolio.build("nonsense", "nonsense", "nonsense", "nonsense")
        self.assertIn("allocations", payload)


if __name__ == "__main__":
    unittest.main()
