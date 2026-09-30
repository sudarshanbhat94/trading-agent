"""Forward-only quality-screen observations, separate from every paper book.

A screen seen on day D is measured from the *next* session's open. It cannot
claim a fill at D's already-passed open or at the prior completed close.
These are diagnostic next-open-to-future-close outcomes, not managed trades.
"""
from __future__ import annotations

import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

from ..costs import round_trip
from .risk import SLIPPAGE, stop_loss_including_costs

PATH = os.getenv("QUALITY_FORWARD_DB", str(Path(__file__).resolve().parents[2] / "var" / "quality_forward.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_forward(
 observed_on TEXT NOT NULL, signal_asof TEXT NOT NULL, symbol TEXT NOT NULL,
 regime TEXT NOT NULL, score REAL NOT NULL, reference_close REAL NOT NULL,
 planned_stop REAL, risk_cap REAL, notional_cap REAL, min_ticket REAL,
 entry_on TEXT, entry_price REAL, qty INTEGER, net5_pct REAL, net20_pct REAL,
 status TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(observed_on,symbol));
"""


def _connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(quality_forward)")}
    for name in ("planned_stop", "risk_cap", "notional_cap", "min_ticket"):
        if name not in cols:
            con.execute(f"ALTER TABLE quality_forward ADD COLUMN {name} REAL")
    # Old rows assumed a fixed Rs 3k fill with no stop-risk check. Preserve
    # them for audit, but never count them as executable performance.
    con.execute("UPDATE quality_forward SET status='legacy_unverified' "
                "WHERE planned_stop IS NULL AND status IN ('pending','complete','unfillable')")
    con.commit()
    return con


def update(result, tails, asof, observed_on, path=PATH):
    """Record today's visible watch, then score only later completed bars."""
    if pd.Timestamp(asof).date() > pd.Timestamp(observed_on).date():
        raise ValueError("signal close cannot postdate its observation")
    decision = next((d for d in result.decisions if d.sleeve == "quality_momentum"), None)
    diagnostics = (decision.diagnostics or {}) if decision else {}
    watch = diagnostics.get("watch", [])
    screen_gate_open = bool(diagnostics.get("screen_gate_open"))
    asof_s = str(asof)[:10]
    with closing(_connect(path)) as con, con:
        for item in watch:
            symbol = item["symbol"]
            frame = tails.get(symbol)
            if frame is None or asof not in frame.index:
                continue
            close = float(frame.loc[asof, "close"])
            score = float(item.get("score") or 0)
            stop = float(item.get("planned_stop") or 0)
            risk_cap = float(item.get("fresh_book_risk_cap") or 0)
            notional_cap = float(item.get("fresh_book_notional_cap") or 0)
            min_ticket = float(item.get("min_ticket") or 0)
            if not all(math.isfinite(v) for v in
                       (close, score, stop, risk_cap, notional_cap, min_ticket)):
                continue
            if (close <= 0 or not 0 < stop < close or risk_cap <= 0
                    or notional_cap <= 0 or min_ticket <= 0):
                continue
            status = ("pending" if result.regime.state == "ON" and screen_gate_open
                      else "entry_blocked")
            con.execute("INSERT OR IGNORE INTO quality_forward"
                        "(observed_on,signal_asof,symbol,regime,score,reference_close,"
                        "planned_stop,risk_cap,notional_cap,min_ticket,status)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (observed_on, asof_s, symbol, result.regime.state, score,
                         close, stop, risk_cap, notional_cap, min_ticket, status))

        rows = con.execute("SELECT observed_on,signal_asof,symbol,reference_close,"
                           "planned_stop,risk_cap,notional_cap,min_ticket "
                           "FROM quality_forward WHERE status='pending'").fetchall()
        for (day, signal_asof, symbol, original_close, stop, risk_cap,
             notional_cap, min_ticket) in rows:
            frame = tails.get(symbol)
            if frame is None or signal_asof not in frame.index:
                continue
            # A later split adjustment changes old OHLC. Do not compare an
            # old unadjusted signal price with newly adjusted future prices.
            if abs(float(frame.loc[signal_asof, "close"]) / original_close - 1) > .01:
                con.execute("UPDATE quality_forward SET status='corporate_action_review' "
                            "WHERE observed_on=? AND symbol=?", (day, symbol))
                continue
            # Restrict settlement to sessions already complete at this pass.
            # Even a caller supplying a longer frame cannot score tomorrow.
            future = frame.loc[(frame.index > pd.Timestamp(day)) &
                               (frame.index <= pd.Timestamp(asof))]
            if future.empty:
                continue
            open_price = float(future.iloc[0]["open"])
            entry = open_price * (1 + SLIPPAGE)
            qty = (int(notional_cap // entry)
                   if math.isfinite(entry) and entry > stop else 0)
            while qty > 0 and stop_loss_including_costs(open_price, stop, qty) > risk_cap:
                qty -= 1
            if qty * entry < min_ticket:
                con.execute("UPDATE quality_forward SET status='unfillable' "
                            "WHERE observed_on=? AND symbol=?", (day, symbol))
                continue

            def outcome(sessions):
                if len(future) < sessions:
                    return None
                exit_price = float(future.iloc[sessions - 1]["close"]) * (1 - SLIPPAGE)
                if not math.isfinite(exit_price) or exit_price <= 0:
                    return None
                gross = qty * (exit_price - entry)
                fee = round_trip(qty * entry, qty * exit_price)
                return round(100 * (gross - fee) / (qty * entry), 3)

            net5, net20 = outcome(5), outcome(20)
            con.execute("UPDATE quality_forward SET entry_on=?,entry_price=?,qty=?,"
                        "net5_pct=COALESCE(net5_pct,?),net20_pct=COALESCE(net20_pct,?),"
                        "status=? WHERE observed_on=? AND symbol=?",
                        (str(future.index[0])[:10], entry, qty, net5, net20,
                         "complete" if net20 is not None else "pending", day, symbol))


def summary(path=PATH):
    """Risk-feasible next-open/20-close diagnostics, not stop-managed trade P&L."""
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT regime,COUNT(*),"
                           "SUM(CASE WHEN status='entry_blocked' THEN 1 ELSE 0 END),"
                           "SUM(CASE WHEN status='unfillable' THEN 1 ELSE 0 END),"
                           "SUM(CASE WHEN status='legacy_unverified' THEN 1 ELSE 0 END),"
                           "SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END),"
                           "SUM(CASE WHEN status='complete' AND net20_pct>0 THEN 1 ELSE 0 END),"
                           "AVG(CASE WHEN status='complete' THEN net20_pct END) "
                           "FROM quality_forward GROUP BY regime").fetchall()
    return [dict(regime=regime, observed=observed, entry_blocked=blocked or 0,
                 unfillable=unfillable or 0,
                 legacy_unverified=legacy or 0, matured=matured or 0,
                 winners=winners or 0, avg_net20_pct=round(avg, 3) if avg is not None else None)
            for regime, observed, blocked, unfillable, legacy, matured, winners, avg in rows]
