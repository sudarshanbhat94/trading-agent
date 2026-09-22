"""Master market gate, read from completed NIFTYBEES sessions.

ON means the latest completed close is above its 200-session mean; NEUTRAL is
the narrow boundary around that mean; OFF blocks all new production entries.
The legacy synthetic regime is used only when the benchmark is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import v2_engine as eng

_LOG = logging.getLogger("openstocks.sleeves.regime")

#: fraction of the universe that must be above its own 20-day mean for the
#: market to count as genuinely healthy rather than index-led
BREADTH_ON = 0.45
BREADTH_NEUTRAL = 0.35


@dataclass
class RegimeView:
    state: str                  # ON | NEUTRAL | OFF
    strong: bool
    breadth: float              # 0..1, share of names above their 20d mean
    raw_state: str              # what v2_engine said before tightening
    reason: str

    @property
    def allows_equity_longs(self) -> bool:
        return self.state in ("ON", "NEUTRAL")

    @property
    def full_system(self) -> bool:
        return self.state == "ON"


class RegimeGate:
    """Computes the regime view once per pass and hands it to every sleeve."""

    def __init__(self, lookback: int = 50):
        self.lookback = lookback

    def view(self, tails: dict, market_df: pd.DataFrame, asof) -> RegimeView:
        raw = eng.regime_state(market_df, asof, self.lookback)
        strong = eng.regime_strong(market_df, asof, self.lookback)
        benchmark = tails.get("NIFTYBEES")
        benchmark_used = False
        if benchmark is not None:
            try:
                close = benchmark["close"].loc[:asof]
                if len(close) >= 200:
                    benchmark_used = True
                    price, sma = float(close.iloc[-1]), float(close.tail(200).mean())
                    earlier = float(close.iloc[:-20].tail(200).mean()) if len(close) >= 220 else sma
                    slope = sma / earlier - 1 if earlier else 0.0
                    distance = price / sma - 1
                    raw = "ON" if distance > .001 else ("NEUTRAL" if distance >= -.001 else "OFF")
                    strong = raw == "ON" and distance > .03 and slope > 0
            except Exception:
                raw = "OFF"
        breadth = self._breadth(tails, asof)

        state, reason = raw, ("Nifty ETF versus its 200-session trend"
                              if benchmark_used else "synthetic fallback")
        if not benchmark_used and raw == "ON" and breadth < BREADTH_ON:
            state = "NEUTRAL"
            reason = (f"index says ON but breadth is {breadth:.0%} "
                      f"(<{BREADTH_ON:.0%}) — index-led, not broad")
        elif not benchmark_used and raw == "NEUTRAL" and breadth < BREADTH_NEUTRAL:
            state = "OFF"
            reason = (f"NEUTRAL with breadth {breadth:.0%} "
                      f"(<{BREADTH_NEUTRAL:.0%}) — the median name is falling")
        elif raw == "OFF":
            reason = "index below its mean or trending down"

        view = RegimeView(state=state, strong=strong, breadth=breadth,
                          raw_state=raw, reason=reason)
        _LOG.info("REGIME %s (raw %s, breadth %.0f%%, strong=%s) — %s",
                  view.state, view.raw_state, view.breadth * 100, view.strong, view.reason)
        return view

    @staticmethod
    def _breadth(tails: dict, asof) -> float:
        """Share of the universe trading above its own 20-day mean.

        This is the check the index-only test cannot make. Computed on the same
        panel the sleeves screen over, so it cannot disagree with them about
        what "the market" is.
        """
        above = total = 0
        for g in tails.values():
            try:
                if asof not in g.index:
                    continue
                c = g["close"].loc[:asof]
                if len(c) < 20:
                    continue
                sma20 = float(c.tail(20).mean())
                if sma20 <= 0 or np.isnan(sma20):
                    continue
                total += 1
                above += float(c.iloc[-1]) > sma20
            except Exception:
                continue
        return (above / total) if total else 0.0
