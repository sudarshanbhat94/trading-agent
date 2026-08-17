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


class WiringTest(unittest.TestCase):
    """The loop must have exactly one entry path."""

    def _loop_src(self):
        import inspect
        from app import v2_live
        return inspect.getsource(v2_live.loop)

    def test_the_loop_calls_the_sleeve_pass(self) -> None:
        self.assertIn("sleeve_pass(m)", self._loop_src())

    def test_the_loop_calls_no_legacy_pass(self) -> None:
        src = self._loop_src()
        for legacy in ("volume_surge_pass(m)", "intraday_momentum_pass(m)",
                       "btst_pass(m)", "index_options_pass(m)"):
            with self.subTest(pass_=legacy):
                self.assertNotIn(legacy, src)

    def test_poll_market_cannot_open_a_position(self) -> None:
        """It still computes signals and publishes ideas, but returns before
        the fill loop — the guarantee must not rest on DISABLED_LANES alone."""
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.poll_market)
        self.assertIn("entries via sleeve_pass", src)
        guard = src.index("entries via sleeve_pass")
        fill = src.index("record_entry(")
        self.assertLess(guard, fill, "the guard must precede the fill loop")

    def test_sleeve_pass_records_sleeve_and_regime(self) -> None:
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.sleeve_pass)
        self.assertIn("sleeve=c.sleeve", src)
        self.assertIn("regime=result.regime.state", src)

    def test_sleeve_pass_routes_equity_only(self) -> None:
        import inspect
        from app import v2_live
        self.assertIn('c.instrument != "EQ"', inspect.getsource(v2_live.sleeve_pass))

    def test_exits_are_not_in_the_sleeve_pass(self) -> None:
        """exit_monitor owns exits for every position, sleeve or legacy."""
        import inspect
        from app import v2_live
        self.assertNotIn("close_position", inspect.getsource(v2_live.sleeve_pass))


class PerformanceSplitTest(unittest.TestCase):
    def _con(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_trades(market TEXT, sleeve TEXT, regime TEXT,"
                    " pnl REAL, shares REAL, entry_price REAL, exit_price REAL,"
                    " entry_date TEXT, risk_amt REAL, closed_at TEXT)")
        rows = [("IN", "mean_reversion", "ON", 120.0, 10, 100, 113, "2026-08-01", 60.0, "2026-08-01T10:00"),
                ("IN", "mean_reversion", "ON", -40.0, 10, 100, 95.5, "2026-08-02", 40.0, "2026-08-02T10:00"),
                ("IN", "early_momentum", "NEUTRAL", -60.0, 10, 100, 93.5, "2026-08-03", 60.0, "2026-08-03T10:00"),
                ("IN", None, None, -500.0, 10, 100, 49.5, "2026-07-01", None, "2026-07-01T10:00")]
        con.executemany("INSERT INTO v2_trades VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("CREATE TABLE v2_book(market TEXT, budget REAL, started_at TEXT)")
        con.execute("INSERT INTO v2_book VALUES('IN', 10000.0, '2026-07-01T00:00')")
        con.execute("CREATE TABLE v2_positions(market TEXT, symbol TEXT, sleeve TEXT,"
                    " strategy TEXT, shares REAL, entry_price REAL, stop REAL)")
        con.execute("INSERT INTO v2_positions VALUES('IN','ACME','mean_reversion',"
                    "'mean_reversion', 3, 625.0, 573.0)")
        return con

    def test_split_by_sleeve(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        self.assertEqual(out["mean_reversion"]["n"], 2)
        self.assertEqual(out["mean_reversion"]["wins"], 1)
        self.assertAlmostEqual(out["mean_reversion"]["net"], 80.0)

    def test_legacy_rows_are_not_folded_into_a_sleeve(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        self.assertIn("legacy", out)
        self.assertEqual(out["legacy"]["n"], 1)

    def test_split_by_regime(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_regime(self._con())
        self.assertEqual(out["ON"]["n"], 2)
        self.assertEqual(out["NEUTRAL"]["n"], 1)

    def test_the_cross_tab_separates_sleeve_from_tape(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve_and_regime(self._con())
        self.assertEqual(out[("mean_reversion", "ON")]["n"], 2)
        self.assertEqual(out[("early_momentum", "NEUTRAL")]["n"], 1)

    def test_cost_is_reported_separately_from_decisions(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        self.assertIn("cost", out["mean_reversion"])

    def test_report_renders(self) -> None:
        from app.sleeves import performance as perf
        text = perf.report(self._con())
        self.assertIn("mean_reversion", text)
        self.assertIn("BY REGIME", text)


class DailyReportTest(unittest.TestCase):
    def _con(self):
        return PerformanceSplitTest._con(PerformanceSplitTest())

    def test_book_state_reports_equity_cash_and_positions(self) -> None:
        from app.sleeves import performance as perf
        snap = perf.book_state(self._con(), "IN", prices={"ACME": {"price": 650.0}})
        self.assertEqual(snap.capital, 10_000.0)
        self.assertEqual(snap.n_positions, 1)
        self.assertAlmostEqual(snap.positions_value, 1_950.0)
        self.assertAlmostEqual(snap.equity, snap.cash + snap.positions_value)

    def test_equity_is_cash_plus_positions(self) -> None:
        from app.sleeves import performance as perf
        snap = perf.book_state(self._con(), "IN")
        self.assertAlmostEqual(snap.equity, snap.cash + snap.positions_value, places=6)

    def test_avg_r_uses_recorded_risk(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        # +120/60 = +2R and -40/40 = -1R  ->  mean +0.5R
        self.assertAlmostEqual(out["mean_reversion"]["avg_r"], 0.5, places=6)

    def test_trades_without_recorded_risk_report_no_r(self) -> None:
        """Excluded from the R column, not counted as zero."""
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        self.assertIsNone(out["legacy"]["avg_r"])

    def test_win_rate_per_sleeve(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_sleeve(self._con())
        self.assertAlmostEqual(out["mean_reversion"]["win_rate"], 50.0)
        self.assertAlmostEqual(out["early_momentum"]["win_rate"], 0.0)

    def test_regime_split_covers_on_and_neutral(self) -> None:
        from app.sleeves import performance as perf
        out = perf.by_regime(self._con())
        self.assertEqual(out["ON"]["n"], 2)
        self.assertEqual(out["NEUTRAL"]["n"], 1)

    def test_report_contains_every_required_section(self) -> None:
        from app.sleeves import performance as perf
        text = perf.report(self._con(), "IN")
        for section in ("BOOK", "OPEN POSITIONS", "BY SLEEVE", "BY REGIME",
                        "SLEEVE x REGIME", "equity", "cash", "avg R"):
            with self.subTest(section=section):
                self.assertIn(section, text)

    def test_the_report_is_read_only(self) -> None:
        """A report must never be able to write to the book."""
        import inspect
        from app.sleeves import performance as perf
        self.assertIn("mode=ro", inspect.getsource(perf.open_readonly))
        src = inspect.getsource(perf)
        for verb in ("INSERT ", "UPDATE ", "DELETE "):
            with self.subTest(verb=verb.strip()):
                self.assertNotIn(verb, src)

    def test_risk_amt_is_carried_from_entry_to_trade(self) -> None:
        import inspect
        from app import v2_live
        self.assertIn("risk_amt", inspect.getsource(v2_live.record_entry))
        self.assertIn("risk_amt", inspect.getsource(v2_live.record_exit))


class WebsiteConsistencyTest(unittest.TestCase):
    """The website must show the same book the server holds."""

    def test_overview_scopes_realised_to_the_epoch(self) -> None:
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web._market_stats)
        self.assertIn("started_at", src)
        self.assertIn("COALESCE(closed_at,'')>=?", src)

    def test_prev_equity_from_the_old_book_is_discarded(self) -> None:
        """An Rs 88,749 baseline against a Rs 10,000 book made every move read
        as a collapse."""
        import inspect
        from app import v2_web
        self.assertIn("float(prev_row[0]) > budget * 3",
                      inspect.getsource(v2_web.api_overview))

    def test_per_user_cash_scopes_to_the_epoch(self) -> None:
        """Scoping is now STRUCTURAL: rows carry the epoch they were written
        in, rather than each query remembering a timestamp comparison. That
        comparison was forgotten in five separate places, and every miss put
        the old Rs 1,00,000 ledger back on the dashboard."""
        import inspect
        from app import books
        src = inspect.getsource(books.cash)
        self.assertIn("current_epoch(con, user_id, market)", src)
        self.assertIn("COALESCE(book_epoch,?)=?", src)

    def test_every_money_read_filters_on_the_stamped_epoch(self) -> None:
        import inspect
        from app import books
        for fn in (books.cash, books.stats, books.positions):
            with self.subTest(fn=fn.__name__):
                self.assertIn("book_epoch", inspect.getsource(fn))

    def test_writes_stamp_the_epoch(self) -> None:
        import inspect
        from app import books
        for fn in (books.buy, books.sell):
            with self.subTest(fn=fn.__name__):
                self.assertIn("book_epoch", inspect.getsource(fn))

    def test_a_read_never_creates_a_book(self) -> None:
        """current_epoch must not bootstrap on a read path — that is how every
        user who merely loads a page gets a phantom book."""
        import inspect
        from app import books
        self.assertNotIn("ensure_book", inspect.getsource(books.current_epoch))

    def test_positions_api_exposes_sleeve_and_regime(self) -> None:
        import inspect
        from app import v2_web, books
        self.assertIn("sleeve=(p.get(\"sleeve\") or p[\"strategy\"])",
                      inspect.getsource(v2_web._my_positions))
        self.assertIn("sleeve", inspect.getsource(books.positions))

    def test_the_options_panel_is_flagged_retired(self) -> None:
        import inspect
        from app import v2_web
        self.assertIn("options_retired", inspect.getsource(v2_web))

    def test_user_books_match_the_house_capital(self) -> None:
        from app import books, v2_live
        self.assertEqual(books.DEFAULT_BUDGET["IN"], v2_live.BUDGET["IN"])
        self.assertEqual(books.MAX_POSITIONS, v2_live.MAXPOS["IN"])


class RetiredOptionsBookTest(unittest.TestCase):
    """A closed book must not render as a live one."""

    def test_the_api_reports_no_live_capital_when_retired(self) -> None:
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web)
        i = src.index("if _opt_retired:")
        window = src[i:i + 900]
        for field in ("options_budget=None", "options_cash=None",
                      "options_equity=None", "options_today=None"):
            with self.subTest(field=field):
                self.assertIn(field, window)

    def test_history_survives_retirement(self) -> None:
        """Retired is not deleted — the record stays readable."""
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web)
        window = src[src.index("if _opt_retired:"):][:900]
        for field in ("options_realised", "options_trades", "options_win"):
            with self.subTest(field=field):
                self.assertIn(field, window)

    def test_the_retired_check_precedes_the_null_guard(self) -> None:
        """A retired book reports no equity, so a null-guard placed first would
        blank the card before the retired branch could render."""
        from app import v2_web
        src = v2_web.SPA_HTML if hasattr(v2_web, "SPA_HTML") else inspect_src()
        i_ret = src.index("if(o.options_retired)")
        i_null = src.index("if(o.options_equity==null)", src.index("renderOptionsTile"))
        self.assertLess(i_ret, i_null)

    def test_the_headline_excludes_a_retired_book(self) -> None:
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web)
        self.assertIn("(ob.options_equity==null||optRetired)?0:ob.options_equity", src)


def inspect_src():
    import inspect
    from app import v2_web
    return inspect.getsource(v2_web)


class IdeasComeFromSleevesTest(unittest.TestCase):
    def test_the_legacy_publisher_is_not_called(self) -> None:
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live.poll_market)
        self.assertNotIn("_publish_ideas(v2, market, tails", src)
        self.assertIn("LEGACY IDEA PATH RETIRED", src)

    def test_sleeve_pass_publishes_ideas(self) -> None:
        import inspect
        from app import v2_live
        self.assertIn("_publish_sleeve_ideas(v2, market, result",
                      inspect.getsource(v2_live.sleeve_pass))

    def test_ideas_carry_the_sleeve_and_regime(self) -> None:
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live._publish_sleeve_ideas)
        self.assertIn("sleeve=c.sleeve", src)
        self.assertIn("regime=result.regime.state", src)

    def test_idea_levels_come_from_the_sleeve_plan(self) -> None:
        """Not a second computation that can drift from what the sleeve
        proposed."""
        import inspect
        from app import v2_live
        src = inspect.getsource(v2_live._publish_sleeve_ideas)
        self.assertIn("stop=float(c.stop)", src)
        self.assertIn("target=float(c.target", src)

    def test_only_equity_ideas_are_published(self) -> None:
        import inspect
        from app import v2_live
        self.assertIn('c.instrument != "EQ"',
                      inspect.getsource(v2_live._publish_sleeve_ideas))

    def test_legacy_ideas_are_hidden_from_the_page(self) -> None:
        """102 ideas on record came from the retired path. They stay in the
        table for history, but a reader must not be handed a recommendation
        from an engine that has been switched off."""
        import inspect
        from app import ideas
        src = inspect.getsource(ideas.visible)
        self.assertIn("strategy IN ({smarks})", src)
        for sleeve in ("mean_reversion", "early_momentum"):
            with self.subTest(sleeve=sleeve):
                self.assertIn(sleeve, ideas.SLEEVE_SOURCES)

    def test_no_legacy_lane_is_a_permitted_idea_source(self) -> None:
        from app import ideas, v2_live
        for lane in v2_live.DISABLED_LANES:
            with self.subTest(lane=lane):
                self.assertNotIn(lane, ideas.SLEEVE_SOURCES)
