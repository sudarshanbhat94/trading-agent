"""Actual Nifty 50 trend and stock breadth; no cash-fund proxy."""
from dataclasses import dataclass, field
import logging
import math
import pandas as pd
from .. import v2_engine as eng

_LOG = logging.getLogger('openstocks.sleeves.regime')
BREADTH_ON = .45
BREADTH_NEUTRAL = .35
MODEL_VERSION = 'actual-index-breadth-v1'

@dataclass
class RegimeView:
    state: str
    strong: bool
    breadth: float
    raw_state: str
    reason: str
    source: str = 'individual-stock universe'
    diagnostics: dict = field(default_factory=dict)

    @property
    def allows_equity_longs(self): return self.state in ('ON','NEUTRAL')
    @property
    def full_system(self): return self.state=='ON'

class RegimeGate:
    def __init__(self,lookback=50): self.lookback=lookback

    def view(self,tails,market_df,asof,eligible_symbols=None):
        equities={s:g for s,g in tails.items() if s not in ('NIFTY','BANKNIFTY')
                  and not s.upper().endswith('BEES')
                  and (eligible_symbols is None or s in eligible_symbols)}
        breadth=self._breadth(equities,asof)
        source='individual-stock universe'
        diagnostic=dict(model_version=MODEL_VERSION,lookback_sessions=self.lookback,
                        breadth_above20_pct=round(breadth*100,1),stocks=len(equities))
        reference=market_df
        try:
            index=tails.get('NIFTY')
            if index is not None and asof in index.index:
                close=pd.to_numeric(index.loc[:asof,'close'],errors='coerce')
                if len(close)>=self.lookback and close.index.is_unique and all(math.isfinite(v) and v>0 for v in close):
                    reference=pd.DataFrame({'mkt_cum':close})
                    source='actual Nifty 50 index'
                    diagnostic.update(symbol='NIFTY',completed_close=round(float(close.iloc[-1]),2),
                                      trend_mean=round(float(close.tail(self.lookback).mean()),2),
                                      source_known_at=index.attrs.get('known_at'))
            raw=eng.regime_state(reference,asof,self.lookback)
            strong=eng.regime_strong(reference,asof,self.lookback)
            values=reference.loc[:asof,'mkt_cum'].tail(self.lookback)
            if len(values)<self.lookback or not values.index.is_unique or not all(math.isfinite(v) and v>0 for v in values):
                raise ValueError('invalid completed market history')
        except (KeyError,TypeError,ValueError,AttributeError):
            raw='OFF';strong=False;source='market evidence unavailable'
        state=raw
        reason=source+' completed-session trend'
        if raw=='ON' and breadth<BREADTH_ON:
            state='NEUTRAL';strong=False
            reason=f'{source} trend is positive but stock breadth is {breadth:.1%}'
        elif raw=='NEUTRAL' and breadth<BREADTH_NEUTRAL:
            state='OFF';strong=False
            reason=f'Weak {source} trend and stock breadth {breadth:.1%}'
        elif raw=='OFF':
            reason=f'{source} is below its {self.lookback}-session trend or has weak 20-session momentum'
        if diagnostic.get('trend_mean'):
            diagnostic['distance_pct']=round((diagnostic['completed_close']/diagnostic['trend_mean']-1)*100,2)
        diagnostic.update(source=source,raw_state=raw)
        _LOG.info('REGIME %s · source=%s · stock breadth %.1f%%',state,source,breadth*100)
        return RegimeView(state,strong,breadth,raw,reason,source,diagnostic)

    @staticmethod
    def _breadth(tails,asof):
        above=total=0
        for symbol,g in tails.items():
            if symbol in ('NIFTY','BANKNIFTY') or symbol.upper().endswith('BEES'):continue
            try:
                if asof not in g.index or not g.index.is_unique:continue
                c=pd.to_numeric(g.loc[:asof,'close'],errors='coerce').tail(20)
                if len(c)!=20 or not all(math.isfinite(v) and v>0 for v in c):continue
                total+=1;above+=float(c.iloc[-1])>float(c.mean())
            except (KeyError,TypeError,ValueError):continue
        return above/total if total else 0.
