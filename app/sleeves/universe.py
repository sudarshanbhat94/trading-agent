"""The tradeable universe: liquid NSE names only, plus the index products.

Two jobs:

  * keep the sleeves inside names that can actually be filled and that move
    enough to clear a round trip at this book size;
  * expose the index products (NIFTY, BANKNIFTY) that the index sleeve trades.

Liquidity is judged on TURNOVER, not price or index membership, because
membership lists go stale and turnover is knowable at the time from data we
already hold. A Rs 25 crore/day floor keeps roughly the Nifty-100-and-better
band without needing a membership file, and rises naturally as the market does.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

_LOG = logging.getLogger("openstocks.sleeves.universe")

#: median daily turnover floor, in rupees. Rs 25 crore.
MIN_TURNOVER = 2.5e8
#: A Rs 10,000 book with 3 slots has ~Rs 3,333 a slot. A Rs 1,200 share is
#: already 36% of a slot in ONE share, so that is the ceiling; above it the
#: position cannot be sized sensibly and rounding to whole shares dominates.
#: Sub-Rs-50 names are where fills stop being real.
MIN_PRICE, MAX_PRICE = 50.0, 1_200.0
#: index products the index sleeve may trade
INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")
#: never treat these as stocks — they are cash-parking or index vehicles
EXCLUDE_PREFIXES = ("NIFTYBEES", "BANKBEES", "LIQUIDBEES", "GOLDBEES", "JUNIORBEES")


def liquid_universe(tails: dict, asof, min_turnover: float = MIN_TURNOVER,
                    max_names: int | None = None) -> list[str]:
    """Symbols that are liquid enough and priced sensibly for this book."""
    rows = []
    for sym, g in tails.items():
        if any(sym.upper().startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        try:
            if asof not in g.index:
                continue
            gi = g.loc[:asof]
            if len(gi) < 60:
                continue
            px = float(gi["close"].iloc[-1])
            if not (MIN_PRICE <= px <= MAX_PRICE):
                continue
            turn = float((gi["close"] * gi["volume"]).tail(20).median())
            if not np.isfinite(turn) or turn < min_turnover:
                continue
            rows.append((turn, sym))
        except Exception:
            continue
    rows.sort(reverse=True)
    out = [s for _t, s in rows]
    if max_names:
        out = out[:max_names]
    _LOG.info("universe: %d liquid names (turnover >= Rs %.0f cr, price Rs %.0f-%.0f)",
              len(out), min_turnover / 1e7, MIN_PRICE, MAX_PRICE)
    return out


def turnover(g: pd.DataFrame, asof, window: int = 20) -> float:
    try:
        gi = g.loc[:asof]
        return float((gi["close"] * gi["volume"]).tail(window).median())
    except Exception:
        return 0.0
