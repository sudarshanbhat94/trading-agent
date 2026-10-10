"""Prospective Ideas admission policy, independent of order/strategy approval.

No publication quota. A screen score only orders candidates that pass every
predicate; it cannot compensate for missing evidence or a bad trade contract.
These thresholds are conservative research hypotheses, not a validated edge.
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .screen import MIN_TURNOVER
from .financials import valid_income_history

MODEL_VERSION = "conditional-pullback-v4"
POLICY = dict(version="selective-ideas-v1", max_ideas=3, min_ideas=0,
              max_per_sector=1, min_roe_pct=15., max_debt_equity=1.,
              min_cash_conversion=.5, min_earnings_years=3,
              min_relative_volume=1.5, min_net_r_at_final_target=1.,
              cadence="qualification only; no daily publication requirement",
              validation="unvalidated; independent forward evidence required")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def reject_reason(row, screen, now=None):
    """Fail closed on missing facts, including apparently flag-free records."""
    if screen.get("status") != "ok" or screen.get("stale") or screen.get("price_stale"):
        return "Current completed-session evidence unavailable; no new idea"
    if row.get("flags"):
        return "; ".join(row["flags"])
    if not row.get("sector") or row["sector"] in ("Unknown", "Other", "NSE Listed Equity"):
        return "Verified sector required for shortlist diversification"
    f, m, p = (row.get(k) or {} for k in ("fundamentals", "metrics", "participation"))
    if not screen.get('price_asof') or not valid_income_history(f,screen['price_asof']):
        return "Consecutive dated annual earnings evidence required; counts alone are insufficient"
    if now is not None and (not isinstance(now,datetime) or now.utcoffset() is None or
            not valid_income_history(f,now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat())):
        return "Annual earnings evidence is unavailable at this decision time"
    roe, debt, cash, growth, margin, income, years, positive = (
        number(f.get(k)) for k in ("roe_pct", "debt_equity", "cash_conversion",
            "earnings_growth_pct", "profit_margin_pct", "annual_income",
            "earnings_years", "positive_earnings_years"))
    if any(v is None for v in (roe, debt, cash, growth, margin, income, years, positive)):
        return "Complete profitability, leverage and cash-flow evidence required"
    if (roe < POLICY["min_roe_pct"] or not 0 <= debt <= POLICY["max_debt_equity"]
            or cash < POLICY["min_cash_conversion"] or growth < 0 or margin <= 0 or income <= 0
            or years < POLICY["min_earnings_years"] or years != int(years) or positive != years):
        return "Quality gate failed: ROE, low leverage, cash conversion and consistently profitable years required"
    turnover, volume = number(m.get("turnover")), number(m.get("relative_volume"))
    delivery, average = number(p.get("delivery_pct")), number(p.get("delivery_avg20_pct"))
    if turnover is None or turnover < MIN_TURNOVER:
        return "Completed-session liquidity below the research turnover floor"
    if m.get("setup") not in ("pullback", "controlled breakout"):
        return "Controlled price structure required; no chasing an extended move"
    if (m.get("above50") is not True or any((number(m.get(k)) or 0) <= 0
            for k in ("rs_vs_nifty20_pct", "return126_pct", "sector_rs20_pct"))):
        return "Stock or sector momentum confirmation missing"
    if (volume is None or volume < POLICY["min_relative_volume"] or delivery is None or average is None
            or not 0 <= average < delivery <= 100 or p.get("session") != screen.get("price_asof")):
        return "Participation gate failed: volume >=1.5x and delivery above its 20-session average required"
    if number(row.get("score")) is None or not 0 <= float(row["score"]) <= 100:
        return "Finite research ranking score required"
    return ""
