from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.sector_rotation import _sector_for_row
from app.signal_quality import fresh_buy_quality_gate
from app.trade_economics import auto_follow_sizing, exit_economics
from app.trading_readiness import (
    build_trading_readiness,
    run_replay_validation,
    set_trading_kill_switch,
)


class RealMoneyReadinessTests(unittest.TestCase):
    def _db(self) -> tuple[tempfile.TemporaryDirectory[str], Database, Settings]:
        tmp = tempfile.TemporaryDirectory()
        settings = Settings(database_path=Path(tmp.name) / "test.db")
        db = Database(settings.database_path)
        db.init()
        return tmp, db, settings

    def test_default_readiness_blocks_live_and_allows_paper(self) -> None:
        tmp, db, settings = self._db()
        self.addCleanup(tmp.cleanup)

        readiness = build_trading_readiness(db, settings)

        self.assertEqual(readiness["status"], "PAPER_ONLY")
        self.assertFalse(readiness["live_order_allowed"])
        self.assertTrue(readiness["paper_trading_allowed"])
        self.assertTrue(readiness["kill_switch"]["engaged"])
        self.assertIn("default_real_money_lock", readiness["blocking_reasons"])

    def test_live_readiness_requires_broker_sync_fresh_data_and_holiday_provider(self) -> None:
        tmp, db, base = self._db()
        self.addCleanup(tmp.cleanup)
        settings = replace(
            base,
            execution_mode="upstox_live",
            live_trading_enabled=True,
            live_trading_confirm="I_UNDERSTAND_THIS_PLACES_REAL_ORDERS",
            upstox_access_token="token",
        )
        set_trading_kill_switch(db, engaged=False, reason="test")
        db.set_state("broker_sync_status", {"status": "SYNCED", "connected": True, "last_sync_at": _now_iso(), "provider": "upstox"})

        readiness = build_trading_readiness(db, settings, market_region="IN", now_utc=datetime(2026, 5, 29, 4, 0, tzinfo=timezone.utc))

        self.assertFalse(readiness["live_order_allowed"])
        self.assertIn("hardcoded_holiday_calendar_fallback_only", readiness["blocking_reasons"])
        self.assertIn("quote_missing", readiness["blocking_reasons"])

    def test_insert_order_creates_immutable_trade_audit(self) -> None:
        tmp, db, _settings = self._db()
        self.addCleanup(tmp.cleanup)
        order_id = db.insert_order("TEST", "BUY", 3, 100.0, "VETOED", "test veto", "unit", '{"gate":"x"}')

        audits = db.latest_trade_audit_events()

        self.assertGreater(order_id, 0)
        self.assertEqual(audits[0]["symbol"], "TEST")
        self.assertEqual(audits[0]["event_type"], "order")
        self.assertEqual(audits[0]["qty"], 3)

    def test_qty_zero_invariant_flags_active_paper_follow_only(self) -> None:
        tmp, db, _settings = self._db()
        self.addCleanup(tmp.cleanup)
        with db.connect() as conn:
            conn.execute("insert into universe(symbol, name, exchange, enabled) values ('QZERO','Q Zero','NSE',1)")
            conn.execute(
                """
                insert into signal_ideas(first_seen_at,last_seen_at,symbol,strategy,signal_type,status,entry_price,latest_price,reason)
                values (?,?,?,?,?,?,?,?,?)
                """,
                (_now_iso(), _now_iso(), "QZERO", "unit", "BUY", "ACTIVE", 100, 100, "unit"),
            )
            conn.execute(
                """
                insert into user_idea_follows(user_id,idea_id,mode,status,qty,entry_price,latest_price,invested_amount,created_at,updated_at)
                values (1,1,'PAPER','ACTIVE',0,100,100,0,?,?)
                """,
                (_now_iso(), _now_iso()),
            )

        readiness = build_trading_readiness(db, _settings)

        self.assertFalse(readiness["zero_qty_invariant"]["passed"])
        self.assertEqual(readiness["zero_qty_invariant"]["samples"][0]["symbol"], "QZERO")

    def test_india_cost_model_exposes_real_charge_fields_and_blocks_tiny_profit(self) -> None:
        economics = exit_economics(100.0, 100.2, 10, "IN", Settings())

        self.assertIn("stt", economics["cost_breakdown"])
        self.assertIn("exchange_charges", economics["cost_breakdown"])
        self.assertIn("gst", economics["cost_breakdown"])
        self.assertFalse(economics["passed"])

    def test_sizing_exposes_product_rules_and_underuse_reason(self) -> None:
        sizing = auto_follow_sizing(1000, 950, max_position_pct=0.10, market_region="IN", settings=Settings())

        self.assertFalse(sizing["passed"])
        self.assertEqual(sizing["product_rules"]["lot_size"], 1)
        self.assertTrue(sizing["underuse_reason"])

    def test_sector_mapping_remaps_generic_india_symbols(self) -> None:
        self.assertEqual(_sector_for_row({"symbol": "JPPOWER", "sector": "NSE Listed Equity"}), "Power Generation")
        self.assertEqual(_sector_for_row({"symbol": "FINCABLES", "sector": "Equity"}), "Electrical Equipment")

    def test_buy_truth_check_no_longer_blocks_old_soft_gate_cases(self) -> None:
        base = {
            "signal_type": "BUY",
            "status": "ACTIVE",
            "latest_price": 100,
            "overall_score_pct": 90,
            "overall_grade": "A",
            "confluence": 24,
            "fresh_action": "BUY_NOW",
            "details": {"data_readiness": {"trade_decision_ready": True}},
        }

        uc = fresh_buy_quality_gate({**base, "details": {**base["details"], "opportunity_scan": {"only_buyers": True}}})
        late = fresh_buy_quality_gate({**base, "details": {**base["details"], "opportunity_scan": {"day_gain_pct": 12}}})
        pivot = fresh_buy_quality_gate({**base, "details": {**base["details"], "opportunity_scan": {"pivot_extension_pct": 6}}})
        squeeze = fresh_buy_quality_gate({**base, "details": {**base["details"], "opportunity_scan": {"setup": "short_covering_squeeze"}}})

        self.assertTrue(uc["passed"])
        self.assertTrue(late["passed"])
        self.assertTrue(pivot["passed"])
        self.assertTrue(squeeze["passed"])
        self.assertEqual(uc["reason"], "legacy_entry_gates_removed")

    def test_replay_validation_records_named_symbols_without_llm(self) -> None:
        tmp, db, _settings = self._db()
        self.addCleanup(tmp.cleanup)

        review = run_replay_validation(db, ["CUMMINSIND", "GRRR"])

        self.assertEqual(review["status_counts"]["absent_from_latest_watchlist"], 2)
        self.assertEqual(db.get_state("replay_review_latest")["symbols"], ["CUMMINSIND", "GRRR"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
