"""Account and execution invariants reproduced in the September audit."""
import sqlite3
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from app import v2_live, live_trade, broker, order_journal
from app.sleeves.config import SleeveSettings
from app.sleeves.risk import BookState, RiskManager
from app.sleeves.base import Candidate
from app.sleeves.feeds import fresh_quotes


class AuditRegressionTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(self.con)
        self.addCleanup(self.con.close)

    def test_deployment_room_includes_all_prior_allocations(self):
        cfg = SleeveSettings()
        cfg.max_deployed = .5
        cfg.min_ticket = 0
        book = BookState(10000, 6000, 4000, 2, {"mean_reversion": 2},
                         10000, 10000, 0, {"mean_reversion": 4000})
        got = RiskManager(cfg).allocate([Candidate("X", "early_momentum", .9, 100, 95, 120)], book)
        self.assertEqual(sum(a.notional for a in got), 1000)

    def test_exact_daily_loss_boundary_and_open_risk(self):
        rm = RiskManager()
        b = BookState(10000, 9850, 0, 0, {}, 9850, 10000, -150)
        self.assertTrue(rm.halted(b)[0])
        b.day_pnl = 0
        b.open_risk = 150
        self.assertFalse(rm.size(Candidate("X", "mean_reversion", .9, 100, 95, 120), b).ok)

    def test_later_session_old_low_does_not_cross_new_trail(self):
        p = dict(strategy="mean_reversion", entry=100., shares=20., stop=90., target=130.,
                 trail=.05, peak=100., edate="2026-09-14", expiry=None)
        with patch.object(v2_live, "trading_days_held", return_value=1):
            q = dict(price=110., high=110., low=99.)
            peak, eff, ex, reason = v2_live.evaluate_exit(p, q, None, date(2026,9,15), "2026-09-15", "IN")
            self.assertIsNone(ex)
            p["peak"] = peak
            q["price"] = 104
            self.assertEqual(v2_live.evaluate_exit(p,q,None,date(2026,9,15),"2026-09-15","IN")[2],104)

    def test_actual_epoch_pnl_normalizes_offsets(self):
        self.con.execute("UPDATE v2_book SET started_at=? WHERE market='IN'", ("2026-08-14T21:34:16+00:00",))
        self.con.executemany("INSERT INTO v2_trades(market,pnl,closed_at) VALUES('IN',?,?)",
                             [(-500,"2026-08-14T22:00:00+05:30"),(25,"2026-08-15T04:00:00+05:30")])
        self.assertEqual(v2_live._epoch_pnl(self.con, "IN"),25)

    def test_freshness_rejects_missing_old_future_and_nonfinite(self):
        now = datetime(2026,9,15,5,tzinfo=timezone.utc)
        quotes = {"ok":dict(price=100,ts=now.isoformat()), "missing":dict(price=100),
                  "old":dict(price=100,ts="2026-09-14T05:00:00Z"),
                  "future":dict(price=100,ts="2026-09-16T05:00:00Z"),
                  "nan":dict(price=float('nan'),ts=now.isoformat())}
        self.assertEqual(set(fresh_quotes(quotes,now)), {"ok"})

    def test_exit_monitor_snapshot_matches_ledger_and_next_heartbeat(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d,"paper.db")
            con = sqlite3.connect(path)
            v2_live.ensure_schema(con)
            today = datetime.now(v2_live.IST).date().isoformat()
            con.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,stop,target,trail,peak) "
                        "VALUES('IN','mean_reversion','X',?,100,20,95,120,0,100)", (today,))
            con.commit(); con.close()
            with patch.object(v2_live,"_rw",side_effect=lambda:sqlite3.connect(path)), \
                 patch.object(v2_live,"_live",return_value={"X":dict(price=94,high=100,low=94)}), \
                 patch.object(v2_live,"_option_live",return_value={}), \
                 patch.object(v2_live,"_session_opens",return_value={}), \
                 patch.object(v2_live,"_stale_symbols",return_value=set()), \
                 patch.object(v2_live,"_live_mirror_exit"), \
                 patch.object(v2_live,"_book_mirror_exit"), \
                 patch("app.telegram_bot.notify_trade"), patch.dict(v2_live._EQ_SNAP,{},clear=True):
                v2_live.exit_monitor("IN")
                con = sqlite3.connect(path)
                expected = 10000 + v2_live._epoch_pnl(con,"IN")
                self.assertAlmostEqual(con.execute("SELECT equity FROM v2_equity ORDER BY date DESC LIMIT 1").fetchone()[0],expected)
                con.close()
                v2_live._EQ_SNAP.clear()
                v2_live.exit_monitor("IN")
                con = sqlite3.connect(path)
                self.assertAlmostEqual(con.execute("SELECT cash FROM v2_equity ORDER BY date DESC LIMIT 1").fetchone()[0],expected)
                con.close()

    def test_daily_risk_includes_marked_losses(self):
        from app.sleeves.accounting import session_pnl
        self.con.execute("UPDATE v2_book SET started_at='2026-09-01T00:00:00Z'")
        self.con.execute("INSERT INTO v2_equity(market,date,equity) VALUES('IN','LIVE_2026-09-14T10:00:00',10000)")
        self.assertEqual(session_pnl(self.con,"IN",9800,"2026-09-15","2026-09-01T00:00:00Z",10000,True),-200)

    def test_budget_zero_is_not_defaulted(self):
        with patch.object(broker,"_read",return_value={"budget":0}):
            self.assertEqual(broker.state(1)["budget"],0)

    def test_schema_upgrade_never_changes_book_epoch_or_capital(self):
        self.con.execute("UPDATE v2_book SET budget=12500,max_pos=2,started_at='2026-09-01T00:00:00Z' WHERE market='IN'")
        v2_live.ensure_schema(self.con)
        self.assertEqual(self.con.execute("SELECT budget,max_pos,started_at FROM v2_book WHERE market='IN'").fetchone(),
                         (12500,2,"2026-09-01T00:00:00Z"))


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(self.con)
        self.addCleanup(self.con.close)
        self.ready = patch.object(broker,"state",return_value={"live_ready":True,"budget":10000})
        self.ready.start()
        self.addCleanup(self.ready.stop)

    def submit(self, side="BUY", outcome=None):
        with patch.object(broker,"place_order",return_value=outcome or {"ok":True,"order_id":side}) as send:
            got = order_journal.submit(self.con,1,"IN","X","NSE_EQ|TEST",side,10,100,"D","test")
            return got,send.call_count

    def update(self, filled, side="BUY", status="complete", uid=1):
        return order_journal.reconcile(self.con, uid, [dict(order_id=side,filled_quantity=filled,
            average_price=100,status=status,instrument_token="NSE_EQ|TEST",transaction_type=side,product="D")])

    def test_acceptance_partial_fill_and_sell_remain_distinct(self):
        self.assertEqual(self.submit()[0], "submitted")
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),0)
        self.update(4,status="open")
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),4)
        self.update(2,status="open")
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),4)
        self.update(10)
        self.submit("SELL")
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),10)
        self.update(10,"SELL")
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),0)

    def test_duplicate_request_and_cross_user_snapshot_do_not_change_exposure(self):
        self.submit()
        self.assertEqual(self.submit()[1],0)
        self.update(10,uid=2)
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),0)

    def test_timeout_persists_unknown_and_does_not_retry(self):
        with patch.object(broker,"place_order",side_effect=TimeoutError):
            self.assertEqual(order_journal.submit(self.con,1,"IN","X","NSE_EQ|TEST","BUY",10,100,"D","test"),"unknown")
        self.assertEqual(self.submit()[1],0)
        tag=self.con.execute("SELECT intent_key FROM v2_live_orders").fetchone()[0]
        order_journal.reconcile(self.con,1,[dict(order_id="recovered",tag=tag,filled_quantity=10,
            average_price=100,status="complete",instrument_token="NSE_EQ|TEST",transaction_type="BUY",product="D")])
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),10)

    def test_rejected_exit_keeps_holdings_and_durable_exit_request(self):
        self.submit(); self.update(10)
        order_journal.request_exit(self.con,1,"X","stop")
        self.submit("SELL",{"ok":False,"status":400})
        self.assertEqual(live_trade.live_qty(self.con,1,"X"),10)
        self.assertEqual(self.con.execute("SELECT exit_reason FROM v2_live_protection").fetchone()[0],"stop")

    def test_terminal_order_cannot_reopen_on_delayed_ack(self):
        self.submit(); self.update(10)
        self.update(0,status="open")
        self.assertEqual(self.con.execute("SELECT status FROM v2_live_orders").fetchone()[0],"filled")
