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
        are what stop it buying rich Bank Nifty premium into an event."""
        src = self._src()
        for gate in ('symbol.upper() != "NIFTY"', 'events["budget_day"]',
                     "max_straddle_pct", "max_premium_pct", "_risk_halt("):
            with self.subTest(gate=gate):
                self.assertIn(gate, src)

    def test_banknifty_is_excluded(self) -> None:
        """Measured: implied 2.20% vs realised 1.13% — premium priced for twice
        the move that happens."""
        self.assertIn('symbol.upper() != "NIFTY"', self._src())

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
        self.assertIn("max_straddle_pct", self._src())

    def test_position_count_is_capped(self) -> None:
        self.assertIn("max_concurrent", self._src())

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
