"""Performance split by SLEEVE and by REGIME.

Both splits are needed and neither is sufficient alone. A sleeve that only ever
traded in an ON regime has not been shown to work — it has been shown that a
kind tape was kind. Splitting by regime is what separates the two, and it is
the reporting the old engine never had: its whole positive record turned out to
be three trades on one day, and nothing in the dashboard made that visible.

Reads `v2_trades.sleeve` / `.regime`, which are written at entry and copied
onto the trade at exit. Rows from the retired lanes have NULL in both and are
reported under "legacy" rather than silently folded into a sleeve's numbers.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    gross: float = 0.0
    net: float = 0.0
    best: float = 0.0
    worst: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.n * 100) if self.n else 0.0

    @property
    def avg(self) -> float:
        return (self.net / self.n) if self.n else 0.0

    def profit_factor(self, wins_sum: float, loss_sum: float) -> float:
        return (wins_sum / loss_sum) if loss_sum else (float("inf") if wins_sum else 0.0)


def _rows(con, market: str, since: str | None):
    q = ("SELECT COALESCE(sleeve,'legacy'), COALESCE(regime,'?'), pnl,"
         " shares*(exit_price-entry_price) FROM v2_trades WHERE market=?")
    args = [market]
    if since:
        q += " AND entry_date>=?"
        args.append(since)
    return list(con.execute(q, args))


def by_sleeve(con, market: str = "IN", since: str | None = None) -> dict:
    """{sleeve: {...}} — the split that answers 'which sleeve earns its slot'."""
    return _aggregate(_rows(con, market, since), key=lambda r: r[0])


def by_regime(con, market: str = "IN", since: str | None = None) -> dict:
    """{regime: {...}} — the split that answers 'or was the tape just kind'."""
    return _aggregate(_rows(con, market, since), key=lambda r: r[1])


def by_sleeve_and_regime(con, market: str = "IN", since: str | None = None) -> dict:
    """{(sleeve, regime): {...}} — the honest cross-tab."""
    return _aggregate(_rows(con, market, since), key=lambda r: (r[0], r[1]))


def _aggregate(rows, key) -> dict:
    buckets: dict = {}
    for r in rows:
        k = key(r)
        b = buckets.setdefault(k, {"n": 0, "wins": 0, "net": 0.0, "gross": 0.0,
                                   "win_sum": 0.0, "loss_sum": 0.0,
                                   "best": None, "worst": None})
        pnl = float(r[2] or 0.0)
        b["n"] += 1
        b["net"] += pnl
        b["gross"] += float(r[3] or 0.0)
        if pnl > 0:
            b["wins"] += 1
            b["win_sum"] += pnl
        else:
            b["loss_sum"] += -pnl
        b["best"] = pnl if b["best"] is None else max(b["best"], pnl)
        b["worst"] = pnl if b["worst"] is None else min(b["worst"], pnl)
    for b in buckets.values():
        b["win_rate"] = (b["wins"] / b["n"] * 100) if b["n"] else 0.0
        b["avg"] = (b["net"] / b["n"]) if b["n"] else 0.0
        b["profit_factor"] = (b["win_sum"] / b["loss_sum"]) if b["loss_sum"] else (
            float("inf") if b["win_sum"] else 0.0)
        b["cost"] = b["gross"] - b["net"]
    return buckets


def report(con, market: str = "IN", since: str | None = None) -> str:
    """A plain-text split, for logs and the daily digest."""
    out = []
    sl = by_sleeve(con, market, since)
    rg = by_regime(con, market, since)
    xt = by_sleeve_and_regime(con, market, since)

    out.append(f"{'sleeve':<20}{'n':>5}{'win%':>7}{'net':>10}{'avg':>9}{'PF':>7}{'cost':>9}")
    for k in sorted(sl, key=lambda x: -sl[x]["net"]):
        b = sl[k]
        out.append(f"{k:<20}{b['n']:>5}{b['win_rate']:>6.0f}%{b['net']:>10,.0f}"
                   f"{b['avg']:>9,.0f}{b['profit_factor']:>7.2f}{b['cost']:>9,.0f}")

    out.append("")
    out.append(f"{'regime':<20}{'n':>5}{'win%':>7}{'net':>10}{'avg':>9}{'PF':>7}")
    for k in sorted(rg, key=lambda x: -rg[x]["net"]):
        b = rg[k]
        out.append(f"{k:<20}{b['n']:>5}{b['win_rate']:>6.0f}%{b['net']:>10,.0f}"
                   f"{b['avg']:>9,.0f}{b['profit_factor']:>7.2f}")

    if xt:
        out.append("")
        out.append(f"{'sleeve x regime':<32}{'n':>5}{'win%':>7}{'net':>10}")
        for k in sorted(xt, key=lambda x: -xt[x]["net"]):
            b = xt[k]
            out.append(f"{k[0]+' / '+k[1]:<32}{b['n']:>5}{b['win_rate']:>6.0f}%"
                       f"{b['net']:>10,.0f}")
    return "\n".join(out)
