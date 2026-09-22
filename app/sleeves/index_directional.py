"""Conservative Nifty exposure through the liquid NIFTYBEES ETF.

The old futures/PCR proposal could never route a position with Rs 10,000.
This rule uses completed NIFTYBEES data, enters only above its 200-session
trend in a broad ON regime, and exits when the master regime turns OFF.
BANKBEES is absent because its independently frozen replay lost money.
"""
from __future__ import annotations

from .base import Candidate, Sleeve

SYMBOL = "NIFTYBEES"
ALLOCATION_PCT = 0.35


def monthly_rebalance(asof, trade_date) -> bool:
    """The first session of a new month, using only the prior session close."""
    try:
        return (asof.year, asof.month) != (trade_date.year, trade_date.month)
    except AttributeError:
        return False


class IndexDirectionalSleeve(Sleeve):
    name = "index_directional"
    allowed_regimes = ("ON",)

    def propose(self, ctx):
        regime = ctx.regime.state
        dec = self._decision(regime)
        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks Nifty exposure"
            return dec
        if not monthly_rebalance(ctx.asof, ctx.trade_date):
            dec.active = False
            dec.note = "monthly rule; next rebalance has not arrived"
            return dec
        bars = ctx.tails.get(SYMBOL)
        quote = ctx.live.get(SYMBOL) or {}
        if bars is None or ctx.asof not in bars.index or len(bars.loc[:ctx.asof]) < 200:
            dec.reject(SYMBOL, "200 completed sessions unavailable")
            return dec
        close = bars["close"].loc[:ctx.asof]
        reference = float(close.iloc[-1])
        sma200 = float(close.tail(200).mean())
        entry = float(quote.get("price") or reference)
        if entry <= sma200:
            dec.reject(SYMBOL, "not above the 200-session trend")
            return dec
        # The researched exit is the next monthly regime read. This far-away
        # disaster stop only guards an exceptional gap; it is not a tuning knob.
        stop = entry * .75
        if stop >= entry:
            dec.reject(SYMBOL, "trend invalidated before entry")
            return dec
        score = min(1.0, .60 + max(0.0, reference / sma200 - 1) * 4)
        dec.note = "NIFTYBEES only; BANKBEES failed the independent replay"
        dec.candidates = [Candidate(
            symbol=SYMBOL, sleeve=self.name, score=score, entry=entry,
            stop=stop, target=0.0, trail_pct=0.0, max_hold_days=0,
            instrument="EQ", allocation_pct=ALLOCATION_PCT,
            why=dict(setup="nifty_monthly_200d_trend",
                completed_close=reference, sma200=sma200, regime=regime,
                research_status="positive candidate; forward paper proof required"))]
        return dec
