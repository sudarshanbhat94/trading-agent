"""Sleeve 4: NIFTY 50 and BANK NIFTY directional.

Two setups, both regime-aligned and both requiring the options data to agree:

  TREND PULLBACK  — regime ON/strong, index above its 20 and 50 day means, and
    pulling back INTO the 20-day rather than extended above it. Entry on the
    pullback, stop below the recent swing.

  EXTREME REVERSION — PCR at a genuine extreme AND price at a tested support or
    resistance AND India VIX not spiking. All three, because any one alone is
    noise: PCR extremes persist in trends, support breaks, and a VIX spike means
    the level is about to be tested properly rather than held.

Instrument: cash index as the reference. Futures are preferred and the plan is
emitted with `instrument="FUT"` so the router can pick them up; at this book
size a single NIFTY futures lot far exceeds the capital, so the risk manager
will size this to zero and say so. That is a capital fact, handled by the
allocator rather than hidden here.

Index risk is capped BELOW the equity book by its `risk_share` in config.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import Candidate, Sleeve, SleeveDecision

_LOG = logging.getLogger("openstocks.sleeves.index")

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")

PCR_HIGH = 1.30              # too many puts -> crowded bearish -> reversion up
PCR_LOW = 0.70
VIX_SPIKE = 20.0             # above this, levels get tested not held
PULLBACK_MAX = 0.02          # within 2% of the 20-day mean counts as "into" it
EXTENDED = 0.04              # more than 4% above the 20d is chasing
SUPPORT_BAND = 0.01          # within 1% of a tested level

ATR_STOP = 1.5
TARGET_ATR = 2.5
MAX_HOLD_DAYS = 5


class IndexDirectionalSleeve(Sleeve):
    name = "index_directional"
    allowed_regimes = ("ON", "NEUTRAL")

    def propose(self, ctx) -> SleeveDecision:
        regime = ctx.regime.state
        dec = self._decision(regime)
        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks index longs"
            return dec

        cands: list[Candidate] = []
        for sym in INDEX_SYMBOLS:
            bars = ctx.index_bars(sym) if callable(getattr(ctx, "index_bars", None)) else None
            if bars is None or len(bars) < 60:
                dec.reject(sym, "no index history")
                continue
            opt = ctx.options_view(sym) if callable(getattr(ctx, "options_view", None)) else {}

            cand, why = self._trend_pullback(sym, bars, opt, ctx)
            if cand is None:
                cand, why2 = self._extreme_reversion(sym, bars, opt, ctx)
                why = why if cand is not None else f"{why}; {why2}"
            if cand is None:
                dec.reject(sym, why)
                continue
            cands.append(cand)

        cands = self._sane_only(cands, dec)
        cfg = getattr(ctx.settings, self.name)
        dec.candidates = self._rank(cands, cfg.max_positions)
        return dec

    # -- setups ---------------------------------------------------------
    def _trend_pullback(self, sym, bars, opt, ctx) -> tuple[Candidate | None, str]:
        try:
            c = bars["close"]
            close = float(c.iloc[-1])
            sma20, sma50 = float(c.tail(20).mean()), float(c.tail(50).mean())
            if not (close > sma50):
                return None, "below the 50-day mean"
            if not ctx.regime.full_system:
                return None, "trend setup needs regime ON"
            ext = close / sma20 - 1
            if ext > EXTENDED:
                return None, f"{ext*100:.1f}% above the 20-day — extended, not a pullback"
            if ext < -PULLBACK_MAX:
                return None, "below the 20-day — trend not intact"

            vix = self._vix(opt, ctx)
            if vix and vix > VIX_SPIKE:
                return None, f"VIX {vix:.1f} — too hot for a trend entry"

            atr = self._atr(bars)
            if atr <= 0:
                return None, "no ATR"
            swing_low = float(bars["low"].tail(10).min())
            stop = min(swing_low, close - ATR_STOP * atr)
            return Candidate(
                symbol=sym, sleeve=self.name,
                score=float(min(max(0.55 + (0.02 - abs(ext)) * 10, 0.0), 1.0)),
                entry=close, stop=stop, target=close + TARGET_ATR * atr,
                max_hold_days=MAX_HOLD_DAYS, instrument="FUT",
                why=dict(setup="index_trend_pullback", ext_from_sma20=round(ext, 4),
                         vix=vix, pcr=opt.get("pcr"), regime=ctx.regime.state)), ""
        except Exception as exc:
            return None, f"trend error: {type(exc).__name__}"

    def _extreme_reversion(self, sym, bars, opt, ctx) -> tuple[Candidate | None, str]:
        try:
            pcr = opt.get("pcr")
            if pcr is None:
                return None, "no PCR available"
            if pcr < PCR_HIGH:
                return None, f"PCR {pcr:.2f} not at a bullish extreme"

            vix = self._vix(opt, ctx)
            if vix and vix > VIX_SPIKE:
                return None, f"VIX {vix:.1f} — level likely to break, not hold"

            c = bars["close"]
            close = float(c.iloc[-1])
            support = float(bars["low"].tail(20).min())
            if support <= 0 or (close / support - 1) > SUPPORT_BAND:
                return None, "not at a tested support"

            max_pain = opt.get("max_pain")
            if max_pain and close < float(max_pain) * 0.97:
                return None, "below max pain — no options support here"

            atr = self._atr(bars)
            if atr <= 0:
                return None, "no ATR"
            return Candidate(
                symbol=sym, sleeve=self.name, score=0.60,
                entry=close, stop=support - ATR_STOP * atr,
                target=close + TARGET_ATR * atr, max_hold_days=MAX_HOLD_DAYS,
                instrument="FUT",
                why=dict(setup="index_pcr_reversion", pcr=pcr, vix=vix,
                         max_pain=max_pain, support=support,
                         regime=ctx.regime.state)), ""
        except Exception as exc:
            return None, f"reversion error: {type(exc).__name__}"

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _vix(opt: dict, ctx) -> float | None:
        v = opt.get("vix")
        if v is None and callable(getattr(ctx, "india_vix", None)):
            try:
                v = ctx.india_vix()
            except Exception:
                v = None
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _atr(bars: pd.DataFrame, window: int = 14) -> float:
        try:
            h, l, c = bars["high"], bars["low"], bars["close"]
            tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()],
                           axis=1).max(axis=1)
            return float(tr.rolling(window).mean().iloc[-1])
        except Exception:
            return 0.0
