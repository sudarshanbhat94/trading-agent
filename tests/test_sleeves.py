"""The multi-sleeve system: regime gate, sizing, and per-sleeve behaviour.

Everything here runs on synthetic panels so the contracts are pinned
independently of whatever the market is doing on any given day.
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from app.sleeves.base import Candidate, Sleeve, SleeveDecision
from app.sleeves.config import SLEEVES
from app.sleeves.engine import PRIORITY, SleeveEngine
from app.sleeves.options_overlay import OptionsOverlaySleeve, Spread
from app.sleeves.regime import BREADTH_NEUTRAL, BREADTH_ON, RegimeGate
from app.sleeves.risk import BookState, RiskManager
from app.sleeves.universe import MAX_PRICE, MIN_PRICE, MIN_TURNOVER, liquid_universe


def _panel(n_days=300, start=100.0, drift=0.0005, vol=0.01, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n_days)
    rets = rng.normal(drift, vol, n_days)
    close = start * np.cumprod(1 + rets)
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.01,
        "low": close * 0.99, "close": close,
        "volume": rng.integers(80_000, 120_000, n_days).astype(float),
    }, index=idx)


def _book(**kw):
    base = dict(capital=10_000.0, cash=10_000.0, deployed=0.0, open_positions=0,
                per_sleeve_positions={}, equity=10_000.0, peak_equity=10_000.0,
                day_pnl=0.0)
    base.update(kw)
    return BookState(**base)


class CapitalIsTenThousandTest(unittest.TestCase):
    """Operator instruction: exactly Rs 10,000, everything inside it."""

    def test_capital(self) -> None:
        self.assertEqual(SLEEVES.capital, 10_000.0)

    def test_v2_book_agrees(self) -> None:
        from app import v2_live
        self.assertEqual(v2_live.BUDGET["IN"], 10_000.0)

    def test_per_user_books_agree(self) -> None:
        from app import books
        self.assertEqual(books.DEFAULT_BUDGET["IN"], 10_000.0)

    def test_shares_cannot_over_allocate_the_book(self) -> None:
        total = sum(getattr(SLEEVES, n).risk_share for n in PRIORITY)
        self.assertLessEqual(total, 1.0 + 1e-9)

    def test_slots_fit_inside_the_book(self) -> None:
        slot = SLEEVES.capital / SLEEVES.max_positions_total
        self.assertLessEqual(slot * SLEEVES.max_positions_total, SLEEVES.capital + 1e-6)
        self.assertGreaterEqual(slot, SLEEVES.min_ticket)


class RegimeIsTheMasterGateTest(unittest.TestCase):
    def _mdf(self, rising: bool):
        idx = pd.bdate_range("2025-01-01", periods=120)
        step = 0.001 if rising else -0.001
        cum = np.cumprod(1 + np.full(120, step))
        return pd.DataFrame({"mkt_ret1": step, "mkt_cum": cum}, index=idx)

    def test_a_falling_market_is_off(self) -> None:
        mdf = self._mdf(rising=False)
        view = RegimeGate().view({}, mdf, mdf.index[-1])
        self.assertEqual(view.state, "OFF")
        self.assertFalse(view.allows_equity_longs)

    def test_narrow_breadth_demotes_a_rising_market(self) -> None:
        """An index above its mean on a handful of heavyweights is not a regime
        a dip-buying book should be long into."""
        mdf = self._mdf(rising=True)
        asof = mdf.index[-1]
        falling = {f"S{i}": _panel(drift=-0.002, seed=i) for i in range(20)}
        for g in falling.values():
            g.index = mdf.index[:len(g)] if len(g) <= len(mdf) else g.index
        view = RegimeGate().view({k: v.reindex(mdf.index).ffill()
                                  for k, v in falling.items()}, mdf, asof)
        self.assertLess(view.breadth, BREADTH_ON)
        self.assertIn(view.state, ("NEUTRAL", "OFF"))
        self.assertNotEqual(view.state, "ON")

    def test_every_sleeve_declares_its_regimes(self) -> None:
        eng = SleeveEngine()
        for name, sleeve in eng.sleeves.items():
            with self.subTest(sleeve=name):
                self.assertTrue(sleeve.allowed_regimes)
                self.assertNotIn("OFF", sleeve.allowed_regimes,
                                 "no sleeve may open longs in an OFF regime")


class RiskManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rm = RiskManager()

    def _cand(self, price=250.0, stop=235.0, sleeve="mean_reversion", target=None):
        return Candidate(symbol="ACME", sleeve=sleeve, score=0.8, entry=price,
                         stop=stop, target=target if target is not None else price * 1.08)

    def test_it_sizes_a_normal_trade(self) -> None:
        a = self.rm.size(self._cand(), _book())
        self.assertGreater(a.shares, 0)
        self.assertLessEqual(a.notional, SLEEVES.capital / SLEEVES.max_positions_total + 1)

    def test_risk_per_trade_is_bounded(self) -> None:
        cap = SLEEVES.capital * SLEEVES.risk_per_trade
        for price, stop in ((250.0, 235.0), (900.0, 850.0), (80.0, 76.0), (120.0, 100.0)):
            with self.subTest(price=price):
                a = self.rm.size(self._cand(price, stop), _book())
                if a.ok:
                    self.assertLessEqual(a.risk_amount, cap + price)

    def test_a_wider_stop_buys_a_smaller_position(self) -> None:
        tight = self.rm.size(self._cand(250.0, 243.0), _book())
        wide = self.rm.size(self._cand(250.0, 200.0), _book())
        self.assertGreater(tight.shares, wide.shares)

    def test_a_tiny_ticket_is_refused(self) -> None:
        a = self.rm.size(self._cand(price=20.0, stop=1.0), _book())
        self.assertFalse(a.ok)
        self.assertIn("minimum", a.reason)

    def test_a_thin_target_is_refused(self) -> None:
        a = self.rm.size(self._cand(target=250.0 * 1.005), _book())
        self.assertFalse(a.ok)
        self.assertIn("minimum edge", a.reason)

    def test_drawdown_halts_everything(self) -> None:
        b = _book(equity=8_500.0, peak_equity=10_000.0)
        halted, why = self.rm.halted(b)
        self.assertTrue(halted)
        self.assertIn("drawdown", why)

    def test_daily_loss_halts_everything(self) -> None:
        b = _book(day_pnl=-250.0)
        halted, why = self.rm.halted(b)
        self.assertTrue(halted)
        self.assertIn("daily loss", why)

    def test_position_cap_halts(self) -> None:
        halted, why = self.rm.halted(_book(open_positions=SLEEVES.max_positions_total))
        self.assertTrue(halted)
        self.assertIn("position cap", why)

    def test_allocate_respects_the_book_cap(self) -> None:
        cands = [Candidate(symbol=f"S{i}", sleeve="mean_reversion", score=0.9,
                           entry=200.0, stop=190.0, target=220.0) for i in range(10)]
        allocs = self.rm.allocate(cands, _book())
        self.assertLessEqual(len(allocs), SLEEVES.max_positions_total)

    def test_allocate_never_spends_more_than_the_book(self) -> None:
        cands = [Candidate(symbol=f"S{i}", sleeve="mean_reversion", score=0.9,
                           entry=200.0, stop=190.0, target=220.0) for i in range(10)]
        spent = sum(a.notional for a in self.rm.allocate(cands, _book()))
        self.assertLessEqual(spent, SLEEVES.capital)

    def test_a_halted_book_allocates_nothing(self) -> None:
        b = _book(equity=8_000.0, peak_equity=10_000.0)
        self.assertEqual(self.rm.allocate([self._cand()], b), [])


class CandidateSanityTest(unittest.TestCase):
    def test_stop_above_entry_is_refused(self) -> None:
        ok, why = Candidate("X", "mean_reversion", 0.9, 100.0, 105.0).is_sane()
        self.assertFalse(ok)
        self.assertIn("stop above entry", why)

    def test_target_below_entry_is_refused(self) -> None:
        ok, why = Candidate("X", "mean_reversion", 0.9, 100.0, 95.0, target=99.0).is_sane()
        self.assertFalse(ok)

    def test_an_absurdly_wide_stop_is_refused(self) -> None:
        ok, why = Candidate("X", "mean_reversion", 0.9, 100.0, 50.0).is_sane()
        self.assertFalse(ok)
        self.assertIn("25%", why)


class UniverseTest(unittest.TestCase):
    def test_it_keeps_liquid_mid_priced_names(self) -> None:
        g = _panel(start=300.0)
        g["volume"] = 1e6                      # ~Rs 30 cr turnover
        out = liquid_universe({"GOOD": g}, g.index[-1])
        self.assertIn("GOOD", out)

    def test_it_drops_illiquid_names(self) -> None:
        g = _panel(start=300.0)
        g["volume"] = 100.0
        self.assertEqual(liquid_universe({"THIN": g}, g.index[-1]), [])

    def test_it_drops_names_too_dear_for_the_book(self) -> None:
        g = _panel(start=MAX_PRICE * 2)
        g["volume"] = 1e6
        self.assertEqual(liquid_universe({"DEAR": g}, g.index[-1]), [])

    def test_the_price_ceiling_fits_a_slot(self) -> None:
        slot = SLEEVES.capital / SLEEVES.max_positions_total
        self.assertLess(MAX_PRICE, slot,
                        "one share must not exceed a whole slot")


class OptionsOverlayIsDefinedRiskTest(unittest.TestCase):
    def test_a_naked_short_is_structurally_refused(self) -> None:
        s = Spread(kind="bull_put", underlying="NIFTY", short_strike=24000,
                   long_strike=None, credit=50, width=0, lot_size=75, expiry="")
        ok, why = OptionsOverlaySleeve._validate(s)
        self.assertFalse(ok)
        self.assertIn("naked", why.lower())

    def test_zero_width_is_a_naked_short(self) -> None:
        s = Spread("bull_put", "NIFTY", 24000, 24000, 50, 0, 75, "")
        ok, why = OptionsOverlaySleeve._validate(s)
        self.assertFalse(ok)

    def test_a_real_spread_passes(self) -> None:
        s = Spread("bull_put", "NIFTY", 24000, 23900, 30, 100, 75, "")
        ok, _ = OptionsOverlaySleeve._validate(s)
        self.assertTrue(ok)
        self.assertEqual(s.max_loss_per_lot, (100 - 30) * 75)

    def test_it_sizes_to_zero_when_a_lot_exceeds_the_book(self) -> None:
        """At Rs 10,000 one NFO lot's defined risk is far above the cap. The
        sleeve must say so rather than pretend to trade."""
        s = Spread("bull_put", "NIFTY", 24000, 23900, 30, 100, 75, "")
        lots, why = OptionsOverlaySleeve.size_spread(s, SLEEVES.capital)
        self.assertEqual(lots, 0)
        self.assertIn("cannot fund", why)

    def test_it_would_size_at_adequate_capital(self) -> None:
        s = Spread("bull_put", "NIFTY", 24000, 23900, 30, 100, 75, "")
        lots, why = OptionsOverlaySleeve.size_spread(s, 2_000_000.0)
        self.assertGreaterEqual(lots, 1)


class EngineWiringTest(unittest.TestCase):
    def test_all_five_sleeves_exist_and_are_prioritised(self) -> None:
        eng = SleeveEngine()
        self.assertEqual(sorted(eng.sleeves), sorted(PRIORITY))
        self.assertEqual(PRIORITY[0], "mean_reversion", "primary gets first claim")
        self.assertEqual(PRIORITY[-1], "options_overlay", "overlay is allocated last")

    def test_every_sleeve_has_a_feature_flag(self) -> None:
        for name in PRIORITY:
            with self.subTest(sleeve=name):
                cfg = getattr(SLEEVES, name, None)
                self.assertIsNotNone(cfg)
                self.assertIsInstance(cfg.enabled, bool)

    def test_a_halted_book_produces_no_allocations(self) -> None:
        eng = SleeveEngine()
        idx = pd.bdate_range("2025-01-01", periods=120)
        mdf = pd.DataFrame({"mkt_ret1": 0.001,
                            "mkt_cum": np.cumprod(1 + np.full(120, 0.001))}, index=idx)
        res = eng.run({}, mdf, idx[-1], {},
                      _book(equity=8_000.0, peak_equity=10_000.0))
        self.assertEqual(res.allocations, [])
        self.assertIn("drawdown", res.halt_reason)


class LegacyLanesAreDeadTest(unittest.TestCase):
    def test_no_legacy_lane_can_trade(self) -> None:
        from app import v2_live
        live = set(v2_live.PLAN) - v2_live.DISABLED_LANES
        self.assertEqual(live, set(), f"legacy lanes still live: {live}")

    def test_option_buying_is_retired(self) -> None:
        from app import v2_live
        self.assertTrue(v2_live.OPTION_BUYING_RETIRED)


if __name__ == "__main__":
    unittest.main()
