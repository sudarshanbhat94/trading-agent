"""What a round trip actually costs at an Indian broker.

REPLACES A FLAT PERCENTAGE. The engine charged COST_SIDE — 0.20% a side, 0.40%
a round trip — regardless of ticket size or product. That is a reasonable
average for the paper book's Rs 16,666 positions and badly wrong everywhere
else, because the two largest components do not scale with the trade:

    Rs 3,000 delivery round trip   Rs 77   2.58%   (modelled 0.40%)
    Rs 9,000 delivery round trip   Rs 91   1.01%
    Rs 9,000 INTRADAY round trip   Rs 24   0.27%
    Rs 50,000 delivery round trip  Rs 182  0.36%

Brokerage is a FLAT Rs 20 per leg on delivery, and the DP charge is a flat
Rs 20 on every sell. On a small ticket those two are most of the cost; on a
large one they vanish. A single percentage cannot express that, and using one
means the paper book reports profits the live account will not see — the
specific way a backtest lies about a small account.

Rates are Upstox's published equity schedule (verified 2026-08-05). They are
constants here rather than settings because getting them wrong is not a
preference, and a broker change should be a visible edit.

  https://upstox.com/calculator/brokerage-calculator/
"""
from __future__ import annotations

# Per LEG unless noted.
BROKERAGE_FLAT = 20.0            # Rs per executed order, both products
BROKERAGE_PCT_DELIVERY = 0.025   # ...or 2.5%, whichever is LOWER
BROKERAGE_PCT_INTRADAY = 0.001   # ...or 0.1%, whichever is lower
STT_DELIVERY = 0.001             # both legs
STT_INTRADAY_SELL = 0.00025      # sell leg only
EXCHANGE_TXN = 0.0000322         # NSE equity, both legs
STAMP_DELIVERY = 0.00015         # BUY leg only
STAMP_INTRADAY = 0.00003         # BUY leg only
SEBI_TURNOVER = 1e-7             # Rs 10 per crore, both legs
DP_CHARGE = 20.0                 # delivery SELL only, per scrip per day
GST = 0.18                       # on brokerage + exchange charges

INTRADAY = "I"
DELIVERY = "D"


def round_trip(buy_value: float, sell_value: float = None, product: str = DELIVERY) -> float:
    """Total charges in rupees for one complete buy-and-sell.

    `sell_value` defaults to the buy value; passing the real one matters on a
    big move, where STT and exchange charges are levied on the larger leg.
    """
    buy_value = max(0.0, float(buy_value or 0.0))
    sell_value = buy_value if sell_value is None else max(0.0, float(sell_value))
    if buy_value <= 0:
        return 0.0
    intraday = str(product).upper() == INTRADAY
    pct = BROKERAGE_PCT_INTRADAY if intraday else BROKERAGE_PCT_DELIVERY
    brokerage = (min(BROKERAGE_FLAT, buy_value * pct)
                 + min(BROKERAGE_FLAT, sell_value * pct))
    if intraday:
        stt = sell_value * STT_INTRADAY_SELL
        stamp = buy_value * STAMP_INTRADAY
        dp = 0.0
    else:
        stt = (buy_value + sell_value) * STT_DELIVERY
        stamp = buy_value * STAMP_DELIVERY
        dp = DP_CHARGE
    exchange = (buy_value + sell_value) * EXCHANGE_TXN
    sebi = (buy_value + sell_value) * SEBI_TURNOVER
    gst = (brokerage + exchange + dp) * GST
    return brokerage + stt + exchange + stamp + sebi + dp + gst


def round_trip_pct(buy_value: float, sell_value: float = None,
                   product: str = DELIVERY) -> float:
    """The same thing as a percentage of the position, for reporting."""
    buy_value = float(buy_value or 0.0)
    if buy_value <= 0:
        return 0.0
    return round_trip(buy_value, sell_value, product) / buy_value * 100.0


def breakeven_move_pct(buy_value: float, product: str = DELIVERY) -> float:
    """How far the stock must move just to get the money back.

    The number that decides whether a strategy is viable at a given account
    size: a 1% intraday move on Rs 9,000 is a LOSS as delivery and a gain as
    intraday, and no amount of signal quality changes that.
    """
    return round_trip_pct(buy_value, buy_value, product)
