"""Recorded P&L must be net of costs, like the cash ledger already was.

Exits credited `cash` net of both sides' costs but wrote the GROSS move into
v2_trades. Equity was therefore honest while everything derived from the trade
table — realised P&L, win rate, profit factor, per-lane attribution — was
flattered by exactly the cost of trading. The dashboard headline overstated the
edge; equity quietly disagreed with it.

Three exit paths each carried their own copy of the arithmetic (the engine's
exit_monitor and two manual-sell endpoints), which is how they drifted apart.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone

from app import v2_live


class NetTradePnlTest(unittest.TestCase):
    def test_costs_are_charged_on_both_sides(self) -> None:
        """Round trip: charges apply to the buy AND the sell notional.

        No longer a flat percentage — brokerage is Rs 20 a leg and the DP fee
        Rs 20 a sell, neither of which scales, so the schedule is computed in
        app/costs.py from Upstox's published rates."""
        from app import costs
        net, _pct = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        expected = 10 * (110.0 - 100.0) - costs.round_trip(1000.0, 1100.0, costs.DELIVERY)
        self.assertAlmostEqual(net, expected)

    def test_a_small_ticket_costs_proportionally_far_more(self) -> None:
        """THE reason the flat model had to go: Rs 20 a leg is 2.58% of a
        Rs 3,000 delivery trade and 0.36% of a Rs 50,000 one."""
        from app import costs
        self.assertGreater(costs.round_trip_pct(3000), 2.0)
        self.assertLess(costs.round_trip_pct(50000), 0.5)

    def test_intraday_costs_a_fraction_of_delivery(self) -> None:
        from app import costs
        self.assertLess(costs.round_trip(9000, product=costs.INTRADAY),
                        costs.round_trip(9000, product=costs.DELIVERY) / 3)

    def test_net_is_always_below_gross(self) -> None:
        gross = 10 * (110.0 - 100.0)
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertLess(net, gross)

    def test_a_loss_is_made_worse_not_better(self) -> None:
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 90.0)
        self.assertLess(net, 10 * (90.0 - 100.0))

    def test_a_marginal_winner_can_become_a_net_loser(self) -> None:
        """The case that corrupts win rate: a move smaller than the round trip
        is a LOSS. Reported gross it counted as a win."""
        net, pct = v2_live.net_trade_pnl("IN", 100, 100.0, 100.1)
        self.assertLess(net, 0)
        self.assertLess(pct, 0)

    def test_a_breakeven_exit_is_a_loss_of_exactly_the_costs(self) -> None:
        from app import costs
        net, _ = v2_live.net_trade_pnl("IN", 10, 100.0, 100.0)
        self.assertAlmostEqual(net, -costs.round_trip(1000.0, 1000.0, costs.DELIVERY))

    def test_return_pct_is_net_and_on_the_entry_basis(self) -> None:
        net, pct = v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)
        self.assertAlmostEqual(pct, net / (10 * 100.0) * 100)

    def test_zero_size_does_not_divide_by_zero(self) -> None:
        self.assertEqual(v2_live.net_trade_pnl("IN", 0, 100.0, 110.0)[1], 0.0)

    def test_zero_entry_price_does_not_divide_by_zero(self) -> None:
        self.assertEqual(v2_live.net_trade_pnl("IN", 10, 0.0, 110.0)[1], 0.0)

    def test_us_uses_its_own_lower_cost(self) -> None:
        self.assertGreater(v2_live.net_trade_pnl("US", 10, 100.0, 110.0)[0],
                           v2_live.net_trade_pnl("IN", 10, 100.0, 110.0)[0])

    def test_an_unknown_market_charges_nothing_rather_than_crashing(self) -> None:
        net, _ = v2_live.net_trade_pnl("XX", 10, 100.0, 110.0)
        self.assertAlmostEqual(net, 100.0)


class SingleDefinitionTest(unittest.TestCase):
    """All three exit paths go through ONE writer, so the cost math cannot
    drift. Previously they shared the helper but each built its own INSERT —
    which is how rows written before the helper landed still carry gross P&L,
    including a -91.18 loss stored as a +14.20 WIN.

    The invariant is now stronger than "they call net_trade_pnl": there is no
    hand-written INSERT into v2_trades left to get wrong, and record_exit
    computes the costs itself so a caller cannot pass a gross figure at all.
    """

    def test_there_is_exactly_one_writer(self) -> None:
        import pathlib
        root = pathlib.Path(inspect.getfile(v2_live)).parent
        inserts = sum((root / n).read_text(encoding="utf-8").count("INSERT INTO v2_trades")
                      for n in ("v2_live.py", "v2_web.py"))
        self.assertEqual(inserts, 1, "v2_trades must only be written by record_exit")
        self.assertIn("INSERT INTO v2_trades", inspect.getsource(v2_live.record_exit))

    def test_every_exit_path_routes_through_it(self) -> None:
        import pathlib
        root = pathlib.Path(inspect.getfile(v2_live)).parent
        self.assertIn("record_exit(", inspect.getsource(v2_live.exit_monitor))
        web = (root / "v2_web.py").read_text(encoding="utf-8")
        # ONE now, not two: manual sell moved to the caller's OWN book
        # (books.sell), leaving only the operator-only house exit here. A
        # subscriber's sell must never write v2_trades.
        self.assertEqual(web.count("record_exit("), 1,
                         "only the house exit may use the single writer")
        self.assertEqual(web.count("from .v2_live import record_exit"), 1)
        self.assertNotIn("net_trade_pnl(market", web,
                         "the web layer must not compute costs itself any more")

    def test_the_writer_computes_costs_itself(self) -> None:
        """A caller cannot hand it a gross number even by mistake — that is the
        whole point of moving the arithmetic inside."""
        src = inspect.getsource(v2_live.record_exit)
        self.assertIn("net_trade_pnl(", src)
        params = inspect.signature(v2_live.record_exit).parameters
        for leaked in ("pnl", "net", "return_pct", "net_pct"):
            self.assertNotIn(leaked, params, f"record_exit must not accept {leaked}")

    def test_no_exit_path_still_writes_a_gross_figure(self) -> None:
        """`shares * (px - entry)` written straight into v2_trades is the bug."""
        import pathlib
        root = pathlib.Path(inspect.getfile(v2_live)).parent
        for name in ("v2_live.py", "v2_web.py"):
            src = (root / name).read_text(encoding="utf-8")
            for bad in ("shares * (px - entry), (px / entry - 1) * 100",
                        "shares * (ex - p[\"entry\"]), (ex / p[\"entry\"] - 1) * 100"):
                with self.subTest(file=name, pattern=bad):
                    self.assertNotIn(bad, src)



class BookSeparationTest(unittest.TestCase):
    """The options book is funded separately, and the reporting must agree.

    INDEX_OPTIONS carries its own budget so a bad options week cannot shrink the
    sizing of the lane that has a measured edge. That ring-fence lived in the
    ENGINE and not in the reporting: _market_stats counted every position and
    every trade for the market, so option P&L landed in the equity book. On
    2026-08-03 two BANKNIFTY winners put +Rs 26,956 into a book that never
    funded them, and the hero card read +27.44% on the day off an equity
    strategy that had not made it.
    """

    def _stats(self, rows_positions, rows_trades):
        import sqlite3
        from app import v2_live, v2_web
        con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(con)
        for strat, sym, entry, shares in rows_positions:
            v2_live.record_entry(con, "IN", strat, sym, "2026-08-03", entry, shares,
                                 entry * 0.9, entry * 1.5, 0.0, 0.5, None)
        for strat, sym, pnl in rows_trades:
            # return_pct must follow the SIGN of pnl, or a loser is recorded as
            # a win and the win-rate assertion tests nothing
            # closed_at is REQUIRED: _market_stats scopes realised P&L to the
            # book epoch on this timestamp, and a real exit always writes it.
            # A row without one predates the column and is legacy by
            # definition, so omitting it here made the fixture unrepresentative.
            con.execute("INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,"
                        "exit_date,exit_price,shares,pnl,return_pct,reason,closed_at)"
                        " VALUES('IN',?,?,'2026-08-03',100,'2026-08-03',110,10,?,?,'target',?)",
                        (strat, sym, pnl, 10.0 if pnl > 0 else -10.0,
                         datetime.now(timezone.utc).isoformat()))
        con.commit()
        live = {sym: {"price": entry} for _s, sym, entry, _sh in rows_positions}
        out = v2_web._market_stats(con, "IN", 100000.0, live)
        con.close()
        return out

    def test_option_positions_are_not_counted_as_stocks(self) -> None:
        s = self._stats([("swing_meanrev", "ITC", 287.0, 92.0),
                         ("index_options", "BANKNIFTY26AUG57400CE", 991.85, 30.0)], [])
        self.assertEqual(s["positions"], 1, "the option is not a stock in the equity book")

    def test_option_profits_do_not_inflate_the_equity_book(self) -> None:
        """The exact shape of the 2026-08-03 report."""
        s = self._stats([], [("index_options", "BANKNIFTY26AUG57400CE", 17906.0),
                             ("volume_surge", "ITC", -500.0)])
        self.assertAlmostEqual(s["overall_pnl"], -500.0, places=2)
        self.assertNotIn(17906.0, [round(s["overall_pnl"], 2)])

    def test_option_trades_are_out_of_the_equity_win_rate(self) -> None:
        s = self._stats([], [("index_options", "X", 9000.0), ("volume_surge", "Y", -100.0)])
        self.assertEqual(s["trades"], 1, "only the equity trade counts")
        self.assertEqual(round(s["win"]), 0, "the one equity trade lost; the option win is not ours")

    def test_the_equity_lanes_are_untouched(self) -> None:
        s = self._stats([("swing_meanrev", "ITC", 287.0, 92.0)],
                        [("volume_surge", "Y", 250.0)])
        self.assertEqual(s["positions"], 1)
        self.assertEqual(s["trades"], 1)
        self.assertAlmostEqual(s["overall_pnl"], 250.0, places=2)

    def test_the_options_book_reports_its_own_equity(self) -> None:
        """Marked to market, not at cost — a book valued at cost hides exactly
        the move the position was opened for."""
        from app import v2_web
        book = v2_web._options_book()
        self.assertIn("options_equity", book)
        self.assertIn("options_value", book)

if __name__ == "__main__":
    unittest.main()
