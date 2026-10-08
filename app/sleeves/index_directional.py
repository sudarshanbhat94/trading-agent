"""Nifty 50 and Bank Nifty analysis using actual index levels.

Index points are not cash shares. Derivative entries require a separately
reviewed contract, lot, margin and adapter. No proxy fund order is emitted.
"""
from .base import Sleeve
import math

INDEX_SYMBOLS=('NIFTY','BANKNIFTY')
# No production cash-index route. Accounting fixtures may inject an identity
# to exercise historical postings without permitting a real index-level buy.
SYMBOL=None

def monthly_rebalance(asof,trade_date):
    try:return (asof.year,asof.month)!=(trade_date.year,trade_date.month)
    except AttributeError:return False

class IndexDirectionalSleeve(Sleeve):
    name='index_directional'
    allowed_regimes=('ON','NEUTRAL')

    def propose(self,ctx):
        dec=self._decision(ctx.regime.state)
        readings=[]
        for symbol in INDEX_SYMBOLS:
            bars=ctx.tails.get(symbol)
            if bars is None or ctx.asof not in bars.index or len(bars.loc[:ctx.asof])<50:
                readings.append(dict(symbol=symbol,status='history unavailable'));continue
            close=bars.loc[:ctx.asof,'close']
            if not close.index.is_unique or not all(math.isfinite(v) and v>0 for v in close):
                readings.append(dict(symbol=symbol,status='invalid index history'));continue
            mean=float(close.tail(50).mean());last=float(close.iloc[-1])
            readings.append(dict(symbol=symbol,status='analysis active',completed_close=round(last,2),
                                 trend_mean=round(mean,2),lookback_sessions=50,
                                 distance_pct=round((last/mean-1)*100,2),
                                 quote=(ctx.live.get(symbol) or {}).get('price'),
                                 execution='Requires an eligible derivative contract and sufficient lot/margin risk budget'))
        dec.diagnostics=dict(indices=readings,source='actual index levels',execution='contract required')
        dec.note='Nifty 50 and Bank Nifty screening active; index levels cannot be bought as cash shares'
        dec.active=any(r['status']=='analysis active' for r in readings)
        return dec
