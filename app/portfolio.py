"""Portfolio analytics: allocation, concentration, drawdown, per-lane curves.

Pure functions over rows the caller has already fetched, so they can be tested
without a database and reused by any endpoint.

Sector exposure is deliberately absent. The `universe.sector` column is a
catch-all — "NSE Listed Equity" covers 2,594 of the Indian names — so a sector
breakdown built on it would be one bar reading 100%. That is worse than no
chart, because it looks like information. See BACKLOG for the real fix.
"""

from __future__ import annotations

import logging

_LOG = logging.getLogger("openstocks.portfolio")


def _num(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if number != number else number


def allocations(positions, equity):
    """Per-position exposure, largest first.

    `positions` are (symbol, strategy, shares, entry_price, live_price) rows;
    live_price may be None, in which case entry is used as the mark.
    """
    equity = _num(equity)
    out = []
    for row in positions or []:
        try:
            symbol, strategy, shares, entry, live = row
        except (TypeError, ValueError):
            continue
        shares, entry = _num(shares), _num(entry)
        mark = _num(live, entry) or entry
        value = shares * mark
        if value <= 0:
            continue
        out.append({
            "symbol": str(symbol),
            "strategy": str(strategy or "unknown"),
            "value": round(value, 2),
            "pct_of_equity": round(value / equity * 100, 2) if equity > 0 else None,
            "unrealised_pct": round((mark / entry - 1) * 100, 2) if entry > 0 else None,
        })
    out.sort(key=lambda a: -a["value"])
    return out


def concentration(allocs, equity):
    """How much of the book rides on its biggest bets.

    `hhi` is the Herfindahl index over position weights (0 = perfectly spread,
    1 = everything in one name), computed on weights as a fraction of EQUITY,
    so idle cash correctly counts as diversification.
    """
    equity = _num(equity)
    values = [a["value"] for a in allocs or []]
    if not values or equity <= 0:
        return {"n_positions": len(values), "largest_pct": 0.0, "top3_pct": 0.0,
                "hhi": 0.0, "deployed_pct": 0.0}
    weights = [v / equity for v in values]
    ordered = sorted(weights, reverse=True)
    return {
        "n_positions": len(values),
        "largest_pct": round(ordered[0] * 100, 2),
        "top3_pct": round(sum(ordered[:3]) * 100, 2),
        "hhi": round(sum(w * w for w in weights), 4),
        "deployed_pct": round(sum(weights) * 100, 2),
    }


def drawdown(curve):
    """Peak-to-trough decline over an equity curve.

    `curve` is a sequence of (date, equity). Returns the worst decline the book
    has suffered and how far below its high-water mark it sits now — the second
    is what tells you whether you are still in the hole.
    """
    points = []
    for row in curve or []:
        try:
            date, equity = row
        except (TypeError, ValueError):
            continue
        value = _num(equity, None)
        if value is not None and value > 0:
            points.append((str(date), value))
    if not points:
        return {"max_drawdown_pct": 0.0, "current_drawdown_pct": 0.0,
                "peak": None, "trough": None, "peak_date": None, "trough_date": None}

    peak = points[0][1]
    peak_date = points[0][0]
    worst = 0.0
    worst_peak = worst_trough = points[0][1]
    worst_peak_date = worst_trough_date = points[0][0]
    for date, value in points:
        if value > peak:
            peak, peak_date = value, date
        decline = (value / peak - 1) * 100 if peak > 0 else 0.0
        if decline < worst:
            worst = decline
            worst_peak, worst_trough = peak, value
            worst_peak_date, worst_trough_date = peak_date, date

    high_water = max(value for _, value in points)
    last = points[-1][1]
    current = (last / high_water - 1) * 100 if high_water > 0 else 0.0
    return {
        "max_drawdown_pct": round(worst, 2),
        "current_drawdown_pct": round(current, 2),
        "peak": round(worst_peak, 2),
        "trough": round(worst_trough, 2),
        "peak_date": worst_peak_date,
        "trough_date": worst_trough_date,
    }


def lane_curves(trades):
    """Cumulative realised P&L per lane, in exit order.

    `trades` are (strategy, exit_date, pnl) rows. This is what shows whether a
    lane's edge is steady or one lucky trade — the aggregate win rate hides it.
    """
    buckets: dict[str, list] = {}
    for row in trades or []:
        try:
            strategy, exit_date, pnl = row
        except (TypeError, ValueError):
            continue
        buckets.setdefault(str(strategy or "unknown"), []).append(
            (str(exit_date or ""), _num(pnl))
        )
    curves = {}
    for lane, rows in buckets.items():
        rows.sort(key=lambda r: r[0])
        running = 0.0
        points = []
        for exit_date, pnl in rows:
            running += pnl
            points.append({"date": exit_date, "cum_pnl": round(running, 2)})
        curves[lane] = points
    return curves


def build(positions, equity_curve, trades, budget, cash=None):
    """Assemble the full analytics payload. Never raises."""
    try:
        budget = _num(budget)
        allocs = allocations(positions, 0)          # values first, equity below
        deployed = sum(a["value"] for a in allocs)
        realised = sum(_num(t[2]) for t in (trades or []) if len(t) >= 3)
        equity = _num(cash, budget + realised - deployed) + deployed if cash is None else _num(cash) + deployed
        if equity <= 0:
            equity = budget or 1.0
        allocs = allocations(positions, equity)     # recompute with real equity
        return {
            "equity": round(equity, 2),
            "cash": round(equity - deployed, 2),
            "deployed": round(deployed, 2),
            "allocations": allocs,
            "concentration": concentration(allocs, equity),
            "drawdown": drawdown(equity_curve),
            "lane_curves": lane_curves(trades),
            "sector_exposure": None,   # see module docstring
            "sector_note": ("Sector data unavailable: the universe table's sector "
                            "column is a catch-all ('NSE Listed Equity' covers 2,594 "
                            "names), so a breakdown would be meaningless."),
        }
    except Exception:
        _LOG.exception("portfolio analytics failed")
        return {"equity": 0.0, "cash": 0.0, "deployed": 0.0, "allocations": [],
                "concentration": concentration([], 0), "drawdown": drawdown([]),
                "lane_curves": {}, "sector_exposure": None, "sector_note": ""}
