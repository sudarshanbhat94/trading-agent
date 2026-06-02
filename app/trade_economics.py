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
    stop_loss: float | None = None,
    confidence: float | None = None,
    avg_daily_turnover: float | None = None,
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
            "product_rules": product_rules(settings, market),
        }

    size_multiplier = max(min(float(size_multiplier or 1.0), 1.0), 0.10)
    max_pct = max(min(float(max_position_pct or 0.25), 0.50), 0.01)
    confidence_multiplier = _confidence_multiplier(confidence)
    target = cash * max_pct * size_multiplier * confidence_multiplier
    min_notional = minimum_auto_follow_notional(settings, market)
    base_cap_pct = min(max_pct * max(size_multiplier, 0.25) * 1.5, 0.60)
    cap = cash * base_cap_pct
    liquidity_cap = None
    if avg_daily_turnover is not None and float(avg_daily_turnover or 0.0) > 0:
        liquidity_cap = max(float(avg_daily_turnover or 0.0) * 0.01, min_notional)
        cap = min(cap, liquidity_cap)
    risk_qty = None
    stop = max(float(stop_loss or 0.0), 0.0)
    if stop > 0 and stop < price:
        risk_budget_pct = _setting(settings, "paper_risk_per_trade_pct", 0.01)
        risk_budget = cash * max(min(float(risk_budget_pct or 0.01), 0.05), 0.001) * max(size_multiplier, 0.10)
        per_share_risk = max(price - stop, price * 0.005)
        risk_qty = max(int(risk_budget // per_share_risk), 0)
    min_qty = max(1, int(math.ceil(min_notional / price))) if min_notional > 0 else 1
    economics_floor_notional = min_qty * price
    economics_floor_applied = False
    if economics_floor_notional > cap and economics_floor_notional <= cash * 0.60:
        cap = economics_floor_notional
        economics_floor_applied = True
    max_qty_by_cap = int(min(cash, cap) // price)
    target_qty = int(min(cash, target) // price)
    if risk_qty is not None:
        max_qty_by_cap = min(max_qty_by_cap, risk_qty)
        target_qty = min(target_qty, risk_qty)

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
            "base_cap_pct": round(base_cap_pct, 4),
            "confidence_multiplier": round(confidence_multiplier, 4),
            "risk_qty": risk_qty,
            "liquidity_cap_notional": round(liquidity_cap, 2) if liquidity_cap is not None else None,
            "economics_floor_applied": economics_floor_applied,
            "minimum_notional": round(min_notional, 2),
            "minimum_qty": min_qty,
            "product_rules": product_rules(settings, market),
            "underuse_reason": "minimum_notional_or_risk_cap_exceeds_available_cash",
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
        "base_cap_pct": round(base_cap_pct, 4),
        "confidence_multiplier": round(confidence_multiplier, 4),
        "risk_qty": risk_qty,
        "liquidity_cap_notional": round(liquidity_cap, 2) if liquidity_cap is not None else None,
        "economics_floor_applied": economics_floor_applied,
        "minimum_notional": round(min_notional, 2),
        "minimum_qty": min_qty,
        "product_rules": product_rules(settings, market),
        "underuse_reason": "",
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
    cost_parts = round_trip_cost_breakdown(entry_notional, exit_notional, market, settings)
    costs = float(cost_parts.get("total") or 0.0)
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
        "cost_breakdown": cost_parts,
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
    breakdown = round_trip_cost_breakdown(entry_notional, exit_notional, "IN", settings)
    return float(breakdown.get("total", 0.0) or 0.0)


def round_trip_cost_breakdown(
    entry_notional: float,
    exit_notional: float,
    market_region: str | None = "IN",
    settings: Any = None,
) -> dict[str, Any]:
    market = normalize_market_region(market_region or "IN", default="IN")
    buy = max(float(entry_notional or 0.0), 0.0)
    sell = max(float(exit_notional or 0.0), 0.0)
    if market == "US":
        one_way_bps = _one_way_cost_bps(settings)
        buy_cost = buy * one_way_bps / 10_000
        sell_cost = sell * one_way_bps / 10_000
        return {
            "market_region": market,
            "brokerage": round((buy + sell) * _setting(settings, "brokerage_bps", 0.0) / 10_000, 4),
            "taxes": round((buy + sell) * _setting(settings, "taxes_bps", 1.0) / 10_000, 4),
            "slippage": round((buy + sell) * _setting(settings, "slippage_bps", 5.0) / 10_000, 4),
            "total": round(buy_cost + sell_cost, 4),
            "cost_bps_each_side": round(one_way_bps, 4),
        }

    brokerage = min(
        _setting(settings, "india_brokerage_flat_per_order", 20.0) * (1 if buy > 0 else 0)
        + _setting(settings, "india_brokerage_flat_per_order", 20.0) * (1 if sell > 0 else 0),
        (buy + sell) * max(_setting(settings, "brokerage_bps", 0.0), 0.0) / 10_000
        if _setting(settings, "brokerage_bps", 0.0) > 0
        else 40.0,
    )
    stt = (buy + sell) * _setting(settings, "stt_bps", 10.0) / 10_000
    exchange = (buy + sell) * _setting(settings, "india_exchange_charges_bps", 0.345) / 10_000
    sebi = (buy + sell) * _setting(settings, "india_sebi_charges_bps", 0.01) / 10_000
    gst = (brokerage + exchange + sebi) * (_setting(settings, "india_gst_pct", 18.0) / 100)
    stamp = buy * _setting(settings, "india_stamp_duty_bps", 1.5) / 10_000
    slippage = (buy + sell) * _setting(settings, "slippage_bps", 5.0) / 10_000
    total = brokerage + stt + exchange + sebi + gst + stamp + slippage
    return {
        "market_region": market,
        "brokerage": round(brokerage, 4),
        "stt": round(stt, 4),
        "exchange_charges": round(exchange, 4),
        "sebi_charges": round(sebi, 4),
        "gst": round(gst, 4),
        "stamp_duty": round(stamp, 4),
        "slippage": round(slippage, 4),
        "total": round(total, 4),
        "cost_bps_each_side": round((total / max(buy + sell, 1.0)) * 10_000, 4),
    }


def product_rules(settings: Any = None, market_region: str | None = "IN") -> dict[str, Any]:
    market = normalize_market_region(market_region or "IN", default="IN")
    lot_size = int(_setting(settings, f"{market.lower()}_equity_lot_size", 1))
    tick_size = _setting(settings, f"{market.lower()}_equity_tick_size", 0.01 if market == "US" else 0.05)
    freeze_qty = int(_setting(settings, f"{market.lower()}_equity_freeze_qty", 100_000 if market == "IN" else 10_000))
    return {
        "market_region": market,
        "product": "equity_delivery" if market == "IN" else "us_equity_paper_probe",
        "lot_size": max(lot_size, 1),
        "tick_size": tick_size,
        "freeze_qty": max(freeze_qty, 1),
        "min_notional": minimum_auto_follow_notional(settings, market),
    }


def _one_way_cost_bps(settings: Any = None) -> float:
    return (
        _setting(settings, "brokerage_bps", 0.0)
        + _setting(settings, "slippage_bps", 5.0)
        + _setting(settings, "taxes_bps", 1.0)
        + _setting(settings, "stt_bps", 10.0)
    )


def _confidence_multiplier(confidence: float | None) -> float:
    if confidence is None:
        return 1.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 1.0
    if value > 1.0:
        value = value / 100.0
    if value >= 0.82:
        return 1.0
    if value >= 0.70:
        return 0.80
    if value >= 0.58:
        return 0.55
    return 0.35


def _setting(settings: Any, name: str, default: float) -> float:
    value = getattr(settings, name, None) if settings is not None else None
    if value is None:
        value = os.getenv(name.upper())
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
