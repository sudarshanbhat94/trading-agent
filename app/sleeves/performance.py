"""Daily performance report: book state, and P&L split by sleeve and by regime.

Read-only. Computes nothing the engine does not already record and changes no
strategy behaviour — it opens the paper book read-only and formats what is
there.

Why both splits, always together: a sleeve that only ever traded in an ON
regime has not been shown to work, it has been shown that a kind tape was kind.
The cross-tab is what separates those two, and its absence is why the old
engine's entire positive record turned out to be three trades on one day with
nothing in the dashboard saying so.

R-MULTIPLE. `risk_amt` is the rupees at risk at entry — shares x (entry - stop)
— stored on the position and copied onto the trade at exit. R = pnl/risk_amt,
so +1R means the trade made exactly what it was prepared to lose. Trades from
before the column existed have no risk_amt and are excluded from the R column
(shown as "-") rather than silently counted as zero.

Rows from the retired lanes carry NULL sleeve/regime and report as `legacy` /
`?`, never folded into a sleeve's numbers.

    python3 scripts/daily_report.py                  # today's book + all-time
    python3 scripts/daily_report.py --since 2026-08-01
    python3 scripts/daily_report.py --epoch          # current book epoch only
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


# ---------------------------------------------------------------- book state
@dataclass
class BookSnapshot:
    capital: float = 0.0
    cash: float = 0.0
    positions_value: float = 0.0
    n_positions: int = 0
    realised: float = 0.0
    epoch: str = ""
    positions: list = field(default_factory=list)

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def deployed_pct(self) -> float:
        return (self.positions_value / self.capital * 100) if self.capital else 0.0

    @property
    def return_pct(self) -> float:
        return (self.equity / self.capital - 1) * 100 if self.capital else 0.0


def book_state(con, market: str = "IN", prices: dict | None = None) -> BookSnapshot:
    """Current equity, cash and open positions, scoped to the book epoch."""
    row = con.execute("SELECT budget,started_at FROM v2_book WHERE market=?",
                      (market,)).fetchone()
    if not row:
        return BookSnapshot()
    capital, epoch = float(row[0]), (row[1] or "")
    prices = prices or {}

    positions = []
    cost = mv = 0.0
    for sym, sleeve, sh, ep, stop in con.execute(
            "SELECT symbol,COALESCE(sleeve,strategy),shares,entry_price,stop"
            " FROM v2_positions WHERE market=?", (market,)):
        sh, ep = float(sh), float(ep)
        px = float((prices.get(sym) or {}).get("price") or ep)
        cost += sh * ep
        mv += sh * px
        positions.append(dict(symbol=sym, sleeve=sleeve, shares=sh, entry=ep,
                              price=px, value=sh * px, stop=stop,
                              pnl=sh * (px - ep)))

    realised = con.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?"
        " AND COALESCE(closed_at,'') >= ?", (market, epoch)).fetchone()[0] or 0.0

    return BookSnapshot(capital=capital, cash=capital - cost + float(realised),
                        positions_value=mv, n_positions=len(positions),
                        realised=float(realised), epoch=epoch, positions=positions)


# ------------------------------------------------------------------- splits
def _rows(con, market: str, since: str | None, epoch: str | None):
    q = ("SELECT COALESCE(sleeve,'legacy'), COALESCE(regime,'?'), pnl,"
         " shares*(exit_price-entry_price), risk_amt FROM v2_trades WHERE market=?")
    args: list = [market]
    if since:
        q += " AND entry_date>=?"
        args.append(since)
    if epoch:
        q += " AND COALESCE(closed_at,'')>=?"
        args.append(epoch)
    return list(con.execute(q, args))


def _aggregate(rows, key) -> dict:
    out: dict = {}
    for r in rows:
        b = out.setdefault(key(r), {"n": 0, "wins": 0, "net": 0.0, "gross": 0.0,
                                    "win_sum": 0.0, "loss_sum": 0.0,
                                    "r_sum": 0.0, "r_n": 0})
        pnl = float(r[2] or 0.0)
        b["n"] += 1
        b["net"] += pnl
        b["gross"] += float(r[3] or 0.0)
        if pnl > 0:
            b["wins"] += 1
            b["win_sum"] += pnl
        else:
            b["loss_sum"] += -pnl
        risk = r[4]
        if risk:                       # R only where the risk was recorded
            b["r_sum"] += pnl / float(risk)
            b["r_n"] += 1
    for b in out.values():
        b["win_rate"] = (b["wins"] / b["n"] * 100) if b["n"] else 0.0
        b["avg"] = (b["net"] / b["n"]) if b["n"] else 0.0
        b["avg_r"] = (b["r_sum"] / b["r_n"]) if b["r_n"] else None
        b["profit_factor"] = (b["win_sum"] / b["loss_sum"]) if b["loss_sum"] else (
            float("inf") if b["win_sum"] else 0.0)
        b["cost"] = b["gross"] - b["net"]
    return out


def by_sleeve(con, market="IN", since=None, epoch=None) -> dict:
    return _aggregate(_rows(con, market, since, epoch), key=lambda r: r[0])


def by_regime(con, market="IN", since=None, epoch=None) -> dict:
    return _aggregate(_rows(con, market, since, epoch), key=lambda r: r[1])


def by_sleeve_and_regime(con, market="IN", since=None, epoch=None) -> dict:
    return _aggregate(_rows(con, market, since, epoch), key=lambda r: (r[0], r[1]))


# ------------------------------------------------------------------- render
def _fmt_r(v) -> str:
    return f"{v:+.2f}R" if v is not None else "     -"


def _table(title, buckets, label_w=22, label=lambda k: str(k)) -> list[str]:
    if not buckets:
        return [title, "  (no trades)"]
    out = [title,
           f"  {'':<{label_w}}{'n':>5}{'win%':>7}{'net Rs':>11}{'avg Rs':>9}"
           f"{'avg R':>8}{'PF':>7}{'cost Rs':>10}"]
    for k in sorted(buckets, key=lambda x: -buckets[x]["net"]):
        b = buckets[k]
        pf = "  inf" if b["profit_factor"] == float("inf") else f"{b['profit_factor']:.2f}"
        out.append(f"  {label(k):<{label_w}}{b['n']:>5}{b['win_rate']:>6.0f}%"
                   f"{b['net']:>11,.0f}{b['avg']:>9,.0f}{_fmt_r(b['avg_r']):>8}"
                   f"{pf:>7}{b['cost']:>10,.0f}")
    return out


def report(con, market: str = "IN", since: str | None = None,
           prices: dict | None = None, epoch_only: bool = False) -> str:
    snap = book_state(con, market, prices)
    epoch = snap.epoch if epoch_only else None
    lines: list[str] = []

    lines.append("=" * 78)
    scope = f"since {since}" if since else ("current book epoch" if epoch_only else "all time")
    lines.append(f"OPENSTOCKS DAILY REPORT · {market} · {scope}")
    lines.append("=" * 78)

    lines.append("")
    lines.append("BOOK")
    lines.append(f"  capital          Rs {snap.capital:>12,.2f}")
    lines.append(f"  equity           Rs {snap.equity:>12,.2f}   ({snap.return_pct:+.2f}%)")
    lines.append(f"  cash             Rs {snap.cash:>12,.2f}")
    lines.append(f"  positions value  Rs {snap.positions_value:>12,.2f}   "
                 f"({snap.deployed_pct:.1f}% deployed)")
    lines.append(f"  realised (epoch) Rs {snap.realised:>12,.2f}")
    lines.append(f"  epoch            {snap.epoch or '(unset)'}")

    lines.append("")
    lines.append(f"OPEN POSITIONS ({snap.n_positions})")
    if snap.positions:
        lines.append(f"  {'symbol':<14}{'sleeve':<18}{'qty':>6}{'entry':>10}"
                     f"{'now':>10}{'value':>11}{'open P&L':>11}")
        for p in snap.positions:
            lines.append(f"  {p['symbol']:<14}{p['sleeve']:<18}{p['shares']:>6.0f}"
                         f"{p['entry']:>10,.2f}{p['price']:>10,.2f}"
                         f"{p['value']:>11,.0f}{p['pnl']:>+11,.0f}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines += _table("BY SLEEVE", by_sleeve(con, market, since, epoch))
    lines.append("")
    lines += _table("BY REGIME", by_regime(con, market, since, epoch))
    lines.append("")
    lines += _table("SLEEVE x REGIME",
                    by_sleeve_and_regime(con, market, since, epoch),
                    label_w=30, label=lambda k: f"{k[0]} / {k[1]}")

    lines.append("")
    lines.append("avg R = mean of pnl / rupees-at-risk-at-entry. '-' means the trade")
    lines.append("predates the risk_amt column, not that the risk was zero.")
    lines.append("'legacy' / '?' are retired-lane rows, never folded into a sleeve.")
    return "\n".join(lines)


def open_readonly(path: str) -> sqlite3.Connection:
    """Read-only handle — a report must never be able to write to the book."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
