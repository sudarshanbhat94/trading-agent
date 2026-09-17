"""Read-only book risk status, using the same allocator as production."""
from datetime import datetime, timedelta, timezone

from .accounting import session_pnl
from .feeds import fresh_quotes
from .performance import book_state
from .risk import BookState, RiskManager


def book_readiness(con, market, quotes, now=None):
    now = now or datetime.now(timezone.utc)
    snap = book_state(con, market, quotes)
    marks = fresh_quotes(quotes, now)
    missing = [p["symbol"] for p in snap.positions if p["symbol"] not in marks]
    day = now.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    pnl = session_pnl(con, market, snap.equity, day, snap.epoch, snap.capital, bool(snap.positions))
    peak = con.execute("SELECT MAX(equity) FROM v2_equity WHERE market=? "
                       "AND date LIKE 'LIVE_%' AND julianday(substr(date,6))>=julianday(?)",
                       (market, snap.epoch)).fetchone()[0]
    counts, notionals = {}, {}
    deployed = open_risk = 0.0
    for p in snap.positions:
        sleeve = p["sleeve"]
        value = p["shares"] * p["entry"]
        deployed += value
        counts[sleeve] = counts.get(sleeve, 0) + 1
        notionals[sleeve] = notionals.get(sleeve, 0) + value
        open_risk += p["shares"] * max(0, p["price"] - (p["stop"] or 0))
    peak = max(float(peak or snap.capital), snap.capital)
    book = BookState(snap.capital, snap.cash, deployed, snap.n_positions, counts,
                     snap.equity, peak, pnl or 0, notionals, open_risk)
    halted, reason = RiskManager().halted(book)
    if missing:
        halted, reason = True, "held-position quotes stale; valuation is provisional"
    elif pnl is None:
        halted, reason = True, "daily equity baseline unavailable"
    return dict(halted=halted, reason=reason, capital=snap.capital,
                equity=round(snap.equity,2), cash=round(snap.cash,2),
                peak=round(peak,2), positions=snap.n_positions,
                drawdown_pct=round(100*(snap.equity/peak-1),2) if peak else None,
                daily_pnl=round(pnl,2) if pnl is not None else None,
                checked_at=now.isoformat(), valuation_complete=not missing,
                scope="house", checks="book risk only; signal and data gates also apply")
