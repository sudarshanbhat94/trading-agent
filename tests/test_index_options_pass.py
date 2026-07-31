"""The index-options execution path.

Everything before this produced analysis that nothing consumed: the direction
engine, chain analytics, levels and the settings toggle all existed while
INDEX_OPTIONS appeared exactly once in v2_live.py, in its config block. The
switch the operator turned on was wired to nothing, and it took him asking "why
isn't it buying" to surface that.

So the first thing these tests assert is WIRING — that a pass exists, that the
engine loop calls it, and that exits can see option prices at all. A lane that
can enter but not exit is worse than one that does nothing.

The gates are the second thing, and each is a measured result rather than a
preference. Bank Nifty's ATM straddle implies 2.20% against 1.13% realised, so
buying its premium is paying twice for the same exposure.
"""

from __future__ import annotations

import inspect
import unittest

from app import v2_live


class WiringTest(unittest.TestCase):
    """The failure that started this: built, tested, and connected to nothing."""

    def test_the_pass_exists(self) -> None:
        self.assertTrue(callable(getattr(v2_live, "index_options_pass", None)))

    def test_the_engine_loop_calls_it(self) -> None:
        """The whole point. A pass nobody calls is dead code."""
        src = inspect.getsource(v2_live.loop)
        self.assertIn("index_options_pass(m)", src)

    def test_exits_can_see_option_prices(self) -> None:
        """Options live in nfo_quotes, not latest_quotes. Without this merge a
        position is enterable and never exitable."""
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("_option_live(", src)

    def test_entry_goes_through_the_single_writer(self) -> None:
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("record_entry(", src)


class GateTest(unittest.TestCase):
    def _src(self):
        return inspect.getsource(v2_live.index_options_pass)

    def test_auto_trade_is_required(self) -> None:
        self.assertIn('cfg.get("auto_trade")', self._src())

    def test_auto_trade_state_is_explicit(self) -> None:
        """Turned ON by the operator on 2026-07-30, with the measured result
        (36% direction accuracy over 404 sessions) stated and overruled. Pinned
        so the state is a decision on record rather than a drift."""
        self.assertTrue(v2_live.INDEX_OPTIONS["auto_trade"])

    def test_the_gates_still_apply_with_auto_trade_on(self) -> None:
        """Enabling trading must not disable the measured constraints — these
        are what stop it buying rich premium into an event."""
        src = self._src()
        for gate in ('events["budget_day"]', "_straddle_limit(",
                     "max_premium_pct", "_risk_halt("):
            with self.subTest(gate=gate):
                self.assertIn(gate, src)

    def test_the_operator_selection_is_honoured(self) -> None:
        """Was `if symbol.upper() != "NIFTY": continue`. The settings page
        offered four instruments, persisted all four, and returned a live call
        for each — and the engine dropped three on the floor. A tick-box that
        does nothing is worse than no tick-box, and this is the second time the
        index settings have silently ignored a selection.

        The measured Bank Nifty caution is now SURFACED rather than enforced;
        see test_the_banknifty_premium_finding_is_still_stated.
        """
        self.assertNotIn('!= "NIFTY"', self._src())
        self.assertIn('cfg.get("instruments"', self._src())

    def test_the_banknifty_premium_finding_is_still_stated(self) -> None:
        """Measured: implied 2.20% vs realised 1.13% — premium priced for twice
        the move that happens. That is a reason to warn the operator, not to
        override a choice they made, so it must still reach the UI."""
        self.assertIn("BANKNIFTY", v2_live.INDEX_PREMIUM_WARNING)
        self.assertIn("2.20%", v2_live.INDEX_PREMIUM_WARNING["BANKNIFTY"])

    def test_only_indices_with_data_can_be_selected(self) -> None:
        """Verified live: all four return 60 daily bars, a populated chain and
        live option quotes. Anything outside this set is skipped."""
        self.assertEqual(set(v2_live.INDEX_ALLOWED),
                         {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"})
        self.assertIn("INDEX_ALLOWED", self._src())

    def test_one_position_per_index_not_one_overall(self) -> None:
        """max_concurrent was 1, and the pass returned after the first fill, so
        a NIFTY CE opened at 09:40 blocked every other index for the session."""
        self.assertGreaterEqual(v2_live.INDEX_OPTIONS["max_concurrent"], 3)
        self.assertIn("h.startswith(symbol)", self._src())

    def test_the_confidence_gate_cannot_exceed_what_the_vote_can_produce(self) -> None:
        """`confidence` is agreeing/5, so a MIN_AGREEING=2 call reports 0.40 and
        no more. The persisted setting was 0.60 — right when MIN_AGREEING was 3,
        left behind when it was loosened to 2 — so the lane refused every call
        it generated. That reads as 'index options are broken'."""
        from app import index_direction
        self.assertAlmostEqual(index_direction.max_confidence(5), 0.4)
        self.assertIn("max_confidence(", self._src())

    def test_the_ceiling_tracks_the_actual_reading_count(self) -> None:
        """Live internals add readings to the five daily ones, so the
        denominator is no longer always 5. A hardcoded 2/5 would let a stored
        threshold out-run what the vote can produce the moment that changes —
        the same failure that had the lane refusing every call."""
        from app import index_direction as idx
        self.assertAlmostEqual(idx.max_confidence(9), 2 / 9)
        self.assertAlmostEqual(idx.max_confidence(5), 2 / 5)
        # unknown or zero falls back to the default count rather than dividing
        # by zero or returning a ceiling above 1.0
        self.assertAlmostEqual(idx.max_confidence(None), 2 / 5)
        self.assertAlmostEqual(idx.max_confidence(0), 2 / 5)
        self.assertLessEqual(idx.max_confidence(3), 1.0)

    def test_a_stale_high_setting_cannot_silently_kill_the_lane(self) -> None:
        from app.v2_web import _effective_min_conf
        self.assertAlmostEqual(_effective_min_conf({"min_confidence": 0.6}), 0.4)
        self.assertAlmostEqual(_effective_min_conf({"min_confidence": 0.2}), 0.2)
        self.assertAlmostEqual(_effective_min_conf({}), 0.4)
        self.assertAlmostEqual(_effective_min_conf({"min_confidence": None}), 0.4)

    def test_every_reader_of_min_confidence_uses_the_same_clamp(self) -> None:
        """There are three: the engine's entry gate, the settings API, and the
        `actionable` flag on the call display. The third was still reading the
        raw setting, so the page reported "not actionable" for a call the
        engine would have taken — the display and the book disagreeing about
        the same number. A raw `cfg.get("min_confidence"` outside the clamp
        helper is that bug returning.
        """
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web)
        active = src[:src.index('SPA_HTML = r"""')]
        # the helper itself is the one legitimate raw read
        body = active.replace(inspect.getsource(v2_web._effective_min_conf), "")
        self.assertNotIn('cfg.get("min_confidence"', body)

    def test_the_saved_selection_applies_without_a_page_view(self) -> None:
        """The loader used to live in v2_web and run only when a web request
        read the settings page. After a restart INDEX_OPTIONS held the code
        default instruments ("NIFTY",) while the saved file listed four, so
        three indices were dropped until somebody opened that page in a
        browser. Verified live on 2026-07-31: a freshly restarted process
        reported one instrument against a file listing four.
        """
        import json, tempfile, os, inspect
        from app import v2_live
        self.assertIn("index_settings_load()",
                      inspect.getsource(v2_live.index_options_pass))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index_options.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"instruments": ["NIFTY", "FINNIFTY", "MIDCPNIFTY"],
                           "min_confidence": 0.4}, fh)
            orig_file = v2_live.INDEX_SETTINGS_FILE
            orig_inst = v2_live.INDEX_OPTIONS["instruments"]
            try:
                v2_live.INDEX_SETTINGS_FILE = path
                v2_live.INDEX_OPTIONS["instruments"] = ("NIFTY",)   # post-restart state
                v2_live.index_settings_load()
                self.assertEqual(v2_live.INDEX_OPTIONS["instruments"],
                                 ("NIFTY", "FINNIFTY", "MIDCPNIFTY"))
            finally:
                v2_live.INDEX_SETTINGS_FILE = orig_file
                v2_live.INDEX_OPTIONS["instruments"] = orig_inst

    def test_a_missing_settings_file_leaves_the_defaults_alone(self) -> None:
        from app import v2_live
        orig_file = v2_live.INDEX_SETTINGS_FILE
        orig_inst = v2_live.INDEX_OPTIONS["instruments"]
        try:
            v2_live.INDEX_SETTINGS_FILE = "/nonexistent/index_options.json"
            v2_live.index_settings_load()
            self.assertEqual(v2_live.INDEX_OPTIONS["instruments"], orig_inst)
        finally:
            v2_live.INDEX_SETTINGS_FILE = orig_file
            v2_live.INDEX_OPTIONS["instruments"] = orig_inst

    def test_a_two_of_five_call_is_actionable(self) -> None:
        """The end state that matters: on 2026-07-31 NIFTY, FINNIFTY and
        MIDCPNIFTY all read CE at 0.40 and all three were refused."""
        from app.v2_web import _effective_min_conf
        self.assertGreaterEqual(0.4, _effective_min_conf({"min_confidence": 0.6}))

    def test_policy_and_budget_days_are_skipped(self) -> None:
        """Scheduled repricings: premium is bid beforehand and collapses after,
        and no intraday edge survives paying that."""
        src = self._src()
        self.assertIn('events["budget_day"]', src)
        self.assertIn('events["days_to_mpc"]', src)

    def test_expiry_day_is_TRADEABLE(self) -> None:
        """Weekly expiry is the highest-volume session for Indian index options
        — where most option traders make their money, not a day to sit out. The
        IV-crush argument applies to HOLDING through an event, not to trading
        the session intraday."""
        src = self._src()
        self.assertNotIn('events["event_risk"]', src)
        self.assertIn("EXPIRY_LAST_ENTRY", src)

    def test_late_expiry_entries_are_still_refused(self) -> None:
        """Past the cutoff on expiry day the remaining premium is nearly all
        decay, so a late entry is buying the part that is guaranteed to go."""
        self.assertGreaterEqual(v2_live.EXPIRY_LAST_ENTRY, "12:00")

    def test_rich_premium_is_refused(self) -> None:
        self.assertIn("_straddle_limit(", self._src())
        self.assertIn("max_straddle_pct", v2_live.INDEX_OPTIONS)

    def test_position_count_is_capped(self) -> None:
        self.assertIn("max_concurrent", self._src())

    def test_the_straddle_cap_scales_with_time_to_expiry(self) -> None:
        """An implied move grows with the SQUARE ROOT of time, so a 25-day
        option quotes a bigger expected move than a 4-day one without being any
        richer. The 1.5% cap was calibrated on NIFTY weeklies; applied raw it
        rejected every monthly contract for being longer-dated.

        Measured live on 2026-07-31, raw straddle -> per sqrt-day:
            NIFTY 4d 1.07% -> 0.54   BANKNIFTY 25d 2.88% -> 0.58
            FINNIFTY 25d 3.04% -> 0.61   MIDCPNIFTY 25d 3.10% -> 0.62
        Nearly identical once normalised.
        """
        from datetime import date as _date
        cfg = v2_live.INDEX_OPTIONS
        today = _date(2026, 7, 31)
        weekly = v2_live._straddle_limit(cfg, "2026-08-04", today)   # 4 days
        monthly = v2_live._straddle_limit(cfg, "2026-08-25", today)  # 25 days
        # the calibrated weekly cap is preserved exactly
        self.assertAlmostEqual(weekly, 1.5, places=6)
        # 25 days -> 1.5 * sqrt(25/4) = 3.75
        self.assertAlmostEqual(monthly, 3.75, places=6)
        # the live readings that were being rejected now pass, NIFTY unaffected
        self.assertLess(1.07, weekly)
        for observed in (2.88, 3.04, 3.10):
            self.assertLess(observed, monthly)

    def test_a_genuinely_rich_contract_is_still_refused(self) -> None:
        """Scaling must not turn the gate off."""
        from datetime import date as _date
        cfg = v2_live.INDEX_OPTIONS
        limit = v2_live._straddle_limit(cfg, "2026-08-25", _date(2026, 7, 31))
        self.assertGreater(5.0, 0)
        self.assertGreater(5.0, limit, "a 5% expected move on a monthly must still be refused")

    def test_an_unknown_expiry_falls_back_to_the_strict_cap(self) -> None:
        from datetime import date as _date
        cfg = v2_live.INDEX_OPTIONS
        for bad in (None, "", "not-a-date"):
            self.assertAlmostEqual(v2_live._straddle_limit(cfg, bad, _date(2026, 7, 31)), 1.5)
        # an already-expired contract must not scale the cap DOWN to zero
        self.assertAlmostEqual(
            v2_live._straddle_limit(cfg, "2026-07-30", _date(2026, 7, 31)), 1.5)

    def test_the_monthly_only_indices_get_a_workable_premium_cap(self) -> None:
        """Their cheapest single lot is Rs 21.8k / 27.7k / 27.1k against a Rs 10k
        default, so they could never fill even with every gate open. Raising the
        cap globally would also quadruple NIFTY's size, since _pick_contract
        buys the nearest AFFORDABLE strike — hence a per-instrument override."""
        cfg = v2_live.INDEX_OPTIONS
        by_index = cfg.get("max_premium_pct_by_index") or {}
        budget = cfg["budget"]
        self.assertEqual(cfg["max_premium_pct"], 0.10, "NIFTY sizing must not change")
        for idx, cheapest in (("BANKNIFTY", 21802), ("FINNIFTY", 27669), ("MIDCPNIFTY", 27066)):
            allowed = budget * by_index.get(idx, cfg["max_premium_pct"])
            self.assertGreater(allowed, cheapest, f"{idx} still cannot buy one lot")
        # NIFTY keeps the default
        self.assertNotIn("NIFTY", by_index)
        self.assertGreater(budget * cfg["max_premium_pct"], 4388)  # cheapest NIFTY lot

    def test_premium_at_risk_is_capped(self) -> None:
        self.assertIn("max_premium_pct", self._src())

    def test_the_risk_halt_is_respected(self) -> None:
        self.assertIn("_risk_halt(", self._src())

    def test_no_selling_to_open_anywhere(self) -> None:
        """A short option has unbounded loss; one gap through a strike would
        exceed the whole book."""
        src = self._src()
        self.assertNotIn("SELL", src.upper().replace("SELLING", ""))


def q(strike, side, price, under="NIFTY", lot=65.0):
    return dict(price=price, lot_size=lot, strike=strike,
                option_type=side, underlying=under)


class SeparateBookTest(unittest.TestCase):
    """Options run on their own Rs 1L, ring-fenced from equity.

    Sharing one pot would let a bad options week shrink the position sizing of
    the lane that actually has a measured edge — and would make both results
    unreadable, since neither could be attributed in a mixed ledger.
    """

    def _src(self):
        return inspect.getsource(v2_live.index_options_pass)

    def test_the_options_book_has_its_own_budget(self) -> None:
        self.assertEqual(v2_live.INDEX_OPTIONS["budget"], 100000.0)

    def test_it_never_reads_the_equity_book(self) -> None:
        """v2_book is the EQUITY book. An option must not be funded from it."""
        self.assertNotIn("FROM v2_book", self._src())

    def test_cash_is_computed_from_this_lane_only(self) -> None:
        src = self._src()
        self.assertIn("options_cash", src)
        self.assertIn('"index_options"', src)

    def test_sizing_uses_remaining_cash_not_the_starting_budget(self) -> None:
        """Otherwise losses would not shrink the next bet and the lane would
        keep staking the original amount after drawing down."""
        self.assertIn("min(options_cash", self._src())

    def test_an_exhausted_book_stops_trading(self) -> None:
        self.assertIn("book exhausted", self._src())


class ContractPickTest(unittest.TestCase):
    """Strike and side are read from STORED COLUMNS, never parsed out of the
    ticker: NIFTY2680424000CE runs the expiry code into the strike, and taking
    digits from the right yields 2,680,424,000 — wrong, but not obviously so,
    which would have silently selected a nonsense contract."""

    QUOTES = {
        "NIFTY2680423900CE": q(23900, "CE", 150.0),
        "NIFTY2680424000CE": q(24000, "CE", 100.0),
        "NIFTY2680424100CE": q(24100, "CE", 60.0),
        "NIFTY2680424000PE": q(24000, "PE", 90.0),
        "BANKNIFTY2680456000CE": q(56000, "CE", 400.0, under="BANKNIFTY", lot=30.0),
    }

    def test_picks_the_strike_nearest_spot(self) -> None:
        out = v2_live._pick_contract("NIFTY", "CE", 24010, self.QUOTES)
        self.assertEqual(out["symbol"], "NIFTY2680424000CE")

    def test_respects_the_side(self) -> None:
        out = v2_live._pick_contract("NIFTY", "PE", 24010, self.QUOTES)
        self.assertTrue(out["symbol"].endswith("PE"))

    def test_does_not_stray_to_another_index(self) -> None:
        out = v2_live._pick_contract("NIFTY", "CE", 24010, self.QUOTES)
        self.assertFalse(out["symbol"].startswith("BANKNIFTY"))

    def test_zero_price_or_lot_is_skipped(self) -> None:
        quotes = {"NIFTY2680424000CE": q(24000, "CE", 0.0),
                  "NIFTY2680424100CE": q(24100, "CE", 60.0, lot=0.0)}
        self.assertIsNone(v2_live._pick_contract("NIFTY", "CE", 24010, quotes))

    def test_empty_quotes_yield_nothing(self) -> None:
        self.assertIsNone(v2_live._pick_contract("NIFTY", "CE", 24010, {}))

    def test_an_expensive_atm_does_not_block_the_trade(self) -> None:
        """The reasoning error this fixes: ATM at ~Rs 22,000 is 22% of a Rs 1L
        book, but a strike slightly out of the money is Rs 2,000-5,000 — an
        ordinary position. Standing aside because ATM is dear is wrong."""
        quotes = {"NIFTY_ATM_CE": q(24000, "CE", 346.0),      # 346 x 65 = 22,490
                  "NIFTY_OTM_CE": q(24300, "CE", 31.6)}       # 31.6 x 65 = 2,054
        out = v2_live._pick_contract("NIFTY", "CE", 24010, quotes, max_cost=10_000)
        self.assertEqual(out["symbol"], "NIFTY_OTM_CE")

    def test_the_nearest_affordable_strike_wins_not_the_cheapest(self) -> None:
        """Further out is cheaper but expires worthless more often, so nearest
        affordable is the trade-off — not the cheapest available."""
        quotes = {"NEAR": q(24100, "CE", 90.0),      # 5,850
                  "FAR": q(24800, "CE", 8.0)}        # 520, but far OTM
        out = v2_live._pick_contract("NIFTY", "CE", 24010, quotes, max_cost=10_000)
        self.assertEqual(out["symbol"], "NEAR")

    def test_nothing_affordable_returns_none(self) -> None:
        quotes = {"NIFTY_ATM_CE": q(24000, "CE", 346.0)}
        self.assertIsNone(v2_live._pick_contract("NIFTY", "CE", 24010, quotes, max_cost=1_000))

    def test_cost_is_reported_on_the_chosen_contract(self) -> None:
        out = v2_live._pick_contract("NIFTY", "CE", 24010, self.QUOTES, max_cost=100_000)
        self.assertAlmostEqual(out["cost"], out["price"] * out["lot_size"])


class NoSymbolParsingTest(unittest.TestCase):
    def test_the_ticker_is_never_parsed_for_a_strike(self) -> None:
        """Guards the bug directly: any digits-from-the-right helper is a
        landmine, because the expiry code sits immediately before the strike."""
        src = inspect.getsource(v2_live._pick_contract)
        self.assertIn('q.get("strike")', src)
        self.assertFalse(hasattr(v2_live, "_strike_from_symbol"))


if __name__ == "__main__":
    unittest.main()
