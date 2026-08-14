"""Per-user paper books: one subscriber's actions never touch another's.

Pro is sold as "your own Rs 10,000 paper book" and until now there was ONE
book — the engine's — that every subscriber saw and any of them could wipe.

The engine's tables are deliberately untouched. It reads and writes
v2_positions in fifty places, all assuming a single book, and the same tables
are the evidence base for every measured claim in this codebase. Reshaping them
to ship a product feature would put that at risk; users get their own tables
instead.
"""
from __future__ import annotations

import sqlite3
import unittest

from app import books, v2_live


def _db():
    con = sqlite3.connect(":memory:")
    v2_live.ensure_schema(con)
    books.ensure_schema(con)
    return con


class IsolationTest(unittest.TestCase):
    """THE point of the exercise."""

    def setUp(self) -> None:
        self.con = _db()

    def test_a_reset_clears_only_the_caller(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.buy(self.con, 2, "IN", "manual", "ITC", 300.0)
        books.reset(self.con, 1)
        self.assertEqual(len(books.positions(self.con, 1)), 0)
        self.assertEqual(len(books.positions(self.con, 2)), 1, "user 2 must be untouched")

    def test_a_reset_does_not_touch_the_engines_book(self) -> None:
        """The failure that started this: unqualified DELETEs took the house
        book and its whole history with them."""
        v2_live.record_entry(self.con, "IN", "swing_meanrev", "RELIANCE",
                             "2026-08-04", 1300.0, 12, 1200.0, 1500.0, 0.0, 0.5, None)
        books.reset(self.con, 1)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM v2_positions").fetchone()[0], 1)

    def test_one_users_buy_is_invisible_to_another(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(books.open_symbols(self.con, 2), set())

    def test_cash_is_per_user(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertLess(books.cash(self.con, 1), books.cash(self.con, 2))

    def test_a_sell_credits_only_the_seller(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.buy(self.con, 2, "IN", "manual", "ITC", 300.0)
        books.sell(self.con, 1, "IN", "ITC", 360.0)
        self.assertEqual(len(books.positions(self.con, 2)), 1)
        self.assertEqual(len(books.positions(self.con, 1)), 0)


class SizingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = _db()

    def test_a_position_is_one_slot_of_the_book(self) -> None:
        """Capital is Rs 10,000 across 3 slots, so a slot is Rs 3,333."""
        slot = books.DEFAULT_BUDGET["IN"] * books.POSITION_FRACTION
        qty = books.size_for(self.con, 1, "IN", 1000.0)
        self.assertEqual(qty, int(slot // 1000.0))
        self.assertGreater(qty, 0, "a slot must afford at least one share")

    def test_it_shrinks_once_cash_is_the_binding_cap(self) -> None:
        """Two caps: one slot per position, and the cash actually free. The
        slot binds on a full book, so size only falls once free cash drops
        below it."""
        slot = books.DEFAULT_BUDGET["IN"] * books.POSITION_FRACTION
        first = books.size_for(self.con, 1, "IN", 1000.0)
        self.assertEqual(first, int(slot // 1000.0))
        # spend almost the whole book so CASH becomes the binding cap
        books.buy(self.con, 1, "IN", "manual", "BIG", 1900.0, shares=5)
        self.assertLess(books.cash(self.con, 1), slot)
        self.assertLess(books.size_for(self.con, 1, "IN", 1000.0), first)

    def test_a_book_never_goes_negative(self) -> None:
        """The house book's manual-buy path computed a NEGATIVE quantity when
        cash ran out. A smaller book must skip, not borrow."""
        for sym in ("A", "B", "C", "D", "E", "F"):
            books.buy(self.con, 1, "IN", "manual", sym, 16000.0)
        self.assertGreaterEqual(books.cash(self.con, 1), 0)

    def test_an_unaffordable_stock_is_skipped_not_an_error(self) -> None:
        self.assertEqual(books.buy(self.con, 1, "IN", "manual", "MRF", 200000.0), 0)

    def test_the_book_is_capped_at_six_positions(self) -> None:
        for sym in "ABCDEFGH":
            books.buy(self.con, 1, "IN", "manual", sym, 100.0)
        self.assertEqual(len(books.positions(self.con, 1)), books.MAX_POSITIONS)

    def test_the_same_symbol_is_not_doubled(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(books.buy(self.con, 1, "IN", "manual", "ITC", 300.0), 0)


class CostsMatchTheHouseTest(unittest.TestCase):
    def test_pnl_is_net_of_the_same_costs(self) -> None:
        """A user book reporting gross while the engine reports net would make
        the two incomparable, which defeats running them side by side."""
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        net, pct = books.sell(con, 1, "IN", "ITC", 110.0)
        expected, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertAlmostEqual(net, expected)

    def test_a_breakeven_round_trip_loses_the_costs(self) -> None:
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        net, _ = books.sell(con, 1, "IN", "ITC", 100.0)
        self.assertLess(net, 0)


class MirrorTest(unittest.TestCase):
    """The engine's decision, applied to each subscriber's own cash."""

    class _DB:
        def __init__(self, users):
            self._u = users

        def list_users(self):
            return self._u

    def setUp(self) -> None:
        self.con = _db()
        from app import plans
        self.plans = plans
        self.db = self._DB([
            dict(id=1, active=True, account_plan="paper"),
            dict(id=2, active=True, account_plan="auto"),
            dict(id=3, active=True, account_plan="watch"),   # no paper book
            dict(id=4, active=False, account_plan="auto"),   # inactive
        ])

    def test_only_entitled_active_users_get_the_trade(self) -> None:
        n = books.mirror_entry(self.con, self.db, self.plans, "IN",
                               "swing_meanrev", "ITC", 300.0)
        self.assertEqual(n, 2)
        self.assertEqual(books.open_symbols(self.con, 1), {"ITC"})
        self.assertEqual(books.open_symbols(self.con, 3), set())
        self.assertEqual(books.open_symbols(self.con, 4), set())

    def test_each_book_sizes_to_its_own_cash(self) -> None:
        """Not the house quantity, and not each other's — that is the whole
        point of a personal book. User 1 is spent down below the per-position
        cap so cash becomes the binding constraint for them and not for user 2."""
        # drain book 1 to ~Rs 500 free; book 2 stays whole
        books.buy(self.con, 1, "IN", "manual", "X", 1900.0, shares=5)  # Rs 9,500
        books.mirror_entry(self.con, self.db, self.plans, "IN", "swing_meanrev",
                           "ITC", 300.0)
        p1 = books.positions(self.con, 1)[-1]["shares"]
        p2 = books.positions(self.con, 2)[-1]["shares"]
        self.assertLess(p1, p2)

    def test_the_exit_closes_every_book_holding_it(self) -> None:
        books.mirror_entry(self.con, self.db, self.plans, "IN", "swing_meanrev",
                           "ITC", 300.0)
        n = books.mirror_exit(self.con, None, self.plans, "IN", "ITC", 330.0, "target")
        self.assertEqual(n, 2)
        self.assertEqual(books.open_symbols(self.con, 1), set())
        self.assertEqual(books.open_symbols(self.con, 2), set())

    def test_an_exit_skips_books_that_never_took_it(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        self.assertEqual(
            books.mirror_exit(self.con, None, self.plans, "IN", "ITC", 330.0, "target"), 1)


class StatsTest(unittest.TestCase):
    def test_an_untouched_book_reports_its_full_budget(self) -> None:
        con = _db()
        s = books.stats(con, 9, "IN", {})
        self.assertEqual(s["equity"], 10000.0)
        self.assertEqual(s["positions"], 0)
        self.assertEqual(s["cash"], 10000.0)

    def test_equity_follows_the_live_price(self) -> None:
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        s = books.stats(con, 1, "IN", {"ITC": {"price": 120.0}})
        self.assertAlmostEqual(s["unrealised"], 200.0)
        self.assertAlmostEqual(s["equity"], 10000.0 + 200.0)


if __name__ == "__main__":
    unittest.main()


class EquitySeriesTest(unittest.TestCase):
    """A personal book with no curve does not feel like yours."""

    def setUp(self) -> None:
        self.con = _db()

    def test_a_snapshot_is_recorded_and_read_back(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.snapshot_equity(self.con, 1, "IN", {"ITC": {"price": 330.0}})
        series = books.equity_series(self.con, 1)
        self.assertEqual(len(series), 1)
        self.assertGreater(series[0][1], 10000.0)

    def test_the_same_day_updates_rather_than_accumulates(self) -> None:
        books.snapshot_equity(self.con, 1, "IN", {})
        books.snapshot_equity(self.con, 1, "IN", {})
        self.assertEqual(len(books.equity_series(self.con, 1)), 1)

    def test_snapshots_are_per_user(self) -> None:
        books.buy(self.con, 1, "IN", "manual", "ITC", 300.0)
        books.snapshot_equity(self.con, 1, "IN", {"ITC": {"price": 400.0}})
        books.snapshot_equity(self.con, 2, "IN", {})
        self.assertNotEqual(books.equity_series(self.con, 1)[0][1],
                            books.equity_series(self.con, 2)[0][1])


class NoAppMainImportTest(unittest.TestCase):
    """The engine thread must not import app.main.

    `from .main import db` pulls the whole FastAPI app in on first call. If that
    import is mid-flight or fails, the caller swallows the exception and user
    books silently stop mirroring — a feature that is off with no error
    anywhere.
    """

    @staticmethod
    def _code(fn):
        """Executable lines only. Both of these functions EXPLAIN in prose why
        they avoid `from .main import`, so matching raw source finds the string
        inside the very comment saying it is not used."""
        import inspect
        out = []
        in_doc = False
        for ln in inspect.getsource(fn).splitlines():
            t = ln.strip()
            if t.startswith('"""') or t.endswith('"""'):
                in_doc = not in_doc if t.count('"""') == 1 else in_doc
                continue
            if in_doc or t.startswith("#"):
                continue
            out.append(ln.split("#")[0])
        return "\n".join(out)

    def test_the_engine_hook_does_not_import_main(self) -> None:
        self.assertNotIn("from .main import", self._code(v2_live._book_mirror_entry))

    def test_books_can_reach_the_auth_db_on_its_own(self) -> None:
        self.assertTrue(callable(books._auth_db))
        self.assertNotIn("from .main import", self._code(books._auth_db))
        self.assertIn("Database(", self._code(books._auth_db))


class EveryPageIsScopedTest(unittest.TestCase):
    """No endpoint may still hand the engine's book to a subscriber by default.

    Home was split first, then positions and trades, then orders and stats.
    Attribution and the per-second stream were missed each time — which is the
    pattern: a rule gets applied to the pages someone happened to be looking at,
    and the rest keep the old behaviour until somebody checks.
    """

    def test_every_book_endpoint_defaults_to_the_caller(self) -> None:
        import inspect
        from app import v2_web
        for name in ("api_positions", "api_orders", "api_trades", "api_stats",
                     "api_attribution"):
            with self.subTest(endpoint=name):
                fn = getattr(v2_web, name)
                params = inspect.signature(fn).parameters
                self.assertIn("scope", params, f"{name} has no scope")
                self.assertEqual(params["scope"].default, "mine")
                self.assertIn("_wants_ai", inspect.getsource(fn))

    def test_the_live_stream_carries_the_callers_own_book(self) -> None:
        import inspect
        from app import v2_web
        # the endpoint takes the session user and captures uid once, before the
        # generator starts — the session cannot change mid-stream
        self.assertIn("user", inspect.signature(v2_web.api_stream).parameters)
        self.assertIn("uid = int(user.get", inspect.getsource(v2_web.api_stream))
        self.assertIn("uid=None", inspect.getsource(v2_web._stream_payload))
        self.assertIn("mine=mine", inspect.getsource(v2_web._stream_payload))

    def test_the_personal_book_gets_an_equity_curve(self) -> None:
        import inspect
        from app import v2_web
        src = inspect.getsource(v2_web.api_overview)
        self.assertIn("books.equity_series(", src)
        self.assertIn('mine["series"]', src)


class ConnectionHygieneTest(unittest.TestCase):
    """SQLite allows ONE writer, and the engine is writing the same file.

    Three self-inflicted hazards shipped in a day of fast work:
      * _my_orders and _my_attribution each opened a write connection and then
        called _my_trades, which opened a SECOND one for the same request;
      * books.stats ran ensure_book, which INSERTs — and stats is on the
        per-second stream, so every open dashboard tab opened a write
        transaction once a second;
      * books.buy used INSERT OR IGNORE and returned qty regardless, so a
        blocked insert reported shares that do not exist.
    """

    def test_no_read_path_opens_a_writer(self) -> None:
        import inspect
        from app import v2_web
        for name in ("_my_positions", "_my_orders", "_my_trades",
                     "_my_attribution", "_stream_payload"):
            with self.subTest(fn=name):
                src = inspect.getsource(getattr(v2_web, name))
                self.assertNotIn("_rw()", src, f"{name} opens a write connection")

    def test_my_trades_can_reuse_a_callers_connection(self) -> None:
        import inspect
        from app import v2_web
        self.assertIn("con", inspect.signature(v2_web._my_trades).parameters)
        self.assertIn("con=rw", inspect.getsource(v2_web._my_orders))
        self.assertIn("con=rw", inspect.getsource(v2_web._my_attribution))

    def test_stats_does_not_write(self) -> None:
        """It is called once a second per connected dashboard."""
        import inspect
        src = inspect.getsource(books.stats) + inspect.getsource(books.cash)
        self.assertNotIn("ensure_book(", src)
        self.assertIn("budget_of(", src)

    def test_reading_a_book_that_does_not_exist_creates_nothing(self) -> None:
        con = _db()
        before = con.execute("SELECT COUNT(*) FROM user_book").fetchone()[0]
        st = books.stats(con, 4242, "IN", {})
        self.assertEqual(st["equity"], 10000.0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user_book").fetchone()[0],
                         before, "a read created a row")

    def test_a_blocked_insert_reports_zero_not_a_phantom_buy(self) -> None:
        con = _db()
        con.execute("INSERT INTO user_positions(user_id,market,symbol,entry_price,shares)"
                    " VALUES(1,'IN','ITC',300,5)")
        con.commit()
        # bypass the open_symbols guard to hit the unique index directly
        import app.books as b
        real = b.open_symbols
        b.open_symbols = lambda *a, **k: set()
        try:
            self.assertEqual(b.buy(con, 1, "IN", "manual", "ITC", 300.0), 0)
        finally:
            b.open_symbols = real


class EndOfDaySnapshotTest(unittest.TestCase):
    """A daily curve must be built from daily CLOSES.

    The snapshot lived inside poll_market, which only runs while the market is
    open — so a book's "daily" value was whatever it happened to be mid-session
    on the last cycle before 15:30. A curve built from arbitrary intraday
    moments is not a daily curve, it is noise with dates on it.
    """

    def test_the_engine_snapshots_after_the_close(self) -> None:
        import inspect
        src = inspect.getsource(v2_live.loop)
        self.assertIn("_EOD_SNAP", src)
        self.assertIn("snapshot_all", src)

    def test_it_runs_once_per_day_not_every_cycle(self) -> None:
        """The loop ticks every 8 seconds; without the day key this would
        rewrite every book eight times a minute all night."""
        import inspect
        src = inspect.getsource(v2_live.loop)
        self.assertIn("_EOD_SNAP.get(m) != _today_key()", src)

    def test_the_close_value_overwrites_the_intraday_one(self) -> None:
        con = _db()
        books.buy(con, 1, "IN", "manual", "ITC", 100.0, shares=10)
        books.snapshot_equity(con, 1, "IN", {"ITC": {"price": 150.0}})   # midday
        midday = books.equity_series(con, 1)[0][1]
        books.snapshot_equity(con, 1, "IN", {"ITC": {"price": 110.0}})   # close
        series = books.equity_series(con, 1)
        self.assertEqual(len(series), 1, "one point per day")
        self.assertNotEqual(series[0][1], midday)

    def test_the_writer_is_closed(self) -> None:
        """It runs on the engine thread every 8s once the market shuts; a leaked
        write connection there would hold the SQLite lock all night."""
        import inspect
        src = inspect.getsource(v2_live.loop)
        self.assertIn("eod.close()", src)
