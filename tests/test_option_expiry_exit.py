"""An option position must never be carried into its expiry.

Nothing closed one. There was no expiry rule anywhere in the exit path, and
index_options was not in HOLD_DAYS so it fell through to the default of 10
TRADING days. The NIFTY CE bought on 2026-07-31 expires 08-04; its time exit
was not due until roughly 08-14.

What happens in between is the part that matters. Once a contract expires it
drops out of the ATM watch list, so either:

  * no quote arrives at all and exit_monitor's `if not lq: continue` skips it
    forever, or
  * the last-written row survives in nfo_quotes and the frozen-quote guard
    skips it forever.

Either way the position is never closed and marks at a stale price for as long
as the book exists. These tests pin the exit, and pin that the frozen guard
does not swallow it.

Hold length is also measured, not chosen. On 405 sessions and 229,229
affordable index-option entries, same entries and same -35%/+60% exits, only
the hold varying: 1 day -5.2%, 2 days -7.3%, 3 days -8.3%, 5 days -8.9%,
10 days -9.2%. About a percent of premium per day, which is theta.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import v2_live


TODAY = date(2026, 8, 4)
TODAY_S = "2026-08-04"


def _pos(strategy="index_options", entry=112.90, stop=73.39, expiry="2026-08-04",
         edate="2026-07-31", peak=None, target=180.64):
    return dict(id=1, strategy=strategy, entry=entry, shares=65.0, stop=stop,
                target=target, trail=0.0, peak=peak or entry, edate=edate, expiry=expiry)


def _quote(price, high=None, low=None, expiry="2026-08-04"):
    return {"price": price, "high": high if high is not None else price,
            "low": low if low is not None else price, "expiry": expiry}


def _ev(p, lq, now_hhmm="11:00", today=TODAY, today_s=TODAY_S):
    return v2_live.evaluate_exit(p, lq, None, today, today_s, "IN", now_hhmm)


class ExpiryExitTest(unittest.TestCase):
    def test_a_position_is_closed_at_the_squareoff_on_its_expiry_day(self) -> None:
        peak, eff, ex, reason = _ev(_pos(), _quote(95.0),
                                    now_hhmm=v2_live.INDEX_OPT_SQUAREOFF)
        self.assertEqual(reason, "expiry")
        self.assertEqual(ex, 95.0)

    def test_it_is_HELD_earlier_on_expiry_day(self) -> None:
        """Was: closed for the whole expiry session. That fought the entry rule,
        which opens positions until EXPIRY_LAST_ENTRY — buy, close seconds later
        on the 8s cadence, re-buy the same strike. Nineteen round trips of
        NIFTY2680424450PE on 2026-08-04 for -Rs 2,610."""
        peak, eff, ex, reason = _ev(_pos(), _quote(95.0), now_hhmm="10:00")
        self.assertNotEqual(reason, "expiry")

    def test_it_closes_on_the_day_not_after(self) -> None:
        """After the close the contract is gone from the feed — there is no
        later chance to sell it at a price."""
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-04"), _quote(95.0),
                                    now_hhmm="15:20",
                                    today=date(2026, 8, 4), today_s="2026-08-04")
        self.assertEqual(reason, "expiry")

    def test_a_contract_already_past_expiry_still_closes(self) -> None:
        """The stranded case: nothing closed it on the day, so it must still
        close whenever it is next seen."""
        # priced between the stop (73.39) and the target (180.64), so only the
        # expiry rule can close it
        peak, eff, ex, reason = _ev(_pos(expiry="2026-07-28"), _quote(100.0))
        self.assertEqual(reason, "expiry")

    def test_a_contract_with_time_left_is_held(self) -> None:
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-11", edate=TODAY_S),
                                    _quote(120.0, expiry="2026-08-11"))
        self.assertIsNone(ex)

    def test_a_position_older_than_the_hold_exits_even_before_expiry(self) -> None:
        """Backstop to the intraday square-off: if the engine was down at
        15:12 the position must still not be carried indefinitely."""
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-11", edate="2026-07-31"),
                                    _quote(120.0, expiry="2026-08-11"))
        self.assertEqual(reason, "time")

    def test_the_stop_still_takes_precedence_on_expiry_day(self) -> None:
        """A -35% stop is a worse fill than the close, but precedence must stay
        predictable: stop first, then target, then expiry."""
        peak, eff, ex, reason = _ev(_pos(), _quote(70.0, low=70.0))
        self.assertEqual(reason, "stop")

    def test_the_target_still_takes_precedence_on_expiry_day(self) -> None:
        peak, eff, ex, reason = _ev(_pos(), _quote(200.0, high=200.0))
        self.assertEqual(reason, "target")

    def test_the_expiry_is_read_from_the_position_not_only_the_quote(self) -> None:
        """The whole point: an expired contract stops being quoted, so an expiry
        knowable only from a quote is not knowable at all."""
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-04"), {"price": 95.0,
                                                                "high": 95.0, "low": 95.0},
                                    now_hhmm=v2_live.INDEX_OPT_SQUAREOFF)
        self.assertEqual(reason, "expiry")

    def test_a_missing_expiry_does_not_force_an_exit(self) -> None:
        """Equity positions have no expiry and must be unaffected."""
        p = _pos(strategy="swing_meanrev", entry=100.0, stop=95.0, target=0.0,
                 expiry=None, edate="2026-07-30")
        peak, eff, ex, reason = _ev(p, {"price": 101.0, "high": 101.0, "low": 99.0})
        self.assertIsNone(ex)

    def test_a_malformed_expiry_is_ignored_rather_than_fatal(self) -> None:
        for bad in ("", "not-a-date", "0000", None):
            self.assertFalse(v2_live._expired_or_expiring(bad, TODAY), bad)


class SquareOffTest(unittest.TestCase):
    """Measured: holding one extra day costs about a percent of premium."""

    def test_index_options_square_off_intraday(self) -> None:
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-11", edate=TODAY_S),
                                    _quote(120.0, expiry="2026-08-11"),
                                    now_hhmm="15:20")
        self.assertEqual(reason, "eod")
        self.assertEqual(ex, 120.0)

    def test_they_are_held_before_the_square_off_time(self) -> None:
        peak, eff, ex, reason = _ev(_pos(expiry="2026-08-11", edate=TODAY_S),
                                    _quote(120.0, expiry="2026-08-11"),
                                    now_hhmm="14:00")
        self.assertIsNone(ex)

    def test_the_hold_is_one_day_not_the_default_ten(self) -> None:
        self.assertEqual(v2_live.HOLD_DAYS["index_options"], 1)

    def test_options_do_not_inherit_the_equity_intraday_lock(self) -> None:
        """INTRA's +1.5% breakeven lock is calibrated for a stock; 1.5% on an
        option premium is noise, and arming a breakeven stop on it would scratch
        every position that ticked up slightly."""
        self.assertNotIn("index_options", v2_live.INTRADAY_STRATS)
        p = _pos(expiry="2026-08-11", edate=TODAY_S, peak=112.90 * 1.02)
        peak, eff, ex, reason = _ev(p, _quote(112.0, expiry="2026-08-11"), now_hhmm="11:00")
        self.assertIsNone(ex, "a 2% pop must not arm a breakeven stop on an option")


class TargetByExpiryTest(unittest.TestCase):
    """A flat target percentage cannot be right for both a 4-day weekly and a
    25-day monthly. A longer-dated option carries more premium and less gamma,
    so the same index move is a far smaller PERCENTAGE move in the premium.

    Measured same-day on 405 sessions — how often the premium reaches +60%, and
    the median of how far it actually gets (ITM/ATM):

        0-2d   42.6-42.9% reach it   median ~46%
        3-7d   36.1-44.1%            median ~37-50%
        8-20d  18.8-21.1%            median ~24-29%
        21d+    6.1-10.4%            median ~17-23%

    So the flat 0.60 was reached 44% of the time on the NIFTY weekly and 10% on
    the FINNIFTY 25-day monthly — one workable, one decorative.
    """

    CFG = v2_live.INDEX_OPTIONS

    def pct(self, expiry, today=date(2026, 7, 31)):
        return v2_live._target_pct(self.CFG, expiry, today)

    def test_a_near_dated_contract_keeps_an_ambitious_target(self) -> None:
        self.assertAlmostEqual(self.pct("2026-08-01"), 0.45)   # 1 day
        self.assertAlmostEqual(self.pct("2026-08-04"), 0.40)   # 4 days, the NIFTY CE

    def test_a_monthly_contract_gets_a_reachable_one(self) -> None:
        """The FINNIFTY position: 25 days out, where +60% printed 10% of the
        time and the median trade only reached +22.6%."""
        self.assertAlmostEqual(self.pct("2026-08-25"), 0.20)

    def test_the_target_falls_as_expiry_lengthens(self) -> None:
        seq = [self.pct(e) for e in ("2026-08-01", "2026-08-05",
                                     "2026-08-15", "2026-09-30")]
        self.assertEqual(seq, sorted(seq, reverse=True))

    def test_an_unknown_expiry_falls_back_rather_than_crashing(self) -> None:
        for bad in (None, "", "not-a-date"):
            self.assertAlmostEqual(self.pct(bad), self.CFG["target_pct"])

    def test_an_expired_contract_does_not_produce_a_negative_target(self) -> None:
        self.assertGreater(self.pct("2026-07-20"), 0)

    def test_the_entry_path_uses_it(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.index_options_pass)
        self.assertIn("_target_pct(cfg, contract.get(\"expiry\"), today,", src)
        self.assertIn("premium * (1 + tgt_pct)", src)


class FrozenQuoteTest(unittest.TestCase):
    """An expired contract's quote stops updating BECAUSE the contract is gone.
    Refusing to act on a stale price there strands the position permanently —
    the last price seen is the only price that will ever exist for it."""

    def test_the_frozen_guard_makes_an_exception_for_expiry(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("if sym in frozen and not _expired_or_expiring(", src)

    def test_the_frozen_exception_also_reads_the_quote(self) -> None:
        """The two option positions open when this shipped predate the column,
        so their stored expiry is NULL. Reading only the position would leave
        exactly those positions stranded — the ones the fix exists for."""
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn('p.get("expiry") or lq.get("expiry")', src)

    def test_a_missing_expiry_is_backfilled_while_the_contract_is_quoted(self) -> None:
        """Once a contract expires it leaves the watch list, so the last chance
        to learn its expiry is while a quote still arrives."""
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("UPDATE v2_positions SET expiry=?", src)
        backfill = src.index("UPDATE v2_positions SET expiry=?")
        evaluate = src.index("evaluate_exit(p, lq,")
        self.assertLess(backfill, evaluate, "backfill must happen before the exit test")

    def test_the_position_loader_reads_the_expiry_column(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("expiry FROM v2_positions", src)

    def test_the_loader_survives_a_book_without_the_column(self) -> None:
        """The live book predates it; the web process cannot migrate."""
        import inspect
        src = inspect.getsource(v2_live.exit_monitor)
        self.assertIn("except Exception:", src)
        self.assertIn("(*r, None)", src)


class SchemaTest(unittest.TestCase):
    def test_expiry_is_migrated_onto_an_existing_book(self) -> None:
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE v2_positions(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " market TEXT, strategy TEXT, symbol TEXT, entry_date TEXT,"
                    " entry_price REAL, shares REAL, stop REAL, target REAL, trail REAL,"
                    " peak REAL, conviction REAL, opened_at TEXT)")
        con.execute("INSERT INTO v2_positions(market,symbol) VALUES('IN','NIFTY24300CE')")
        v2_live.ensure_schema(con)
        cols = {r[1] for r in con.execute("PRAGMA table_info(v2_positions)")}
        self.assertIn("expiry", cols)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0], 1)
        con.close()

    def test_record_entry_persists_the_expiry(self) -> None:
        import sqlite3
        con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(con)
        ok = v2_live.record_entry(con, "IN", "index_options", "NIFTY2680424300CE",
                                  "2026-07-31", 112.90, 65.0, 73.39, 180.64, 0.0,
                                  0.3, None, expiry="2026-08-04")
        self.assertTrue(ok)
        self.assertEqual(con.execute("SELECT expiry FROM v2_positions").fetchone()[0],
                         "2026-08-04")
        con.close()


if __name__ == "__main__":
    unittest.main()
