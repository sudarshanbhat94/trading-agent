"""Sleeve 1 (primary): regime-adaptive mean reversion.

Keeps the validated v2 core — buy a meaningful dip in a relatively STRONG name,
never chase the high — and hardens it in four places:

  * REGIME. Runs in ON and NEUTRAL only, and in NEUTRAL takes exactly one name,
    its single best. The old engine treated NEUTRAL as fully tradeable, which
    is how dip-buying ran all the way through a flat tape.
  * CONVICTION. Threshold raised well above the old 0.55, and only the top 1-3
    survive. Fewer, better.
  * CONFIRMATION. A dip alone is not a setup. Requires relative strength versus
    the market AND participation (rvol), so we buy names being accumulated on
    the dip rather than names being abandoned.
  * NOT A FALLING KNIFE. Rejects anything still making new lows, in a downtrend
    on its 50-day, or gapping down hard.

Exits: ATR stop at 1.8-2.2x, an ATR target, a trail once in profit, and a time
stop. A setup that has not worked in `max_hold` sessions is wrong regardless of
price.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import v2_engine as eng
from .base import Candidate, Sleeve, SleeveDecision
from .universe import liquid_universe

# --- gates -------------------------------------------------------------
CONVICTION_ON = 0.62         # was 0.55 and took everything above it
CONVICTION_NEUTRAL = 0.72    # NEUTRAL demands a materially better setup
MAX_NAMES_ON = 3
MAX_NAMES_NEUTRAL = 1

MIN_RS20 = 0.0               # must be beating the market over 20 sessions
MIN_RVOL = 1.0               # participation on the dip, not abandonment
MIN_DIP = 0.04               # at least 4% off the 20-day high — a real dip
MAX_DIP = 0.18               # more than 18% off is damage, not a dip

ATR_STOP = 2.0               # spec band 1.8-2.2
ATR_TARGET = 3.5
TRAIL_PCT = 0.06
MAX_HOLD_DAYS = 8


class MeanReversionSleeve(Sleeve):
    name = "mean_reversion"
    allowed_regimes = ("ON", "NEUTRAL")

    def propose(self, ctx) -> SleeveDecision:
        regime = ctx.regime.state
        dec = self._decision(regime)

        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks all new equity longs"
            return dec

        threshold = CONVICTION_ON if regime == "ON" else CONVICTION_NEUTRAL
        limit = MAX_NAMES_ON if regime == "ON" else MAX_NAMES_NEUTRAL
        dec.note = f"threshold {threshold:.2f}, top {limit}"

        allowed = set(liquid_universe(ctx.tails, ctx.asof))
        cands: list[Candidate] = []

        for sig in eng.signals_for_date(ctx.tails, ctx.market_df, ctx.asof,
                                        threshold=threshold,
                                        atr_stop=ATR_STOP, atr_target=ATR_TARGET):
            sym = sig["symbol"]
            if sym not in allowed:
                dec.reject(sym, "not in the liquid universe")
                continue
            g = ctx.tails.get(sym)
            if g is None or ctx.asof not in g.index:
                dec.reject(sym, "no bar for asof")
                continue

            ok, why = self._confirm(g.loc[:ctx.asof], sig)
            if not ok:
                dec.reject(sym, why)
                continue

            entry = float(ctx.live.get(sym, {}).get("price") or sig["ref_close"])
            atr = float(sig["atr"])
            if entry <= 0 or atr <= 0:
                dec.reject(sym, "no usable price/atr")
                continue

            cands.append(Candidate(
                symbol=sym, sleeve=self.name, score=float(sig["conviction"]),
                entry=entry, stop=entry - ATR_STOP * atr,
                target=entry + ATR_TARGET * atr,
                trail_pct=TRAIL_PCT, max_hold_days=MAX_HOLD_DAYS,
                why=dict(setup="mean_reversion", regime=regime,
                         conviction=sig["conviction"], rs20=sig.get("rs20"),
                         dip=sig.get("dist_hi20"), atr_pct=sig.get("atr_pct"))))

        cands = self._sane_only(cands, dec)
        dec.candidates = self._rank(cands, limit)
        return dec

    # -- confirmation -------------------------------------------------
    @staticmethod
    def _confirm(gi: pd.DataFrame, sig: dict) -> tuple[bool, str]:
        """A dip is only a setup with strength and participation behind it."""
        try:
            c = gi["close"]
            close = float(c.iloc[-1])
            dip = -float(sig.get("dist_hi20") or 0.0)      # positive = below high
            if dip < MIN_DIP:
                return False, f"only {dip*100:.1f}% off the 20d high — not a dip yet"
            if dip > MAX_DIP:
                return False, f"{dip*100:.1f}% off the high — damage, not a dip"

            if float(sig.get("rs20") or -1) < MIN_RS20:
                return False, "underperforming the market over 20 sessions"

            gf_rvol = float(sig.get("rvol") or 0.0)
            if gf_rvol and gf_rvol < MIN_RVOL:
                return False, f"rvol {gf_rvol:.2f} — no participation on the dip"

            if len(c) >= 50:
                sma50 = float(c.tail(50).mean())
                if close < sma50:
                    return False, "below its own 50-day mean (downtrend)"

            lo20 = float(gi["low"].tail(20).min())
            if close <= lo20 * 1.005:
                return False, "at/near a new 20-day low — falling knife"

            if len(c) >= 2:
                prev = float(c.iloc[-2])
                if prev > 0 and close / prev - 1 < -0.06:
                    return False, "down >6% today — catching it mid-fall"
            return True, ""
        except Exception as exc:                            # pragma: no cover
            return False, f"confirmation error: {type(exc).__name__}"
