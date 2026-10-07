"""Read-only stock coverage, separate from the engine's execution permission.

Passing evidence gates is not an approved idea, a risk-sized order or a fill.
The market benchmark must never be presented as the stock-screen universe.
"""
from collections import Counter
from datetime import datetime, timezone

from .selection import reject_reason


def equity_screen_status(screen, now=None):
    now = now or datetime.now(timezone.utc)
    result = dict(status="unavailable", universe_count=None, screened_count=None,
                  evidence_passes=None, generated_at=None, price_asof=None,
                  rejections=[], note="The individual-stock evidence screen is unavailable.")
    if not isinstance(screen, dict) or screen.get("status") != "ok":
        return result
    rows = screen.get("equities")
    if not isinstance(rows, list) or any(not isinstance(r, dict)
            or not isinstance(r.get("symbol"), str) or not r["symbol"].strip() for r in rows):
        return result
    symbols = {r["symbol"] for r in rows}
    result.update(universe_count=screen.get("universe_count"), screened_count=len(symbols),
                  generated_at=screen.get("generated_at"), price_asof=screen.get("price_asof"))
    if screen.get("stale") or screen.get("price_stale"):
        result.update(status="stale", note="Last stock screen is stale; its counts are historical.")
        return result
    rejected, seen, passed = Counter(), set(), 0
    duplicated = {s for s, n in Counter(r["symbol"] for r in rows).items() if n > 1}
    for row in rows:
        symbol = row["symbol"]
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            if symbol in duplicated:
                reason = "Duplicate stock evidence requires review"
            elif any(not isinstance(row.get(key), dict) for key in ("fundamentals", "metrics", "participation")):
                reason = "Incomplete stock evidence requires review"
            else:
                reason = reject_reason(row, screen, now)
        except (KeyError, TypeError, ValueError):
            reason = "Incomplete stock evidence requires review"
        if reason:
            rejected[reason] += 1
        else:
            passed += 1
    result.update(status="current", evidence_passes=passed,
                  rejections=[dict(reason=reason, count=count) for reason, count in rejected.most_common(3)],
                  note="Evidence passes precede entry-level, cost, account-risk and execution approval checks.")
    return result


def paper_execution_scope():
    """Describe the actual allowlist and feature flags; never enable a sleeve."""
    from ..sleeves.config import PRODUCTION_SLEEVES, SLEEVES
    from ..sleeves.index_directional import SYMBOL
    enabled = [name for name in PRODUCTION_SLEEVES if getattr(SLEEVES, name).enabled]
    stocks = [name for name in enabled if name in ("mean_reversion", "quality_momentum", "early_momentum")]
    from .automation import MODEL_VERSION
    return dict(production_sleeves=enabled, automated_stock_sleeves=stocks,
                stock_entries_enabled=bool(stocks),
                stock_model_version=MODEL_VERSION if 'quality_momentum' in stocks else None,
                stock_validation='unvalidated paper trial' if stocks else None,
                automated_index_instruments=[SYMBOL] if "index_directional" in enabled else [])
