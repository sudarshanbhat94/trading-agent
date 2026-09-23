"""Paper-only NSE large-cap quality/momentum screen.

Quality means verified membership in NSE's Momentum Quality 50 index,
intersected with Nifty 100. Missing constituent data blocks entries.
"""
from __future__ import annotations

import math
import pandas as pd
from .base import Candidate, Sleeve
from .index_directional import monthly_rebalance

MIN_TURNOVER = 250_000_000
MAX_PRICE = 3_300
ATR_STOP = 3.0
MAX_HOLD_DAYS = 45


class QualityMomentumSleeve(Sleeve):
    name = "quality_momentum"
    allowed_regimes = ("ON",)

    def propose(self, ctx):
        dec = self._decision(ctx.regime.state)
        members = getattr(ctx, "factor_symbols", None)
        # A regime block stops orders, not observation. Screen the same verified
        # universe so the Ideas page can explain what the engine sees while it
        # stands aside. Observations never enter dec.candidates.
        gate = ""
        if not self.may_run(ctx.regime.state):
            gate = f"regime {ctx.regime.state} blocks new stock longs"
        elif not getattr(ctx.regime, "strong", False):
            gate = "ON regime lacks the existing strong-trend confirmation"
        if ctx.require_reference_data and not members:
            dec.active = False
            dec.note = "verified NSE quality/momentum constituents unavailable"
            return dec
        if not gate and not monthly_rebalance(ctx.asof, ctx.trade_date):
            gate = "monthly review has not arrived"

        scored = []
        universe = members if members is not None else set(ctx.tails)
        for sym in sorted(universe):
            g = ctx.tails.get(sym)
            if g is None or ctx.asof not in g.index:
                dec.reject(sym, "completed price history unavailable")
                continue
            gi = g.loc[:ctx.asof]
            if len(gi) < 253:
                dec.reject(sym, "less than 253 completed sessions")
                continue
            close = gi.close.astype(float)
            price = float((ctx.live.get(sym) or {}).get("price") or close.iloc[-1])
            turnover = float((gi.close * gi.volume).tail(20).median())
            if not 50 <= price <= MAX_PRICE or turnover < MIN_TURNOVER:
                dec.reject(sym, "price or turnover outside liquid Rs 10k universe")
                continue
            # Six/twelve-month momentum, excluding the most recent month.
            r6 = float(close.iloc[-22] / close.iloc[-127] - 1)
            r12 = float(close.iloc[-22] / close.iloc[-253] - 1)
            vol = float(close.pct_change().tail(252).std())
            sma50 = float(close.tail(50).mean())
            if min(r6, r12) <= 0 or not math.isfinite(vol) or vol <= 0:
                dec.reject(sym, "intermediate momentum absent")
                continue
            if price > sma50 * 1.10:
                dec.reject(sym, "more than 10% above 50-session mean")
                continue
            atr = self._atr(gi)
            if not math.isfinite(atr) or atr <= 0 or price - ATR_STOP * atr <= 0:
                dec.reject(sym, "ATR stop unavailable")
                continue
            raw = (r6 + r12) / (2 * vol * math.sqrt(252))
            scored.append((raw, Candidate(
                symbol=sym, sleeve=self.name, score=0.5, entry=price,
                stop=price - ATR_STOP * atr, target=0.0, trail_pct=0.12,
                max_hold_days=MAX_HOLD_DAYS,
                why={"setup": "large_cap_quality_momentum",
                     "quality_source": "NSE factor-index membership",
                     "return_6m_ex_recent": round(r6, 4),
                     "return_12m_ex_recent": round(r12, 4),
                     "median_turnover_inr": round(turnover),
                     "research_status": "experimental paper; no validated net profit track",
                     "regime": ctx.regime.state})))
        scored.sort(key=lambda item: (-item[0], item[1].symbol))
        if scored:
            top = max(scored[0][0], 1e-9)
            for raw, cand in scored:
                cand.score = round(min(max(raw / top, 0.0), 1.0), 4)
        # Offer three ranked names to the unified risk manager. At Rs 10k the
        # top score can be unaffordable even when the second fits one slot.
        # max_positions still enforces at most one funded stock.
        dec.candidates = [] if gate else [cand for _, cand in scored[:3]]
        dec.diagnostics = {"verified_members": len(universe), "passed": len(scored),
                           "source": "NSE Nifty100 intersection Momentum Quality 50",
                           "watch": [dict(symbol=cand.symbol, price=round(cand.entry, 2),
                                          score=cand.score,
                                          return_6m_pct=round(cand.why["return_6m_ex_recent"] * 100, 1),
                                          return_12m_pct=round(cand.why["return_12m_ex_recent"] * 100, 1))
                                     for _, cand in scored[:3]]}
        if gate:
            dec.active = False
            dec.note = gate
        else:
            dec.note = f"{len(scored)} verified large caps passed price and liquidity checks"
        return dec

    @staticmethod
    def _atr(gi: pd.DataFrame) -> float:
        h, l, c = gi.high, gi.low, gi.close
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        return float(tr.tail(14).mean())
