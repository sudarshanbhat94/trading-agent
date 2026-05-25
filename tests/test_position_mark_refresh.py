from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.models import Quote


class PositionMarkRefreshTests(unittest.TestCase):
    def test_active_follow_marks_refresh_from_latest_quote_without_touching_last_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            with db.connect() as conn:
                conn.execute(
                    """
                    insert into universe (symbol, name, exchange, upstox_instrument_key, enabled)
                    values ('ENTERO', 'Entero Healthcare', 'NSE', 'NSE_EQ|INE010601016', 1)
                    """
                )
                conn.execute(
                    """
                    insert into signal_ideas (
                        id, first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                        entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                        confidence, combined_score, confluence, overall_score_pct, overall_grade,
                        decision_id, latest_decision_id, reason, details_json
                    )
                    values (
                        1, '2026-05-25T04:00:00+00:00', '2026-05-25T04:00:00+00:00',
                        'ENTERO', 'darvas_box_breakout', 'breakout', 'BUY', 'ACTIVE',
                        100, 100, 0, 0, 0, 0.8, 0.8, 20, 90, 'A',
                        null, null, 'active buy', ?
                    )
                    """,
                    (json.dumps({"targets": [{"label": "T1", "price": 110}], "stop_loss": 95}),),
                )
                conn.execute(
                    """
                    insert into user_idea_follows (
                        user_id, idea_id, mode, status, qty, entry_price, latest_price,
                        invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                    )
                    values (
                        2, 1, 'PAPER', 'ACTIVE', 2, 100, 100,
                        200, 0, 0, '2026-05-25T04:01:00+00:00', '2026-05-25T04:01:00+00:00', '{}'
                    )
                    """
                )

            db.upsert_quotes(
                {
                    "ENTERO": Quote(
                        symbol="ENTERO",
                        price=105,
                        source="upstox-live",
                        asof="2026-05-25T04:02:00+00:00",
                    )
                }
            )
            marked = db.refresh_active_position_marks(["ENTERO"])

            with db.connect() as conn:
                idea = conn.execute("select * from signal_ideas where symbol = 'ENTERO'").fetchone()
                follow = conn.execute("select * from user_idea_follows where idea_id = 1").fetchone()

        self.assertEqual(marked, 1)
        self.assertEqual(idea["latest_price"], 105)
        self.assertEqual(idea["current_return_pct"], 5)
        self.assertEqual(idea["last_seen_at"], "2026-05-25T04:00:00+00:00")
        self.assertEqual(follow["latest_price"], 105)
        self.assertEqual(follow["unrealized_pnl"], 10)
        self.assertEqual(follow["return_pct"], 5)

    def test_active_position_universe_is_market_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "agent.db")
            db.init()
            with db.connect() as conn:
                conn.executemany(
                    "insert into universe (symbol, name, exchange, enabled) values (?, ?, ?, 1)",
                    [("ENTERO", "Entero", "NSE"), ("DIA", "Dow ETF", "ARCA")],
                )
                for idx, symbol in enumerate(("ENTERO", "DIA"), start=1):
                    conn.execute(
                        """
                        insert into signal_ideas (
                            id, first_seen_at, last_seen_at, symbol, strategy, plan_code, signal_type, status,
                            entry_price, latest_price, current_return_pct, peak_return_pct, worst_return_pct,
                            confidence, combined_score, confluence, overall_score_pct, overall_grade,
                            decision_id, latest_decision_id, reason, details_json
                        )
                        values (?, '2026-05-25T04:00:00+00:00', '2026-05-25T04:00:00+00:00', ?, 'test', 'test',
                                'BUY', 'ACTIVE', 100, 100, 0, 0, 0, 0.8, 0.8, 20, 90, 'A', null, null, 'active', '{}')
                        """,
                        (idx, symbol),
                    )
                    conn.execute(
                        """
                        insert into user_idea_follows (
                            user_id, idea_id, mode, status, qty, entry_price, latest_price,
                            invested_amount, unrealized_pnl, return_pct, created_at, updated_at, details_json
                        )
                        values (2, ?, 'PAPER', 'ACTIVE', 1, 100, 100, 100, 0, 0,
                                '2026-05-25T04:01:00+00:00', '2026-05-25T04:01:00+00:00', '{}')
                        """,
                        (idx,),
                    )

            india_rows = db.active_position_universe("IN")
            us_rows = db.active_position_universe("US")
            db.upsert_quotes(
                {
                    "ENTERO": Quote(
                        symbol="ENTERO",
                        price=106,
                        source="upstox-live",
                        asof="2026-05-25T04:02:00+00:00",
                    )
                }
            )
            db.refresh_active_position_marks(["ENTERO"])
            with db.connect() as conn:
                follows = {
                    row["symbol"]: row
                    for row in conn.execute(
                        """
                        select i.symbol, f.latest_price, f.updated_at
                        from user_idea_follows f
                        join signal_ideas i on i.id = f.idea_id
                        """
                    ).fetchall()
                }

        self.assertEqual([row["symbol"] for row in india_rows], ["ENTERO"])
        self.assertEqual([row["symbol"] for row in us_rows], ["DIA"])
        self.assertEqual(follows["ENTERO"]["latest_price"], 106)
        self.assertEqual(follows["DIA"]["latest_price"], 100)
        self.assertEqual(follows["DIA"]["updated_at"], "2026-05-25T04:01:00+00:00")


if __name__ == "__main__":
    unittest.main()
