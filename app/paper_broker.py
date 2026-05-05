from __future__ import annotations

from typing import Any

from .config import Settings
from .db import Database
from .models import Decision, Quote, utc_now
from .order_router import OrderRouter


class PaperBroker:
    def __init__(self, settings: Settings, db: Database, order_router: OrderRouter | None = None) -> None:
        self.settings = settings
        self.db = db
        self.order_router = order_router
        if self.db.get_state("cash") is None:
            self.db.set_state("cash", settings.initial_cash_inr)

    @property
    def cash(self) -> float:
        return float(self.db.get_state("cash", self.settings.initial_cash_inr))

    def positions_by_symbol(self) -> dict[str, dict[str, Any]]:
        return {row["symbol"]: row for row in self.db.positions()}

    def sync_marks(self, quotes: dict[str, Quote]) -> None:
        with self.db.connect() as conn:
            for symbol, quote in quotes.items():
                conn.execute(
                    "update positions set market_price = ?, updated_at = ? where symbol = ?",
                    (quote.price, utc_now(), symbol),
                )

    def execute(self, decision: Decision, portfolio_equity: float) -> bool:
        if decision.action == "HOLD":
            return False
        if self._daily_loss_limit_hit(portfolio_equity):
            self.db.insert_order(
                decision.symbol,
                decision.action,
                0,
                decision.price,
                "VETOED",
                "daily loss limit reached",
                decision.strategy,
            )
            return False

        if decision.action == "BUY":
            return self._buy(decision, portfolio_equity)
        if decision.action == "SELL":
            return self._sell(decision)
        return False

    def snapshot(self) -> dict[str, float]:
        positions = self.db.positions()
        cash = self.cash
        invested = sum(row["qty"] * row["avg_price"] for row in positions)
        market_value = sum(row["qty"] * row["market_price"] for row in positions)
        realized = sum(row["realized_pnl"] for row in positions)
        unrealized = market_value - invested
        equity = cash + market_value
        row = {
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                insert into portfolio_snapshots
                    (ts, cash, invested, market_value, equity, realized_pnl, unrealized_pnl)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    row["cash"],
                    row["invested"],
                    row["market_value"],
                    row["equity"],
                    row["realized_pnl"],
                    row["unrealized_pnl"],
                ),
            )
        return row

    def _buy(self, decision: Decision, portfolio_equity: float) -> bool:
        positions = self.db.positions()
        if len(positions) >= self.settings.max_positions:
            self.db.insert_order(decision.symbol, "BUY", 0, decision.price, "VETOED", "max positions reached", decision.strategy)
            return False

        current_value = 0.0
        for row in positions:
            if row["symbol"] == decision.symbol:
                current_value = row["qty"] * row["market_price"]
                break
        max_position_value = portfolio_equity * self.settings.max_position_pct
        if current_value >= max_position_value:
            self.db.insert_order(decision.symbol, "BUY", 0, decision.price, "VETOED", "max position size reached", decision.strategy)
            return False

        max_order_value = portfolio_equity * self.settings.max_order_value_pct
        cash_before = self.cash
        spend = min(max_order_value, max_position_value - current_value, cash_before)
        qty = int(spend // decision.price)
        if qty <= 0:
            self.db.insert_order(decision.symbol, "BUY", 0, decision.price, "VETOED", "insufficient cash", decision.strategy)
            return False

        with self.db.connect() as conn:
            existing = conn.execute(
                "select * from positions where symbol = ?",
                (decision.symbol,),
            ).fetchone()
            if existing:
                new_qty = existing["qty"] + qty
                new_avg = ((existing["qty"] * existing["avg_price"]) + (qty * decision.price)) / new_qty
                conn.execute(
                    """
                    update positions
                    set strategy = ?, qty = ?, avg_price = ?, market_price = ?, updated_at = ?
                    where symbol = ?
                    """,
                    (decision.strategy, new_qty, new_avg, decision.price, utc_now(), decision.symbol),
                )
            else:
                conn.execute(
                    """
                    insert into positions (symbol, strategy, qty, avg_price, market_price, realized_pnl, updated_at)
                    values (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (decision.symbol, decision.strategy, qty, decision.price, decision.price, utc_now()),
                )
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(cash_before - (qty * decision.price)),),
            )
        self.db.insert_order(decision.symbol, "BUY", qty, decision.price, "FILLED", decision.reason, decision.strategy)
        if self.order_router:
            self.order_router.route(decision, qty)
        return True

    def _sell(self, decision: Decision) -> bool:
        cash_before = self.cash
        with self.db.connect() as conn:
            row = conn.execute("select * from positions where symbol = ?", (decision.symbol,)).fetchone()
            if not row or row["qty"] <= 0:
                self.db.insert_order(decision.symbol, "SELL", 0, decision.price, "VETOED", "no long position", decision.strategy)
                return False

            qty = int(row["qty"])
            strategy = row["strategy"] or decision.strategy
            proceeds = qty * decision.price
            realized = row["realized_pnl"] + (decision.price - row["avg_price"]) * qty
            conn.execute(
                """
                update positions
                set qty = 0, market_price = ?, realized_pnl = ?, updated_at = ?
                where symbol = ?
                """,
                (decision.price, realized, utc_now(), decision.symbol),
            )
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(cash_before + proceeds),),
            )
        self.db.insert_order(decision.symbol, "SELL", qty, decision.price, "FILLED", decision.reason, strategy)
        if self.order_router:
            routed_decision = Decision(
                symbol=decision.symbol,
                action=decision.action,
                confidence=decision.confidence,
                price=decision.price,
                technical_score=decision.technical_score,
                sentiment_score=decision.sentiment_score,
                reason=decision.reason,
                asof=decision.asof,
                strategy=strategy,
            )
            self.order_router.route(routed_decision, qty)
        return True

    def _daily_loss_limit_hit(self, equity: float) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                select equity from portfolio_snapshots
                where substr(ts, 1, 10) = substr(?, 1, 10)
                order by id asc limit 1
                """,
                (utc_now(),),
            ).fetchone()
        if row is None:
            return False
        start_equity = float(row["equity"])
        if start_equity <= 0:
            return False
        drawdown = (start_equity - equity) / start_equity
        return drawdown >= self.settings.daily_loss_limit_pct
