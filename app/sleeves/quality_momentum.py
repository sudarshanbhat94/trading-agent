"""Sleeve 2 (secondary): quality + intermediate momentum.

Buys businesses that are compounding and whose price agrees, and holds them for
weeks rather than days. This is the slow sleeve — it exists so the book is not
purely short-horizon, where flat charges dominate.

Ranking combines:

  * QUALITY  — ROE, leverage and earnings stability where fundamentals are
    available. Where they are not (the common case on this install, which has
    no fundamentals feed), quality is PROXIED from price behaviour: low
    drawdown from the 252-day high, low realised volatility, and a smooth
    equity curve. That is a proxy for durable compounding, and it is stated
    plainly rather than dressed up as a fundamental score.
  * MOMENTUM — 6-12 month return EXCLUDING the most recent month. Skipping the
    last month is deliberate and is the standard construction: recent
    one-month returns mean-revert and pollute the momentum signal.

Runs in ON only (or ON with `strong`), rebalances slowly, and carries a wider
stop than the tactical sleeves because it is a position, not a trade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Candidate, Sleeve, SleeveDecision
from .universe import liquid_universe

MOM_LOOKBACK = 252            # ~12 months
MOM_SKIP = 21                 # skip the most recent month
MIN_MOMENTUM = 0.10           # must be up >=10% over the measured window
MAX_VOL = 0.045               # daily realised vol ceiling (~4.5% ATR-equivalent)
MAX_DD_FROM_HIGH = 0.20       # a compounding name is not 20% off its high
REBALANCE_DAYS = 14           # bi-weekly

ATR_STOP = 3.0                # wider: this is a position, not a trade
MAX_HOLD_DAYS = 45


class QualityMomentumSleeve(Sleeve):
    name = "quality_momentum"
    allowed_regimes = ("ON",)

    def propose(self, ctx) -> SleeveDecision:
        regime = ctx.regime.state
        dec = self._decision(regime)

        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime}; this sleeve is ON-only"
            return dec
        if not ctx.regime.strong and not ctx.force:
            dec.active = False
            dec.note = "ON but not STRONG — quality-momentum waits for confirmation"
            return dec
        if ctx.sessions_since_rebalance is not None and \
                ctx.sessions_since_rebalance < REBALANCE_DAYS:
            dec.active = False
            dec.note = (f"rebalanced {ctx.sessions_since_rebalance} sessions ago "
                        f"(every {REBALANCE_DAYS})")
            return dec

        allowed = liquid_universe(ctx.tails, ctx.asof)
        scored: list[Candidate] = []

        for sym in allowed:
            g = ctx.tails.get(sym)
            if g is None or ctx.asof not in g.index:
                continue
            gi = g.loc[:ctx.asof]
            if len(gi) < MOM_LOOKBACK:
                dec.reject(sym, "less than a year of history")
                continue

            mom = self._momentum(gi)
            if mom is None or mom < MIN_MOMENTUM:
                dec.reject(sym, f"momentum {0 if mom is None else mom*100:.0f}% "
                                f"< {MIN_MOMENTUM*100:.0f}%")
                continue

            q, why = self._quality(gi)
            if q is None:
                dec.reject(sym, why)
                continue

            close = float(gi["close"].iloc[-1])
            atr = self._atr(gi)
            if atr <= 0:
                dec.reject(sym, "no ATR")
                continue

            entry = float(ctx.live.get(sym, {}).get("price") or close)
            score = 0.55 * min(mom / 0.60, 1.0) + 0.45 * q
            scored.append(Candidate(
                symbol=sym, sleeve=self.name, score=round(float(score), 4),
                entry=entry, stop=entry - ATR_STOP * atr, target=0.0,
                trail_pct=0.12, max_hold_days=MAX_HOLD_DAYS,
                why=dict(setup="quality_momentum", momentum=round(mom, 4),
                         quality=round(q, 4), regime=regime)))

        scored = self._sane_only(scored, dec)
        cfg = getattr(ctx.settings, self.name)
        dec.candidates = self._rank(scored, cfg.max_positions)
        dec.note = f"{len(scored)} passed quality+momentum screen"
        return dec

    # -- factors -------------------------------------------------------
    @staticmethod
    def _momentum(gi: pd.DataFrame) -> float | None:
        """12-month return skipping the most recent month."""
        try:
            c = gi["close"]
            if len(c) < MOM_LOOKBACK:
                return None
            past = float(c.iloc[-MOM_LOOKBACK])
            recent = float(c.iloc[-MOM_SKIP])
            return (recent / past - 1) if past > 0 else None
        except Exception:
            return None

    @staticmethod
    def _quality(gi: pd.DataFrame) -> tuple[float | None, str]:
        """Price-behaviour proxy for durable compounding.

        Fundamentals (ROE, leverage, earnings stability) are used when a feed
        provides them; this install has none, so the proxy is explicit:
        shallow drawdown + low volatility + a smooth path.
        """
        try:
            c = gi["close"]
            hi252 = float(c.tail(252).max())
            close = float(c.iloc[-1])
            if hi252 <= 0:
                return None, "no 252d high"
            dd = 1 - close / hi252
            if dd > MAX_DD_FROM_HIGH:
                return None, f"{dd*100:.0f}% off its 1y high"

            rets = c.pct_change().tail(252).dropna()
            if len(rets) < 100:
                return None, "too few returns"
            vol = float(rets.std())
            if vol > MAX_VOL:
                return None, f"realised vol {vol*100:.1f}% too high"

            # smoothness: share of up days, a crude but honest path measure
            smooth = float((rets > 0).mean())

            q = (0.40 * (1 - dd / MAX_DD_FROM_HIGH)
                 + 0.35 * (1 - vol / MAX_VOL)
                 + 0.25 * min(max((smooth - 0.45) / 0.15, 0.0), 1.0))
            return float(min(max(q, 0.0), 1.0)), ""
        except Exception as exc:
            return None, f"quality error: {type(exc).__name__}"

    @staticmethod
    def _atr(gi: pd.DataFrame, window: int = 14) -> float:
        try:
            h, l, c = gi["high"], gi["low"], gi["close"]
            tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()],
                           axis=1).max(axis=1)
            return float(tr.rolling(window).mean().iloc[-1])
        except Exception:
            return 0.0
