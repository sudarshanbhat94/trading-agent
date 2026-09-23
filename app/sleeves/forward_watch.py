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

PATH = os.getenv("QUALITY_FORWARD_DB", str(Path(__file__).resolve().parents[2] / "var" / "quality_forward.db"))
TICKET = 3_000.0
MIN_TICKET = 1_500.0
SLIPPAGE = 0.002  # 20 bp each side, as in the frozen stock replay

SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_forward(
 observed_on TEXT NOT NULL, signal_asof TEXT NOT NULL, symbol TEXT NOT NULL,
 regime TEXT NOT NULL, score REAL NOT NULL, reference_close REAL NOT NULL,
 entry_on TEXT, entry_price REAL, qty INTEGER, net5_pct REAL, net20_pct REAL,
 status TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(observed_on,symbol));
"""


def _connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def update(result, tails, asof, observed_on, path=PATH):
    """Record today's visible watch, then score only later completed bars."""
    if pd.Timestamp(asof).date() > pd.Timestamp(observed_on).date():
        raise ValueError("signal close cannot postdate its observation")
    decision = next((d for d in result.decisions if d.sleeve == "quality_momentum"), None)
    watch = (decision.diagnostics or {}).get("watch", []) if decision else []
    asof_s = str(asof)[:10]
    with closing(_connect(path)) as con, con:
        for item in watch:
            symbol = item["symbol"]
            frame = tails.get(symbol)
            if frame is None or asof not in frame.index:
                continue
            close = float(frame.loc[asof, "close"])
            score = float(item.get("score") or 0)
            if not math.isfinite(close) or close <= 0 or not math.isfinite(score):
                continue
            con.execute("INSERT OR IGNORE INTO quality_forward"
                        "(observed_on,signal_asof,symbol,regime,score,reference_close)"
                        " VALUES(?,?,?,?,?,?)",
                        (observed_on, asof_s, symbol, result.regime.state, score, close))

        rows = con.execute("SELECT observed_on,signal_asof,symbol,reference_close "
                           "FROM quality_forward WHERE status='pending'").fetchall()
        for day, signal_asof, symbol, original_close in rows:
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
            entry = float(future.iloc[0]["open"]) * (1 + SLIPPAGE)
            qty = int(TICKET // entry) if math.isfinite(entry) and entry > 0 else 0
            if qty * entry < MIN_TICKET:
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
    """Counts and after-cost outcomes by observed regime; no profit claim."""
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        rows = con.execute("SELECT regime,COUNT(*),"
                           "SUM(CASE WHEN net20_pct IS NOT NULL THEN 1 ELSE 0 END),"
                           "SUM(CASE WHEN net20_pct>0 THEN 1 ELSE 0 END),"
                           "AVG(net20_pct) FROM quality_forward GROUP BY regime").fetchall()
    return [dict(regime=regime, observed=observed, matured=matured or 0,
                 winners=winners or 0, avg_net20_pct=round(avg, 3) if avg is not None else None)
            for regime, observed, matured, winners, avg in rows]
