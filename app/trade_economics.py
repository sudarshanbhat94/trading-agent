from __future__ import annotations

import math
import os
from typing import Any

from .market_regions import normalize_market_region


DEFAULT_MIN_AUTO_FOLLOW_NOTIONAL_INR = 7_500.0
DEFAULT_MIN_AUTO_FOLLOW_NOTIONAL_USD = 250.0
DEFAULT_MIN_EXIT_NET_PROFIT_INR = 75.0
DEFAULT_MIN_EXIT_NET_PROFIT_USD = 2.0
DEFAULT_MIN_EXIT_NET_PROFIT_BPS = 15.0


def auto_follow_sizing(
    cash: float,
    price: float,
    *,
    max_position_pct: float,
    size_multiplier: float = 1.0,
    market_region: str | None = "IN",
    settings: Any = None,
) -> dict[str, Any]:
    cash = max(float(cash or 0.0), 0.0)
    price = max(float(price or 0.0), 0.0)
    market = normalize_market_region(market_region or "IN", default="IN")
    if cash <= 0 or price <= 0:
        return {
            "passed": False,
            "amount": 0.0,
            "qty": 0,
            "reason": "missing_cash_or_price",
            "market_region": market,
        }

    size_multiplier = max(min(float(size_multiplier or 1.0), 1.0), 0.10)
    max_pct = max(min(float(max_position_pct or 0.25), 0.50), 0.01)
    target = cash * max_pct * size_multiplier
    cap = cash * min(max_pct * max(size_multiplier, 0.25) * 1.5, 0.60)
    min_notional = minimum_auto_follow_notional(settings, market)
    min_qty = max(1, int(math.ceil(min_notional / price))) if min_notional > 0 else 1
    max_qty_by_cap = int(min(cash, cap) // price)
    target_qty = int(min(cash, target) // price)

    qty = target_qty
    if qty < min_qty and min_qty <= max_qty_by_cap:
        qty = min_qty
    if qty <= 0 and price <= min(cash, cap) and min_qty <= max_qty_by_cap:
        qty = min_qty
    if qty < min_qty:
        return {
            "passed": False,
            "amount": 0.0,
            "qty": 0,
            "reason": "position_size_below_minimum_trade_economics",
            "market_region": market,
            "cash": round(cash, 4),
            "price": round(price, 4),
            "target_notional": round(target, 2),
            "cap_notional": round(cap, 2),
            "minimum_notional": round(min_notional, 2),
            "minimum_qty": min_qty,
        }

    amount = min(qty * price, cash)
    return {
        "passed": True,
        "amount": round(amount, 4),
        "qty": qty,
        "reason": "sized_after_trade_economics",
        "market_region": market,
        "cash": round(cash, 4),
        "price": round(price, 4),
        "target_notional": round(target, 2),
        "cap_notional": round(cap, 2),
        "minimum_notional": round(min_notional, 2),
        "minimum_qty": min_qty,
    }


def entry_size_economics(
    entry_price: float,
    qty: int,
    market_region: str | None = "IN",
    settings: Any = None,
) -> dict[str, Any]:
    market = normalize_market_region(market_region or "IN", default="IN")
    price = max(float(entry_price or 0.0), 0.0)
    qty = max(int(qty or 0), 0)
    notional = price * qty
    minimum = minimum_auto_follow_notional(settings, market)
    return {
        "passed": bool(qty > 0 and price > 0 and notional >= minimum),
        "market_region": market,
        "qty": qty,
        "entry_price": round(price, 4),
        "notional": round(notional, 2),
        "minimum_notional": round(minimum, 2),
    }


def exit_economics(
    entry_price: float,
    exit_price: float,
    qty: int,
    market_region: str | None = "IN",
    settings: Any = None,
) -> dict[str, Any]:
    market = normalize_market_region(market_region or "IN", default="IN")
    qty = max(int(qty or 0), 0)
    entry = max(float(entry_price or 0.0), 0.0)
    exit_ = max(float(exit_price or 0.0), 0.0)
    entry_notional = entry * qty
    exit_notional = exit_ * qty
    gross_pnl = (exit_ - entry) * qty
    costs = round_trip_cost(entry_notional, exit_notional, settings)
    net_pnl = gross_pnl - costs
    minimum_net = minimum_exit_net_profit(settings, market, exit_notional)
    return {
        "passed": bool(qty > 0 and net_pnl >= minimum_net),
        "market_region": market,
        "qty": qty,
        "entry_notional": round(entry_notional, 2),
        "exit_notional": round(exit_notional, 2),
        "gross_pnl": round(gross_pnl, 2),
        "estimated_round_trip_cost": round(costs, 2),
        "estimated_net_pnl": round(net_pnl, 2),
        "minimum_net_profit": round(minimum_net, 2),
        "cost_bps_each_side": round(_one_way_cost_bps(settings), 4),
    }


def should_block_low_value_profit_exit(action_key: str, economics: dict[str, Any]) -> bool:
    key = str(action_key or "").upper()
    if key not in {"TARGET_1_PARTIAL", "TARGET_2_REDUCE", "MFE_BREAKEVEN_REDUCE", "MFE_PROFIT_PROTECT"}:
        return False
    gross_pnl = float(economics.get("gross_pnl") or 0.0)
    return gross_pnl > 0 and not bool(economics.get("passed"))


def minimum_auto_follow_notional(settings: Any = None, market_region: str | None = "IN") -> float:
    market = normalize_market_region(market_region or "IN", default="IN")
    if market == "US":
        return _setting(settings, "paper_min_auto_follow_notional_usd", DEFAULT_MIN_AUTO_FOLLOW_NOTIONAL_USD)
    return _setting(settings, "paper_min_auto_follow_notional_inr", DEFAULT_MIN_AUTO_FOLLOW_NOTIONAL_INR)


def minimum_exit_net_profit(settings: Any = None, market_region: str | None = "IN", exit_notional: float = 0.0) -> float:
    market = normalize_market_region(market_region or "IN", default="IN")
    absolute = (
        _setting(settings, "paper_min_exit_net_profit_usd", DEFAULT_MIN_EXIT_NET_PROFIT_USD)
        if market == "US"
        else _setting(settings, "paper_min_exit_net_profit_inr", DEFAULT_MIN_EXIT_NET_PROFIT_INR)
    )
    bps = _setting(settings, "paper_min_exit_net_profit_bps", DEFAULT_MIN_EXIT_NET_PROFIT_BPS)
    return max(float(absolute or 0.0), max(float(exit_notional or 0.0), 0.0) * max(float(bps or 0.0), 0.0) / 10_000)


def round_trip_cost(entry_notional: float, exit_notional: float, settings: Any = None) -> float:
    one_way = _one_way_cost_bps(settings) / 10_000
    return max(float(entry_notional or 0.0), 0.0) * one_way + max(float(exit_notional or 0.0), 0.0) * one_way


def _one_way_cost_bps(settings: Any = None) -> float:
    return (
        _setting(settings, "brokerage_bps", 0.0)
        + _setting(settings, "slippage_bps", 5.0)
        + _setting(settings, "taxes_bps", 1.0)
        + _setting(settings, "stt_bps", 10.0)
    )


def _setting(settings: Any, name: str, default: float) -> float:
    value = getattr(settings, name, None) if settings is not None else None
    if value is None:
        value = os.getenv(name.upper())
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
