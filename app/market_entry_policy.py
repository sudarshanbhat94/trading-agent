"""Paper stock admission, separate from the measured market trend.

An index downtrend is context, not proof that every constituent is unsuitable.
Paper stocks still require their own frozen evidence, confirmation and account
risk checks. Unknown/stale context never grants entry. Live permission and the
index route retain their separate conservative gates.
"""

VERSION = 'stock-specific-paper-v1'
STOCK_MODEL_VERSION = 'selective-paper-v4-observe-first'
KNOWN_TRENDS = frozenset(('ON', 'NEUTRAL', 'OFF'))


def allows_paper_entry(regime, sleeve=None):
    return isinstance(regime, str) and regime in KNOWN_TRENDS and (sleeve != 'index_directional' or regime == 'ON')


def allows_live_entry(regime):
    return regime in ('ON', 'NEUTRAL')


def describe(regime):
    return {'ON': 'Supportive market', 'NEUTRAL': 'Mixed market',
            'OFF': 'Defensive market'}.get(regime, 'Market data unavailable')
