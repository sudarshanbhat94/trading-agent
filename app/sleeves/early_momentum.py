"""Sleeve 3 (tactical): early momentum — catch the move BEFORE the gainer list.

The goal is the stock at the START of a high-probability move, not the one
already up 6% and printing on Moneycontrol's top-gainers page. Those are two
different trades: by the time a name is on that list the move is priced, the
spread is wide, and the remaining upside is what is left after everyone has
seen it.

So this sleeve deliberately inverts the usual screen. It does NOT want:
  * a name at the high of its day (that is the chase);
  * a name already up several percent (that is the list).

It wants the bar BEFORE that: volume arriving while price is still inside a
consolidation, relative strength turning up, and structure about to give way.

IGNITION SCORE combines six components, each 0..1, weighted:

    0.28  volume surge      rvol vs 20-day, 2-3x+ while price is RISING
    0.20  structure         tight consolidation about to resolve (NR/squeeze)
    0.18  relative strength  RS vs Nifty improving, not just positive
    0.14  delivery %        rising or above average (real buying, not churn)
    0.12  proximity         close to, but not through, resistance
    0.08  catalyst          news / bulk deal / FII-DII footprint when present

`delivery` and `catalyst` degrade gracefully to neutral when their feeds are
unavailable, so the sleeve still works on price+volume alone rather than
silently scoring everything zero.

ENTRY is a controlled pullback or a measured breakout — never the day's high.
EXITS are faster and tighter than the other equity sleeves: this is a tactical
trade with a short shelf life.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import Candidate, Sleeve, SleeveDecision
from .universe import liquid_universe

_LOG = logging.getLogger("openstocks.sleeves.early_momentum")

# --- ignition gates ----------------------------------------------------
RVOL_MIN = 2.0               # volume must be genuinely unusual
RVOL_STRONG = 3.0
MOVE_MIN = 0.005             # some upward intent today...
MOVE_MAX = 0.035             # ...but NOT already a gainer-list name
NEAR_HIGH_MAX = 0.985        # must NOT be within 1.5% of the day's high
SQUEEZE_MAX = 0.75           # 20d ATR vs its own 20-sessions-ago value
MIN_RS_IMPROVING = 0.0
IGNITION_MIN = 0.60          # score floor to be a candidate at all

ATR_STOP = 1.5               # tighter than the other equity sleeves
TARGET_PCT = 0.06
TRAIL_PCT = 0.035
MAX_HOLD_DAYS = 4            # fast: a failed ignition is wrong within days


class EarlyMomentumSleeve(Sleeve):
    name = "early_momentum"
    allowed_regimes = ("ON", "NEUTRAL")

    def propose(self, ctx) -> SleeveDecision:
        regime = ctx.regime.state
        dec = self._decision(regime)
        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks new equity longs"
            return dec

        # more active when the tape supports it, as specified
        floor = IGNITION_MIN if regime == "ON" else IGNITION_MIN + 0.08
        dec.note = f"ignition floor {floor:.2f}"

        cands: list[Candidate] = []
        for sym in liquid_universe(ctx.tails, ctx.asof):
            g = ctx.tails.get(sym)
            if g is None or ctx.asof not in g.index:
                continue
            gi = g.loc[:ctx.asof]
            if len(gi) < 60:
                continue
            lq = ctx.live.get(sym) or {}

            score, parts, why = self._ignition(gi, lq, ctx)
            if score is None:
                dec.reject(sym, why)
                continue
            if score < floor:
                dec.reject(sym, f"ignition {score:.2f} < {floor:.2f}")
                continue

            entry, ereason = self._entry_price(gi, lq)
            if entry is None:
                dec.reject(sym, ereason)
                continue
            atr = self._atr(gi)
            if atr <= 0:
                dec.reject(sym, "no ATR")
                continue

            cands.append(Candidate(
                symbol=sym, sleeve=self.name, score=round(score, 4),
                entry=entry, stop=entry - ATR_STOP * atr,
                target=entry * (1 + TARGET_PCT), trail_pct=TRAIL_PCT,
                max_hold_days=MAX_HOLD_DAYS,
                why=dict(setup="early_momentum", ignition=round(score, 4),
                         components=parts, regime=regime, entry_style=ereason)))

        cands = self._sane_only(cands, dec)
        cfg = getattr(ctx.settings, self.name)
        dec.candidates = self._rank(cands, cfg.max_positions)
        return dec

    # -- the score ------------------------------------------------------
    def _ignition(self, gi: pd.DataFrame, lq: dict, ctx
                  ) -> tuple[float | None, dict, str]:
        try:
            c, v, h, l = gi["close"], gi["volume"], gi["high"], gi["low"]
            close = float(lq.get("price") or c.iloc[-1])
            prev = float(c.iloc[-2])
            if prev <= 0:
                return None, {}, "no prior close"

            move = close / prev - 1
            if move < MOVE_MIN:
                return None, {}, f"no upward intent ({move*100:+.1f}%)"
            if move > MOVE_MAX:
                return None, {}, (f"already +{move*100:.1f}% — this is the "
                                  f"gainer list, not the ignition")

            # must NOT be pinned at the day's high: that is the chase
            day_hi = float(lq.get("high") or h.iloc[-1])
            day_lo = float(lq.get("low") or l.iloc[-1])
            if day_hi > 0 and close / day_hi > NEAR_HIGH_MAX:
                return None, {}, "at the day's high — that is the chase"

            # 1. volume surge while rising
            rvol = float(v.iloc[-1] / max(v.tail(20).mean(), 1e-9))
            if rvol < RVOL_MIN:
                return None, {}, f"rvol {rvol:.1f} < {RVOL_MIN}"
            s_vol = min((rvol - RVOL_MIN) / (RVOL_STRONG - RVOL_MIN), 1.0)

            # 2. structure: volatility squeeze resolving
            atr = self._atr(gi)
            atr_prev = self._atr(gi.iloc[:-20]) if len(gi) > 40 else atr
            squeeze = (atr / atr_prev) if atr_prev > 0 else 1.0
            s_struct = min(max((SQUEEZE_MAX - squeeze) / SQUEEZE_MAX + 0.5, 0.0), 1.0)

            # 3. relative strength IMPROVING (not merely positive)
            rs_now, rs_then = self._rs(gi, ctx, 20), self._rs(gi, ctx, 20, offset=10)
            if rs_now is None:
                return None, {}, "no relative strength"
            improving = rs_now - (rs_then if rs_then is not None else 0.0)
            if rs_now < MIN_RS_IMPROVING:
                return None, {}, "lagging the market"
            s_rs = min(max(0.5 + improving * 10.0, 0.0), 1.0)

            # 4. delivery % — degrades to neutral when the feed is absent
            s_deliv, deliv_note = self._delivery(gi, ctx)

            # 5. proximity to resistance without having blown through it
            hi20 = float(h.tail(20).max())
            dist = (hi20 / close - 1) if close > 0 else 1.0
            s_prox = min(max(1.0 - dist / 0.05, 0.0), 1.0) if dist >= 0 else 0.3

            # 6. catalyst — neutral when unavailable
            s_cat = self._catalyst(gi, ctx)

            score = (0.28 * s_vol + 0.20 * s_struct + 0.18 * s_rs
                     + 0.14 * s_deliv + 0.12 * s_prox + 0.08 * s_cat)
            parts = dict(volume=round(s_vol, 3), structure=round(s_struct, 3),
                         rel_strength=round(s_rs, 3), delivery=round(s_deliv, 3),
                         proximity=round(s_prox, 3), catalyst=round(s_cat, 3),
                         rvol=round(rvol, 2), move_pct=round(move * 100, 2),
                         delivery_src=deliv_note)
            return float(min(max(score, 0.0), 1.0)), parts, ""
        except Exception as exc:                            # pragma: no cover
            return None, {}, f"ignition error: {type(exc).__name__}"

    # -- entry ----------------------------------------------------------
    @staticmethod
    def _entry_price(gi: pd.DataFrame, lq: dict) -> tuple[float | None, str]:
        """Pullback preferred; a controlled breakout otherwise. Never the high."""
        try:
            close = float(lq.get("price") or gi["close"].iloc[-1])
            day_hi = float(lq.get("high") or gi["high"].iloc[-1])
            day_lo = float(lq.get("low") or gi["low"].iloc[-1])
            if close <= 0:
                return None, "no price"
            if day_hi > day_lo:
                pos = (close - day_lo) / (day_hi - day_lo)
                if pos <= 0.65:
                    return close, "pullback"
                if pos <= 0.85:
                    return close, "controlled_breakout"
                return None, f"too extended in the day's range ({pos:.0%})"
            return close, "flat_range"
        except Exception:
            return None, "entry error"

    # -- inputs that may be missing -------------------------------------
    @staticmethod
    def _delivery(gi: pd.DataFrame, ctx) -> tuple[float, str]:
        """Delivery % confirmation. Neutral 0.5 when the feed is unavailable —
        a missing input must not silently veto or inflate every name."""
        fn = getattr(ctx, "delivery_pct", None)
        if not callable(fn):
            return 0.5, "unavailable"
        try:
            sym = gi.attrs.get("symbol") or ""
            cur, avg = fn(sym)
            if cur is None or avg is None or avg <= 0:
                return 0.5, "unavailable"
            ratio = cur / avg
            return float(min(max((ratio - 0.9) / 0.4, 0.0), 1.0)), f"{cur:.0f}% vs {avg:.0f}%"
        except Exception:
            return 0.5, "unavailable"

    @staticmethod
    def _catalyst(gi: pd.DataFrame, ctx) -> float:
        fn = getattr(ctx, "catalyst_score", None)
        if not callable(fn):
            return 0.5
        try:
            sym = gi.attrs.get("symbol") or ""
            s = fn(sym)
            return float(min(max(s, 0.0), 1.0)) if s is not None else 0.5
        except Exception:
            return 0.5

    @staticmethod
    def _rs(gi: pd.DataFrame, ctx, window: int, offset: int = 0) -> float | None:
        try:
            c = gi["close"]
            if len(c) < window + offset + 1:
                return None
            i = -1 - offset
            stock = float(c.iloc[i]) / float(c.iloc[i - window]) - 1
            mc = ctx.market_df["mkt_cum"]
            mc = mc.loc[:gi.index[i]]
            if len(mc) < window + 1:
                return None
            mkt = float(mc.iloc[-1]) / float(mc.iloc[-1 - window]) - 1
            return stock - mkt
        except Exception:
            return None

    @staticmethod
    def _atr(gi: pd.DataFrame, window: int = 14) -> float:
        try:
            h, l, c = gi["high"], gi["low"], gi["close"]
            tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()],
                           axis=1).max(axis=1)
            return float(tr.rolling(window).mean().iloc[-1])
        except Exception:
            return 0.0
