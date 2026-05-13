from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .db import Database
from .market_regions import market_region_for_row, normalize_market_region
from .models import Decision, Quote, utc_now
from .order_router import OrderRouter
from .trading_rules import capital_position_limit

MARKET_REGIONS = ("IN", "US")


class PaperBroker:
    def __init__(self, settings: Settings, db: Database, order_router: OrderRouter | None = None) -> None:
        self.settings = settings
        self.db = db
        self.order_router = order_router
        if self.db.get_state("cash_by_market") is None:
            self.db.set_state("cash_by_market", self._bootstrap_cash_by_market())
        self._sync_legacy_cash_state()

    @property
    def cash(self) -> float:
        return round(sum(self.cash_by_market().values()), 6)

    def cash_by_market(self) -> dict[str, float]:
        raw = self.db.get_state("cash_by_market", None)
        if not isinstance(raw, dict):
            raw = self._bootstrap_cash_by_market()
            self.db.set_state("cash_by_market", raw)
        cash_map: dict[str, float] = {}
        for market in MARKET_REGIONS:
            value = raw.get(market, self.settings.initial_cash_inr)
            try:
                cash_map[market] = float(value)
            except (TypeError, ValueError):
                cash_map[market] = float(self.settings.initial_cash_inr)
        return cash_map

    def cash_for_market(self, market_region: str) -> float:
        market = normalize_market_region(market_region, default="IN")
        if market == "BOTH":
            return self.cash
        return self.cash_by_market().get(market, float(self.settings.initial_cash_inr))

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
        market_region = self._market_region_from_decision(decision)
        market_equity = float(self.portfolio_for_market(market_region).get("equity", portfolio_equity) or portfolio_equity)
        daily_loss = self._daily_loss_status(portfolio_equity)
        if daily_loss["hit"]:
            self.db.insert_order(
                decision.symbol,
                decision.action,
                0,
                decision.price,
                "VETOED",
                "daily loss limit reached",
                decision.strategy,
                _order_details_json(decision, {"daily_loss": daily_loss, "portfolio_equity": portfolio_equity}),
            )
            return False

        if decision.action == "BUY":
            return self._buy(decision, market_equity)
        if decision.action == "SELL":
            return self._sell(decision)
        return False

    def snapshot(self) -> dict[str, float]:
        positions = self.db.positions()
        portfolio_by_market = self.portfolio_by_market(positions)
        cash = sum(float(row["cash"]) for row in portfolio_by_market.values())
        invested = sum(float(row["invested"]) for row in portfolio_by_market.values())
        market_value = sum(float(row["market_value"]) for row in portfolio_by_market.values())
        realized = sum(float(row["realized_pnl"]) for row in portfolio_by_market.values())
        unrealized = sum(float(row["unrealized_pnl"]) for row in portfolio_by_market.values())
        equity = sum(float(row["equity"]) for row in portfolio_by_market.values())
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
        self.db.set_state("portfolio_by_market", portfolio_by_market)
        self.db.set_state("cash_by_market", self.cash_by_market())
        return row

    def portfolio_by_market(self, positions: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
        positions = positions if positions is not None else self.db.positions()
        return {market: self.portfolio_for_market(market, positions) for market in MARKET_REGIONS}

    def portfolio_for_market(
        self,
        market_region: str,
        positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        market = normalize_market_region(market_region, default="IN")
        if market == "BOTH":
            market = "IN"
        positions = positions if positions is not None else self.db.positions()
        market_positions = [row for row in positions if _position_market(row) == market]
        cash = self.cash_for_market(market)
        invested = sum(float(row["qty"]) * float(row["avg_price"]) for row in market_positions)
        market_value = sum(float(row["qty"]) * float(row["market_price"]) for row in market_positions)
        realized = sum(float(row["realized_pnl"]) for row in market_positions)
        unrealized = market_value - invested
        return {
            "market_region": market,
            "currency": "USD" if market == "US" else "INR",
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
        }

    def _buy(self, decision: Decision, portfolio_equity: float) -> bool:
        if self.settings.llm_decision_mode == "primary" and self.settings.llm_provider != "offline":
            approval = _llm_primary_approval_from_decision(decision)
            if not approval["approved"]:
                self.db.insert_order(
                    decision.symbol,
                    "BUY",
                    0,
                    decision.price,
                    "VETOED",
                    "llm_primary_approval_required",
                    decision.strategy,
                    _order_details_json(decision, {"veto_gate": "llm_primary_approval_required", "llm_primary_approval": approval}),
                )
                return False

        market_region = self._market_region_from_decision(decision)
        all_positions = self.db.positions()
        positions = [row for row in all_positions if _position_market(row) == market_region]
        dynamic_position_limit = min(self.settings.max_positions, capital_position_limit(portfolio_equity))
        if len(positions) >= dynamic_position_limit:
            self.db.insert_order(
                decision.symbol,
                "BUY",
                0,
                decision.price,
                "VETOED",
                "max positions reached",
                decision.strategy,
                _order_details_json(
                    decision,
                    {
                        "veto_gate": "max_positions",
                        "open_positions": len(positions),
                        "max_positions": dynamic_position_limit,
                        "settings_max_positions": self.settings.max_positions,
                        "capital_pool_position_limit": capital_position_limit(portfolio_equity),
                        "portfolio_equity": portfolio_equity,
                    },
                ),
            )
            return False

        sector_veto = self._sector_concentration_veto(decision, positions, portfolio_equity)
        if sector_veto["veto"]:
            self.db.insert_order(
                decision.symbol,
                "BUY",
                0,
                decision.price,
                "VETOED",
                "sector_concentration_limit_35pct",
                decision.strategy,
                _order_details_json(decision, sector_veto),
            )
            return False

        current_value = 0.0
        for row in positions:
            if row["symbol"] == decision.symbol:
                current_value = row["qty"] * row["market_price"]
                break
        sizing_grade = _sizing_grade_from_decision(decision)
        rule_audit = _rule_audit_from_decision(decision)
        if rule_audit.get("hard_blocked"):
            self.db.insert_order(
                decision.symbol,
                "BUY",
                0,
                decision.price,
                "VETOED",
                "system_rule_hard_block",
                decision.strategy,
                _order_details_json(decision, {"veto_gate": "system_rule_hard_block", "system_gate_audit": rule_audit}),
            )
            return False
        max_position_pct = sizing_grade.get("recommended_max_position_pct") or self.settings.max_position_pct
        allocation_cap = rule_audit.get("allocation_cap_multiplier")
        if allocation_cap is not None:
            max_position_pct = min(float(max_position_pct), self.settings.max_position_pct * float(allocation_cap))
        absolute_cap = self.settings.max_position_pct * 1.5
        max_position_pct = min(float(max_position_pct), absolute_cap)
        max_position_value = portfolio_equity * max_position_pct
        if current_value >= max_position_value:
            self.db.insert_order(
                decision.symbol,
                "BUY",
                0,
                decision.price,
                "VETOED",
                "max position size reached",
                decision.strategy,
                _order_details_json(
                    decision,
                    {
                        "veto_gate": "max_position_pct",
                        "current_position_value": round(current_value, 2),
                        "max_position_value": round(max_position_value, 2),
                        "max_position_pct": max_position_pct,
                        "sizing_grade": sizing_grade,
                        "portfolio_equity": portfolio_equity,
                    },
                ),
            )
            return False

        max_order_value = portfolio_equity * self.settings.max_order_value_pct
        cash_before = self.cash_for_market(market_region)
        sizing_plan = _sizing_plan_from_decision(decision, portfolio_equity, max_order_value)
        spend = min(max_order_value, max_position_value - current_value, cash_before, sizing_plan["max_notional"])
        fill_price = _paper_fill_price(decision.price, "BUY", self.settings)
        unit_cash_required = fill_price * (1 + (_fee_bps(self.settings) / 10_000))
        qty = min(int(spend // unit_cash_required), int(sizing_plan["risk_qty"]))
        if qty <= 0:
            self.db.insert_order(
                decision.symbol,
                "BUY",
                0,
                decision.price,
                "VETOED",
                "insufficient cash",
                decision.strategy,
                _order_details_json(
                    decision,
                    {
                        "veto_gate": "available_cash",
                        "cash_before": round(cash_before, 2),
                        "max_order_value": round(max_order_value, 2),
                        "atr_risk_sizing": sizing_plan,
                        "max_position_value_remaining": round(max_position_value - current_value, 2),
                        "planned_spend": round(spend, 2),
                        "price": decision.price,
                        "fill_price_after_slippage": round(fill_price, 4),
                        "unit_cash_required_with_costs": round(unit_cash_required, 4),
                    },
                ),
            )
            return False
        gross_notional = qty * fill_price
        estimated_costs = _trade_cost(gross_notional, self.settings)
        cash_after = cash_before - gross_notional - estimated_costs

        cash_by_market = self.cash_by_market()
        cash_by_market[market_region] = cash_after
        with self.db.connect() as conn:
            existing = conn.execute(
                "select * from positions where symbol = ?",
                (decision.symbol,),
            ).fetchone()
            if existing:
                new_qty = existing["qty"] + qty
                new_avg = ((existing["qty"] * existing["avg_price"]) + (qty * fill_price)) / new_qty
                conn.execute(
                    """
                    update positions
                    set strategy = ?, qty = ?, avg_price = ?, market_price = ?, updated_at = ?, details_json = ?
                    where symbol = ?
                    """,
                    (decision.strategy, new_qty, new_avg, fill_price, utc_now(), _position_details_json(decision), decision.symbol),
                )
            else:
                conn.execute(
                    """
                    insert into positions (symbol, strategy, qty, avg_price, market_price, realized_pnl, updated_at, details_json)
                    values (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (decision.symbol, decision.strategy, qty, fill_price, fill_price, utc_now(), _position_details_json(decision)),
                )
            self._persist_cash_by_market(conn, cash_by_market)
        self.db.insert_order(
            decision.symbol,
            "BUY",
            qty,
            fill_price,
            "FILLED",
            decision.reason,
            decision.strategy,
            _order_details_json(
                decision,
                {
                    "risk_checks": {
                        "max_positions_passed": len(positions) < self.settings.max_positions,
                        "max_position_pct_passed": current_value < max_position_value,
                        "cash_available": cash_before >= decision.price,
                    },
                    "sizing": {
                        "portfolio_equity": round(portfolio_equity, 2),
                        "market_region": market_region,
                        "cash_before": round(cash_before, 2),
                        "cash_after": round(cash_after, 2),
                        "current_position_value_before": round(current_value, 2),
                        "max_position_pct": max_position_pct,
                        "max_position_value": round(max_position_value, 2),
                        "max_order_value_pct": self.settings.max_order_value_pct,
                        "max_order_value": round(max_order_value, 2),
                        "atr_risk_sizing": sizing_plan,
                        "sizing_grade": sizing_grade,
                        "planned_spend": round(spend, 2),
                        "filled_qty": qty,
                        "decision_price": decision.price,
                        "filled_notional": round(gross_notional, 2),
                        "estimated_costs": round(estimated_costs, 2),
                        "cost_model": _cost_model(self.settings),
                    },
                },
            ),
        )
        if self.order_router:
            self.order_router.route(decision, qty)
        return True

    def _sell(self, decision: Decision) -> bool:
        partial_pct = _partial_sell_pct_from_decision(decision)
        if partial_pct is not None and 0 < partial_pct < 1:
            return self.partial_sell(decision.symbol, partial_pct, decision.reason, decision.strategy, decision)
        market_region = self._market_region_for_symbol(decision.symbol)
        cash_before = self.cash_for_market(market_region)
        cash_by_market = self.cash_by_market()
        with self.db.connect() as conn:
            row = conn.execute("select * from positions where symbol = ?", (decision.symbol,)).fetchone()
            if not row or row["qty"] <= 0:
                self.db.insert_order(
                    decision.symbol,
                    "SELL",
                    0,
                    decision.price,
                    "VETOED",
                    "no long position",
                    decision.strategy,
                    _order_details_json(decision, {"veto_gate": "no_long_position"}),
                )
                return False

            qty = int(row["qty"])
            strategy = row["strategy"] or decision.strategy
            fill_price = _paper_fill_price(decision.price, "SELL", self.settings)
            proceeds = qty * fill_price
            estimated_costs = _trade_cost(proceeds, self.settings)
            net_proceeds = proceeds - estimated_costs
            realized = row["realized_pnl"] + (fill_price - row["avg_price"]) * qty - estimated_costs
            conn.execute(
                """
                update positions
                set qty = 0, market_price = ?, realized_pnl = ?, updated_at = ?
                where symbol = ?
                """,
                (fill_price, realized, utc_now(), decision.symbol),
            )
            cash_by_market[market_region] = cash_before + net_proceeds
            self._persist_cash_by_market(conn, cash_by_market)
        self.db.insert_order(
            decision.symbol,
            "SELL",
            qty,
            fill_price,
            "FILLED",
            decision.reason,
            strategy,
            _order_details_json(
                decision,
                {
                    "risk_checks": {"long_position_exists": True},
                    "sizing": {
                        "cash_before": round(cash_before, 2),
                        "cash_after": round(cash_before + net_proceeds, 2),
                        "market_region": market_region,
                        "filled_qty": qty,
                        "avg_price": round(float(row["avg_price"]), 2),
                        "decision_price": decision.price,
                        "filled_price": fill_price,
                        "filled_notional": round(proceeds, 2),
                        "estimated_costs": round(estimated_costs, 2),
                        "cost_model": _cost_model(self.settings),
                        "realized_pnl_after": round(realized, 2),
                    },
                },
            ),
        )
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
                details_json=decision.details_json,
            )
            self.order_router.route(routed_decision, qty)
        return True

    def partial_sell(
        self,
        symbol: str,
        pct_of_position: float,
        reason: str,
        strategy: str,
        decision: Decision | None = None,
    ) -> bool:
        pct = max(min(float(pct_of_position), 1.0), 0.0)
        market_region = self._market_region_from_decision(decision) if decision else self._market_region_for_symbol(symbol)
        cash_before = self.cash_for_market(market_region)
        cash_by_market = self.cash_by_market()
        price = float(decision.price if decision else 0.0)
        fill_price = _paper_fill_price(price, "SELL", self.settings) if price > 0 else 0.0
        with self.db.connect() as conn:
            row = conn.execute("select * from positions where symbol = ?", (symbol,)).fetchone()
            if not row or row["qty"] <= 0 or fill_price <= 0:
                self.db.insert_order(symbol, "SELL", 0, price, "VETOED", "partial sell unavailable", strategy, "{}")
                return False
            qty = max(int(int(row["qty"]) * pct), 1)
            qty = min(qty, int(row["qty"]))
            remaining = int(row["qty"]) - qty
            proceeds = qty * fill_price
            estimated_costs = _trade_cost(proceeds, self.settings)
            net_proceeds = proceeds - estimated_costs
            realized = row["realized_pnl"] + (fill_price - row["avg_price"]) * qty - estimated_costs
            details = _json_object(row["details_json"])
            if pct <= 0.34 and not details.get("tier1_hit"):
                details["tier1_hit"] = True
                details["trailing_stop"] = row["avg_price"]
            elif pct <= 0.34:
                details["tier2_hit"] = True
            conn.execute(
                """
                update positions
                set qty = ?, market_price = ?, realized_pnl = ?, updated_at = ?, details_json = ?
                where symbol = ?
                """,
                (remaining, fill_price, realized, utc_now(), json.dumps(details, default=str, separators=(",", ":")), symbol),
            )
            cash_by_market[market_region] = cash_before + net_proceeds
            self._persist_cash_by_market(conn, cash_by_market)
        self.db.insert_order(
            symbol,
            "SELL",
            qty,
            fill_price,
            "FILLED",
            reason,
            strategy,
            _order_details_json(
                decision
                or Decision(symbol, "SELL", 0.99, price, 0.0, 0.0, reason, utc_now(), strategy),
                {
                    "partial_sell": True,
                    "pct_of_position": pct,
                    "cash_before": round(cash_before, 2),
                    "cash_after": round(cash_before + net_proceeds, 2),
                    "market_region": market_region,
                    "remaining_qty": remaining,
                    "filled_qty": qty,
                    "filled_notional": round(proceeds, 2),
                    "decision_price": price,
                    "estimated_costs": round(estimated_costs, 2),
                    "cost_model": _cost_model(self.settings),
                },
            ),
        )
        return True

    def _sector_concentration_veto(self, decision: Decision, positions: list[dict[str, Any]], portfolio_equity: float) -> dict[str, Any]:
        if portfolio_equity <= 0:
            return {"veto": False}
        try:
            audit = json.loads(decision.details_json or "{}")
        except json.JSONDecodeError:
            audit = {}
        context = audit.get("context") or {}
        sector = context.get("sector")
        rule_audit = audit.get("system_gate_audit") or context.get("system_gate_audit") or {}
        if "SECTOR_MISSING" in (rule_audit.get("active_flags") or []):
            return {"veto": False, "skipped": True, "reason": "sector_missing_excluded_from_sector_concentration"}
        if not sector:
            return {"veto": False}
        exposure = 0.0
        for row in positions:
            universe_row = self.db.universe_row(row["symbol"]) or {}
            if universe_row.get("sector") == sector:
                exposure += float(row["qty"]) * float(row["market_price"])
        pct = exposure / portfolio_equity
        return {
            "veto": pct > 0.35,
            "veto_gate": "sector_concentration_limit_35pct",
            "sector": sector,
            "sector_exposure": round(exposure, 2),
            "sector_exposure_pct": round(pct, 4),
            "limit_pct": 0.35,
        }

    def _daily_loss_limit_hit(self, equity: float) -> bool:
        return self._daily_loss_status(equity)["hit"]

    def _daily_loss_status(self, equity: float) -> dict[str, Any]:
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
            return {
                "hit": False,
                "reason": "no starting equity snapshot for today",
                "current_equity": round(equity, 2),
                "daily_loss_limit_pct": self.settings.daily_loss_limit_pct,
            }
        start_equity = float(row["equity"])
        if start_equity <= 0:
            return {
                "hit": False,
                "reason": "starting equity is not positive",
                "start_equity": start_equity,
                "current_equity": round(equity, 2),
                "daily_loss_limit_pct": self.settings.daily_loss_limit_pct,
            }
        drawdown = (start_equity - equity) / start_equity
        return {
            "hit": drawdown >= self.settings.daily_loss_limit_pct,
            "start_equity": round(start_equity, 2),
            "current_equity": round(equity, 2),
            "drawdown_pct": round(drawdown, 6),
            "daily_loss_limit_pct": self.settings.daily_loss_limit_pct,
        }

    def _bootstrap_cash_by_market(self) -> dict[str, float]:
        initial_cash = float(self.settings.initial_cash_inr)
        positions = self.db.positions()
        if not positions:
            legacy_cash = self.db.get_state("cash", None)
            try:
                india_cash = float(legacy_cash) if legacy_cash is not None else initial_cash
            except (TypeError, ValueError):
                india_cash = initial_cash
            return {"IN": round(india_cash, 6), "US": round(initial_cash, 6)}
        invested = {market: 0.0 for market in MARKET_REGIONS}
        for row in positions:
            market = _position_market(row)
            invested[market] += float(row["qty"]) * float(row["avg_price"])
        return {
            market: round(max(initial_cash - invested.get(market, 0.0), 0.0), 6)
            for market in MARKET_REGIONS
        }

    def _sync_legacy_cash_state(self) -> None:
        self.db.set_state("cash", self.cash)

    def _persist_cash_by_market(self, conn: Any, cash_by_market: dict[str, float]) -> None:
        normalized = {
            market: round(float(cash_by_market.get(market, self.settings.initial_cash_inr) or 0.0), 6)
            for market in MARKET_REGIONS
        }
        combined = round(sum(normalized.values()), 6)
        conn.execute(
            """
            insert into agent_state (key, value) values ('cash_by_market', ?)
            on conflict(key) do update set value = excluded.value
            """,
            (json.dumps(normalized),),
        )
        conn.execute(
            """
            insert into agent_state (key, value) values ('cash', ?)
            on conflict(key) do update set value = excluded.value
            """,
            (json.dumps(combined),),
        )

    def _market_region_from_decision(self, decision: Decision) -> str:
        details = _json_object(decision.details_json)
        context = details.get("context") or {}
        market = (
            context.get("market_region")
            or details.get("market_region")
            or (details.get("decision") or {}).get("market_region")
        )
        if market:
            return normalize_market_region(market, default="IN")
        return self._market_region_for_symbol(decision.symbol)

    def _market_region_for_symbol(self, symbol: str) -> str:
        row = self.db.universe_row(symbol) or {}
        return normalize_market_region(row.get("market_region"), default=_position_market(row))

 
def _position_market(row: dict[str, Any]) -> str:
    explicit = row.get("market_region")
    if explicit:
        return normalize_market_region(explicit, default="IN")
    return normalize_market_region(market_region_for_row(row), default="IN")


def _order_details_json(decision: Decision, execution: dict[str, Any]) -> str:
    return json.dumps(
        {
            "audit_version": 1,
            "decision": _decision_summary(decision),
            "execution": execution,
        },
        default=str,
        separators=(",", ":"),
    )


def _sizing_plan_from_decision(decision: Decision, portfolio_equity: float, max_order_value: float) -> dict[str, Any]:
    try:
        details = json.loads(decision.details_json or "{}")
    except json.JSONDecodeError:
        details = {}
    full = (details.get("context") or {}).get("full_spectrum_analysis") or {}
    trade_plan = full.get("trade_plan") or {}
    sizing = trade_plan.get("position_sizing") or {}
    stop = _float_or_none(trade_plan.get("stop_loss"))
    risk_pct = _float_or_none(sizing.get("max_capital_at_risk_pct"))
    risk_pct = min(max(risk_pct if risk_pct is not None else 0.01, 0.0025), 0.02)
    risk_budget = portfolio_equity * risk_pct
    risk_per_share = max(decision.price - stop, decision.price * 0.005) if stop and stop < decision.price else decision.price * 0.02
    risk_qty = max(int(risk_budget // risk_per_share), 1)
    max_notional = min(max_order_value, risk_qty * decision.price)
    return {
        "method": "atr_stop_risk",
        "stop_loss": round(stop, 4) if stop else None,
        "risk_pct": round(risk_pct, 4),
        "risk_budget": round(risk_budget, 2),
        "risk_per_share": round(risk_per_share, 4),
        "risk_qty": risk_qty,
        "max_notional": round(max_notional, 2),
    }


def _sizing_grade_from_decision(decision: Decision) -> dict[str, Any]:
    details = _json_object(decision.details_json)
    sizing = (
        details.get("sizing_grade")
        or (details.get("risk_gates") or {}).get("sizing_grade")
        or (details.get("context") or {}).get("sizing_grade")
        or {}
    )
    return sizing if isinstance(sizing, dict) else {}


def _rule_audit_from_decision(decision: Decision) -> dict[str, Any]:
    details = _json_object(decision.details_json)
    audit = (
        details.get("system_gate_audit")
        or (details.get("risk_gates") or {}).get("system_gate_audit")
        or (details.get("context") or {}).get("system_gate_audit")
        or {}
    )
    return audit if isinstance(audit, dict) else {}


def _partial_sell_pct_from_decision(decision: Decision) -> float | None:
    details = _json_object(decision.details_json)
    gates = details.get("risk_gates") or {}
    value = gates.get("partial_sell_pct") or details.get("partial_sell_pct")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _llm_primary_approval_from_decision(decision: Decision) -> dict[str, Any]:
    details = _json_object(decision.details_json)
    risk_gates = details.get("risk_gates") or {}
    confidence_gate = details.get("confidence_gate") or {}
    approved = (
        details.get("decision_path") == "llm_primary"
        and details.get("final_action") == "BUY"
        and decision.action == "BUY"
        and not details.get("llm_error")
        and not details.get("json_synthetic")
        and not details.get("llm_timeout")
        and confidence_gate.get("passed", True) is not False
        and risk_gates.get("llm_policy_gates_passed", True) is not False
    )
    return {
        "approved": bool(approved),
        "decision_path": details.get("decision_path"),
        "final_action": details.get("final_action"),
        "provider": details.get("provider"),
        "model": details.get("model"),
        "confidence_gate_passed": confidence_gate.get("passed"),
        "policy_gates_passed": risk_gates.get("llm_policy_gates_passed"),
        "llm_error_present": bool(details.get("llm_error")),
        "reason": "completed_llm_primary_buy" if approved else "BUY requires a completed LLM primary decision with all LLM policy gates passed.",
    }


def _position_details_json(decision: Decision) -> str:
    details = _json_object(decision.details_json)
    full = (details.get("context") or {}).get("full_spectrum_analysis") or {}
    return json.dumps(
        {
            "opened_from_decision": decision.to_dict(),
            "trade_plan": full.get("trade_plan") or {},
            "sizing_grade": details.get("sizing_grade") or {},
            "system_gate_audit": details.get("system_gate_audit") or {},
            "tier1_hit": False,
            "tier2_hit": False,
        },
        default=str,
        separators=(",", ":"),
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_fill_price(price: float, side: str, settings: Settings) -> float:
    slippage = max(float(settings.slippage_bps or 0.0), 0.0) / 10_000
    multiplier = 1 + slippage if side.upper() == "BUY" else 1 - slippage
    return round(max(float(price) * multiplier, 0.01), 4)


def _fee_bps(settings: Settings) -> float:
    return (
        max(float(settings.brokerage_bps or 0.0), 0.0)
        + max(float(settings.taxes_bps or 0.0), 0.0)
        + max(float(settings.stt_bps or 0.0), 0.0)
    )


def _trade_cost(notional: float, settings: Settings) -> float:
    return max(float(notional), 0.0) * (_fee_bps(settings) / 10_000)


def _cost_model(settings: Settings) -> dict[str, float]:
    return {
        "brokerage_bps": float(settings.brokerage_bps or 0.0),
        "slippage_bps": float(settings.slippage_bps or 0.0),
        "taxes_bps": float(settings.taxes_bps or 0.0),
        "stt_bps": float(settings.stt_bps or 0.0),
        "fee_bps_charged_on_notional": round(_fee_bps(settings), 4),
    }


def _decision_summary(decision: Decision) -> dict[str, Any]:
    data = decision.to_dict()
    raw_details = data.pop("details_json", "{}")
    try:
        data["details"] = json.loads(raw_details or "{}")
    except json.JSONDecodeError:
        data["details_json"] = raw_details
    return data
