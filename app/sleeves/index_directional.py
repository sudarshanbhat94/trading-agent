"""Conservative Nifty exposure through the liquid NIFTYBEES ETF.

The old futures/PCR proposal could never route a position with Rs 10,000.
This rule uses completed NIFTYBEES data, enters only above its 200-session
trend in a broad ON regime, and exits when the master regime turns OFF.
BANKBEES is absent because its independently frozen replay lost money.
"""
from __future__ import annotations

from .base import Candidate, Sleeve

SYMBOL = "NIFTYBEES"
# Frozen external replay, Rs 10,000 and full delivery costs at 20 bps slip:
# development +28.45% / -9.36% DD; holdout +4.86% / -6.87% DD.  Larger
# allocations breached the book's 10% development drawdown limit.
ALLOCATION_PCT = 0.50


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
        bootstrap = bool(getattr(ctx, "bootstrap_entry", False))
        bars = ctx.tails.get(SYMBOL)
        quote = ctx.live.get(SYMBOL) or {}
        if bars is None or ctx.asof not in bars.index or len(bars.loc[:ctx.asof]) < 200:
            dec.active = False
            dec.note = "200 completed NIFTYBEES sessions unavailable"
            return dec
        close = bars["close"].loc[:ctx.asof]
        reference = float(close.iloc[-1])
        sma200 = float(close.tail(200).mean())
        entry = float(quote.get("price") or reference)
        dec.diagnostics = dict(
            symbol=SYMBOL, completed_close=round(reference, 2),
            live_price=round(entry, 2), sma200=round(sma200, 2),
            distance_pct=round((reference / sma200 - 1) * 100, 2) if sma200 else None,
            trigger="completed close above 200-session mean",
            review_today=(monthly_rebalance(ctx.asof, ctx.trade_date) or bootstrap),
            bootstrap_entry=bootstrap,
            allocation_pct=ALLOCATION_PCT)
        if not self.may_run(regime):
            dec.active = False
            dec.note = f"regime {regime} blocks Nifty exposure"
            return dec
        if not monthly_rebalance(ctx.asof, ctx.trade_date) and not bootstrap:
            dec.active = False
            dec.note = "monthly rule; next rebalance has not arrived"
            return dec
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
        dec.note = ("fresh-book trend entry" if bootstrap else "monthly trend entry")
        dec.candidates = [Candidate(
            symbol=SYMBOL, sleeve=self.name, score=score, entry=entry,
            stop=stop, target=0.0, trail_pct=0.0, max_hold_days=0,
            instrument="EQ", allocation_pct=ALLOCATION_PCT,
            why=dict(setup="nifty_monthly_200d_trend",
                completed_close=reference, sma200=sma200, regime=regime,
                bootstrap_entry=bootstrap,
                research_status="positive candidate; forward paper proof required"))]
        return dec
