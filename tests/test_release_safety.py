"""Safety regression fixtures use isolated SQLite and never broker order APIs."""
import sqlite3
import json
import shutil
import subprocess
import tempfile
import unittest
from tests.contract_storage_fixtures import ContractStorageCase
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from unittest.mock import patch
from pathlib import Path

from app import books, v2_live
from app.exit_policy import ExitPolicy
from app.sleeves.risk import RiskManager, stop_loss_including_costs


def quotes(**prices):
    ts = datetime.now(timezone.utc).isoformat()
    return {symbol: {"price": price, "ts": ts} for symbol, price in prices.items()}


class ReleaseSafetyTest(ContractStorageCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        v2_live.ensure_schema(self.con)
        self.addCleanup(self.con.close)
        self.mirrors = patch.object(books, "subscribers", return_value=[])
        self.mirrors.start()
        self.addCleanup(self.mirrors.stop)
        self.brokers = patch("app.broker.linked_users", return_value=[])
        self.brokers.start()
        self.addCleanup(self.brokers.stop)

    def buy(self, uid=1, symbol="TEST", src_id=None, policy=None):
        return books.buy(self.con, uid, "IN", "manual", symbol, 100,
                         stop=99, target=110, src_id=src_id, exit_policy=policy,
                         quotes=quotes(TEST=100))

    def test_daily_loss_halt_applies_to_manual_entry(self):
        books.ensure_book(self.con, 1)
        epoch = books.current_epoch(self.con, 1)
        self.con.execute("INSERT INTO user_trades(user_id,market,symbol,pnl,return_pct,book_epoch) "
                         "VALUES(1,'IN','PREVIOUS',-200,-2,?)", (epoch,))
        self.con.commit()
        self.assertEqual(self.buy(), 0)
        self.assertIn("daily loss", books.refusal(self.con, 1, "IN"))
        self.assertEqual(books.positions(self.con, 1), [])

    def test_manual_account_uses_after_cost_stop_risk(self):
        qty = self.buy()
        self.assertGreater(qty, 0)
        state, reason = books.risk_state(self.con, 1, quotes=quotes(TEST=100))
        self.assertEqual(reason, "")
        self.assertLessEqual(stop_loss_including_costs(100, 99, qty),
                             10000 * RiskManager().s.daily_loss_limit)
        self.assertLess(books.cash(self.con, 1), 10000 - qty * 100)
        self.assertAlmostEqual(books.stats(self.con, 1, "IN", quotes(TEST=100))["equity"],
                               10000 - books.positions(self.con, 1)[0]["entry_fee"], places=2)

    def test_manual_holding_is_not_closed_by_a_house_symbol_exit(self):
        self.assertGreater(self.buy(), 0)
        self.assertEqual(books.mirror_exit(self.con, None, None, "IN", "TEST", 101,
                                           "target", src_id=7), 0)
        self.assertEqual(len(books.positions(self.con, 1)), 1)

    def test_only_exact_origin_mirrors_close(self):
        self.assertGreater(self.buy(uid=1, src_id=7), 0)
        self.assertGreater(self.buy(uid=2, src_id=8), 0)
        self.assertEqual(books.mirror_exit(self.con, None, None, "IN", "TEST", 101,
                                           "target", src_id=7), 1)
        self.assertEqual(books.positions(self.con, 1), [])
        self.assertEqual(len(books.positions(self.con, 2)), 1)

    def test_unknown_origin_cannot_close_any_personal_position(self):
        self.assertGreater(self.buy(), 0)
        self.assertEqual(books.mirror_exit(self.con, None, None, "IN", "TEST", 101, "target"), 0)
        self.assertEqual(len(books.positions(self.con, 1)), 1)

    def test_monthly_policy_does_not_exit_after_five_sessions(self):
        policy = ExitPolicy.create("index_directional", 75)
        p = dict(strategy="index_directional", entry=100, stop=75, target=0,
                 trail=0, peak=100, shares=10, edate="2026-09-28",
                 exit_policy=policy.encode())
        with patch.object(v2_live, "trading_days_held", return_value=5):
            result = v2_live.evaluate_exit(p, dict(price=100, high=100, low=100),
                                          None, date(2026, 10, 6), "2026-10-06", "IN")
        self.assertEqual(result[2:], (None, None))

    def test_persisted_policy_survives_a_configuration_change(self):
        policy = ExitPolicy.create("early_momentum", 99, 110, max_hold_days=4)
        p = dict(strategy="early_momentum", entry=100, stop=99, target=110,
                 trail=0, peak=100, shares=10, edate="2026-10-01",
                 exit_policy=policy.encode())
        with patch.dict(v2_live.HOLD_DAYS, early_momentum=1), \
                patch.object(v2_live, "trading_days_held", return_value=2):
            result = v2_live.evaluate_exit(p, dict(price=100, high=100, low=100),
                                          None, date(2026, 10, 6), "2026-10-06", "IN")
        self.assertEqual(result[2:], (None, None))

    def test_personal_only_stop_is_managed_without_a_subscription(self):
        self.assertGreater(self.buy(), 0)
        self.assertEqual(books.monitor_positions(self.con, "IN", quotes(TEST=98)), 1)
        self.assertEqual(books.positions(self.con, 1), [])
        self.assertEqual(self.con.execute("SELECT reason FROM user_trades").fetchone()[0], "stop")

    def test_stale_price_does_not_fabricate_a_personal_exit(self):
        self.assertGreater(self.buy(), 0)
        self.assertEqual(books.monitor_positions(self.con, "IN",
                         {"TEST": {"price": 98, "ts": "2020-01-01T00:00:00Z"}}), 0)
        self.assertEqual(len(books.positions(self.con, 1)), 1)

    def test_config_defaults_cannot_implicitly_resize_an_existing_book(self):
        books.ensure_book(self.con, 1)
        epoch = books.current_epoch(self.con, 1)
        self.con.execute("UPDATE user_book SET budget=20000 WHERE user_id=1")
        self.con.commit()
        self.assertEqual(books.ensure_book(self.con, 1), 20000)
        self.assertEqual(books.current_epoch(self.con, 1), epoch)

    def test_daily_snapshot_replacement_cannot_erase_peak(self):
        self.assertGreater(self.buy(), 0)
        books.snapshot_equity(self.con, 1, "IN", quotes(TEST=105))
        first = self.con.execute("SELECT peak_equity FROM account_risk_state").fetchone()[0]
        books.snapshot_equity(self.con, 1, "IN", quotes(TEST=100))
        self.assertEqual(self.con.execute("SELECT peak_equity FROM account_risk_state").fetchone()[0], first)

    def test_existing_chart_peak_blocks_entries_and_survives_replacement(self):
        books.ensure_book(self.con, 1)
        epoch = books.current_epoch(self.con, 1)
        day = epoch[:10]
        self.con.execute("INSERT INTO user_equity(user_id,market,date,equity,cash,positions_value,n_positions) "
                         "VALUES(1,'IN',?,12000,12000,0,0)", (day,))
        self.con.commit()
        self.assertEqual(self.buy(), 0)
        self.assertIn("drawdown", books.refusal(self.con, 1, "IN"))
        books.snapshot_equity(self.con, 1, "IN", {}, day=day)
        self.assertEqual(self.con.execute("SELECT peak_equity FROM account_risk_state").fetchone()[0], 12000)
        self.assertEqual(self.con.execute("SELECT equity FROM user_equity").fetchone()[0], 10000)

    def test_chart_peak_from_before_current_epoch_does_not_contaminate_risk(self):
        books.ensure_book(self.con, 1)
        self.con.execute("INSERT INTO user_equity(user_id,market,date,equity,cash,positions_value,n_positions) "
                         "VALUES(1,'IN','2020-01-01',100000,100000,0,0)")
        self.con.commit()
        self.assertGreater(self.buy(), 0)

    def test_sell_is_once_and_does_not_double_charge_entry_fees(self):
        qty = self.buy()
        result = books.sell(self.con, 1, "IN", "TEST", 110)
        self.assertIsNotNone(result)
        self.assertIsNone(books.sell(self.con, 1, "IN", "TEST", 110))
        self.assertAlmostEqual(books.cash(self.con, 1), 10000 + result[0])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM user_trades").fetchone()[0], 1)

    def test_competing_accounts_requests_cannot_spend_the_same_risk_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            con = sqlite3.connect(path)
            v2_live.ensure_schema(con)
            con.close()
            def enter(symbol):
                db = sqlite3.connect(path, timeout=10)
                try:
                    return books.buy(db, 1, "IN", "manual", symbol, 100, stop=99,
                                     target=110, quotes=quotes(A=100, B=100))
                finally:
                    db.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(enter, ("A", "B")))
            self.assertEqual(sum(q > 0 for q in results), 1)
            with sqlite3.connect(path) as db:
                state, error = books.risk_state(db, 1, quotes=quotes(A=100, B=100))
                self.assertFalse(error)
                self.assertGreaterEqual(state.cash, 0)
                self.assertLessEqual(state.open_risk, 150)

    def test_calendar_failure_refuses_a_manual_order(self):
        from app import v2_web
        with patch.object(v2_live, "market_open", side_effect=RuntimeError("calendar unavailable")):
            response = v2_web._market_shut("IN")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "MARKET_STATE_UNKNOWN")

    def test_failed_migration_does_not_start_an_engine_thread(self):
        with patch.object(v2_live, "_started", False), patch.object(v2_live, "_rw") as rw, \
                patch.object(v2_live, "ensure_schema", side_effect=RuntimeError("migration failed")), \
                patch.object(v2_live.threading, "Thread") as thread:
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                v2_live.start_background()
            self.assertFalse(v2_live._started)
            thread.assert_not_called()
            rw.return_value.close.assert_called_once()

    def test_readiness_preserves_durable_house_peak_after_chart_pruning(self):
        from app import account_safety
        from app.sleeves.readiness import book_readiness
        self.con.execute("UPDATE v2_book SET budget=10000,max_pos=3,started_at=? WHERE market='IN'",
                         (datetime.now(timezone.utc).isoformat(),))
        epoch = self.con.execute("SELECT started_at FROM v2_book WHERE market='IN'").fetchone()[0]
        account_safety.observe(self.con, "house", 0, "IN", epoch, 12000, 10000, "2026-10-06")
        self.con.commit()
        result = book_readiness(self.con, "IN", {})
        self.assertEqual(result["peak"], 12000)
        self.assertTrue(result["halted"])
        self.assertIn("drawdown", result["reason"])

    def test_readiness_refuses_unknown_held_stop(self):
        from app.sleeves.readiness import book_readiness
        now = datetime.now(timezone.utc)
        self.con.execute("UPDATE v2_book SET budget=10000,max_pos=3,started_at=? WHERE market='IN'", (now.isoformat(),))
        self.con.execute("INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,stop) "
                         "VALUES('IN','manual','TEST',?,100,10,NULL)", (now.astimezone(books.IST).date().isoformat(),))
        self.con.commit()
        result = book_readiness(self.con, "IN", quotes(TEST=100))
        self.assertTrue(result["halted"])
        self.assertIn("stop unavailable", result["reason"])

    def test_hot_feed_includes_personal_pending_and_residual_exposure(self):
        from scripts import v2_quote_feed
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            con = sqlite3.connect(path)
            v2_live.ensure_schema(con)
            books.buy(con, 1, "IN", "manual", "PERSONAL", 100, stop=99, target=110)
            con.execute("INSERT INTO v2_live_orders(user_id,market,symbol,side,qty,status,filled_qty) "
                        "VALUES(2,'IN','PENDING','BUY',20,'unknown',0)")
            con.execute("INSERT INTO v2_live_orders(user_id,market,symbol,side,qty,status,filled_qty) "
                        "VALUES(2,'IN','RESIDUAL','BUY',20,'filled',20)")
            con.execute("INSERT INTO v2_live_protection(user_id,symbol,stop) VALUES(2,'PROTECTED',99)")
            con.commit()
            con.close()
            with patch.object(v2_quote_feed, "V2_DB", path):
                hot = v2_quote_feed._held()["IN"]
            self.assertTrue({"PERSONAL", "PENDING", "RESIDUAL", "PROTECTED"} <= hot)

    def test_closed_broker_credential_does_not_authorize_a_disarmed_exit(self):
        from app import broker, order_journal
        with patch.object(broker, "state", return_value={"live_ready": False, "exit_ready": False}), \
                patch.object(broker, "place_order") as send:
            result = order_journal.submit(self.con, 1, "IN", "TEST", "NSE_EQ|TEST",
                                           "SELL", 1, 100, "D", "stop")
        self.assertIn("credentials unavailable", result)
        send.assert_not_called()

    def test_filing_text_and_symbols_cannot_become_html_or_inline_handlers(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable; DOM renderer acceptance remains unverified")
        source = (Path(__file__).resolve().parents[1] / "app/v2_web.py").read_text()
        code = source[source.index("function loadCatalysts(){"):source.index("function toast(t){")]
        payload = {"symbol": "TEST');globalThis.pwned=1;//", "kind": "<svg onload=attack()>",
                   "subject": '<img src=x onerror="attack()">'}
        harness = '''const assert=require('assert');
class Element {
 constructor(){this.children=[];this.style={};this.textContent='';this.events={};}
 set innerHTML(value){throw Error('unsafe HTML sink');}
 replaceChildren(){this.children=[];}
 appendChild(value){this.children.push(value);}
 addEventListener(name,fn){this.events[name]=fn;}
}
const root=new Element();
const document={getElementById:()=>root,createElement:()=>new Element()};
let selected=null;function stock(sym,mkt){selected=[sym,mkt];}
function api(){return Promise.resolve({ok:true,j:[PAYLOAD]});}
'''.replace("PAYLOAD", json.dumps(payload))
        harness += code + '''\nloadCatalysts();
setImmediate(()=>{
 assert.strictEqual(root.children.length,1);
 const content=root.children[0].children[0];
 assert.strictEqual(content.children[2].textContent,PAYLOAD.subject);
 root.children[0].events.click();
 assert.strictEqual(selected[0],PAYLOAD.symbol);
 assert.strictEqual(globalThis.pwned,undefined);
});'''.replace("PAYLOAD", json.dumps(payload))
        result = subprocess.run([node, "-e", harness], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
