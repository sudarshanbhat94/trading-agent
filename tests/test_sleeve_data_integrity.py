"""Production-boundary tests: no network, real orders or production book writes."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app import bars5m, v2_live, v2_web
from app.sleeves import reference
from app.sleeves.base import Candidate, SleeveDecision
from app.sleeves.engine import SleeveEngine
from app.sleeves.feeds import delivery_reader
from app.sleeves.regime import RegimeView
from app.sleeves.risk import BookState


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "reference.db")
        self.con = sqlite3.connect(self.path)
        self.addCleanup(self.con.close)
        self.now = datetime(2026, 9, 15, tzinfo=timezone.utc)

    def data(self, stamp):
        return dict(source="synthetic test provider", known_at=stamp.isoformat(),
                    membership={"NIFTY500": [f"S{i}" for i in range(500)]},
                    fundamentals=[dict(symbol=f"S{i}",roe_pct=i,debt_equity=1,
                                       eps_growth_std_pct=2) for i in range(20)])

    def test_missing_future_and_expired_data_never_become_confirmation(self):
        self.assertEqual(reference.snapshot(self.now, self.path), (None, {}))
        reference.import_snapshot(self.con, self.data(self.now), now=self.now)
        self.assertEqual(reference.snapshot(self.now-timedelta(seconds=1), self.path), (None, {}))
        members, scores = reference.snapshot(self.now, self.path)
        self.assertEqual(len(members),500)
        self.assertEqual(len(scores),20)
        self.assertGreater(scores["S19"],scores["S0"])
        self.assertEqual(reference.snapshot(self.now+timedelta(days=32), self.path), (None, {}))

    def test_invalid_batch_does_not_partially_import(self):
        data = self.data(self.now)
        data["fundamentals"][-1]["roe_pct"] = float("nan")
        with self.assertRaises(ValueError):
            reference.import_snapshot(self.con,data,now=self.now)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],0)
        with self.assertRaises(ValueError):
            reference.import_snapshot(self.con,self.data(self.now+timedelta(days=1)),now=self.now)
        data = self.data(self.now)
        data["membership"]["NIFTY500"] = ["ONE"]
        with self.assertRaises(ValueError):
            reference.import_snapshot(self.con,data,now=self.now)

    def test_csv_quoted_company_names_do_not_shift_symbols(self):
        response = Mock(status_code=200, text='Company Name,Industry,Symbol,Series\n"Example, Limited",Finance,EXAMPLE,EQ\n')
        with patch.object(bars5m.httpx,"Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            self.assertEqual(bars5m.fetch_members(),frozenset({"EXAMPLE"}))

    def test_delivery_reader_uses_only_completed_data(self):
        self.con.execute("CREATE TABLE delivery_data(symbol,date,delivery_pct)")
        self.con.executemany("INSERT INTO delivery_data VALUES('X',?,?)",
                             [("2026-09-11",40),("2026-09-14",60),("2026-09-15",99)])
        self.assertEqual(delivery_reader(self.con,"2026-09-14")("X"),(60,40))
        self.assertEqual(delivery_reader(self.con,"2026-09-16")("X"),(None,None))


class BoundaryTest(unittest.TestCase):
    def test_production_paper_pass_writes_only_funded_sleeves_and_blocks_off(self):
        """Exercise the actual writer, not just the engine's returned candidates."""
        from contextlib import ExitStack
        for regime in ("ON", "OFF"):
            with self.subTest(regime=regime), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                path = str(Path(tmp)/"paper.db")
                con = sqlite3.connect(path)
                v2_live.ensure_schema(con)
                con.close()
                engine = SleeveEngine()
                engine.gate.view = Mock(return_value=RegimeView(regime,True,.7,regime,"test"))
                for name, sleeve in engine.sleeves.items():
                    sleeve.propose = Mock(return_value=SleeveDecision(name,regime,True))
                engine.sleeves["mean_reversion"].propose.return_value.candidates = [
                    Candidate("TEST", "mean_reversion", .9,100,95,120)]
                now = datetime.now(timezone.utc)
                stack.enter_context(patch.object(v2_live,"_SLEEVE_ENGINE",engine))
                stack.enter_context(patch.object(v2_live,"market_open",return_value=True))
                stack.enter_context(patch.object(v2_live,"_rw",side_effect=lambda:sqlite3.connect(path)))
                stack.enter_context(patch.object(v2_live,"_ro",side_effect=lambda _:sqlite3.connect(":memory:")))
                stack.enter_context(patch.object(v2_live,"_live",return_value={"TEST":dict(price=100,ts=now.isoformat())}))
                stack.enter_context(patch.object(v2_live,"_hist",return_value=({"TEST":None},None)))
                stack.enter_context(patch.object(v2_live.eng,"complete_trading_dates",return_value=[now.astimezone(v2_live.IST).date()-timedelta(days=1)]))
                stack.enter_context(patch("app.sleeves.reference.refresh_membership"))
                stack.enter_context(patch("app.sleeves.reference.snapshot",return_value=({"TEST"},{})))
                stack.enter_context(patch.object(v2_live,"_publish_sleeve_ideas"))
                live_mirror = stack.enter_context(patch.object(v2_live,"_live_mirror_entry"))
                stack.enter_context(patch.object(v2_live,"_book_mirror_entry"))
                broker_send = stack.enter_context(patch("app.broker.place_order",side_effect=AssertionError("real broker forbidden in test")))
                v2_live.sleeve_pass("IN")
                con = sqlite3.connect(path)
                rows = con.execute("SELECT symbol,sleeve,regime,shares,entry_price,risk_amt FROM v2_positions").fetchall()
                if regime == "ON":
                    self.assertEqual(len(rows),1)
                    self.assertEqual(rows[0][:3],("TEST","mean_reversion","ON"))
                    self.assertLessEqual(float(rows[0][5]),150)
                    self.assertLessEqual(rows[0][3]*rows[0][4],9000)
                    self.assertEqual(live_mirror.call_count,1)
                else:
                    self.assertEqual(rows,[])
                    live_mirror.assert_not_called()
                self.assertEqual(con.execute("SELECT budget FROM v2_book WHERE market='IN'").fetchone()[0],10000)
                broker_send.assert_not_called()
                con.close()

    def test_production_rejects_stale_and_incomplete_history_before_writing(self):
        now = datetime.now(timezone.utc)
        today = now.astimezone(v2_live.IST).date()
        for dates in ([today], [today-timedelta(days=10)]):
            with self.subTest(dates=dates), \
                 patch.object(v2_live,"market_open",return_value=True), \
                 patch.object(v2_live,"_live",return_value={"TEST":dict(price=100,ts=now.isoformat())}), \
                 patch.object(v2_live,"_hist",return_value=({},None)), \
                 patch.object(v2_live.eng,"complete_trading_dates",return_value=dates), \
                 patch.object(v2_live,"_rw") as writer:
                v2_live.sleeve_pass("IN")
                writer.assert_not_called()

    def test_rejected_candidates_cannot_leak_into_ideas(self):
        engine = SleeveEngine()
        engine.gate.view = Mock(return_value=RegimeView("ON",True,.7,"ON","test"))
        for name, sleeve in engine.sleeves.items():
            sleeve.propose = Mock(return_value=SleeveDecision(name,"ON",True))
        candidates = [Candidate(s,"mean_reversion",.9,100,95,120)
                      for s in ("GOOD","STALE","OUTSIDE")]
        candidates.append(Candidate("INDEX","mean_reversion",.9,100,95,120,instrument="FUT"))
        engine.sleeves["mean_reversion"].propose.return_value.candidates = candidates
        book = BookState(10000,10000,0,0,{},10000,10000,0)
        result = engine.run({s:None for s in ("GOOD","STALE","OUTSIDE")},None,None,
                            {s:{"price":100} for s in ("GOOD","OUTSIDE","INDEX")}, book,
                            require_live_quotes=True, require_reference_data=True,
                            eligible_symbols={"GOOD","STALE"}, routable_instruments=("EQ",))
        self.assertEqual([c.symbol for c in result.decisions[0].candidates],["GOOD"])
        self.assertEqual(len(result.decisions[0].rejected),3)
        ctx = engine.sleeves["mean_reversion"].propose.call_args.args[0]
        self.assertEqual(set(ctx.tails),{"GOOD"})
        self.assertEqual([a.candidate.symbol for a in result.allocations],["GOOD"])
        with patch.object(v2_live,"market_open",return_value=True), \
             patch("app.ideas.track"), patch("app.ideas.publish",return_value=0) as publish:
            con = sqlite3.connect(":memory:")
            self.addCleanup(con.close)
            v2_live.ensure_schema(con)
            v2_live._publish_sleeve_ideas(con,"IN",result,"2026-09-15",{})
            self.assertEqual([r["symbol"] for r in publish.call_args.args[2]],["GOOD"])

    def test_overview_curves_exclude_old_epoch_even_when_below_three_times_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"paper.db")
            con = sqlite3.connect(path)
            v2_live.ensure_schema(con)
            now = datetime.now(timezone.utc)
            epoch = now-timedelta(minutes=2)
            con.execute("UPDATE v2_book SET started_at=? WHERE market='IN'",(epoch.isoformat(),))
            con.execute("DELETE FROM v2_equity")
            con.executemany("INSERT INTO v2_equity(market,date,equity) VALUES('IN',?,?)",[
                ("LIVE_"+(epoch-timedelta(days=1)).isoformat(),9811.99),
                ("LIVE_"+(epoch-timedelta(seconds=1)).isoformat(),25000),
                ("LIVE_"+now.isoformat(),10000)])
            con.commit(); con.close()
            with patch.object(v2_web,"V2_DB",path), patch.object(v2_web,"_live_map",return_value={}), \
                 patch.object(v2_web,"_options_book",return_value={}), \
                 patch.object(v2_web,"_regime",return_value=False), \
                 patch.object(v2_web,"_regime_state",return_value="OFF"), \
                 patch.object(v2_web,"_prev_close_map",return_value={}), \
                 patch.object(v2_live,"_option_live",return_value={}), \
                 patch("app.broker.state",return_value={}):
                data = json.loads(v2_web.api_overview({"id":999,"account_plan":"free"}).body)
            market = next(m for m in data["markets"] if m["market"]=="IN")
            self.assertEqual(market["equity_series"],[10000])
            self.assertEqual(market["today_series"],[10000])
            self.assertEqual(market["daily_series"],[10000])
            self.assertEqual(market["prev_equity"],10000)
            self.assertEqual(market["realised"],0)


if __name__ == "__main__":
    unittest.main()
