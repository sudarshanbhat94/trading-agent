from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .db import Database
from .models import Decision, Quote, utc_now
from .order_router import OrderRouter
from .trading_rules import capital_position_limit


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
        cash_before = self.cash
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
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(cash_after),),
            )
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
        cash_before = self.cash
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
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(cash_before + net_proceeds),),
            )
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
        cash_before = self.cash
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
            conn.execute(
                """
                insert into agent_state (key, value) values ('cash', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(cash_before + net_proceeds),),
            )
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
