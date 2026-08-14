"""The master gate. Nothing enters in any sleeve without passing here first.

Wraps the existing validated `v2_engine.regime_state` and tightens it. The old
engine treated NEUTRAL as tradeable for everything, which is how dip-buying ran
through a flat, directionless tape. Here:

    ON       full system: every sleeve may propose
    NEUTRAL  primary mean-reversion only, and only its very best setup
    OFF      no new equity longs at all, from any sleeve

The tightening is the `breadth` and `vol` confirmation added on top of the raw
index-vs-mean test. A market can sit above its 50-day mean on the back of five
heavyweights while the median name is falling; that is not a regime a
dip-buying book should be long into, and the raw test cannot see it.
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
        breadth = self._breadth(tails, asof)

        state, reason = raw, "matches v2_engine"
        if raw == "ON" and breadth < BREADTH_ON:
            state = "NEUTRAL"
            reason = (f"index says ON but breadth is {breadth:.0%} "
                      f"(<{BREADTH_ON:.0%}) — index-led, not broad")
        elif raw == "NEUTRAL" and breadth < BREADTH_NEUTRAL:
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
