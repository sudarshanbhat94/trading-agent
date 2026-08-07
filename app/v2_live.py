"""v2 live engine - one shared capital pool per market, both strategies.

Budget is a TOTAL per market (US $20,000, India ₹1,00,000), shared across the
swing + gap strategies - NOT per trade, NOT per book. Positions are sized from
that single pool; cash is deducted on buy and returned on exit. Runs as a
background thread inside opentrade.service, only during real market hours.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import v2_engine as eng
from . import market_regions
from . import factor_investigation as fi
from . import meta_filter

MAIN_DB = os.environ.get("OPENSTOCKS_DB", "/opt/opentrade/var/trading_agent.db")
V2_DB = os.environ.get("V2_PAPER_DB", "/opt/opentrade/var/v2_paper.db")
IST = timezone(timedelta(hours=5, minutes=30))
LIVE_SOURCE = {"IN": "upstox-live", "US": "alpaca-iex-live"}
BUDGET = {"IN": 100000.0, "US": 20000.0}     # TOTAL paper capital per market
# Markets the engine actually trades. US is parked while we stabilise India first
# (add "US" back here to re-enable it — all US config/code below stays intact).
ENABLED_MARKETS = ["IN"]
MAX_ROUND_TRIPS_PER_DAY = 3   # per symbol, per market. Churn circuit breaker —
                              # see record_entry. A lane trades a name once or
                              # twice a day; past that it is oscillating, not
                              # deciding. 0 disables.
MAXPOS = {"IN": 6, "US": 14}                 # max concurrent positions per market.
                                             # IN 14->6 (user call): the meta filter already
                                             # keeps only the top few signals/day, so fewer,
                                             # BIGGER positions (~16.6k vs 7.1k) make each win
                                             # meaningful without changing which trades we take.
                                             # Backtested 10 vs 14 (2024->now): IN ret -13.1->-7.1%,
                                             # maxDD 13.6->9.6%; US equal Sharpe. More names, not
                                             # bigger bets -> better capital use at same risk.
COST_SIDE = {"IN": 0.40 / 200, "US": 0.20 / 200}   # round-trip cost incl. ~5bps/side slippage
                                                    # (paper fills at the poll price flatter reality;
                                                    #  this keeps the P&L honest vs a live broker)
EARNINGS_BLOCK_DAYS = 3                             # no NEW entries within this many days of earnings
# Exchange holidays the busday hold-clock should skip (weekends it already knows).
# US 2026 is deterministic; IN lists the fixed-date holidays (movable festival
# dates omitted rather than guessed - a partial list still fixes those weeks).
MARKET_HOLIDAYS = {
    "IN": ["2026-01-26", "2026-04-03", "2026-04-14", "2026-05-01", "2026-10-02", "2026-12-25"],
    "US": ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
}
# per-strategy trade plan; both draw from the shared market pool
PLAN = {
    "gap_momentum":  dict(regime_gated=False, threshold=0.38, atr_stop=1.5, atr_target=0.0, trail=0.10, priority=0),
    # 52w-high breakout sleeve: STRONG uptrend only (see eng.regime_strong), ATR
    # trail set per-entry, 40d hold. Backtested: US +26.5->+51.4%, Sharpe 2.19,
    # maxDD down; IN improves even in the bear window.
    # Operator requires a strict target on this lane. Measured cost of each
    # choice (scripts/breakout_target_bt.py, 2201 entries, point-in-time
    # universe, avg net % per trade):
    #   trail-only +0.896  |  8xATR +0.833  |  6xATR +0.701
    #   4xATR      +0.565  |  3xATR +0.351
    # Wider is strictly better, so the target is set at the widest tested value
    # that is still a hard, definite exit: 8xATR keeps ~93% of the trail-only
    # edge, where the original 4xATR gave up 37% of it. Stop 2xATR => 4:1 R:R.
    # The 2.5xATR trail also stays active, so the exit is whichever comes first
    # — the target is a ceiling, not the only way out.
    "mom_breakout":  dict(regime_gated=False, threshold=0.10, atr_stop=2.0, atr_target=8.0, trail=0.0,  priority=1),
    # regime_gated stays TRUE: this lane buys dips, and the gate is what stops it
    # buying into a falling market. Briefly set False on 2026-07-29 and restored
    # the same day once the operator saw what the regime actually represents.
    # The evidence backs the gate for mean-reversion — daily-portfolio A/B on a
    # point-in-time universe, out-of-sample:
    #     regime ON   +2.2%  maxDD  -6.6%   95 trades
    #     regime OFF  -0.4%  maxDD -18.4%  356 trades
    # The difference is almost entirely DRAWDOWN, not hit rate. Note this is the
    # opposite call to mom_breakout, where the same gate cost half the entries
    # for +0.02pp and was removed — the asymmetry is deliberate.
    # Cost of the gate: while the regime is OFF the lane queues signals and buys
    # nothing. That is the protection working, not a fault.
    # stop 2.0 -> 3.0 ATR and the fixed target REMOVED, on measured evidence.
    # Same 19,510 entries, only the exit varied (scripts/exit_cost_analysis.py),
    # average net % per trade:
    #     hold-only                +0.775   (worst single trade -45.9%)
    #     stop 3ATR                +0.680   (worst -29.7%)
    #     stop 2ATR                +0.569   (worst -19.9%)
    #     stop 2ATR + target 3.5   +0.506   <- what this lane used to run
    # The old settings gave up 35% of the entry edge. The target alone cost
    # -0.06%/trade and capped the best trade at +28% against +69% without it —
    # the same "targets clip the winners that pay for the losers" result the
    # breakout study found independently.
    # The stop is NOT removed even though hold-only scores best: it is what
    # bounds the worst trade at -30% instead of -46%, and one -46% position
    # would undo a quarter of a year's edge.
    "swing_meanrev": dict(regime_gated=True,  threshold=0.55, atr_stop=3.0, atr_target=0.0, trail=0.0,  priority=2),
    # -- DISABLED_LANES defined just below the dict; gap_momentum is quarantined --
    # intraday news-momentum sleeve — entered by intraday_news_pass, never by the
    # daily signal path. atr_stop=1.0 so exit_monitor's atr_est reconstruction
    # ((entry-stop)/atr_stop) stays consistent with the % stop set at entry.
    "intraday_news": dict(regime_gated=False, threshold=0.0, atr_stop=1.0, atr_target=0.0, trail=0.0, priority=0),
    # volume-surge + NSE-catalyst lane (see VOLSURGE). atr_stop=1.0 like intraday_news
    # so exit_monitor's atr_est reconstruction stays consistent with the % stop.
    "volume_surge": dict(regime_gated=False, threshold=0.0, atr_stop=1.0, atr_target=0.0, trail=0.0, priority=0),
}
# Lanes quarantined from live trading (signals still compute for radar, but they
# never open a position). gap_momentum disabled 2026-07-27: proven net LOSER —
# the gap/premove signal ran -0.506%/trade, PF 0.81 over 44,585 backtested trades
# (win 39%, non-monotonic conviction), matching its live record (0/6, -Rs2,336)
# and the meta-filter's own "net loser OOS even after meta" note. A stop-cap A/B
# also showed tightening the stop HURTS (exp +0.63->+0.14%/trade at a -4% cap) and
# doesn't fix the rare overnight gap tail — so the swing stop is left as-is. Re-enable
# gap_momentum only if a reworked signal beats baseline out-of-sample.
# 2026-07-28 (later the same day): every lane EXCEPT gap_momentum is back ON.
# A live system that never opens a position cannot be measured, and the
# catalyst-continuation replacement failed its own backtest (+0.573%/trade
# collapsed to -0.165% once the top 5 names were dropped — it was five stocks,
# not an edge), so there was nothing to run in their place.
#
# This is deliberately a MEASUREMENT run, not a claim of edge. The book was
# reset 2026-07-28T10:22Z, so every lane starts from a clean ledger and the
# next few weeks produce the per-lane record that was wiped. Last known live
# numbers, carried forward honestly:
#   gap_momentum  : STAYS OFF. 0 wins in 6 live trades (-Rs 2,336) and
#                   -0.51%/trade, PF 0.81 over 44,585 backtested trades.
#   swing_meanrev : the only positive live record — 23 trades, +Rs 795,
#                   PF 1.21, win 52%. 23 trades proves nothing; treat as noise
#                   until it rebuilds a sample. Its ~6.6%/yr backtest figure
#                   still carries the load_market look-ahead.
#   volume_surge  : -Rs 302 live when last measured. Re-enabled to gather a
#                   real sample, NOT because it is validated.
#   mom_breakout  : never measured separately.
#   intraday_news : never measured separately.
#   btst          : ~149-trade backtest only; unproven live.
# Judge these on the fresh ledger, not on the numbers above.
# 2026-07-28 (evening): operator narrowed to THREE lanes — mom_breakout,
# volume_surge, intraday_news. swing_meanrev and btst are parked, not deleted.
# Note swing_meanrev held the only positive live record (+Rs 795, PF 1.21 on 23
# trades), so this trades a small measured edge for focus; say so if it is ever
# reviewed rather than letting the ledger imply the lane failed.
DISABLED_LANES = {"gap_momentum", "btst"}
MOM_SLOT_CAP = 2                        # momentum sleeve: at most 2 of the 6-slot book
# mom_breakout used to require a STRONG market uptrend, which is why on
# 2026-07-29 — regime OFF — it took zero trades all morning while the operator
# watched an idle book. Measured on a point-in-time universe
# (scripts/breakout_target_bt.py --regime both), trail-only exit:
#     gate ON   1067 entries, +0.937%/trade
#     gate OFF  2221 entries, +0.919%/trade
# Per trade the gate is worth +0.02pp — nothing — while it removes HALF the
# opportunities, so turning it off captures roughly twice the total edge. That
# matches the separate finding that the regime filter has no timing skill
# (scripts/regime_isolation.py: it is invested on the worse days, and a static
# exposure beats it). The stock-level uptrend filter still applies: a breakout
# is only taken above the name's own 50d average. Set True to restore.
MOM_REQUIRE_STRONG = False
# ---- intraday news-momentum sleeve (user spec: trade TODAY's tape, take the
# money fast, flat by the close). 5-min-bar backtest (150 syms, 58 days):
# intraday momentum ALONE loses (PF ~0.70 every config); the ONLY positive
# variants require a fresh news catalyst (PF 1.3-1.4) — small sample (news feed
# starts 2026-05-17), so this runs as a small capped sleeve to prove itself
# live before it gets more capital.
INTRA = dict(
    move_min=0.02,     # up >= 2% vs TODAY's open (not yesterday's close)
    move_max=0.12,     # >12% intraday = circuit-limit chase risk, skip
    rvol_min=2.0,      # day volume so far >= 2x its usual pace for this time of day
    news_min=0.30,     # fresh positive catalyst (<=24h) REQUIRED — no news, no trade
    min_turnover=2e8,  # >= Rs.20cr avg daily turnover — the backtest evidence is from
                       # liquid names only; micro-cap "fills" in paper are fantasy
    slots=3,           # max concurrent intraday positions (capped paper trial)
    start="09:20", last_entry="11:00",   # backtest: early entries were the best variant;
                                         # afternoon entries LOSE even on fresh news
                                         # (all-day PF 0.97, fresh-6h PF 0.70 vs morning 1.41)
    watch_until="14:30",                 # after last_entry: watch-only Telegram radar, no buys
    squareoff="15:12", # hard flat before the 15:30 close — NO overnight risk
    tp=0.035,          # take +3.5% and get out
    sl=0.025,          # hard stop -2.5%. Widened with volume_surge on 2026-07-29:
                       # the operator requires the two intraday lanes to carry the
                       # same stop/target, and both buy an intraday move that
                       # routinely pulls back more than 1.75% without failing.
    lock=0.015,        # once up +1.5%, stop moves to breakeven: green never goes red
)
# ---- volume-surge + catalyst lane: the "day-1 mover" catcher ----
# Validation (Jul 21-22 movers) showed the intraday edge needs a REAL catalyst,
# and our Bing-RSS news feed was too late/incomplete (tagged only 1 of 4 movers
# same-day). This lane instead gates on NSE's OWN real-time corporate-
# announcements (results / orders / board outcomes) joined with a big volume
# surge + price strength. Full send: competes for the whole book (MAXPOS-bound).
VOLSURGE = dict(
    # 4% -> 2% on 2026-07-29. Requiring a name to be up 4% AND within 1.5% of its
    # day high meant buying at the top of a move that had already happened: all
    # five of that morning's entries reversed straight into the stop. The volume
    # gate (rvol >= 3) is what identifies a real surge; the price threshold was
    # only making the entry late. 2% still filters noise but enters while the
    # move is developing rather than after it.
    move_min=0.02,       # up >= 2% vs PREV CLOSE (a real day move, not noise)
    move_max=0.19,       # >19% => likely upper-circuit locked / un-fillable — skip
    rvol_min=3.0,        # >= 3x usual volume pace for this time of day
    near_high=0.985,     # price within 1.5% of day's high (holding strong, not fading)
    min_turnover=2.5e8,  # >= Rs.25cr avg daily turnover — liquid, fillable names only
    catalyst_sessions=1, # a material NSE filing within this many TRADING sessions
                         # is REQUIRED. Tightened 3 -> 1 on 2026-07-28: operator
                         # rule is act on the latest news only, matching
                         # intraday_news. Still weekend/holiday-aware, so a Friday
                         # result is fresh on Monday (a flat 48h window used to
                         # expire it over the weekend and miss Monday movers like
                         # Dr Lal/KFin). Expect FEWER volume_surge entries.
    slots=6,             # full send: may use the whole book (still MAXPOS-bound)
    # 09:18 (was 09:20) + a 10s scan cadence: operator wants this lane quick on
    # entry. Not opened before 09:18 — the first minutes have too little volume
    # for rvol to mean anything, so entering there is guessing, not speed.
    start="09:18", last_entry="14:00",   # results/orders drop all day -> wide window
    squareoff="15:12",   # hard flat before the close — no overnight risk
    # sl 1.75% -> 2.5%: a stock that has just moved several percent on 3x volume
    # routinely pulls back more than 1.75% without the setup failing, and that is
    # exactly how 2026-07-29's entries died. Note this worsens reward:risk from
    # 2.0:1 to 1.4:1, so the lane now needs a ~42% win rate rather than ~33% —
    # the bet is that far fewer trades get shaken out.
    tp=0.035, sl=0.025, lock=0.015,
)
# BTST (Buy Today, Sell Tomorrow): near the close, buy a strong-CLOSING catalyst
# momentum name and hold ONE overnight to capture the gap-up. Validated on 2mo of
# real daily data (scripts/btst_bt.py): catalyst names -> ~64% win, +0.20%/trade
# after cost, PF ~1.6, tight tail (p05 -1.6%). The edge is the OVERNIGHT GAP only,
# so the lane MUST sell at the next open — holding into the next day gives it back.
BTST = dict(
    move_min=0.03,        # up >= 3% on the day (real momentum, not noise)
    close_pos_min=0.70,   # closed in the top 30% of the day's range (strong into close)
    rvol_min=1.5,         # >= 1.5x usual volume
    min_turnover=2.5e8,   # >= Rs.25cr liquidity floor
    catalyst_sessions=3,  # fresh NSE catalyst REQUIRED — the edge is catalyst-only
    sl=0.02,              # hard stop for a bad overnight down-gap
    slots=5, size_frac=0.6,   # small size, several names (the per-trade edge is thin)
    entry_start="15:05", entry_last="15:25",   # buy near the close
)
# Intraday momentum lane, added 2026-07-28 from a measured backtest rather than
# intuition. Rule: 60 minutes after the open, buy the SINGLE strongest mover of
# the day, target +2%, stop -1%, square off with the other intraday lanes.
#
# Measured over 58 sessions of 5-minute data across the 150 most liquid NSE
# names: +0.306% per trade after 0.10% costs, 50% win rate, +18.7% total.
# Positive in both halves of the sample, and still +12.2% with the three best
# days removed.
#
# Three findings are baked into these numbers and should not be "improved"
# without re-testing:
#   * slots=1 is the strategy, not a risk preference. Taking the 2nd and 3rd
#     ranked movers collapsed the same test from +18.7% to +3.7% and +3.2%.
#   * tp=2% / sl=1% is what works. Every 1% target tested lost or barely broke
#     even — you are risking 1% to make 1% and paying costs both ways.
#   * NO catalyst requirement. Filtering by real NSE announcements made it worse
#     in 9 of 9 configurations: by the time a name is the day's top mover, the
#     news is already in the price, and the filter only discards trades.
#
# Caveats, stated because they matter: 58 sessions is a small sample, this
# configuration was chosen from a sweep of 16, and the universe was ranked by
# CURRENT turnover, which leaks a little hindsight. Treat live results as the
# real test.
INTRAMOM = dict(
    # DISABLED. The +18.7% above did NOT survive removing the universe
    # look-ahead. Ranking liquidity as of before the test window instead of
    # today drops it to +1.6% over the same 58 sessions, win rate 50% -> 39.7%,
    # per-trade +0.306% -> +0.035%. The original number was largely harvesting
    # the 37 of 150 names that only BECAME liquid during the window — names the
    # strategy could not have known to watch at the time.
    #
    # The lane is kept, not deleted: the code and its tests are correct, and the
    # idea deserves a retest once intraday_recorder.py has accumulated real
    # forward data with no selection bias at all. Set enabled=True only with a
    # clean out-of-sample number behind it.
    enabled=False,
    start="10:15",        # 60 min after the 09:15 open; earlier entries tested worse
    last_entry="10:45",   # narrow window — the edge was measured at the 60-min mark
    min_move=0.010,       # at least +1% from the day's OPEN (not previous close)
    min_turnover=5e7,     # only names you can actually get filled in
    tp=0.020,
    sl=0.010,
    slots=1,
    size_frac=3.0,        # ~50% of the book at MAXPOS=6. The backtest assumed the
                          # full book; halving it roughly halves the return and the
                          # damage. Raise deliberately, not casually.
)

INTRADAY_STRATS = ("intraday_news", "volume_surge", "intraday_momentum")   # share exit_monitor's intraday handling
# Lanes that ALWAYS enter MID-SESSION. On their entry day the day's high and low
# were set before the trade existed, so neither may be used to exit it or to arm
# a breakeven lock — the position never traded at those prices.
#
# index_options was missing from this list and it enters mid-session like the
# rest. On 2026-07-30 the NIFTY 24300 CE was bought at 10:52 for 87.75; the
# 3-ATR breakeven lock armed off the day HIGH of 139.45 and the stop then fired
# against the day LOW of 67.05, booking breakeven on a contract that closed at
# 106.75 — up 21.7%. Neither extreme was necessarily reached while we held it.
MIDSESSION_STRATS = INTRADAY_STRATS + ("index_options", "manual", "btst")
# Freqtrade-style protections: temporarily HALT new entries when the book is
# bleeding, so a bad tape can't chew through the whole book. Pure safety — only
# ever reduces trading. Both reset next session.
RISK = dict(
    maxdd_halt=0.06,     # pause new buys if equity is >6% below TODAY's peak
    stopguard_n=4,       # pause new buys after this many stop/trail exits today
)
STALE_QUOTE_SEC = 600        # a symbol whose quote lags the market's freshest by > this is FROZEN.
                             # 10min (not tighter): the full universe repolls every ~300s, so a
                             # tighter bar would false-block names merely mid-refresh; real freezes
                             # (e.g. GUJGASLTD stuck since Jun 30) lag by hours/days and still trip it.
# Risk profile per market. Dynamic allocation (idle cash flows into open slots)
# roughly doubles both return AND drawdown (US backtest: +110%/16.7%DD vs
# +50%/9.1%DD). The user's demonstrated tolerance for US drawdowns is low ->
# US runs the conservative profile; IN keeps dynamic (tested better there and
# the IN book is a fraction of the US book in real-money terms).
DYN_ALLOC = {"IN": True, "US": False}
# Index/sector/leveraged ETFs - never traded by the single-stock strategies. They
# don't behave like gap/mean-reversion setups and create correlated, duplicate
# exposure (e.g. holding QQQ + QQQM, both Nasdaq-100, at the same time).
ETF_EXCLUDE = {
    "QQQ", "QQQM", "ONEQ", "SPY", "VOO", "IVV", "SPLG", "DIA", "IWM", "IWB", "VTI", "VEA", "VWO",
    "SMH", "SOXX", "SOXL", "SOXS", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE",
    "TQQQ", "SQQQ", "UPRO", "SPXL", "SPXU", "ARKK", "ARKG", "VUG", "VTV", "SCHD", "JEPI", "JEPQ",
    "GLD", "SLV", "USO", "TLT", "HYG", "LQD", "VXX", "UVXY", "XBI", "KRE", "KWEB", "FXI", "EEM", "EFA", "VIG",
}
MIN_PRICE = {"IN": 50.0, "US": 5.0}     # quality/liquidity floor - skip the cheapest, most manipulable names
SLOT_MIN_UTIL = 0.55                    # IN whole-share fill must use >= this fraction of its slot (else capital waste)
GAP_SLOT_CAP = 3                        # cap gap_momentum slots so it can't monopolize the book (scaled with MAXPOS IN=6)
GAP_TARGET = {"IN": 0.10, "US": 0.0}    # gap_momentum profit target by market: IN momentum mean-reverts,
                                        # so take profit at +10% (backtested: less give-back, edge intact,
                                        # win rate 37%->46%); US trends, a target chops the big runners -> trail only.
# volatility-normalized sizing (equalize risk per position). Reference ATR% ~=
# a typical IN swing name; a name at 2x that ATR gets ~half size, at 0.5x gets
# up to VOL_SIZE_MAX. Clamped so no single position gets wildly over/under-sized.
VOL_TARGET_ATR = 0.030
# Stop distance the sizing model is calibrated against. A lane with a wider
# stop is sized down in proportion, so risk per trade stays constant when an
# exit rule changes.
BASE_ATR_STOP = 2.0
VOL_SIZE_MIN, VOL_SIZE_MAX = 0.55, 1.60
# Hard ceiling on what ONE overnight-held position may be worth, as a fraction
# of equity. This is a guardrail, NOT a re-tuning of the sizing model.
#
# Why it exists: the exit audit showed the killer is oversized losses from
# overnight GAP-THROUGH, and the stop-cap A/B proved tightening the stop makes
# things worse (+0.63 -> +0.14%/trade at a -4% cap) while the worst single
# trade stayed identical at every cap, because a gap opens below any level.
# Tail risk on overnight holds is therefore a SIZING problem, not a stop-level
# problem, and a notional cap is the only lever the data supports.
#
# Why 0.30: normal 6-slot sizing puts 9.2%-26.7% of equity in a name
# (equity/MAXPOS x vol_mult, vol_mult in [0.55, 1.60]), so 0.30 NEVER binds in
# normal operation and leaves every backtested result untouched. It only clips
# the pathological case where DYN_ALLOC hands nearly all free cash to the last
# open slot. Tightening below ~0.27 would start re-sizing normal trades and is
# a tuning decision that needs its own backtest — do not lower it casually.
# Set to 0.0 to disable.
OVERNIGHT_MAX_POS_FRAC = {"IN": 0.30, "US": 0.30}

_LOG = logging.getLogger("openstocks.v2_live")

# Alert evaluation cadence. 20s is well inside a price alert's useful
# resolution and keeps the engine loop cheap; _check_alerts() throttles itself
# to 5s on top of this.
ALERT_INTERVAL = 20
_last_alerts: dict = {}


def cap_overnight_shares(shares: float, entry: float, equity: float, market: str,
                         strategy: str, symbol: str = "") -> float:
    """Clip a position that would hold too much of the book overnight.

    Only applies to strategies that carry risk through the close. Intraday
    lanes square off the same session, so they are not exposed to the overnight
    gap this guards against, and they already size off a fixed slot allocation.

    Returns the share count unchanged whenever the cap does not bind, so the
    normal path is untouched.
    """
    if strategy in INTRADAY_STRATS:
        return shares
    frac = OVERNIGHT_MAX_POS_FRAC.get(market, 0.0)
    if frac <= 0 or equity <= 0 or entry <= 0 or shares <= 0:
        return shares
    max_shares = (frac * equity) / entry
    if shares <= max_shares:
        return shares
    _LOG.warning(
        "Overnight size cap: %s %s %.0f -> %.0f shares (%.1f%% -> %.1f%% of equity)",
        market, symbol or strategy, shares, max_shares,
        shares * entry / equity * 100.0, frac * 100.0,
    )
    return max_shares
# BREAKEVEN LOCK: OFF. It was shipped as "backtested NEUTRAL", protecting a
# monster winner from round-tripping at no cost to the edge. Re-measured against
# the config the lane ACTUALLY runs today (3 ATR stop, no target, no trail),
# 19,752 entries, same signals, only the lock varying:
#
#     stop3, no BE lock   +0.719%/trade   PF 1.29   worst -29.7%
#     stop3 + BE @3ATR    +0.693%/trade   PF 1.28   worst -29.7%   <- was live
#     stop3 + BE @4ATR    +0.706%/trade   PF 1.28   worst -29.7%
#     stop3 + BE @6ATR    +0.718%/trade   PF 1.29   worst -29.7%
#
# The worst trade is IDENTICAL at every trigger, so the lock buys no drawdown
# protection whatsoever — the thing it was added for. It only converts winners
# that would have recovered into breakeven exits, at -0.026%/trade, which is 3%
# of the entry edge.
#
# The original "NEUTRAL" result was honest at the time: it was measured when the
# lane ran a 2 ATR stop with a 3.5 ATR target, where the target capped the
# winners the lock would otherwise have cut. Once the target was removed the
# lock started costing money, and nothing re-ran the study.
#
# None disables it. The INTRADAY lock (INTRA["lock"], "green never goes red") is
# a different rule with its own spec and is untouched.
BE_TRIGGER_ATR = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_book(market TEXT PRIMARY KEY, budget REAL, max_pos INTEGER, started_at TEXT);
CREATE TABLE IF NOT EXISTS v2_positions(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, shares REAL, stop REAL, target REAL, trail REAL, peak REAL, conviction REAL, opened_at TEXT);
CREATE TABLE IF NOT EXISTS v2_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, strategy TEXT, symbol TEXT,
  entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL, shares REAL, pnl REAL, return_pct REAL, reason TEXT, conviction REAL,
  opened_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS v2_equity(market TEXT, date TEXT, equity REAL, cash REAL, positions_value REAL, n_positions INTEGER, PRIMARY KEY(market,date));
CREATE TABLE IF NOT EXISTS v2_signals(market TEXT, strategy TEXT, date TEXT, symbol TEXT, conviction REAL, ref_close REAL, rank INTEGER);
-- Published ideas. UNIQUE(market,symbol,published_date) is the whole
-- idempotency story: poll_market runs every 5 minutes and must be able to
-- publish the same day's list repeatedly without ever creating a second copy or
-- moving a level after a subscriber has acted on it.
CREATE TABLE IF NOT EXISTS v2_ideas(
  id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, symbol TEXT, strategy TEXT,
  published_date TEXT, published_at TEXT, tier TEXT, rank INTEGER,
  entry REAL, atr REAL, conviction REAL,
  stop REAL, t1 REAL, t2 REAL, t3 REAL, qty INTEGER, risk_amt REAL, notional REAL,
  status TEXT DEFAULT 'open', best_target TEXT, hit_price REAL, result_pct REAL,
  mfe REAL, mae REAL, last_price REAL, last_at TEXT, closed_at TEXT,
  UNIQUE(market, symbol, published_date));
CREATE INDEX IF NOT EXISTS ix_ideas_date ON v2_ideas(published_date DESC, rank);
CREATE INDEX IF NOT EXISTS ix_ideas_open ON v2_ideas(status, market);
-- REAL orders sent to a real broker. Separate table from v2_trades on purpose:
-- the paper book must never be able to include a live fill in its statistics,
-- and a live order must never be silently reconciled away as paper.
CREATE TABLE IF NOT EXISTS v2_live_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market TEXT, symbol TEXT,
  instrument_key TEXT, side TEXT, qty INTEGER, price REAL, notional REAL,
  status TEXT, broker_order_id TEXT, reason TEXT, response TEXT, user_id INTEGER,
  product TEXT);
CREATE INDEX IF NOT EXISTS ix_live_orders_day ON v2_live_orders(substr(ts,1,10));
"""
_HIST: dict = {}
_EQ_SNAP: dict = {}
_SESSION_OPENED_AT: dict = {}            # market -> ts when the session last transitioned open
# PRE-OPEN WARM-UP. NSE runs its call auction 09:00-09:08 and normal trading
# starts 09:15. Until now every pass sat inside `if is_open`, so the heavy
# signal computation only BEGAN at 09:15 — the engine spent the first minutes of
# the session working out what it wanted while the moves it was ranking were
# already happening. Warming from 09:05 means the candidate list, panel and
# features are already built when the bell goes, so entries fire at the open
# instead of several minutes into it.
# This cannot place a trade: poll_market only fills inside the entry window,
# which is measured from _SESSION_OPENED_AT and is not set until the session
# actually opens. Pre-open it computes and stores signals, then returns.
# ---- index options (CE / PE) ----------------------------------------------
# The direction call is computed and shown whenever this is enabled; it only
# places a trade when auto_trade is ALSO true. Two switches rather than one
# because seeing the call and acting on it are different decisions, and the
# call has not been validated against history yet.
#
# Sizing is bounded by what a single lot costs, not by a percentage: one NIFTY
# weekly ATM lot is roughly 6-9% of a Rs 1L book, and a lot is the minimum
# tradeable unit. There is no way to take a 3% position in index options at
# this book size, so max_premium_pct is the honest cap rather than a target.
#
# LONG OPTIONS ONLY. Selling an option to open is not supported anywhere in
# this engine: a short call has unbounded loss, and one gap through a strike
# would exceed the entire book. Maximum loss on every position here is the
# premium paid, known at entry.
INDEX_OPTIONS = dict(
    enabled=True,               # compute and display the CE/PE call
    auto_trade=True,            # ON at the operator's explicit instruction (2026-07-30)
    instruments=("NIFTY",),     # which indices to call; BANKNIFTY is 2x the tick risk
    expiry="weekly",            # "weekly" or "monthly"
    # NOTE: `instruments` is what the operator ticked. It is now honoured — see
    # the loop in index_options_pass. INDEX_ALLOWED below is the set the engine
    # has data for; anything outside it is not offered in the UI either.
    # SEPARATE CAPITAL. Options do not share the equity book: a leveraged,
    # decaying instrument drawing on the same cash would let a bad options week
    # shrink the position sizing of the lane that actually has a measured edge.
    # Ring-fencing also makes the two independently readable — mixed into one
    # ledger, neither result can be attributed.
    budget=100000.0,
    max_premium_pct=0.10,       # cap on premium at risk per position, of the OPTIONS book
    # NSE kept weekly expiry only for NIFTY. BANKNIFTY, FINNIFTY and MIDCPNIFTY
    # are month-dated in the feed, so their CHEAPEST single lot is Rs 21.8k,
    # Rs 27.7k and Rs 27.1k — every one of them above the Rs 10k cap, which is
    # why they never filled even once the gates were fixed.
    #
    # Stated PER INSTRUMENT rather than by raising the global cap. _pick_contract
    # buys the nearest strike that FITS, so a global 0.30 would also quadruple
    # NIFTY's position size from ~Rs 7k to ~Rs 30k — a four-fold size increase on
    # the one index that actually trades, which is not what was asked for.
    max_premium_pct_by_index={"BANKNIFTY": 0.30, "FINNIFTY": 0.30, "MIDCPNIFTY": 0.30},
    # THE BINDING CAP: most that may be LOST on one position, as a fraction of
    # the options book. The premium caps above answer "how much may I spend",
    # which is the wrong question when the stop is 35% wide — 30% of the book
    # spent is 10.5% of the book risked. 6% x max_concurrent 3 bounds the lane
    # at 18% rather than 31%.
    max_risk_pct=0.06,
    # One position PER INDEX, not one across all of them. At 1 the first fill
    # returned out of the whole pass, so a NIFTY CE opened at 09:40 blocked
    # FINNIFTY and MIDCPNIFTY for the rest of the session even though both had
    # live calls. Premium per position is still capped by max_premium_pct, so
    # three concurrent is at most 30% of the options book at risk.
    max_concurrent=3,
    min_confidence=0.4,         # 2 of 5 readings; risk is held by size+stop, not by abstaining
    stop_pct=0.35,              # premium stop: options move far, a tight stop is noise
    # TARGET BY TIME TO EXPIRY, not one flat number. A longer-dated option
    # carries more premium and less gamma, so the same index move is a far
    # smaller PERCENTAGE move in the premium — a flat target is reachable on a
    # weekly and unreachable on a monthly.
    #
    # Measured, same-day, on 405 sessions: how often the premium reaches +60%,
    # and the median of how far it actually gets (ITM/ATM contracts):
    #
    #     0-2d   42.6-42.9% reach it   median excursion ~46%
    #     3-7d   36.1-44.1%            median ~37-50%
    #     8-20d  18.8-21.1%            median ~24-29%
    #     21d+    6.1-10.4%            median ~17-23%
    #
    # A flat 0.60 therefore meant 44% on the NIFTY weekly and 10% on the
    # FINNIFTY 25-day monthly — the same setting, one reachable and one
    # decorative. Targets below sit near the measured median for each bucket,
    # so the number on screen is one the contract can actually print.
    target_pct=0.60,            # fallback when the expiry is unknown
    target_by_dte=((2, 0.45), (7, 0.40), (20, 0.28), (10 ** 6, 0.20)),
    # The ATM straddle is the market's own expected move. Above this the
    # option is priced for more than the signal is playing for.
    #
    # SCALED BY TIME TO EXPIRY — see _straddle_limit. This 1.5% was calibrated
    # on NIFTY weeklies (~4 days to run). Compared raw against a 25-day monthly
    # it rejects the contract for being longer-dated, not for being rich.
    max_straddle_pct=1.5,
    straddle_base_dte=4,        # the horizon max_straddle_pct was measured on
    # Minimum traded volume for a strike, as a SHARE of the busiest strike in
    # that index's own chain. Relative, not absolute: volume accumulates through
    # the session, so a fixed floor would refuse every morning trade and wave
    # anything through by the afternoon.
    #
    # There was no liquidity test at all. Median live option volume by index —
    # NIFTY 60,284,250 · BANKNIFTY 696,180 · MIDCPNIFTY 106,200 · FINNIFTY
    # 3,360, minimum ZERO — and the lane put 28% of the options book into a
    # FINNIFTY contract without ever looking. A paper fill in a contract nobody
    # trades is fiction, and it is the kind of fiction that only shows up as a
    # loss when the book goes live.
    min_volume_share=0.05,
)
# Indices the engine has daily futures bars, an option chain and live quotes
# for — verified live, all four return 60 bars and a populated chain. This is
# the single source of truth for what the settings page may offer; it used to
# live in v2_web while the engine hardcoded NIFTY, which is how the UI came to
# present three choices that did nothing.
INDEX_ALLOWED = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
INDEX_SETTINGS_FILE = os.path.join(os.path.dirname(V2_DB), "index_options.json")
INDEX_EDITABLE = ("enabled", "auto_trade", "instruments", "expiry",
                  "min_confidence", "budget")


def index_settings_load():
    """Apply the operator's saved choices onto INDEX_OPTIONS.

    This lives in the ENGINE, and the engine calls it on every pass, because it
    used to be called only when a web request read the settings page. After a
    restart INDEX_OPTIONS therefore held the code defaults — instruments
    ("NIFTY",) — and the saved selection of four indices did not apply until
    somebody happened to open that page. Verified on 2026-07-31: a freshly
    restarted process reported one instrument while the JSON file listed four.

    Re-reading each pass also means an edit takes effect without a restart,
    which is the behaviour the settings page already implies.
    """
    try:
        with open(INDEX_SETTINGS_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
    except Exception:
        return                              # no file yet -> code defaults stand
    for key in INDEX_EDITABLE:
        if key in saved:
            INDEX_OPTIONS[key] = tuple(saved[key]) if key == "instruments" else saved[key]
# Measured on the 404-session study and shown next to the tick-box rather than
# enforced behind it: buying these is paying materially over the realised move.
INDEX_PREMIUM_WARNING = {
    "BANKNIFTY": "straddle implies 2.20% vs 1.13% realised — premium is roughly "
                 "double the move that actually happens",
}

PREOPEN = {"IN": ("09:05", "09:15")}
PREOPEN_INTERVAL = 120                   # re-warm every 2 min through the window
# Using the auction to TRADE, not merely to look at.
#
# The problem it solves: volume_surge gates on rvol (intraday volume vs its
# usual pace), which is meaningless in the first minutes — there is barely any
# intraday volume yet, so the lane cannot confirm a surge and sat out the open
# entirely (start was 09:18). Meanwhile the auction has already told us both the
# gap and how much money participated in it.
#
# So in the early window ONLY, auction participation substitutes for rvol. Every
# other gate is untouched: a material NSE catalyst is still REQUIRED, as are the
# turnover floor, the price floor, near-day-high, and the frozen-quote skip.
#
# Why this is not gap_momentum, which lost money (0/6 live, PF 0.81 over 44k):
# that lane bought gaps with no catalyst and no strength confirmation. Here the
# gap only decides WHEN a name may be considered; what makes it eligible is
# still the filing.
#
# NOT BACKTESTABLE: no historical pre-open snapshots exist, only today's. This
# cannot be validated in advance, so entries taken this way are tagged
# `preopen_seeded` in the position's `why` and must be scored separately before
# anyone trusts them. Set enabled=False to revert to the old 09:18 behaviour.
PREOPEN_SEED = dict(
    enabled=True,
    until="09:30",          # after this, real rvol exists and takes over
    min_gap=0.04,           # same threshold as VOLSURGE["move_min"]
    min_auction_value=1e7,  # Rs 1cr crossed in the auction = real participation,
                            # not the Rs 3.5 lakh that printed PPSL +15.6%
)


def _fo_refresh():
    """Pull the day's index F&O bhavcopy after the close.

    fo_ingest had NO scheduler — exactly the failure that killed the delivery
    feed. It went two sessions stale (28-Jul while trading on the 30th), so the
    direction call and every chain reading were scoring on bars from two days
    earlier while reporting them as today's.
    """
    import subprocess
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "fo_ingest.py")
    if not os.path.exists(script):
        _LOG.warning("fo_ingest not found at %s", script)
        return
    try:
        out = subprocess.run([sys.executable, script, "--backfill-days", "5"],
                             capture_output=True, text=True, timeout=900)
        tail = (out.stdout or out.stderr or "").strip().splitlines()[-2:]
        _LOG.info("F&O refresh: %s", " | ".join(t.strip() for t in tail))
    except subprocess.TimeoutExpired:
        _LOG.warning("F&O refresh timed out")
    except Exception:
        _LOG.exception("F&O refresh failed")


def _nse_reference_refresh():
    """Pull the day's delivery figures, institutional flows, bulk deals and
    sector labels once the session has closed.

    Driven from the engine rather than a systemd timer deliberately: the last
    delivery ingestion had NO script, timer or service anywhere and died on
    2026-06-17 with nothing reporting it. Anything the engine itself depends on
    should fail where the engine can see it.

    Runs in a subprocess so a hung NSE request or a parser fault cannot stall
    the trading loop, and is capped so it cannot outlive the evening.
    """
    import subprocess
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "nse_reference_ingest.py")
    if not os.path.exists(script):
        _LOG.warning("NSE reference ingester not found at %s", script)
        return
    try:
        out = subprocess.run([sys.executable, script, "--backfill-days", "3"],
                             capture_output=True, text=True, timeout=900)
        tail = (out.stdout or out.stderr or "").strip().splitlines()[-3:]
        _LOG.info("NSE reference refresh: %s", " | ".join(t.strip() for t in tail))
    except subprocess.TimeoutExpired:
        _LOG.warning("NSE reference refresh timed out")
    except Exception:
        _LOG.exception("NSE reference refresh failed")


def _bars5m_janitor():
    """Close out the 5-min recorder for the day: flush, prune, and VACUUM once a
    month. Runs at the session-close transition, so the VACUUM — which locks the
    file — never lands while the engine is trading. DELETE alone does not shrink
    a SQLite file; skipping the VACUUM is how trading_agent.db reached 12 GB.
    """
    try:
        from . import bars5m
        written = bars5m.flush(bars5m.bar_start())
        deleted = bars5m.prune()
        info = bars5m.stats()
        vacuumed = False
        if datetime.now(IST).day == 1:                # once a month, market closed
            vacuumed = bars5m.vacuum()
        _LOG.info("5m janitor: flushed %d, pruned %d, now %d rows / %d symbols / %.0f MB%s",
                  written, deleted, info["rows"], info["symbols"],
                  info["bytes"] / 1e6, " (vacuumed)" if vacuumed else "")
    except Exception:
        _LOG.exception("5m janitor failed")


def preopen_seed_map(hm, now=None):
    """Auction map to lean on right now, or {} once real rvol is available.

    Returns {} when seeding is disabled, outside the early window, or when the
    fetch failed — every one of which must leave the lane behaving exactly as
    it did before, rather than trading on absent data.
    """
    if not PREOPEN_SEED.get("enabled"):
        return {}
    if hm >= PREOPEN_SEED["until"]:
        return {}
    try:
        from . import preopen
        return preopen.cached(now=now)
    except Exception:
        return {}


def _option_live(symbols=None):
    """Live option quotes from nfo_quotes, shaped like the equity feed.

    Options are stored separately on purpose (they must not appear in the
    equity lanes' universe), which means every consumer of `_live` needs this
    merge or an option position becomes invisible — enterable but never
    exitable.
    """
    out = {}
    try:
        con = _ro(MAIN_DB)
        rows = con.execute("SELECT symbol, price, open, high, low, volume, lot_size,"
                           " strike, option_type, underlying, expiry"
                           " FROM nfo_quotes").fetchall()
        con.close()
    except Exception:
        return out
    wanted = {str(s).upper() for s in symbols} if symbols else None
    for symbol, price, open_, high, low, volume, lot, strike, opt_type, under, expiry in rows:
        sym = str(symbol).upper()
        if wanted is not None and sym not in wanted:
            continue
        price = _f(price, 0.0)
        if price <= 0:
            continue
        out[sym] = dict(price=price, open=_f(open_, price), high=_f(high, price),
                        low=_f(low, price), close=price, vol=_f(volume, 0.0),
                        lot_size=_f(lot, 0.0), strike=_f(strike, 0.0),
                        option_type=(opt_type or "").upper(),
                        underlying=(under or "").upper(), expiry=expiry)
    return out


def _refresh_preopen():
    """Pull the NSE call-auction snapshot. Isolated and best-effort: NSE 503s
    under load, and a failed pre-market fetch must never delay or break the
    open — the engine simply proceeds on prior closes as it always did."""
    try:
        from . import preopen
        data = preopen.refresh()
        if data:
            top = preopen.gappers(limit=5)
            _LOG.info("pre-open: %d symbols; top gaps %s", len(data),
                      ", ".join(f"{r['symbol']} {r['gap_pct']:+.1f}%" for r in top) or "none")
    except Exception:
        _LOG.exception("pre-open refresh failed")


def in_preopen(market, now=None):
    """True inside the pre-open warm-up window on a real trading day."""
    window = PREOPEN.get(market)
    if not window:
        return False
    moment = now or datetime.now(IST)
    if moment.weekday() >= 5:
        return False
    if moment.date().isoformat() in MARKET_HOLIDAYS.get(market, ()):
        return False
    return window[0] <= moment.strftime("%H:%M") < window[1]
ENTRY_WINDOW_SEC = 30 * 60               # "at the open" — the validated fill window
# Freed capital may be redeployed for this long after the open (~14:15 IST).
# Before 2026-07-28 the daily lanes filled ONLY in the 30-min open window, so a
# position that exited at 11:00 left its capital idle until the next session —
# the book sat half-invested all day by construction.
# Honesty note: only the open-window fill is backtested. The validated result
# buys at the open from prior-close signals; a fill at 13:00 is acting on a
# signal the tape has already moved past. Late fills are therefore TAGGED in the
# position's `why` (late_entry=true) so they can be scored separately later. If
# they underperform, narrow this window rather than assuming the lane is broken.
REENTRY_WINDOW_SEC = 5 * 3600
_started = False
_status: dict = {m: "init" for m in ENABLED_MARKETS}


def ensure_schema(v2):
    v2.executescript(SCHEMA)
    for m in ENABLED_MARKETS:
        if not v2.execute("SELECT 1 FROM v2_book WHERE market=?", (m,)).fetchone():
            v2.execute("INSERT INTO v2_book(market,budget,max_pos,started_at) VALUES(?,?,?,?)",
                       (m, BUDGET[m], MAXPOS[m], datetime.now(timezone.utc).isoformat()))
    try:  # additive migration: entry-time investigation snapshot ("why we bought")
        v2.execute("ALTER TABLE v2_positions ADD COLUMN why TEXT")
    except Exception:
        pass
    # Additive migration: a closed trade only ever recorded DATES, so the whole
    # sell side of the ledger had no time of day. The UI showed "05:30 IST" for
    # every one of them — midnight UTC shifted into IST, an hour before the
    # market opens, invented from a field that never held a time. Carrying
    # opened_at across on exit also keeps the buy leg's real fill time, which
    # was previously discarded the moment the position closed.
    for column in ("opened_at", "closed_at"):
        try:
            v2.execute(f"ALTER TABLE v2_trades ADD COLUMN {column} TEXT")
        except Exception:
            pass
    # Additive: the Upstox product code an order was placed with. An intraday
    # BUY cannot be closed by a delivery SELL, so the exit has to read back what
    # the entry actually used rather than assume.
    try:
        v2.execute("ALTER TABLE v2_live_orders ADD COLUMN product TEXT")
    except Exception:
        pass
    # Additive migration: an OPTION position must carry its own expiry. Reading
    # it off the live quote is not enough — once the contract expires it drops
    # out of the ATM watch list, and a position whose expiry is only knowable
    # from a quote that no longer arrives can never be closed.
    try:
        v2.execute("ALTER TABLE v2_positions ADD COLUMN expiry TEXT")
    except Exception:
        pass
    try:                                  # per-user paper books (see app/books.py)
        from . import books as _books
        _books.ensure_schema(v2)
    except Exception:
        _LOG.exception("user-book schema failed; house book unaffected")
    v2.commit()


def market_open(market):
    try:
        return bool(market_regions.market_session_for_region(market).get("is_open"))
    except Exception:
        return False


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)


def _rw():
    c = sqlite3.connect(V2_DB, timeout=30)
    c.execute("PRAGMA busy_timeout=8000")
    c.execute("PRAGMA journal_mode=WAL")   # readers never queue behind engine writes
    return c


def _hist(market):
    h = _HIST.get(market)
    if h and time.time() - h[0] < 6 * 3600:
        return h[1], h[2]
    con = _ro(MAIN_DB)
    syms, mdf = eng.load_panel(con, market, topn=eng.DEFAULTS["topn"])
    con.close()
    # Back-adjust unadjusted splits/bonuses BEFORE anything reads the series.
    # The feed stores raw traded prices, so a 1:5 split is a -80% "return" that
    # never happened — and that is precisely what the dip-buying lane hunts for,
    # so one artefact outranks every genuine setup in the book.
    try:
        from . import corpactions
        syms, _fixed = corpactions.clean_all(syms)
    except Exception:
        _LOG.exception("corporate-action adjustment failed; using raw prices")
    # keep ~1y of bars so the pre-trade factor investigation has enough history
    # (drawdown-from-252d-high, RSI, 50d-slope, etc.); signals only need the tail.
    tails = {s: g.tail(300).copy() for s, g in syms.items() if len(g) >= 70}
    _HIST[market] = (time.time(), tails, mdf)
    return tails, mdf


# Score the daily lanes on TODAY's forming bar instead of only completed
# sessions. Until 2026-07-29 `asof` was dates[-1], the last COMPLETED trading
# day, so at noon the engine was ranking yesterday's close and buying at today's
# price — a full session stale, which is what made the picks look off.
#
# This is legitimate live (the partial bar is information you genuinely have
# now) but it is NOT what the backtest validated: that scored completed bars
# only, precisely to avoid look-ahead. So entries taken this way are tagged
# `intraday_bar` and can be scored separately. Set False to restore.
INTRADAY_SIGNAL_BAR = True


def append_today_bar(tails, mdf, market, live, stale=None):
    """Add today's forming OHLCV bar, built from the live Upstox feed.

    The quote already carries a true session open/high/low, so the partial bar
    is real rather than synthesised from the last print. Symbols with a frozen
    quote are skipped — a stale price would otherwise be written in as though it
    were today's action, which is the same failure the entry guard exists for.

    Returns (tails, market_df). Both are new objects; the cached history is left
    untouched so the next call rebuilds from clean completed bars.
    """
    today = pd.Timestamp(datetime.now(IST).date() if market == "IN"
                         else datetime.now(timezone.utc).date())
    stale = stale or set()
    out, rets = {}, []
    for sym, g in tails.items():
        lq = live.get(sym)
        px = (lq or {}).get("price")
        if not lq or sym in stale or not px or float(px) <= 0 or g.empty:
            out[sym] = g
            continue
        if g.index[-1] >= today:            # already ingested by the nightly job
            out[sym] = g
            continue
        px = float(px)
        prev_close = float(g["close"].iloc[-1])
        ret1 = (px / prev_close - 1) if prev_close > 0 else 0.0
        row = {c: None for c in g.columns}
        row.update(open=_f(lq.get("open"), px), high=_f(lq.get("high"), px),
                   low=_f(lq.get("low"), px), close=px,
                   volume=float(lq.get("vol") or 0.0), ret1=ret1)
        if "symbol" in row:
            row["symbol"] = sym
        if "ts" in row:
            row["ts"] = today.isoformat()
        out[sym] = pd.concat([g, pd.DataFrame([row], index=[today])])
        rets.append(ret1)
    # The regime reads mdf, so it has to advance too — otherwise the lanes would
    # score today while the market filter still described yesterday.
    if rets and mdf is not None and not mdf.empty and mdf.index[-1] < today:
        med = float(pd.Series(rets).median())
        cum = float(mdf["mkt_cum"].iloc[-1]) * (1 + med)
        mdf = pd.concat([mdf, pd.DataFrame({"mkt_ret1": [med], "mkt_cum": [cum]},
                                           index=[today])])
    return out, mdf


# Falling-knife guard for the dip-buying lane. On 2026-07-29 swing_meanrev's
# top-ranked signals were THANGAMAYL (-10.75% and sitting at the very low of its
# day), J&KBANK (-6.55%) and PNGJL (-1.74%, 4% off its low) — stocks still in
# free fall, not stocks that had finished falling. Only the market regime gate
# was keeping them out of the book.
#
# The distinction that matters for mean reversion is not "how far has it
# dropped" but "has it stopped dropping". A name down 10% that has bounced back
# into the middle of its range is the setup this lane wants; the same name
# pinned at the low is a knife. Range position separates the two; a raw
# percentage change cannot.
#
# Deliberately NOT applied to mom_breakout — that lane buys names pressing their
# highs, where this test is trivially satisfied and adds nothing.
KNIFE_MIN_RANGE_POS = 0.25              # must be out of the bottom quarter of the day


def day_range_position(lq):
    """Where the live price sits in today's range: 0.0 at the low, 1.0 at the high.

    Returns None when the range is unusable (missing, zero-width, or a price
    outside its own high/low, which means a stale or broken quote). Callers must
    treat None as "unknown" and not as a pass or a fail on its own.
    """
    if not lq:
        return None
    try:
        high, low, price = float(lq.get("high") or 0), float(lq.get("low") or 0), float(lq.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or high <= 0 or low <= 0 or high <= low:
        return None
    if price < low or price > high:      # quote inconsistent with its own range
        return None
    return (price - low) / (high - low)


def clear_of_the_day_low(lq, floor=KNIFE_MIN_RANGE_POS):
    """True if the name has lifted off its low. Unknown range => allowed, so a
    thin or missing quote never silently blocks the whole lane."""
    pos = day_range_position(lq)
    return True if pos is None else pos >= floor


def _f(v, d):
    try:
        x = float(v)
        return x if x > 0 else d
    except (TypeError, ValueError):
        return d


_SECTOR_CACHE: dict = {}


def _sector_map(market):
    """symbol -> sector, cached 6h. Used for the concentration cap. Best-effort:
    if the universe table has no sector data the cap simply doesn't bind."""
    c = _SECTOR_CACHE.get(market)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    out = {}
    try:
        con = _ro(MAIN_DB)
        rows = con.execute("SELECT symbol, sector FROM universe").fetchall()
        con.close()
        # catch-all labels ('US Equity' covers 5,212 names) are not sectors —
        # counting them freezes the whole book via the concentration cap. Any
        # label covering >5% of the universe is treated as no-sector.
        from collections import Counter
        counts = Counter(str(sec).strip() for _, sec in rows if sec)
        generic = {lab for lab, n in counts.items() if n > max(50, len(rows) * 0.05)}
        for sym, sec in rows:
            if sym:
                lab = str(sec).strip() if sec else "unknown"
                out[str(sym).upper()] = "unknown" if lab in generic else lab
    except Exception:
        pass
    _SECTOR_CACHE[market] = (time.time(), out)
    return out


# severe-negative catalysts a pro would NOT buy into
NEG_EVENTS = {"fraud_governance", "legal_regulatory", "debt_liquidity", "analyst_downgrade"}


def _news_state(mcon, symbol):
    """Return (net_score, severe_negative) from the last 3 days of news."""
    try:
        rows = mcon.execute("SELECT score,events_json FROM sentiment_events WHERE symbol=? "
                            "AND ts>=datetime('now','-3 days') ORDER BY ts DESC LIMIT 3", (symbol,)).fetchall()
    except Exception:
        return 0.0, False
    if not rows:
        return 0.0, False
    import json
    score = 0.0
    for sc, _ in rows:
        try:
            score = float(sc); break
        except (TypeError, ValueError):
            continue
    severe = False
    for _, ej in rows:
        try:
            for e in json.loads(ej or "[]"):
                if e.get("event_type") in NEG_EVENTS and float(e.get("score") or 0) < -0.15:
                    severe = True
        except Exception:
            continue
    return score, (severe or score <= -0.35)


def _live(market, symbols=None):
    con = _ro(MAIN_DB)
    if symbols:
        syms = list(symbols)
        rows = con.execute(
            "SELECT symbol,price,open,high,low,close,volume FROM latest_quotes WHERE source=? AND symbol IN (%s)"
            % ",".join("?" * len(syms)), (LIVE_SOURCE[market], *syms)).fetchall()
    else:
        rows = con.execute("SELECT symbol,price,open,high,low,close,volume FROM latest_quotes WHERE source=?",
                           (LIVE_SOURCE[market],)).fetchall()
    con.close()
    out = {}
    for sym, p, o, h, l, c, v in rows:
        try:
            price = float(p)
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[sym] = dict(price=price, open=_f(o, price), high=_f(h, price), low=_f(l, price), vol=_f(v, 0))
    return out


_SESS_OPEN: dict = {}
_SESS_FILE = os.path.join(os.path.dirname(V2_DB), "v2_session.json")
_SESS_SAVED = [0.0]


def _sess_load():
    """Restore session opens/hi/lo across engine restarts — otherwise a mid-session
    restart re-baselines 'open' to the restart price and blinds US gap detection
    for the rest of the day."""
    if _SESS_OPEN:
        return
    try:
        with open(_SESS_FILE) as f:
            raw = json.load(f)
        for m, (key, d) in raw.items():
            _SESS_OPEN[m] = (key, {s: list(v) for s, v in d.items()})
    except Exception:
        pass


def _sess_save():
    now = time.time()
    if now - _SESS_SAVED[0] < 30:
        return
    _SESS_SAVED[0] = now
    try:
        with open(_SESS_FILE, "w") as f:
            json.dump({m: [st[0], st[1]] for m, st in _SESS_OPEN.items()}, f)
    except Exception:
        pass


def _earnings_soon(v2, sym, today_s, days=None):
    """True if the symbol reports earnings within `days` days — a technical setup
    has no edge against an earnings surprise, so entries are blocked."""
    try:
        r = v2.execute("SELECT next_earnings FROM earnings_calendar WHERE symbol=?", (sym,)).fetchone()
        if not r or not r[0]:
            return False
        delta = (date.fromisoformat(str(r[0])[:10]) - date.fromisoformat(today_s)).days
        return 0 <= delta <= (days if days is not None else EARNINGS_BLOCK_DAYS)
    except Exception:
        return False


def _session_opens(market, live):
    """First price seen per symbol this session. The IN feed carries a true quote
    open; the US feed sends only `price` (open is NULL -> coalesced), so without
    this the US 'gap' was really intraday day-change (chase entries that faded).
    Keyed by IST date for IN and UTC date for US (neither session crosses its
    key's midnight). If the engine restarts mid-session, opens re-baseline to the
    restart price -> gap ~0 -> conservatively no gap entries that day."""
    _sess_load()
    key = datetime.now(IST).date().isoformat() if market == "IN" else datetime.now(timezone.utc).date().isoformat()
    st = _SESS_OPEN.get(market)
    if not st or st[0] != key:
        st = (key, {})
        _SESS_OPEN[market] = st
    sess = st[1]
    for s, lq in live.items():
        r = sess.get(s)
        if r is None:
            # [open, session_high, session_low] — the US feed has no OHLC, so we
            # accumulate the session range from our own samples (feeds features
            # a real today-bar and lets trails ratchet within a session).
            sess[s] = [lq["open"], max(lq["high"], lq["price"]), min(lq["low"], lq["price"])]
        else:
            r[1] = max(r[1], lq["high"], lq["price"])
            r[2] = min(r[2], lq["low"], lq["price"])
    _sess_save()
    return sess


def _signals(tails, mdf, live, opens=None):
    opens = opens or {}
    today = pd.Timestamp(datetime.now(IST).date())
    rets = [live[s]["price"] / t["close"].iloc[-1] - 1 for s, t in tails.items()
            if s in live and t["close"].iloc[-1] > 0]
    mret = float(pd.Series(rets).median()) if rets else 0.0
    mdf_live = mdf.copy()
    mdf_live.loc[today] = {"mkt_ret1": mret, "mkt_cum": mdf["mkt_cum"].iloc[-1] * (1 + mret)}
    out = []
    for s, t in tails.items():
        lq = live.get(s)
        if not lq:
            continue
        sr = opens.get(s)
        sess_open = sr[0] if sr else lq["open"]
        sess_hi = max(sr[1], lq["price"]) if sr else lq["high"]
        sess_lo = min(sr[2], lq["price"]) if sr else lq["low"]
        tl = t.copy()
        tl.loc[today] = {"open": sess_open, "high": sess_hi, "low": sess_lo,
                         "close": lq["price"], "volume": lq["vol"] or t["volume"].iloc[-1]}
        try:
            row = eng.compute_features(tl, mdf_live).iloc[-1]
        except Exception:
            continue
        atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0
        if atr <= 0:
            continue
        c = eng.conviction(row)
        if c > 0:
            out.append(dict(symbol=s, strategy="swing_meanrev", score=round(c, 4), atr=atr, price=lq["price"]))
        prevc = t["close"].iloc[-1]
        g = sess_open / prevc - 1 if prevc > 0 else 0
        rv = float(row["rvol"]) if not pd.isna(row["rvol"]) else 0
        # gap must be HOLDING (price at/above the session open): gap-and-go, not
        # gap-and-fade. Session open comes from _session_opens (the US feed has no
        # quote open, which silently turned 'gap' into intraday chase before).
        if 0.03 <= g <= 0.15 and rv >= 1.5 and lq["price"] >= sess_open:
            out.append(dict(symbol=s, strategy="gap_momentum", score=round(min(g / 0.15, 1.0), 4), atr=atr, price=lq["price"]))
        # 52w-high breakout: near the 1y high on volume with strong 3m momentum
        # (George & Hwang factor; entry only in a STRONG market regime)
        ch = t["close"]
        hi252 = float(ch.tail(252).max()) if len(ch) >= 60 else 0.0
        mom63 = (lq["price"] / float(ch.iloc[-63]) - 1) if len(ch) >= 63 and float(ch.iloc[-63]) > 0 else 0.0
        if hi252 > 0 and lq["price"] >= 0.98 * hi252 and rv >= 1.5 and mom63 > 0.10:
            out.append(dict(symbol=s, strategy="mom_breakout", score=round(min(mom63, 2.0), 4), atr=atr, price=lq["price"]))
    return out, mdf_live, today


def _signals_completed(tails, mdf, asof, live):
    """Signals from COMPLETED daily bars only — exactly the validated backtest
    convention (signal at day-t close, entry at t+1 open). The old path
    synthesized a live intraday bar and bought mid-session: an unvalidated
    timing that live results exposed as materially worse (US gap 0/10 wins,
    swing 23% vs 54% expected)."""
    out = []
    for s in eng.signals_for_date(tails, mdf, asof, threshold=PLAN["swing_meanrev"]["threshold"],
                                  atr_stop=PLAN["swing_meanrev"]["atr_stop"],
                                  atr_target=PLAN["swing_meanrev"]["atr_target"]):
        lq = live.get(s["symbol"])
        out.append(dict(symbol=s["symbol"], strategy="swing_meanrev", score=s["conviction"],
                        atr=s["atr"], price=(lq["price"] if lq else s["ref_close"])))
    for s in eng.gap_signals_for_date(tails, mdf, asof):
        lq = live.get(s["symbol"])
        out.append(dict(symbol=s["symbol"], strategy="gap_momentum", score=s["conviction"],
                        atr=s["atr"], price=(lq["price"] if lq else s["ref_close"])))
    for sym, g in tails.items():
        gi = g.loc[:asof]
        if len(gi) < 70 or asof not in gi.index:
            continue
        ch, vv = gi["close"], gi["volume"]
        c0 = float(ch.iloc[-1])
        hi252 = float(ch.tail(252).max())
        v20 = float(vv.tail(20).mean()) if len(vv) >= 20 else 0.0
        rvol = float(vv.iloc[-1]) / v20 if v20 > 0 else 0.0
        mom63 = c0 / float(ch.iloc[-63]) - 1 if len(ch) >= 63 and float(ch.iloc[-63]) > 0 else 0.0
        tr = (gi["high"] - gi["low"]).tail(14).mean()
        atr = float(tr) if tr == tr else 0.0
        if hi252 > 0 and c0 >= 0.98 * hi252 and rvol >= 1.5 and mom63 > 0.10 and atr > 0:
            lq = live.get(sym)
            out.append(dict(symbol=sym, strategy="mom_breakout", score=round(min(mom63, 2.0), 4),
                            atr=atr, price=(lq["price"] if lq else c0)))
    return out


_TG_DAILY: dict = {}   # (kind, market) -> date string already alerted, so each fires once/day


def _tg_radar_digest(market, today, cand, positions):
    """Once/day, Telegram the top candidates the engine is watching to buy next."""
    try:
        ds = today.date().isoformat()
        if not cand or _TG_DAILY.get(("radar", market)) == ds:
            return
        items = []
        for _, _, s, _pl in cand:
            if s["symbol"] in positions:
                continue
            items.append({"symbol": s["symbol"], "note": s["strategy"].replace("_", " ")})
            if len(items) >= 6:
                break
        if not items:
            return
        _TG_DAILY[("radar", market)] = ds
        from . import telegram_bot
        telegram_bot.notify_radar(items, market)
    except Exception:
        pass


def _tg_daily_summary(market):
    """Once/day at market close, Telegram how the book is progressing."""
    try:
        ds = datetime.now(IST).date().isoformat()
        if _TG_DAILY.get(("summary", market)) == ds:
            return
        _TG_DAILY[("summary", market)] = ds
        from . import telegram_bot
        v2 = _ro(V2_DB)
        budget = (v2.execute("SELECT budget FROM v2_book WHERE market=?", (market,)).fetchone() or [BUDGET.get(market, 0.0)])[0]
        rows = v2.execute("SELECT symbol, entry_price, shares FROM v2_positions WHERE market=?", (market,)).fetchall()
        realized_all = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
        realized_today = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND substr(exit_date,1,10)=?",
                                    (market, ds)).fetchone()[0] or 0.0
        total_tr = v2.execute("SELECT COUNT(*) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0
        wins = v2.execute("SELECT COUNT(*) FROM v2_trades WHERE market=? AND pnl>0", (market,)).fetchone()[0] or 0
        v2.close()
        live = _live(market)
        cost = sum(ep * sh for _, ep, sh in rows)
        unreal = sum((live.get(sym, {}).get("price", ep) - ep) * sh for sym, ep, sh in rows)
        equity = budget + realized_all + unreal
        ccy = "₹" if market == "IN" else "$"
        win = round(wins / total_tr * 100) if total_tr else 0
        deploy = round(cost / budget * 100) if budget else 0
        overall_pct = (equity / budget - 1) * 100 if budget else 0.0
        tsign = "+" if realized_today >= 0 else ""
        osign = "+" if overall_pct >= 0 else ""
        nm = "India" if market == "IN" else "US"
        txt = ("\U0001f4ca <b>OpenStocks</b> · Daily summary — %s\n"
               "<b>Portfolio:</b> %s%s (%s%.2f%%)\n"
               "<b>Today:</b> %s%s%s realized\n"
               "<b>Positions:</b> %d · %d%% deployed\n"
               "<b>Win rate:</b> %d%% (%d trades)") % (
            nm, ccy, "{:,.0f}".format(equity), osign, overall_pct,
            tsign, ccy, "{:,.0f}".format(realized_today), len(rows), deploy, win, total_tr)
        telegram_bot.notify_summary(txt)
    except Exception:
        pass


def breakeven_price(market, entry, shares=1.0, strategy=None):
    """The exit price at which a round trip nets EXACTLY zero.

    "Breakeven" was defined in GROSS price terms — entry * 1.001, i.e. +0.1% —
    while a round trip costs ~0.4% (COST_SIDE both sides). So the lock that was
    meant to guarantee "a green trade never goes red" was guaranteeing a LOSS of
    roughly 0.3% every time it fired.

    Measured on the live book: 9 of volume_surge's 25 closed trades exited
    within 0.15% of entry, every one booked as "stop", every one a loser,
    together -Rs 693 of pure cost. TATACAP entered 370.95 and exited 371.15 —
    twenty paise HIGHER — and was recorded as -0.35%.

    Solving  shares*(x-e) - c*shares*(e+x) = 0  for x gives x = e*(1+c)/(1-c).
    """
    entry = float(entry or 0)
    if entry <= 0:
        return entry
    if market != "IN":
        c = COST_SIDE.get(market, 0.0)
        return entry * (1.0 + c) / (1.0 - c) if 0 < c < 1 else entry
    # Solve for the exit that nets zero, using the REAL schedule.
    #
    # SHARES MATTER. Brokerage and the DP charge are FLAT rupees on the whole
    # position, so the per-share cost is charge/shares. Computing the charge on
    # the per-share PRICE — as the first version of this did — prices a
    # one-share trade and puts the breakeven stop wildly too high on any real
    # position. One pass is enough; the charge barely moves over the fraction of
    # a percent involved.
    from . import costs as _costs
    shares = max(1.0, float(shares or 1.0))
    product = _costs.DELIVERY
    if strategy is not None:
        try:
            from . import live_trade as _lt
            product = _lt.product_for(strategy)
        except Exception:
            pass
    basis = entry * shares
    # Two passes. The charge is levied on the EXIT value too, so raising the
    # exit raises the charge slightly; one pass leaves a small residual loss,
    # which is the whole failure mode this function exists to prevent.
    x = entry + _costs.round_trip(basis, basis, product) / shares
    for _ in range(3):
        x = entry + _costs.round_trip(basis, x * shares, product) / shares
    return x


def net_trade_pnl(market, shares, entry, exit_price, strategy=None):
    """(net_pnl, net_return_pct) after round-trip costs — the ONE definition.

    Exits recorded the GROSS move while the cash ledger was charged costs on
    both sides. Equity was therefore honest but every statistic derived from
    v2_trades — realised P&L, win rate, profit factor, per-lane attribution —
    was flattered by exactly the cost of trading, and a small loser could be
    reported as a winner. Three separate exit paths (the engine's exit_monitor
    and two manual-sell endpoints) each had their own copy of the arithmetic;
    this is the shared one, charged on the same basis as the cash ledger so the
    two can no longer disagree.

    COSTS ARE NOW COMPUTED, NOT ASSUMED. This charged a flat 0.40% round trip
    at every ticket size, which is a fair average for the paper book's
    Rs 16,666 positions and wrong by 6x for a Rs 3,000 one — because brokerage
    (Rs 20 a leg) and the DP charge (Rs 20 a sell) do not scale with the trade.
    A percentage cannot express a fixed fee, and using one meant the book
    reported profits a smaller live account would never see.

        Rs 3,000 delivery   2.58% real   vs 0.40% modelled
        Rs 9,000 intraday   0.27% real
        Rs 50,000 delivery  0.36% real

    `strategy` selects the product, because an intraday round trip costs a
    quarter of a delivery one at the same size. Omitted, it assumes delivery —
    the more expensive of the two, so an unknown lane is never flattered.

    US keeps the flat model: COST_SIDE was fitted for it and there is no Indian
    fee schedule to apply.
    """
    basis = shares * entry
    if market != "IN":
        cside = COST_SIDE.get(market, 0.0)
        net = shares * (exit_price - entry) - cside * shares * (entry + exit_price)
        return net, (net / basis * 100) if basis else 0.0
    from . import costs as _costs
    product = _costs.DELIVERY
    if strategy is not None:
        try:
            from . import live_trade as _lt
            product = _lt.product_for(strategy)
        except Exception:
            product = _costs.DELIVERY
    charge = _costs.round_trip(basis, shares * exit_price, product)
    net = shares * (exit_price - entry) - charge
    return net, (net / basis * 100) if basis else 0.0


def record_exit(v2, market, position_id, exit_date, exit_price, shares, reason,
                closed_at=None):
    """THE single writer for v2_trades. Returns (net_pnl, net_return_pct).

    The counterpart to record_entry, and here for the same reason: the exit
    INSERT existed in THREE places — the engine's exit_monitor and both manual
    sell endpoints — each with its own copy of the P&L arithmetic. When exits
    were recording the gross move, fixing it meant finding all three, and the
    rows written before that landed still carry gross numbers today. On the
    live book that is a -605.61 loss stored as -500.18, and a -91.18 loss
    stored as a +14.20 WIN.

    Costs are computed HERE from net_trade_pnl rather than accepted as an
    argument, so a caller cannot pass a gross figure even by mistake. That is
    the whole point: the previous shape made the wrong thing easy to write and
    invisible afterwards.

    The row is built by SELECT from the position, so strategy, entry price,
    conviction and opened_at carry across rather than being retyped.
    """
    row_s = v2.execute("SELECT strategy FROM v2_positions WHERE id=?",
                       (position_id,)).fetchone()
    net, net_pct = net_trade_pnl(market, shares,
                                 *_entry_of(v2, position_id, exit_price),
                                 strategy=(row_s[0] if row_s else None))
    v2.execute(
        "INSERT INTO v2_trades(market,strategy,symbol,entry_date,entry_price,exit_date,"
        "exit_price,shares,pnl,return_pct,reason,conviction,opened_at,closed_at)"
        " SELECT market,strategy,symbol,entry_date,entry_price,?,?,?,?,?,?,conviction,"
        "opened_at,? FROM v2_positions WHERE id=?",
        (exit_date, exit_price, shares, net, net_pct, reason,
         closed_at or datetime.now(timezone.utc).isoformat(), position_id))
    # LIVE MIRROR — sell whatever the sleeve actually holds. Read the symbol
    # BEFORE the caller deletes the position row, and never pass `shares`: that
    # is the paper size, roughly ten times the live one.
    try:
        row = v2.execute("SELECT symbol FROM v2_positions WHERE id=?",
                         (position_id,)).fetchone()
        if row:
            _live_mirror_exit(v2, market, row[0], exit_price, reason)
    except Exception:
        _LOG.exception("live mirror (exit) failed for position %s", position_id)
    try:
        if row:
            _book_mirror_exit(v2, market, row[0], exit_price, reason)
    except Exception:
        _LOG.exception("user-book mirror (exit) failed for position %s", position_id)
    return net, net_pct


def _entry_of(v2, position_id, exit_price):
    """(entry_price, exit_price) for the cost math, read from the position."""
    row = v2.execute("SELECT entry_price FROM v2_positions WHERE id=?", (position_id,)).fetchone()
    return (float(row[0]) if row else 0.0), exit_price


def record_entry(v2, market, strategy, symbol, entry_date, entry_price, shares,
                 stop, target, trail, conviction, why, peak=None, expiry=None):
    """THE single writer for v2_positions. Returns True if the row was written.

    Every lane had its own copy of this INSERT — five of them, identical column
    lists retyped by hand. That is how the daily lane ended up missing the
    frozen-quote guard the other four had, and it is the same shape as the
    positional `INSERT ... VALUES(?,?,?)` that broke the moment a column was
    added. One writer means one place to audit.

    It also refuses to record a position that is already broken on arrival. Each
    condition below is unambiguously wrong rather than merely unusual, so a
    refusal is always a bug caught, never a trade needlessly skipped:

      * a non-positive price or size is not a trade;
      * a stop at or above entry exits instantly at a loss the moment it is
        checked — the position is dead before it opens;
      * a target at or below entry closes instantly for no gain.

    A refusal is logged loudly and the trade is skipped. Silently writing a
    broken position is worse: it corrupts the ledger we judge the lanes on.
    """
    problem = None
    if not symbol:
        problem = "empty symbol"
    elif not (entry_price > 0):
        problem = "entry_price=%r" % (entry_price,)
    elif not (shares > 0):
        problem = "shares=%r" % (shares,)
    elif stop and stop >= entry_price:
        problem = "stop %.4f >= entry %.4f (would exit instantly)" % (stop, entry_price)
    elif target and target <= entry_price:
        problem = "target %.4f <= entry %.4f (would exit instantly)" % (target, entry_price)
    if problem:
        _LOG.error("REFUSED %s entry %s/%s: %s", strategy, market, symbol, problem)
        return False
    # CIRCUIT BREAKER: cap round trips per symbol per day.
    #
    # Not aimed at any one bug. Twice now a disagreement between an entry rule
    # and an exit rule has become an 8-second buy/sell loop that ran until the
    # bell — 19 round trips of one contract on 2026-08-04, then 83 of another on
    # 2026-08-07 for -Rs 7,721. Both times the root cause was fixed and both
    # times NOTHING capped the blast radius, because nothing in the system ever
    # asked "why am I buying this for the twentieth time today?".
    #
    # A lane legitimately trades a symbol once or twice a day. Past that it is
    # not expressing a view, it is oscillating, and each cycle pays the spread
    # and costs for a price move of roughly zero. So this cannot cost a real
    # trade, and it turns the next loop of this shape into a bounded Rs 200
    # nuisance instead of an all-day bleed.
    if strategy != "manual" and MAX_ROUND_TRIPS_PER_DAY:
        try:
            spun = v2.execute(
                "SELECT COUNT(*) FROM v2_trades WHERE market=? AND symbol=?"
                " AND entry_date=?", (market, symbol, entry_date)).fetchone()[0]
        except Exception:
            spun = 0
        if spun >= MAX_ROUND_TRIPS_PER_DAY:
            _LOG.error("REFUSED %s entry %s/%s: already %d round trips today "
                       "(cap %d) — churn guard", strategy, market, symbol,
                       spun, MAX_ROUND_TRIPS_PER_DAY)
            return False
    v2.execute(
        "INSERT INTO v2_positions(market,strategy,symbol,entry_date,entry_price,shares,"
        "stop,target,trail,peak,conviction,opened_at,why,expiry)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (market, strategy, symbol, entry_date, entry_price, shares, stop, target, trail,
         entry_price if peak is None else peak, conviction,
         datetime.now(timezone.utc).isoformat(), why, expiry))
    # LIVE MIRROR — real money. Runs only when the sleeve is armed AND connected;
    # `live_ready` is false by default and this is a no-op on every other
    # install. Placed AFTER the paper row is written and wrapped, so a broker
    # outage can never prevent the paper book from recording what it decided:
    # the paper book is the evidence and must survive the broker.
    try:
        _live_mirror_entry(v2, market, strategy, symbol, entry_price)
    except Exception:
        _LOG.exception("live mirror (entry) failed for %s — paper book unaffected", symbol)
    # EVERY SUBSCRIBER'S OWN BOOK gets the same decision, sized to their cash.
    # Wrapped and after the house row for the same reason as the broker mirror:
    # the engine's record must survive anything that happens downstream of it.
    try:
        _book_mirror_entry(v2, market, strategy, symbol, entry_price, stop, target)
    except Exception:
        _LOG.exception("user-book mirror (entry) failed for %s", symbol)
    return True


def _book_mirror_entry(v2, market, strategy, symbol, price, stop, target):
    from . import books as _books, plans as _plans
    # None -> books builds the auth DB itself; see books._auth_db for why this
    # must not be `from .main import db` on the engine thread.
    n = _books.mirror_entry(v2, None, _plans, market, strategy, symbol, price, stop, target)
    if n:
        _LOG.info("user books: %s opened in %d book(s)", symbol, n)


def _book_mirror_exit(v2, market, symbol, price, reason):
    from . import books as _books, plans as _plans
    n = _books.mirror_exit(v2, None, _plans, market, symbol, price, reason)
    if n:
        _LOG.info("user books: %s closed in %d book(s)", symbol, n)


def _live_mirror_entry(v2, market, strategy, symbol, price):
    """Fan the entry out to EVERY user who has linked and armed their own
    broker. Each order goes to that user's account, sized to their own margin —
    there is no shared sleeve any more."""
    from . import broker, live_trade
    # verify() before fanning out: a revoked token must fail the gate here, not
    # produce a burst of 401s that look like rejected orders
    users = []
    for u in broker.linked_users():
        broker.verify(u)
        if broker.state(u).get("live_ready"):
            users.append(u)
    if not users:
        return
    main = _ro(MAIN_DB)
    try:
        for uid in users:
            try:
                _LOG.info("live mirror entry u%s %s: %s", uid, symbol,
                          live_trade.mirror_entry(v2, main, uid, market, symbol,
                                                  price, strategy))
            except Exception:
                _LOG.exception("live mirror entry failed for user %s", uid)
    finally:
        main.close()


def _live_mirror_exit(v2, market, symbol, price, reason):
    """Exit is attempted for every CONNECTED user, armed or not: mirror_exit
    records that real shares are still held when disarmed, and a silent skip
    would leave a live position nobody is tracking."""
    from . import broker, live_trade
    users = [u for u in broker.linked_users() if broker.state(u).get("connected")]
    if not users:
        return
    main = _ro(MAIN_DB)
    try:
        for uid in users:
            try:
                _LOG.info("live mirror exit u%s %s: %s", uid, symbol,
                          live_trade.mirror_exit(v2, main, uid, market, symbol,
                                                 price, reason))
            except Exception:
                _LOG.exception("live mirror exit failed for user %s", uid)
    finally:
        main.close()


def poll_market(market):
    live = _live(market)
    if not live:
        _status[market] = "no quotes"
        return
    tails, mdf = _hist(market)
    _session_opens(market, live)                     # keep session open/hi/lo tracking fresh
    # Bring the history up to NOW, not to last night's close.
    today_bar = False
    if INTRADAY_SIGNAL_BAR and market_open(market):
        tails, mdf = append_today_bar(tails, mdf, market, live, _stale_symbols(market))
        today_bar = True
    dates = eng.complete_trading_dates(tails, 0.5)
    if not dates:
        _status[market] = "no history"
        return
    asof = dates[-1]                                 # last COMPLETED trading day
    sigs = _signals_completed(tails, mdf, asof, live)
    today = pd.Timestamp(datetime.now(IST).date())
    rstate = eng.regime_state(mdf, asof, eng.DEFAULTS["regime_lookback"])
    regime = rstate != "OFF"   # allow dip-buys unless the market is genuinely weak
    strong = eng.regime_strong(mdf, asof, eng.DEFAULTS["regime_lookback"])
    v2 = _rw()
    book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
    if not book:
        v2.close(); return
    budget, max_pos = book[0], int(book[1])
    cside = COST_SIDE[market]
    today_s = today.date().isoformat()
    alloc = budget / max_pos
    # refresh live signal lists (for the watchlist UI), per strategy
    for strat in PLAN:
        v2.execute("DELETE FROM v2_signals WHERE market=? AND strategy=?", (market, strat))
    ranked = {}
    for s in sigs:
        ranked.setdefault(s["strategy"], []).append(s)
    for strat, lst in ranked.items():
        lst.sort(key=lambda x: -x["score"])
        for rank, s in enumerate(lst[:max_pos], 1):
            v2.execute("INSERT INTO v2_signals VALUES(?,?,?,?,?,?,?)",
                       (market, strat, today_s, s["symbol"], s["score"], round(s["price"], 2), rank))
    # current shared book
    positions = {r[0]: dict(id=r[1], strategy=r[2], entry=r[3], shares=r[4], stop=r[5], target=r[6], trail=r[7], peak=r[8])
                 for r in v2.execute("SELECT symbol,id,strategy,entry_price,shares,stop,target,trail,peak "
                                     "FROM v2_positions WHERE market=?", (market,))}
    traded = {r[0] for r in v2.execute("SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
    # ---- portfolio circuit breaker: risk guard, exits always keep running ----
    # No NEW entries when today is badly red (>3% of budget) or the book is in a
    # deep drawdown (>15% off its equity peak). A feed glitch / crash day should
    # stop the buying, not grind through every stop with fresh capital.
    pv_now = sum(p["shares"] * live.get(sym, {}).get("price", p["entry"]) for sym, p in positions.items())
    equity_now = cash + pv_now
    try:
        prev = v2.execute("SELECT equity FROM v2_equity WHERE market=? AND date LIKE 'LIVE_%' "
                          "AND substr(date,6,10) < ? ORDER BY date DESC LIMIT 1",
                          (market, today_s)).fetchone()
        peak_eq = v2.execute("SELECT MAX(equity) FROM v2_equity WHERE market=?", (market,)).fetchone()
        day_pnl = (equity_now - prev[0]) / budget if prev and prev[0] else 0.0
        dd = (equity_now / peak_eq[0] - 1) if peak_eq and peak_eq[0] else 0.0
        if day_pnl < -0.03 or dd < -0.15:
            v2.commit(); v2.close()
            _status[market] = (f"CIRCUIT BREAKER {datetime.now(IST).strftime('%H:%M IST')} · "
                               f"day {day_pnl*100:+.1f}% dd {dd*100:+.1f}% · entries paused")
            return
    except Exception:
        pass
    # hold back cash for the intraday sleeve while its entry window is open —
    # otherwise DYN_ALLOC lets the daily book consume the whole budget at the
    # 09:15 open, starving the sleeve before it can find anything (IN only).
    reserve = 0.0
    if market == "IN" and datetime.now(IST).strftime("%H:%M") <= INTRA["last_entry"]:
        n_intra_open = sum(1 for p in positions.values() if p["strategy"] == "intraday_news")
        reserve = min(max(0, INTRA["slots"] - n_intra_open) * alloc, budget / 3.0)
    # FROZEN-QUOTE GUARD. Every intraday lane skips symbols whose quote has
    # stalled; the daily lane did not, so it could enter at a price that had not
    # moved for days (GUJGASLTD sat frozen from Jun 30) and book a fill that never
    # existed at that price. That is a fantasy trade: it corrupts the live record
    # in the lane whose record we most rely on. poll_market runs once per
    # SIGNAL_INTERVAL (300s), so this universe-wide query is cheap here — the
    # 2026-07-24 lesson was that such a query must never run at the 8s cadence.
    stale = _stale_symbols(market)
    # candidate ordering: catalysts (gap) first, then swing; each must clear its own gate
    cand = []
    knives = 0                                           # dip-buys rejected for still falling
    for s in sigs:
        if s["strategy"] in DISABLED_LANES:              # quarantined lanes never trade
            continue
        if s["symbol"] in stale:                         # frozen quote -> fantasy fill
            continue
        pl = PLAN[s["strategy"]]
        if s["score"] < pl["threshold"]:
            continue
        if pl["regime_gated"] and not regime:
            continue
        if s["strategy"] == "mom_breakout" and MOM_REQUIRE_STRONG and not strong:
            continue
        if s["symbol"] in ETF_EXCLUDE:                  # no index/sector/leveraged ETFs
            continue
        if s["price"] < MIN_PRICE.get(market, 0.0):     # quality/liquidity floor
            continue
        if s["strategy"] == "swing_meanrev" and not clear_of_the_day_low(live.get(s["symbol"])):
            knives += 1                                 # still falling — not a dip yet
            continue
        cand.append((pl["priority"], -s["score"], s, pl))
    # ---- meta-label gate: secondary P(win) model on top of the primary engine
    # (proven OOS on 15mo of real data to turn the net-losing raw signal set into
    # a positive-expectancy one by trading only higher-confidence setups). Wrapped
    # so any failure or a missing model leaves the engine exactly as before.
    mp, mfloor = {}, None
    try:
        mp = meta_filter.score(sigs, tails, mdf, asof, rstate, strong, market)
        mfloor = meta_filter.floor(market)
        if mfloor is not None:
            kept = []
            for pr, negscore, s, pl in cand:
                p = mp.get(s["symbol"])
                s["meta_p"] = p
                # gap_momentum was a net loser OOS even after meta -> hold it to a
                # stricter confidence bar than the swing sleeve where meta works.
                thr = mfloor + (0.07 if s["strategy"] == "gap_momentum" else 0.0)
                if p is not None and p < thr:
                    continue                       # drop low-confidence swing/gap signals
                # rank primarily by model confidence when we have it, else by conviction
                rankkey = -p if p is not None else negscore
                kept.append((pr, rankkey, s, pl))
            cand = kept
    except Exception as _mexc:
        pass
    cand.sort(key=lambda x: (x[0], x[1]))
    _tg_radar_digest(market, today, cand, positions)   # once/day: what the AI is watching
    # PUBLISHED IDEAS — from THIS list, in THIS order, after the meta filter.
    # Not a parallel computation: if the ideas page and the book ever disagreed
    # about what looks good, one of them would be lying and there would be no
    # way to tell which. Wrapped so a failure here can never stop the book from
    # trading — ideas are a product feature, the book is the product.
    try:
        from . import books as _bk2, plans as _pl2
        _bk2.snapshot_all(v2, _pl2, market, live)
    except Exception:
        _LOG.exception("user equity snapshot failed")
    try:
        _publish_ideas(v2, market, tails, mdf, asof, rstate, strong,
                       mfloor, live, stale)
    except Exception:
        _LOG.exception("idea publication failed (book unaffected)")
    # strategy balance: hold back slots for swing ONLY when swing actually has
    # eligible candidates, so we never leave cash idle in a gap-only (risk-off) tape
    strat_count = {}
    for p in positions.values():
        strat_count[p["strategy"]] = strat_count.get(p["strategy"], 0) + 1
    swing_avail = sum(1 for _, _, s, _ in cand
                      if s["strategy"] == "swing_meanrev" and s["symbol"] not in positions and s["symbol"] not in traded)
    gap_cap = max_pos - min(max_pos - GAP_SLOT_CAP, swing_avail)
    # ---- pre-trade factor investigation: score the whole universe once ----
    sector_map = _sector_map(market)
    held_sectors = {}
    for psym in positions:
        sec = sector_map.get(str(psym).upper(), "unknown")
        held_sectors[sec] = held_sectors.get(sec, 0) + 1
    try:
        fasof = eng.complete_trading_dates(tails, 0.5)[-1]
        fpanel = fi.build_factor_panel(tails, mdf, fasof)
        fscores = fi.score_panel(fpanel) if len(fpanel) else None
    except Exception as _fexc:
        fpanel = fscores = None
        _status[market] = f"factor panel err: {str(_fexc)[:30]}"
    # ---- ENTRY WINDOW: the validated strategy buys at the session OPEN from
    # prior-close signals. Outside the window, signals/radar refresh but no fills.
    opened_at = _SESSION_OPENED_AT.get(market, 0)
    since_open = (time.time() - opened_at) if opened_at else None
    at_open = since_open is not None and since_open <= ENTRY_WINDOW_SEC
    in_window = since_open is not None and since_open <= REENTRY_WINDOW_SEC
    if not in_window:
        v2.commit(); v2.close()
        _status[market] = (f"signals {datetime.now(IST).strftime('%H:%M IST')} · "
                           f"{len(cand)} candidates · entries at next open")
        return
    halt, hreason = _risk_halt(v2, market)         # protections: don't add risk while bleeding
    if halt:
        v2.commit(); v2.close()
        _status[market] = "entries paused · " + hreason
        return
    mcon = _ro(MAIN_DB)
    fills = exits = vetoed = investig = 0
    for _, _, s, pl in cand:
        if len(positions) >= max_pos or (cash - reserve) < 0.25 * alloc:   # stop when only crumbs remain
            break
        sym = s["symbol"]
        if sym in positions or sym in traded:
            continue
        if s["strategy"] == "gap_momentum" and strat_count.get("gap_momentum", 0) >= gap_cap:
            continue                      # don't let gap_momentum monopolize the book
        if s["strategy"] == "mom_breakout" and strat_count.get("mom_breakout", 0) >= MOM_SLOT_CAP:
            continue                      # momentum sleeve capped at 5 slots
        nscore, severe = _news_state(mcon, sym)
        if severe:                       # pro check: never buy into bad news
            vetoed += 1
            continue
        if _earnings_soon(v2, sym, today_s):   # no fresh entries into an earnings print
            vetoed += 1
            continue
        # ---- THE GATE: investigation HARD GATES must clear (liquidity, drawdown,
        # regime, sector, news). Ranking stays with the proven conviction score and
        # position size comes from the investigation. This HYBRID backtested best
        # (US Sharpe 1.80 vs 1.37, max-DD 10% vs 27%); composite-RANKING was worse.
        size_mult = 1.0
        why = None
        # Defensive: don't trade if the live entry price diverges wildly from the
        # last candle close (a feed glitch / wrong instrument). This check used to
        # sit INSIDE the `fscores is not None` branch below, so whenever the
        # investigation panel was unavailable the engine lost its liquidity gates
        # AND this sanity check at the same moment — precisely when a bad price is
        # most likely and least likely to be caught. It is independent of the
        # investigation, so it now runs unconditionally.
        try:
            cclose = float(fpanel.loc[sym, "close"])
            if cclose > 0 and abs(s["price"] / cclose - 1) > 0.30:
                investig += 1
                continue
        except Exception:
            pass
        if fscores is not None:
            rep = fi.investigate(sym, fpanel, fscores, market, s["strategy"], rstate, severe, held_sectors, sector_map)
            if rep["gates_failed"]:
                investig += 1
                continue
            why = json.dumps(dict(composite=rep.get("composite"), factors=rep.get("factors"),
                                  setup=rep.get("setup"), reasons=rep.get("reasons"),
                                  size_mult=rep.get("size_mult"), regime=rstate,
                                  signal_score=s["score"],
                                  # only open-window fills match the backtest; tag
                                  # the rest so they can be scored on their own
                                  late_entry=not at_open,
                                  # scored on today's partial bar rather than the
                                  # last completed session — also untested
                                  intraday_bar=today_bar))
            size_mult = rep.get("size_mult", 1.0)
        entry, atr = s["price"], s["atr"]
        # volatility-normalized sizing: equal RISK per position, not equal rupees
        # — calmer names get more, jumpier names less. Backtested at MAXPOS=6 this
        # lifted Sharpe 0.50->0.69 and cut max drawdown 8.1%->6.8% vs equal-weight
        # (probability-weighted and rank-by-P were tested too and did NOT help).
        atr_pct = (atr / entry) if entry > 0 else 0.0
        vol_mult = 1.0
        if atr_pct > 0:
            vol_mult = max(VOL_SIZE_MIN, min(VOL_SIZE_MAX, VOL_TARGET_ATR / atr_pct))
        # Sizing normalised by STOP DISTANCE, not just volatility. The rupee at
        # risk on a position is shares x atr_stop x ATR, so widening
        # swing_meanrev's stop from 2.0 to 3.0 ATR would have raised risk per
        # trade by 50% while the size stayed put — a better exit rule silently
        # bought with more risk. Scaling by BASE_ATR_STOP/atr_stop holds the
        # loss on a stopped-out trade at what it was before the change, so the
        # measured improvement is the exit's and not extra leverage.
        stop_atr = pl.get("atr_stop") or BASE_ATR_STOP
        if stop_atr > 0:
            vol_mult *= BASE_ATR_STOP / stop_atr
        remaining = max(1, max_pos - len(positions))
        base_alloc = equity_now / max_pos
        if DYN_ALLOC.get(market, True):
            base_alloc = max(base_alloc, (cash - reserve) / remaining)   # dynamic: idle cash flows to open slots
        shares = min(base_alloc * size_mult * vol_mult, (cash - reserve) / (1 + cside)) / entry   # risk-scaled, never overdraws
        # Bound the rare catastrophic overnight gap. DYN_ALLOC can otherwise
        # hand nearly all free cash to the last open slot, putting the whole
        # book in one name through the close.
        shares = cap_overnight_shares(shares, entry, equity_now, market, s["strategy"], sym)
        if market == "IN":               # NSE: whole shares only, no fractions
            shares = float(int(shares))
            if shares < 1:               # stock too pricey for the per-position budget
                continue
            if shares * entry < SLOT_MIN_UTIL * alloc:   # 1-share fill wastes the slot (e.g. a 5,400 stock in a 10,000 slot)
                continue
        cash -= shares * entry * (1 + cside)
        tgt = entry + pl["atr_target"] * atr if pl["atr_target"] else 0.0
        if s["strategy"] == "gap_momentum" and GAP_TARGET.get(market):   # per-market gap profit target
            tgt = entry * (1 + GAP_TARGET[market])
        trail = pl["trail"]
        if s["strategy"] == "mom_breakout":              # ATR-proportional trail (2.5x entry ATR)
            trail = min(0.20, max(0.04, 2.5 * atr / entry))
        if not record_entry(v2, market, s["strategy"], sym, today_s, entry, shares,
                            entry - pl["atr_stop"] * atr, tgt, trail, s["score"], why):
            cash += shares * entry * (1 + cside)   # debited above; no position opened
            continue
        positions[sym] = dict(id=None, strategy=s["strategy"], entry=entry, shares=shares,
                              stop=entry - pl["atr_stop"] * atr, target=tgt, trail=trail, peak=entry)
        try:
            from . import telegram_bot
            telegram_bot.notify_trade("BUY", sym, (int(shares) if float(shares).is_integer() else round(shares, 2)),
                                      round(entry, 2), market, strategy=s["strategy"],
                                      stop=round(entry - pl["atr_stop"] * atr, 2),
                                      target=(round(tgt, 2) if tgt else 0), trail=trail)
        except Exception:
            pass
        strat_count[s["strategy"]] = strat_count.get(s["strategy"], 0) + 1
        sec = sector_map.get(sym, "unknown")
        held_sectors[sec] = held_sectors.get(sec, 0) + 1
        traded.add(sym); fills += 1
    mcon.close()
    v2.commit(); v2.close()
    _status[market] = (f"signals {datetime.now(IST).strftime('%H:%M IST')} · +{fills} new · "
                       f"{vetoed} news-vetoed · {investig} investigation-rejected · "
                       f"{knives} still-falling")


_INTRA_AVGVOL: dict = {}   # market -> (date, {sym: 20d avg daily volume}) refreshed daily
_INTRA_WATCHED: dict = {}  # date -> symbols already radar-alerted (watch-only mode dedupe)

# NSE intraday volume is U-shaped (heavy open, quiet lunch, heavy close), NOT
# linear. Points = (minutes since 09:15 open, approx cumulative fraction of the
# day's volume). A linear pace overstated rvol late morning (fake "surges") and
# understated it right after the open (missed real ones).
_VOL_CURVE = [(0, 0.02), (5, 0.05), (15, 0.10), (30, 0.16), (60, 0.25),
              (105, 0.35), (165, 0.46), (225, 0.57), (285, 0.70), (345, 0.88), (375, 1.0)]


# Cash-parking instruments. The meta model rates them near-certain winners
# because they drift up a fraction of a percent a day and essentially never
# fall — LIQUID scored 0.708 today, the highest of anything. They are not
# trades, and without this they would be the top of every ideas list the moment
# the ranking stops being the engine's trading gate.
_CASH_ETF_MARKERS = ("LIQUID", "LIQUIDCASE", "LIQUIDBEES", "GILT", "AAAGILT")


def _is_cash_etf(symbol):
    s = str(symbol or "").upper()
    return any(m in s for m in _CASH_ETF_MARKERS)


        # The ideas pool is built from a LOWER conviction threshold than the
        # book trades on. poll_market's `sigs` is already filtered to >= 0.55 by
        # _signals_completed, so ideas never saw the names the confidence model
        # actually likes: today SONACOMS scored p(win) 0.617 on a conviction of
        # 0.499, and ENDURANCE 0.607 on 0.410. Both were cut upstream, which is
        # why "publish from the model, not the buy list" changed nothing on its
        # own — the list handed in had already had the model's best names
        # removed by a different filter.
IDEAS_MIN_CONVICTION = 0.15


def _publish_ideas(v2, market, tails, mdf, asof, rstate, strong,
                   mfloor, live, stale):
    """Publish today's ideas and advance the open ones.

    IDEAS ARE NOT THE BOOK'S BUY LIST, and tying them to it was a design
    mistake. The book's candidate list answers "would I risk MY capital right
    now, in this regime, in this lane, with a slot free" — and in a NEUTRAL
    regime that list is empty, so a subscriber paying for daily ideas saw an
    empty page. A daily-ideas product that publishes nothing most days is not a
    product.

    The bar here is the CONFIDENCE MODEL instead: the same p(win) meta-label the
    engine trusts to decide what is worth trading, applied without the
    regime gate and without the book's slot and cash constraints, which are
    facts about the book rather than about the stock.

    What is NOT relaxed: the model floor itself, the stale-quote guard, the
    minimum price, disabled lanes, and cash-parking ETFs. Those exclude things
    that are wrong, not things that are merely unavailable today.

    Publication still requires the market to be OPEN — an idea carries a
    specific entry price and nobody can act on it outside market hours.
    """
    from . import ideas as _ideas
    now = datetime.now(IST)
    today_s = now.date().isoformat()
    # track first: an idea published earlier must be resolved against today's
    # tape before any new one is written, so a stop hit is never reported after
    # the day's fresh list has already pushed it down the page
    _ideas.track(v2, market, live, now.isoformat(), today_s)
    if not market_open(market):
        return
    # Its OWN signal sweep at a lower conviction bar, then the model ranks it.
    sigs = []
    for s in eng.signals_for_date(tails, mdf, asof,
                                  threshold=IDEAS_MIN_CONVICTION,
                                  atr_stop=PLAN["swing_meanrev"]["atr_stop"],
                                  atr_target=PLAN["swing_meanrev"]["atr_target"]):
        lq = live.get(s["symbol"])
        sigs.append(dict(symbol=s["symbol"], strategy="swing_meanrev",
                         score=s["conviction"], atr=s["atr"],
                         price=(lq["price"] if lq else s["ref_close"])))
    from . import meta_filter as _mf
    mp = _mf.score(sigs, tails, mdf, asof, rstate, strong, market)
    pool = []
    for s in sigs:
        sym = s.get("symbol")
        if s.get("strategy") in DISABLED_LANES or sym in stale:
            continue
        if sym in ETF_EXCLUDE or _is_cash_etf(sym):
            continue
        if float(s.get("price") or 0) < MIN_PRICE.get(market, 0.0):
            continue
        p = mp.get(sym)
        if mfloor is not None and (p is None or p < mfloor):
            continue
        pool.append((p if p is not None else float(s.get("score") or 0), s))
    pool.sort(key=lambda r: -r[0])
    rows = [dict(s, meta_p=p) for p, s in pool]
    n = _ideas.publish(v2, market, rows,
                       lambda strat: float(PLAN.get(strat, {}).get("atr_stop") or 0.0),
                       today_s, now.isoformat())
    # ALWAYS log, including the zero case. "No ideas today" and "idea publishing
    # is broken" look identical from the outside, and the only way to tell them
    # apart after the fact is a line that says which one happened.
    have = v2.execute("SELECT COUNT(*) FROM v2_ideas WHERE market=? AND published_date=?",
                      (market, today_s)).fetchone()[0]
    _LOG.info("ideas: %d qualified (of %d signals) -> %d new (%d today)%s",
              len(rows), len(sigs), n, have,
              "" if rows else f" — nothing cleared the p(win) floor {mfloor}")


def trading_days_held(entry_date, today, market):
    """Sessions elapsed between entry and `today`, skipping weekends and the
    exchange holidays in MARKET_HOLIDAYS.

    Extracted verbatim from exit_monitor so the hold clock can be tested; the
    semantics are unchanged. Calendar counting used to force-sell roughly two
    sessions early around weekends, truncating the bounce the 8-bar hold was
    validated on.
    """
    try:
        import numpy as _np
        return int(_np.busday_count(str(entry_date)[:10], today.isoformat(),
                                    holidays=MARKET_HOLIDAYS.get(market, [])))
    except ValueError:
        return 0


def _vol_frac(mins):
    """Piecewise-linear cumulative volume fraction for `mins` after the open."""
    if mins <= 0:
        return 0.04
    for (m0, f0), (m1, f1) in zip(_VOL_CURVE, _VOL_CURVE[1:]):
        if mins <= m1:
            return max(0.04, f0 + (f1 - f0) * (mins - m0) / (m1 - m0))
    return 1.0


# sentiment_events / latest_quotes store ISO timestamps with a 'T' separator;
# sqlite's datetime('now') emits a space. A plain string >= comparison therefore
# passed EVERY event on the boundary date, silently widening a "24h" window to
# 24-48h. strftime with an explicit 'T' makes the comparison correct.
FRESH_NEWS_SQL = ("SELECT MAX(score) FROM sentiment_events WHERE symbol=? "
                  "AND ts>=?")


def fresh_news_cutoff(today=None):
    """UTC ISO cutoff for 'today or the previous trading session, nothing older'.

    Was a rolling '-1 day', which at 14:00 reached back to 14:00 yesterday and
    on a Monday reached into Sunday — i.e. it could act on a stale Friday item
    while silently ignoring a Friday-evening one. Operator requirement is
    strict: same day or at most one session prior, never older. Anchoring to
    midnight IST of one trading session back is weekend- and holiday-aware, so
    on Monday the window starts Friday 00:00 IST and on Tuesday it starts
    Monday 00:00 IST.
    """
    epoch = _catalyst_cutoff_epoch(1, today=today)
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _intra_avgvol(market, tails):
    today_s = datetime.now(IST).date().isoformat()
    c = _INTRA_AVGVOL.get(market)
    if c and c[0] == today_s:
        return c[1]
    av = {}
    for sym, g in tails.items():
        try:
            v = float(g["volume"].tail(20).mean())
            if v > 0:
                av[sym] = v
        except Exception:
            pass
    _INTRA_AVGVOL[market] = (today_s, av)
    return av


def intraday_news_pass(market):
    """Intraday news-momentum sleeve (user spec): buy a stock that is up vs
    TODAY's open, pressing its intraday high on a genuine volume surge, WITH a
    fresh positive news catalyst. Exits: +3.5% target / -1.75% stop / breakeven
    lock at +1.5% / hard square-off 15:12 IST (handled in exit_monitor).
    Runs every engine cycle inside the entry window; cheap (one quotes read)."""
    if "intraday_news" in DISABLED_LANES:      # honour the quarantine list
        return
    if market != "IN":
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    if hm < INTRA["start"] or hm > INTRA["watch_until"]:
        return
    news_cutoff = fresh_news_cutoff()   # today or one session back — nothing older
    # after the proven entry window: keep scanning ALL day, but alert-only —
    # afternoon entries backtested as losers even on fresh news, so the book
    # never buys them; the user still sees every qualified mover on Telegram.
    watch_only = hm > INTRA["last_entry"]
    # stale-feed guard: if the quote feed has stalled, "fills" would happen at
    # frozen prices — fantasy trades that would corrupt the live trial.
    try:
        con = _ro(MAIN_DB)
        age = con.execute("SELECT (julianday('now')-julianday(MAX(ts)))*86400 FROM latest_quotes WHERE source=?",
                          (LIVE_SOURCE[market],)).fetchone()[0]
        con.close()
        if age is not None and age > 120:
            return
    except Exception:
        pass
    live = _live(market)
    if not live:
        return
    tails, _mdf = _hist(market)
    av = _intra_avgvol(market, tails)
    # cumulative volume expected by now — U-shaped session curve, not linear
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    frac = _vol_frac(mins)
    v2 = _rw()
    book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
    if not book:
        v2.close(); return
    budget, max_pos = book[0], int(book[1])
    positions = {r[0]: r[1] for r in v2.execute("SELECT symbol,strategy FROM v2_positions WHERE market=?", (market,))}
    n_intra = sum(1 for st in positions.values() if st == "intraday_news")
    if n_intra >= INTRA["slots"] and not watch_only:
        v2.close(); return
    today_s = now.date().isoformat()
    # daily churn cap: slots + one refill. 3 stop-outs then 3 more losers would
    # otherwise be possible in a single bad tape.
    n_today = v2.execute("SELECT COUNT(*) FROM v2_trades WHERE market=? AND strategy='intraday_news' AND entry_date=?",
                         (market, today_s)).fetchone()[0] or 0
    if n_intra + n_today >= 2 * INTRA["slots"] and not watch_only:
        v2.close(); return
    traded = {r[0] for r in v2.execute("SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    invested = v2.execute("SELECT COALESCE(SUM(shares*entry_price),0) FROM v2_positions WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - invested + realised
    alloc = budget / max_pos
    cside = COST_SIDE[market]
    cands = []
    for sym, lq in live.items():
        if sym in positions or sym in traded or sym in ETF_EXCLUDE:
            continue
        if sym not in av:
            continue
        if lq["price"] < MIN_PRICE.get(market, 0.0):
            continue
        if av[sym] * lq["price"] < INTRA["min_turnover"]:   # liquid names only
            continue
        o = lq.get("open") or 0.0
        if o <= 0:
            continue
        move = lq["price"] / o - 1
        if not (INTRA["move_min"] <= move <= INTRA["move_max"]):
            continue
        if lq["price"] < 0.998 * lq["high"]:      # must be pressing today's high NOW
            continue
        rvol = (lq.get("vol") or 0.0) / (av[sym] * frac)
        if rvol < INTRA["rvol_min"]:
            continue
        cands.append((move * min(rvol, 6.0), move, rvol, sym, lq))
    if not cands:
        v2.close(); return
    cands.sort(key=lambda x: -x[0])
    mcon = _ro(MAIN_DB)
    if watch_only:
        # afternoon: alert qualified movers (all gates incl news), never buy.
        for k in list(_INTRA_WATCHED):
            if k != today_s:
                del _INTRA_WATCHED[k]
        seen = _INTRA_WATCHED.setdefault(today_s, set())
        items = []
        for _, move, rvol, sym, lq in cands:
            if len(items) >= 3:
                break
            if sym in seen:
                continue
            try:
                row = mcon.execute(FRESH_NEWS_SQL, (sym, news_cutoff)).fetchone()
                ns = float(row[0]) if row and row[0] is not None else 0.0
            except Exception:
                ns = 0.0
            if ns < INTRA["news_min"]:
                continue
            _sc, severe = _news_state(mcon, sym)
            if severe:
                continue
            seen.add(sym)
            items.append({"symbol": sym,
                          "note": "intraday mover %+.1f%% on news — watch only, outside entry window" % (move * 100)})
        mcon.close(); v2.close()
        if items:
            try:
                from . import telegram_bot
                telegram_bot.notify_radar(items, market)
            except Exception:
                pass
        return
    fills = 0
    for _, move, rvol, sym, lq in cands:
        if n_intra + fills >= INTRA["slots"] or cash < 0.25 * alloc:
            break
        # the backtested edge REQUIRES a fresh (<=24h) positive catalyst
        try:
            row = mcon.execute(FRESH_NEWS_SQL, (sym, news_cutoff)).fetchone()
            ns = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            ns = 0.0
        if ns < INTRA["news_min"]:
            continue
        _sc, severe = _news_state(mcon, sym)
        if severe:                                 # never buy into bad news
            continue
        entry = lq["price"]
        shares = float(int(min(alloc, cash / (1 + cside)) / entry))
        if shares < 1 or shares * entry < SLOT_MIN_UTIL * alloc:
            continue
        cash -= shares * entry * (1 + cside)
        stop, tgt = entry * (1 - INTRA["sl"]), entry * (1 + INTRA["tp"])
        why = json.dumps(dict(setup="intraday_news", move_pct=round(move * 100, 2),
                              rvol=round(rvol, 1), news=round(ns, 2)))
        if not record_entry(v2, market, "intraday_news", sym, today_s, entry, shares,
                            stop, tgt, 0.0, round(min(move / 0.05, 1.0), 4), why):
            continue
        try:
            from . import telegram_bot
            telegram_bot.notify_trade("BUY", sym, int(shares), round(entry, 2), market,
                                      strategy="intraday_news", stop=round(stop, 2), target=round(tgt, 2))
        except Exception:
            pass
        traded.add(sym); fills += 1
    mcon.close()
    v2.commit(); v2.close()
    if fills:
        _status[market] = f"intraday +{fills} @ {hm} IST · " + _status.get(market, "")


_PREVCLOSE: dict = {}   # market -> (date, {sym: last daily-candle close}) refreshed daily


def _prev_close(market):
    """Last COMPLETED daily-candle close per symbol (= the reference the day's %
    move is measured against). Cached per day."""
    today_s = datetime.now(IST).date().isoformat()
    c = _PREVCLOSE.get(market)
    if c and c[0] == today_s:
        return c[1]
    out = {}
    try:
        con = _ro(MAIN_DB)
        src = eng.DAILY_SOURCE.get(market, "upstox-live:day")
        for sym, cl in con.execute(
            "SELECT symbol, close FROM (SELECT symbol, close, ROW_NUMBER() OVER "
            "(PARTITION BY symbol ORDER BY ts DESC) rn FROM candles WHERE source=?) WHERE rn=1", (src,)):
            try:
                v = float(cl)
                if v > 0:
                    out[str(sym).upper()] = v
            except (TypeError, ValueError):
                pass
        con.close()
    except Exception:
        pass
    _PREVCLOSE[market] = (today_s, out)
    return out


def _stale_symbols(market, lag=STALE_QUOTE_SEC):
    """Symbols whose latest_quotes timestamp lags the market's FRESHEST quote by
    more than `lag` seconds — i.e. frozen (e.g. GUJGASLTD stuck since Jun 30).
    Used to skip entries on frozen data AND to avoid marking/exiting positions
    off a stale price (a frozen HIGH quote would phantom-hit a target)."""
    out = set()
    try:
        con = _ro(MAIN_DB)
        rows = con.execute("SELECT symbol, ts FROM latest_quotes WHERE source=?",
                           (LIVE_SOURCE[market],)).fetchall()
        con.close()
        eps = {}
        for sym, ts in rows:
            try:
                eps[str(sym).upper()] = datetime.fromisoformat(ts).timestamp()
            except Exception:
                pass
        if not eps:
            return out
        fresh = max(eps.values())
        out = {s for s, e in eps.items() if fresh - e > lag}
    except Exception:
        pass
    return out


CATALYST_DB = os.environ.get("CATALYST_DB", "/opt/opentrade/var/catalysts.db")


def _catalyst_cutoff_epoch(sessions, today=None):
    """Epoch of 00:00 IST on the trading day `sessions` sessions before today, so
    the catalyst-freshness window bridges weekends/holidays instead of a flat 48
    wall-clock hours (which expired Friday results before Monday's move).

    `today` is injectable for testing only; production callers omit it and get
    the current IST date exactly as before.
    """
    today = today or datetime.now(IST).date()
    try:
        import numpy as _np
        hol = _np.array(MARKET_HOLIDAYS.get("IN", []), dtype="datetime64[D]")
        cd = _np.busday_offset(_np.datetime64(today, "D"), -sessions,
                               roll="backward", holidays=hol)
        d = cd.astype("datetime64[D]").astype(object)   # -> datetime.date
        return int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    except Exception:
        return int(time.time()) - (sessions + 2) * 24 * 3600   # weekend-padded fallback


def _nse_catalyst_symbols(sessions=VOLSURGE["catalyst_sessions"]):
    """Symbols with a MATERIAL NSE filing (results/order/corp_action) within the
    last `sessions` TRADING sessions (weekend/holiday-aware). Real-time source
    (scripts/nse_announcements.py), replacing the late/incomplete Bing-RSS score
    gate for the volume_surge lane. Reads its OWN WAL db so it never contends with
    the trading_agent.db. Returns {symbol: category} — the freshest material
    catalyst type per symbol (so each buy can record WHY, e.g. 'results' / 'order').
    The lane still requires a SAME-DAY volume+price surge before buying, so an
    older-but-in-window catalyst only makes the name eligible, never buys it stale."""
    out = {}
    try:
        con = _ro(CATALYST_DB)
        cutoff = _catalyst_cutoff_epoch(sessions)
        for sym, cat in con.execute(
            "SELECT symbol, category FROM nse_announcements WHERE an_epoch >= ? "
            "AND category IN ('results','order','corp_action') ORDER BY an_epoch DESC", (cutoff,)):
            out.setdefault(str(sym).upper(), cat)   # first seen = freshest (DESC order)
        con.close()
    except Exception:
        pass
    return out


def _risk_halt(v2, market):
    """Freqtrade-style protections. Returns (halt_bool, reason). Halts NEW entries
    (never touches open positions) when:
      - StoplossGuard: >= RISK[stopguard_n] stop/trail exits already today, OR
      - MaxDrawdown:   book equity is > RISK[maxdd_halt] below today's peak."""
    today = datetime.now(IST).date().isoformat()
    try:
        n_stops = v2.execute("SELECT COUNT(*) FROM v2_trades WHERE market=? AND substr(exit_date,1,10)=? "
                             "AND reason IN ('stop','trail')", (market, today)).fetchone()[0] or 0
        if n_stops >= RISK["stopguard_n"]:
            return True, "stoploss-guard (%d stops today)" % n_stops
    except Exception:
        pass
    try:
        utc_d = datetime.now(timezone.utc).date().isoformat()
        eqs = [r[0] for r in v2.execute("SELECT equity FROM v2_equity WHERE market=? AND date LIKE ? ORDER BY date",
                                        (market, "LIVE_" + utc_d + "%"))]
        if len(eqs) >= 3:
            peak, cur = max(eqs), eqs[-1]
            if peak > 0 and (peak - cur) / peak > RISK["maxdd_halt"]:
                return True, "max-drawdown halt (%.1f%% off today's peak)" % ((peak - cur) / peak * 100)
    except Exception:
        pass
    return False, ""


def volume_surge_pass(market):
    """The day-1 mover catcher: buy a LIQUID stock up >= 4% vs prev close, holding
    near its day high on a >= 3x volume surge, WITH a fresh material NSE catalyst
    (results/order/board-outcome). Skips frozen quotes and circuit-locked names.
    Exits handled by exit_monitor (TP/stop/breakeven-lock/square-off 15:12)."""
    if "volume_surge" in DISABLED_LANES:      # honour the quarantine list
        return
    if market != "IN":
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    # The auction lets this lane work from the bell instead of 09:18: the gap and
    # its participation are already known, so there is nothing to wait for.
    seed = preopen_seed_map(hm)
    earliest = "09:15" if seed else VOLSURGE["start"]
    if hm < earliest or hm > VOLSURGE["last_entry"]:
        return
    live = _live(market)
    if not live:
        return
    catalysts = _nse_catalyst_symbols()
    if not catalysts:
        return                               # no catalyst => nothing this lane trades
    stale = _stale_symbols(market)
    tails, _mdf = _hist(market)
    av = _intra_avgvol(market, tails)
    prevc = _prev_close(market)
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    frac = _vol_frac(mins)
    v2 = _rw()
    book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
    if not book:
        v2.close(); return
    budget, max_pos = book[0], int(book[1])
    halt, hreason = _risk_halt(v2, market)         # protections: don't add risk while bleeding
    if halt:
        _status[market] = "volsurge paused · " + hreason
        v2.close(); return
    positions = {r[0]: r[1] for r in v2.execute("SELECT symbol,strategy FROM v2_positions WHERE market=?", (market,))}
    n_vs = sum(1 for st in positions.values() if st == "volume_surge")
    if n_vs >= VOLSURGE["slots"] or len(positions) >= max_pos:
        v2.close(); return
    today_s = now.date().isoformat()
    traded = {r[0] for r in v2.execute("SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    invested = v2.execute("SELECT COALESCE(SUM(shares*entry_price),0) FROM v2_positions WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - invested + realised
    alloc = budget / max_pos
    cside = COST_SIDE[market]
    cands = []
    for sym in catalysts:                    # iterate the SHORT catalyst list, not 2600 quotes
        if sym in positions or sym in traded or sym in ETF_EXCLUDE or sym in stale:
            continue
        lq = live.get(sym)
        if not lq or sym not in av or sym not in prevc:
            continue
        if lq["price"] < MIN_PRICE.get(market, 0.0):
            continue
        if av[sym] * lq["price"] < VOLSURGE["min_turnover"]:
            continue
        pc = prevc[sym]
        move = lq["price"] / pc - 1
        if not (VOLSURGE["move_min"] <= move <= VOLSURGE["move_max"]):
            continue
        if lq["price"] < VOLSURGE["near_high"] * lq["high"]:   # holding near day high, not fading
            continue
        rvol = (lq.get("vol") or 0.0) / (av[sym] * frac)
        seeded = False
        if rvol < VOLSURGE["rvol_min"]:
            # In the first minutes rvol cannot confirm anything — there is not
            # enough intraday volume yet. Fall back to what the auction already
            # proved: this name gapped, and real money crossed to do it.
            auction = seed.get(sym) if seed else None
            if not (auction
                    and auction["gap_pct"] / 100.0 >= PREOPEN_SEED["min_gap"]
                    and auction["value"] >= PREOPEN_SEED["min_auction_value"]):
                continue
            seeded = True
        cands.append((move * min(max(rvol, 1.0), 8.0), move, rvol, sym, lq, seeded))
    if not cands:
        v2.close(); return
    cands.sort(key=lambda x: -x[0])
    fills = 0
    for _, move, rvol, sym, lq, seeded in cands:
        if n_vs + fills >= VOLSURGE["slots"] or len(positions) + fills >= max_pos or cash < 0.25 * alloc:
            break
        entry = lq["price"]
        atr_pct = 0.0175                      # % stop drives sizing (no ATR for intraday)
        vol_mult = max(VOL_SIZE_MIN, min(VOL_SIZE_MAX, VOL_TARGET_ATR / max(atr_pct, 0.005)))
        shares = float(int(min(alloc * vol_mult, cash / (1 + cside)) / entry))
        if shares < 1 or shares * entry < SLOT_MIN_UTIL * alloc:
            continue
        cash -= shares * entry * (1 + cside)
        stop, tgt = entry * (1 - VOLSURGE["sl"]), entry * (1 + VOLSURGE["tp"])
        cat_label = {"results": "Q results", "order": "new order",
                     "corp_action": "corporate action"}.get(catalysts.get(sym), "NSE filing")
        why = json.dumps(dict(setup="volume_surge", move_pct=round(move * 100, 2),
                              rvol=round(rvol, 1), catalyst=cat_label,
                              # tagged because this path cannot be backtested —
                              # score these separately before trusting them
                              preopen_seeded=seeded))
        if not record_entry(v2, market, "volume_surge", sym, today_s, entry, shares,
                            stop, tgt, 0.0, round(min(move / 0.10, 1.0), 4), why):
            continue
        try:
            from . import telegram_bot
            telegram_bot.notify_trade("BUY", sym, int(shares), round(entry, 2), market,
                                      strategy="volume_surge", stop=round(stop, 2), target=round(tgt, 2))
        except Exception:
            pass
        traded.add(sym); fills += 1
    v2.commit(); v2.close()
    if fills:
        _status[market] = f"volsurge +{fills} @ {hm} IST · " + _status.get(market, "")


def rank_movers(live, prev_close, min_move, min_turnover, stale=()):
    """Rank symbols by move from the DAY'S OPEN, strongest first.

    Pure, so the selection rule is testable without a database or a market.
    Returns [(move_fraction, symbol, price)].

    Move is measured from the open, not the previous close: an overnight gap is
    already in the price by 10:15, and the strategy is buying today's intraday
    strength rather than yesterday's news.
    """
    out = []
    for symbol, q in (live or {}).items():
        if symbol in stale:
            continue
        try:
            price = float(q.get("price") or 0.0)
            day_open = float(q.get("open") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if price <= 0 or day_open <= 0:
            continue
        move = price / day_open - 1.0
        if move < min_move:
            continue
        if min_turnover and price * float(q.get("volume") or 0.0) < min_turnover:
            continue
        out.append((move, symbol, price))
    out.sort(reverse=True)
    return out


def intraday_momentum_pass(market):
    """Buy the single strongest mover of the day, 60 minutes after the open.

    See the INTRAMOM comment for the measured basis and its caveats. Exits are
    handled by exit_monitor: +2% target, -1% stop, square-off at 15:12 with the
    other intraday lanes.
    """
    if market != "IN" or not INTRAMOM.get("enabled"):
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    if hm < INTRAMOM["start"] or hm > INTRAMOM["last_entry"]:
        return
    live = _live(market)
    if not live:
        return

    v2 = _rw()
    try:
        book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
        if not book:
            return
        budget, max_pos = book[0], int(book[1])
        halt, reason = _risk_halt(v2, market)
        if halt:
            _status[market] = "intramom paused · " + reason
            return
        positions = {r[0]: r[1] for r in v2.execute(
            "SELECT symbol,strategy FROM v2_positions WHERE market=?", (market,))}
        held = sum(1 for st in positions.values() if st == "intraday_momentum")
        if held >= INTRAMOM["slots"] or len(positions) >= max_pos:
            return

        today_s = now.date().isoformat()
        traded = {r[0] for r in v2.execute(
            "SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
        ranked = rank_movers(_live(market), None, INTRAMOM["min_move"],
                             INTRAMOM["min_turnover"], _stale_symbols(market))
        ranked = [r for r in ranked if r[1] not in positions and r[1] not in traded]
        if not ranked:
            return

        realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?",
                              (market,)).fetchone()[0] or 0.0
        invested = v2.execute("SELECT COALESCE(SUM(shares*entry_price),0) FROM v2_positions "
                              "WHERE market=?", (market,)).fetchone()[0] or 0.0
        cash = budget - invested + realised
        cside = COST_SIDE[market]
        alloc = budget / max_pos

        move, symbol, price = ranked[0]        # ONLY the top mover — see INTRAMOM
        shares = float(int(min(alloc * INTRAMOM["size_frac"], cash / (1 + cside)) / price))
        if shares < 1:
            return
        stop = price * (1 - INTRAMOM["sl"])
        target = price * (1 + INTRAMOM["tp"])
        why = json.dumps(dict(setup="intraday_momentum", move_pct=round(move * 100, 2),
                              rank=1, entry_window=INTRAMOM["start"]))
        if not record_entry(v2, market, "intraday_momentum", symbol, today_s, price, shares,
                            stop, target, 0.0, round(min(move / 0.05, 1.0), 4), why):
            return
        v2.commit()
        _LOG.info("intraday_momentum BUY %s %.0f @ %.2f (top mover, %+.2f%% from open)",
                  symbol, shares, price, move * 100)
        try:
            from . import telegram_bot
            telegram_bot.notify_trade("BUY", symbol, int(shares), round(price, 2), market,
                                      strategy="intraday_momentum", stop=round(stop, 2),
                                      target=round(target, 2))
        except Exception:
            pass
        _status[market] = f"intramom +1 {symbol}"
    finally:
        v2.close()


def btst_pass(market, force=False):
    """BTST lane: near the close, buy strong-closing catalyst momentum names to hold
    ONE overnight for the gap-up. Sells at the next open (handled in exit_monitor).
    `force` bypasses the close-time window for a one-off seed. Mirrors
    volume_surge_pass' structure/guards; the only differences are the close-position
    filter, the overnight hold, and no intraday square-off."""
    if "btst" in DISABLED_LANES:      # honour the quarantine list
        return
    if market != "IN":
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    if not force and not (BTST["entry_start"] <= hm <= BTST["entry_last"]):
        return
    live = _live(market)
    if not live:
        return
    catalysts = _nse_catalyst_symbols(BTST["catalyst_sessions"])
    if not catalysts:
        return
    stale = _stale_symbols(market)
    tails, _mdf = _hist(market)
    av = _intra_avgvol(market, tails)
    prevc = _prev_close(market)
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    frac = _vol_frac(mins)
    v2 = _rw()
    book = v2.execute("SELECT budget,max_pos FROM v2_book WHERE market=?", (market,)).fetchone()
    if not book:
        v2.close(); return
    budget, max_pos = book[0], int(book[1])
    halt, hreason = _risk_halt(v2, market)         # don't add overnight risk while bleeding
    if halt:
        _status[market] = "btst paused · " + hreason
        v2.close(); return
    positions = {r[0]: r[1] for r in v2.execute("SELECT symbol,strategy FROM v2_positions WHERE market=?", (market,))}
    n_bt = sum(1 for st in positions.values() if st == "btst")
    if n_bt >= BTST["slots"] or len(positions) >= max_pos:
        v2.close(); return
    today_s = now.date().isoformat()
    traded = {r[0] for r in v2.execute("SELECT symbol FROM v2_trades WHERE market=? AND entry_date=?", (market, today_s))}
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    invested = v2.execute("SELECT COALESCE(SUM(shares*entry_price),0) FROM v2_positions WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - invested + realised
    alloc = budget / max_pos
    cside = COST_SIDE[market]
    cands = []
    for sym in catalysts:
        if sym in positions or sym in traded or sym in ETF_EXCLUDE or sym in stale:
            continue
        lq = live.get(sym)
        if not lq or sym not in av or sym not in prevc:
            continue
        if lq["price"] < MIN_PRICE.get(market, 0.0):
            continue
        if av[sym] * lq["price"] < BTST["min_turnover"]:
            continue
        move = lq["price"] / prevc[sym] - 1
        if move < BTST["move_min"]:
            continue
        hi = lq.get("high") or lq["price"]
        lo = lq.get("low") or lq["price"]
        rng = hi - lo
        cpos = (lq["price"] - lo) / rng if rng > 0 else 1.0
        if cpos < BTST["close_pos_min"]:                # must close near the day high
            continue
        rvol = (lq.get("vol") or 0.0) / (av[sym] * frac) if (av[sym] and frac) else 0.0
        if rvol < BTST["rvol_min"]:
            continue
        cands.append((move * min(rvol, 8.0), move, rvol, cpos, sym, lq))
    if not cands:
        v2.close(); return
    cands.sort(key=lambda x: -x[0])
    fills = 0
    for _, move, rvol, cpos, sym, lq in cands:
        if n_bt + fills >= BTST["slots"] or len(positions) + fills >= max_pos or cash < 0.25 * alloc:
            break
        entry = lq["price"]
        shares = float(int(min(alloc * BTST["size_frac"], cash / (1 + cside)) / entry))
        if shares < 1:
            continue
        cash -= shares * entry * (1 + cside)
        stop = entry * (1 - BTST["sl"])
        cat_label = {"results": "Q results", "order": "new order",
                     "corp_action": "corporate action"}.get(catalysts.get(sym), "NSE filing")
        why = json.dumps(dict(setup="btst", move_pct=round(move * 100, 2), rvol=round(rvol, 1),
                              close_pos=round(cpos, 2), catalyst=cat_label, exit="next open"))
        if not record_entry(v2, market, "btst", sym, today_s, entry, shares,
                            stop, 0.0, 0.0, round(min(move / 0.10, 1.0), 4), why):
            continue
        try:
            from . import telegram_bot
            telegram_bot.notify_trade("BUY", sym, int(shares), round(entry, 2), market,
                                      strategy="btst", stop=round(stop, 2))
        except Exception:
            pass
        fills += 1
    v2.commit(); v2.close()
    if fills:
        _status[market] = f"btst +{fills} @ {hm} IST (sell at open) · " + _status.get(market, "")


_SECTOR_ALERTED: dict = {}   # date -> set of sectors already radar-alerted (dedupe)


def index_options_pass(market):
    """Buy an index CE or PE when the call is strong enough — the execution path.

    Everything before this produced analysis and nothing consumed it: the
    direction engine, the chain analytics, the levels and the settings toggle
    all existed while INDEX_OPTIONS appeared exactly once in this file, in its
    config block. The switch was wired to nothing.

    The gates below are not style — each one is a measured result from the
    404-session study, and without them this lane loses money by construction:

      * NOT INTO AN EVENT. Implied volatility is bid before Budget/policy
        and collapses after, so a directionally correct trade still loses to the
        crush.
      * NOT WHEN PREMIUM IS RICH. The ATM straddle is the market's own expected
        move; if it already exceeds what the signal is playing for, the option is
        priced against us regardless of direction.
      * ONE POSITION PER INDEX, defined risk, premium capped as a share of the
        book, and the whole lane funded by its OWN ring-fenced capital.

    WHICH INDICES is the operator's call, not this function's. It used to skip
    everything except NIFTY on the Bank Nifty premium finding (implied 2.20% vs
    1.13% realised) — but the settings page was offering all four, so three
    tick-boxes did nothing. The finding now reaches the UI through
    INDEX_PREMIUM_WARNING instead of being enforced behind a control that
    claimed otherwise.

    The direction call itself measured at 36% accuracy over 404 sessions. This
    makes the path REAL; it does not make the signal good.
    """
    index_settings_load()                 # the operator's saved choices, every pass
    cfg = INDEX_OPTIONS
    if not cfg.get("enabled") or not cfg.get("auto_trade"):
        return
    if market != "IN" or not market_open(market):
        return
    hm = datetime.now(IST).strftime("%H:%M")
    if hm < "09:20" or hm > "14:30":          # skip the open's noise and the close
        return
    try:
        from . import event_calendar, index_direction, market_internals, option_chain
    except Exception:
        _LOG.exception("index options: analytics unavailable")
        return

    today = datetime.now(IST).date()
    events = event_calendar.events(today, MARKET_HOLIDAYS.get("IN", ()))
    # EXPIRY DAY IS TRADEABLE and is deliberately NOT blocked. Weekly expiry is
    # the highest-volume session for Indian index options — it is where most
    # option traders make their money, not a day to sit out. The IV-crush
    # argument applies to HOLDING a position through an event, not to trading
    # the session, and these are intraday positions squared off before the
    # close.
    #
    # Budget and RBI policy still block: those are scheduled repricings where
    # premium is bid beforehand and collapses after, and no intraday edge
    # survives paying that.
    if events["budget_day"] or (events["days_to_mpc"] is not None
                                and events["days_to_mpc"] <= 1):
        _status[market] = "index options: sitting out a policy/budget event"
        return
    if events["is_expiry_day"] and hm > EXPIRY_LAST_ENTRY:
        # Late on expiry day the remaining premium is almost all decay.
        _status[market] = f"index options: past {EXPIRY_LAST_ENTRY} on expiry day"
        return

    v2 = _rw()
    try:
        # The OPTIONS book, computed only from this lane's own trades — the
        # equity book is never touched and never funds an option.
        budget = float(cfg.get("budget", 100000.0))
        spent = v2.execute(
            "SELECT COALESCE(SUM(entry_price*shares),0) FROM v2_positions"
            " WHERE market=? AND strategy=?", (market, "index_options")).fetchone()[0] or 0.0
        realised = v2.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=? AND strategy=?",
            (market, "index_options")).fetchone()[0] or 0.0
        options_cash = budget - spent + realised
        held = [r[0] for r in v2.execute(
            "SELECT symbol FROM v2_positions WHERE market=? AND strategy=?",
            (market, "index_options"))]
        if len(held) >= cfg.get("max_concurrent", 3):
            return
        halt, reason = _risk_halt(v2, market)
        if halt:
            _status[market] = "index options paused · " + reason
            return

        quotes = _option_live()
        if not quotes:
            _status[market] = "index options: no live option prices"
            return
        # Read ONCE per pass: it scans ~2,400 live quotes, and every index in
        # the loop shares the same market-wide reading.
        try:
            internals = market_internals.read()
        except Exception as exc:
            # {} not None: votes_for treats None as "go and read it", which
            # would retry the failing scan once per index in the loop.
            _LOG.warning("market internals unavailable: %s", exc)
            internals = {}

        # HONOUR THE OPERATOR'S SELECTION. This used to be a hardcoded skip of
        # everything except the first index, so the settings page offered four
        # tick-boxes, persisted all four, and returned a live call for each —
        # and the engine silently dropped three. A control that does nothing is
        # worse than one that is absent.
        #
        # The measured caution stands and is surfaced rather than enforced:
        # Bank Nifty's ATM straddle implies 2.20% against 1.13% realised, so
        # buying it is paying roughly double for the same exposure. That is a
        # reason to warn the operator, not to override a choice they made.
        for symbol in cfg.get("instruments", ()):
            symbol = symbol.upper()
            if symbol not in INDEX_ALLOWED:
                continue
            if len(held) >= cfg.get("max_concurrent", 3):
                break                          # book full: stop, don't churn
            if any(h.startswith(symbol) for h in held):
                continue                       # already positioned in this index
            bars = _index_bars(symbol)
            if len(bars) < 55:
                continue
            o, h, l, c, v = ([float(b[i] or 0) for b in bars] for i in range(1, 6))
            chain, spot = _index_chain(symbol), c[-1]
            put_oi = sum(r["oi"] for r in chain if r["opt_type"] == "PE") or None
            call_oi = sum(r["oi"] for r in chain if r["opt_type"] == "CE") or None
            # LIVE MARKET INTERNALS. The five readings above are all computed
            # from DAILY bars, so an intraday decision was being made on
            # yesterday's candle. Breadth, FII index-futures positioning, the
            # heavyweight contribution and VIX were all already in the database
            # and unused — which is how a CE was bought on a tape that was 32%
            # advancing with FIIs 10:1 short.
            extra = market_internals.votes_for(symbol, internals)
            verdict = index_direction.decide(o, h, l, c, v, put_oi=put_oi,
                                             call_oi=call_oi, extra_votes=extra)
            side = verdict["call"]
            # CLAMPED to what the vote threshold can actually produce.
            # `confidence` is agreeing / number-of-readings, so a MIN_AGREEING=2
            # call cannot exceed 2/n. The persisted setting was 0.60 — correct
            # back when MIN_AGREEING was 3 of 5, left behind when it loosened to
            # 2 — and the lane then refused every call it generated, which reads
            # as "index options are broken" rather than a threshold mismatch.
            # Computed from the ACTUAL reading count, which grows when live
            # internals are available and shrinks when they are not.
            min_conf = min(float(cfg.get("min_confidence", 0.4) or 0.4),
                           index_direction.max_confidence(verdict["n_readings"]))
            if not side or verdict["confidence"] < min_conf:
                continue

            straddle = option_chain.atm_straddle(chain, spot) if chain else None
            # Cap scaled to the contract's remaining life — a 25-day monthly
            # quotes a bigger expected move than a 4-day weekly without being
            # richer. Judging both against a flat 1.5% rejected every monthly.
            straddle_cap = _straddle_limit(cfg, _chain_expiry(symbol), today)
            if straddle and straddle["pct"] > straddle_cap:
                _status[market] = (f"index options: {symbol} premium too rich "
                                   f"({straddle['pct']:.2f}% vs {straddle_cap:.2f}% allowed)")
                continue

            # Give the selector the budget so it picks a strike that FITS,
            # rather than insisting on ATM and standing aside when ATM is dear.
            # Sized off the options book's REMAINING cash, not its starting
            # budget, so losses shrink the next position instead of letting the
            # lane keep betting the original stake.
            # Per-instrument premium cap: the monthly-only indices cannot buy a
            # single lot inside the 10% default, and raising it globally would
            # also quadruple NIFTY's size (nearest AFFORDABLE strike).
            premium_pct = (cfg.get("max_premium_pct_by_index") or {}).get(
                symbol, cfg.get("max_premium_pct", 0.10))
            # RISK CAP, not just a premium cap. The premium cap alone allowed a
            # position worth 30% of the book; with a -35% stop that is 10.5% of
            # the whole book riding on one contract, and max_concurrent=3 means
            # up to 31% at risk at once.
            #
            # Live example that exposed it: FINNIFTY26AUG26700CE, 60 x Rs 452 =
            # Rs 27,120 (27% of the book), stop -35% => Rs 9,492 at risk on ONE
            # trade. It is currently -18% and -Rs 4,926.
            #
            # Cap what can be LOST, then derive the premium allowance from the
            # stop distance. A contract whose smallest lot breaches this is not
            # sized down — it is skipped, because one lot is indivisible.
            stop_pct = float(cfg.get("stop_pct", 0.35)) or 0.35
            risk_cap = budget * float(cfg.get("max_risk_pct", 0.06))
            budget_per_trade = min(options_cash, budget * premium_pct,
                                   risk_cap / stop_pct)
            if budget_per_trade <= 0:
                _status[market] = "index options: book exhausted"
                return
            contract = _pick_contract(symbol, side, spot, quotes,
                                      max_cost=budget_per_trade,
                                      min_vol_share=cfg.get("min_volume_share", 0.05),
                                      today=today, now_hhmm=hm)
            if not contract:
                _status[market] = (f"index options: no liquid {side} strike under "
                                   f"Rs {budget_per_trade:.0f}")
                continue
            premium, lot = contract["price"], contract["lot_size"]
            # SIZE IN LOTS. It always bought exactly ONE, whatever the budget
            # allowed — a Rs 4,397 NIFTY lot against a Rs 10,000 cap left more
            # than half the allowance unused on every trade, which is a large
            # part of why the options book sat a third deployed. Options trade
            # in whole lots, so this is floor division, never fractional.
            lots = max(1, int(budget_per_trade // (premium * lot)))
            shares = lot * lots
            cost = premium * shares
            if cost > budget_per_trade:          # one lot already exceeds the cap
                _status[market] = (f"index options: one {symbol} lot is Rs {cost:.0f}, "
                                   f"over the Rs {budget_per_trade:.0f} cap")
                continue

            # The internals reading is RECORDED with the entry, not just used.
            # It has no history to backtest against, so the only way to find out
            # whether it helps is to store what it said at the moment of each
            # trade and read it back later (scripts/experiment_report.py).
            why = json.dumps(dict(setup="index_options", side=side, symbol=symbol,
                                  confidence=verdict["confidence"],
                                  n_readings=verdict["n_readings"],
                                  reasons=verdict["reasons"],
                                  internals={k: vote for k, (vote, _r) in extra.items()},
                                  straddle_pct=straddle["pct"] if straddle else None,
                                  straddle_cap=round(straddle_cap, 2),
                                  days_to_expiry=events["days_to_expiry"],
                                  experiment="internals_in_index_call",
                                  unvalidated=True))
            stop = premium * (1 - cfg.get("stop_pct", 0.35))
            # Target scaled to the contract's remaining life, so it is a level
            # this option can realistically print rather than a flat number that
            # happens to suit a weekly.
            tgt_pct = _target_pct(cfg, contract.get("expiry"), today,
                                  contract.get("symbol") or symbol)
            target = premium * (1 + tgt_pct)
            # expiry is PERSISTED, not left to be read off a quote later: once
            # the contract expires it leaves the ATM watch list, and a position
            # whose expiry is only knowable from a quote that no longer arrives
            # can never be closed.
            if not record_entry(v2, market, "index_options", contract["symbol"],
                                today.isoformat(), premium, shares, stop, target, 0.0,
                                verdict["confidence"], why,
                                expiry=contract.get("expiry")):
                continue
            v2.commit()
            _LOG.info("INDEX OPTION BUY %s %s %dx%.0f=%.0f @ %.2f (cost Rs %.0f, "
                      "target +%.0f%%, conf %.2f)",
                      contract["symbol"], side, lots, lot, shares, premium, cost,
                      tgt_pct * 100, verdict["confidence"])
            _status[market] = f"index options: bought {contract['symbol']} @ {premium:.2f}"
            # KEEP FILLING. This used to `return` after one buy, so with a 60s
            # cadence and an entry window that shuts at 14:30 the remaining
            # slots often never filled — on 2026-07-31 the book sat at 35%
            # deployed with two of three slots used and Rs 65k idle. The loop
            # already re-checks max_concurrent and skips an index we hold, and
            # the cash is decremented here, so continuing cannot over-commit.
            held.append(contract["symbol"])
            options_cash -= cost
    finally:
        v2.close()


def _index_bars(symbol, limit=60):
    """Daily OHLCV for an index from its near-month future."""
    try:
        path = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM ("
            "  SELECT date, open, high, low, close, volume,"
            "         ROW_NUMBER() OVER (PARTITION BY date ORDER BY expiry) rn"
            "    FROM fo_bhav WHERE symbol=? AND instrument='FUT' AND close>0"
            ") WHERE rn=1 ORDER BY date DESC LIMIT ?", (symbol, limit)).fetchall()
        con.close()
        return list(reversed(rows))
    except Exception:
        return []


def _index_chain(symbol):
    """Nearest-expiry chain rows for the chain analytics."""
    try:
        path = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
        day = con.execute("SELECT MAX(date) FROM fo_bhav WHERE symbol=?", (symbol,)).fetchone()[0]
        rows = con.execute(
            "SELECT strike, opt_type, close, oi, volume FROM fo_bhav"
            " WHERE symbol=? AND date=? AND instrument='OPT' AND expiry=("
            "   SELECT MIN(expiry) FROM fo_bhav WHERE symbol=? AND date=? AND expiry>=date)",
            (symbol, day, symbol, day)).fetchall()
        con.close()
        return [dict(strike=r[0], opt_type=r[1], close=r[2], oi=r[3], volume=r[4])
                for r in rows]
    except Exception:
        return []


def _chain_expiry(symbol):
    """Expiry date of the nearest-expiry chain, as an ISO string or None.

    Needed because the straddle cap has to know the HORIZON it is judging. See
    _straddle_limit.
    """
    try:
        path = os.environ.get("FO_DB", "/opt/opentrade/var/fo.db")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
        day = con.execute("SELECT MAX(date) FROM fo_bhav WHERE symbol=?", (symbol,)).fetchone()[0]
        row = con.execute("SELECT MIN(expiry) FROM fo_bhav WHERE symbol=? AND date=?"
                          " AND expiry>=date", (symbol, day)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _straddle_limit(cfg, expiry, today):
    """The straddle cap, scaled to the contract's remaining life.

    An implied move grows with the SQUARE ROOT of time, so a 25-day option
    naturally quotes a bigger expected move than a 4-day one without being any
    richer. max_straddle_pct was calibrated on NIFTY weeklies; applied raw to a
    monthly it rejects the contract for being longer-dated.

    Measured on the live chain, 2026-07-31 — raw straddle, and the same number
    divided by the square root of days to expiry:

        NIFTY       4d   1.07%   ->  0.54 %/sqrt-day
        BANKNIFTY  25d   2.88%   ->  0.58
        FINNIFTY   25d   3.04%   ->  0.61
        MIDCPNIFTY 25d   3.10%   ->  0.62

    Nearly identical once normalised: the spread was almost entirely time, not
    price. Against a flat 1.5% the three monthlies were all rejected; against a
    scaled cap they are judged on the same footing as NIFTY.
    """
    base = float(cfg.get("max_straddle_pct", 1.5))
    base_dte = max(1, int(cfg.get("straddle_base_dte", 4)))
    if not expiry:
        return base                       # unknown horizon -> the strict cap
    try:
        dte = (date.fromisoformat(str(expiry)[:10]) - today).days
    except (TypeError, ValueError):
        return base
    if dte <= 0:
        return base
    return base * (dte / base_dte) ** 0.5


def _pick_contract(symbol, side, spot, quotes, max_cost=None, min_vol_share=None,
                   today=None, now_hhmm=None):
    """The closest-to-the-money contract whose ONE LOT fits `max_cost`.

    Not simply ATM. Demanding at-the-money and refusing everything else is how
    this lane concluded a Rs 1L book "cannot trade options" — at ~Rs 22,000 a
    lot, ATM is 22% of the book, while a strike a little out of the money runs
    Rs 2,000-5,000, which is an ordinary 2-5% position. Real traders take the
    strike that fits; they do not stand aside because the ATM is dear.

    Preference order is therefore: affordable first, then nearest the money.
    Further out is cheaper but expires worthless more often, so the nearest
    affordable strike is the right trade-off rather than the cheapest one.

    Strike and side come from the STORED COLUMNS, never from parsing the ticker.
    A symbol like NIFTY2680424000CE runs the expiry code straight into the
    strike, and a digits-from-the-right parse yields 2,680,424,000 — not
    obviously wrong, so it would have silently chosen a nonsense contract.
    """
    want = "CE" if side == "CE" else "PE"
    mine = [(sym, q) for sym, q in quotes.items()
            if (q.get("underlying") or "").upper() == symbol.upper()
            and (q.get("option_type") or "").upper() == want]
    # NEVER ENTER WHAT THE EXIT WOULD IMMEDIATELY CLOSE. This is the same
    # function the exit path uses, deliberately, so the two sides cannot drift
    # apart again — an entry rule that merely *resembles* the exit rule is how
    # this bug came back.
    #
    # It came back because the first fix was aimed one door down. Making the
    # exit time-aware killed the 0-DTE loop (buy a contract expiring today,
    # exit closes it seconds later). It did nothing for a contract that expired
    # DAYS ago and is still in the feed: nfo_quotes is never pruned, so 40 rows
    # for the 2026-08-04 expiry sat there frozen at their last traded price and
    # kept passing every gate here — underlying, type, strike, price, lot and
    # volume all look perfectly healthy on a dead contract.
    #
    # On 2026-08-07 that ran EIGHTY-THREE round trips of NIFTY2680424650CE,
    # expired three days earlier, for -Rs 7,721. Every cycle was 8 seconds long
    # and moved 0.80 -> 0.80: no view, no price change, pure transaction cost.
    if today is not None:
        mine = [(sym, q) for sym, q in mine
                if not _expired_or_expiring(q.get("expiry"), today, now_hhmm)]
    # LIQUIDITY, judged against this index's own chain rather than an absolute
    # floor. Volume accumulates through the session, so a fixed number would
    # refuse every trade in the morning and accept anything by the afternoon;
    # a share of the most active strike is the same test at 09:20 and 14:20.
    #
    # It matters more than it looks. Measured live, median option volume by
    # index: NIFTY 60,284,250 · BANKNIFTY 696,180 · MIDCPNIFTY 106,200 ·
    # FINNIFTY 3,360 with a minimum of ZERO. There was no liquidity test at
    # all, and the lane put 28% of the options book into a FINNIFTY contract on
    # that basis. A paper fill in a contract nobody trades is fiction.
    busiest = max((_f(q.get("vol"), 0.0) for _s, q in mine), default=0.0)
    floor = busiest * (min_vol_share if min_vol_share is not None else 0.0)
    affordable = []
    for sym, q in mine:
        strike = _f(q.get("strike"), 0.0)
        price, lot = _f(q.get("price"), 0.0), _f(q.get("lot_size"), 0.0)
        if strike <= 0 or price <= 0 or lot <= 0:
            continue
        if floor > 0 and _f(q.get("vol"), 0.0) < floor:
            continue                            # too thin to treat a fill as real
        if max_cost is None or price * lot <= max_cost:
            affordable.append((abs(strike - spot), dict(q, symbol=sym, cost=price * lot)))
    if not affordable:
        return None
    return min(affordable, key=lambda r: r[0])[1]


def sector_watch_pass(market):
    """WATCH-ONLY (no auto-trade — unvalidated): surface two patterns the engine
    otherwise ignores, as Telegram radar alerts —
      1. sector co-movement: >= 2 liquid names in one sector up >= 3% on volume
         (e.g. HEG + Graphite India; TVS + Bajaj) -> the theme + its laggards.
      2. NSE 'Spurt in Volume' notices -> names the exchange itself flagged.
    Deduped once per sector / once per symbol per day."""
    if market != "IN":
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    if not ("09:30" <= hm <= "15:00"):
        return
    live = _live(market)
    if not live:
        return
    prevc = _prev_close(market)
    tails, _mdf = _hist(market)
    av = _intra_avgvol(market, tails)
    smap = _sector_map(market)
    stale = _stale_symbols(market)
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    frac = _vol_frac(mins)
    today = now.date().isoformat()
    for d in list(_SECTOR_ALERTED):          # prune old days
        if d != today:
            del _SECTOR_ALERTED[d]
    alerted = _SECTOR_ALERTED.setdefault(today, set())
    # group liquid movers by sector
    by_sector = {}
    for sym, lq in live.items():
        if sym in ETF_EXCLUDE or sym in stale or sym not in prevc or sym not in av:
            continue
        if av[sym] * lq["price"] < 1.5e8:    # >= Rs.15cr turnover
            continue
        move = lq["price"] / prevc[sym] - 1
        if move < 0.03:
            continue
        rvol = (lq.get("vol") or 0.0) / (av[sym] * frac)
        if rvol < 2.0:
            continue
        sec = smap.get(sym, "unknown")
        if sec in ("unknown", "NSE Listed Equity", ""):
            continue
        by_sector.setdefault(sec, []).append((move, sym))
    for sec, movers in by_sector.items():
        key = "sec:" + sec
        if len(movers) < 2 or key in alerted:
            continue
        alerted.add(key)
        movers.sort(reverse=True)
        items = [{"symbol": s, "note": "%s +%.1f%% (sector on the move)" % (sec, m * 100)}
                 for m, s in movers[:4]]
        try:
            from . import telegram_bot
            telegram_bot.notify_radar(items, market)
        except Exception:
            pass
    # NSE 'Spurt in Volume' notices (exchange-flagged movers) -> radar
    try:
        con = _ro(CATALYST_DB)
        cutoff = int(time.time()) - 6 * 3600
        spurts = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM nse_announcements WHERE an_epoch>=? "
            "AND (lower(subject) LIKE '%spurt%' OR lower(text) LIKE '%spurt in volume%')", (cutoff,))]
        con.close()
        fresh = [s for s in spurts if ("spurt:" + s) not in alerted and s in live]
        for s in fresh:
            alerted.add("spurt:" + s)
        if fresh:
            from . import telegram_bot
            telegram_bot.notify_radar([{"symbol": s, "note": "NSE flagged a volume spurt"} for s in fresh[:5]], market)
    except Exception:
        pass


HOLD_DAYS = {"swing_meanrev": 8, "gap_momentum": 20, "mom_breakout": 40, "intraday_news": 1,
             "volume_surge": 1,
             # MEASURED on 405 sessions / 229,229 affordable index-option entries.
             # Same entries, same -35%/+60% exits, only the hold varies:
             #   1 day -5.2% | 2 days -7.3% | 3 days -8.3% | 5 days -8.9% | 10 days -9.2%
             # Roughly a percent of premium per day, which is theta doing exactly
             # what theta does. It used to fall through to the default of 10.
             "index_options": 1}
# Index options are squared off intraday, like the equity intraday lanes, but
# with their OWN time and WITHOUT inheriting INTRA's +1.5% breakeven lock — that
# lock is calibrated for a stock, and 1.5% on an option premium is noise.
INDEX_OPT_SQUAREOFF = "15:12"


# Per-index target scaling, measured on the live book.
#
# NIFTY moves far less in premium-percentage terms than the bank indices, and
# the flat DTE ladder never accounted for it. 21 NIFTY option trades: average
# -0.1%, net -Rs 865, and NOT ONE reached its target — every one rode to expiry
# and decayed. The best NIFTY trade ever managed +24.1% against a 40-45%
# target. The 4 BANKNIFTY trades cleared theirs comfortably (+61.7%, +43.7%).
#
# A target that cannot be reached is not a target — it is a guarantee of
# holding to expiry, which is precisely what the exit study says destroys
# option premium.
# 0.48 is set so the 0-2 DTE NIFTY target lands at 21.6%, just UNDER the best
# NIFTY trade the book has ever printed (+24.1%). A target above the observed
# maximum has a zero hit rate by construction.
TARGET_INDEX_SCALE = {"NIFTY": 0.48, "BANKNIFTY": 1.0, "FINNIFTY": 0.8,
                      "MIDCPNIFTY": 0.8}


def _index_of(symbol):
    """Longest-prefix match: MIDCPNIFTY and BANKNIFTY both end in NIFTY."""
    s = str(symbol or "").upper()
    best = ""
    for name in TARGET_INDEX_SCALE:
        if s.startswith(name) and len(name) > len(best):
            best = name
    return best or None


def _target_pct(cfg, expiry, today, symbol=None):
    """Target as a fraction of premium, for THIS contract's remaining life.

    See INDEX_OPTIONS["target_by_dte"] for the DTE measurement: premium
    percentage moves shrink as time to expiry grows, so one flat number cannot
    be right for both a 4-day weekly and a 25-day monthly.

    Then scaled per INDEX — see TARGET_INDEX_SCALE. A NIFTY target set off
    BANKNIFTY's behaviour is unreachable, and an unreachable target means the
    position always rides to expiry.
    """
    table = cfg.get("target_by_dte")
    fallback = float(cfg.get("target_pct", 0.60))
    pct = fallback
    if table and expiry:
        try:
            dte = (date.fromisoformat(str(expiry)[:10]) - today).days
        except (TypeError, ValueError):
            dte = None
        if dte is not None:
            for limit, p in table:
                if dte <= limit:
                    pct = float(p)
                    break
    idx = _index_of(symbol)
    if idx:
        pct *= TARGET_INDEX_SCALE.get(idx, 1.0)
    return pct


def _expired_or_expiring(expiry, today, now_hhmm=None):
    """True once the contract must be closed.

    ON EXPIRY DAY THIS IS TIME-AWARE, and that is the whole point. It used to
    return True for the entire expiry session, while index_options_pass happily
    opened new positions until EXPIRY_LAST_ENTRY. The two rules fought each
    other on the 8-second exit cadence: buy a 0-DTE contract, have the expiry
    rule close it seconds later, re-enter the same strike, repeat.

    On 2026-08-04 that loop ran NINETEEN round trips of NIFTY2680424450PE in one
    session for -Rs 2,610 — pure churn, no view, every cycle paying the spread
    and costs. Nineteen of the book's twenty-six option trades were this one
    contract going nowhere.

    So: a contract past its expiry date always closes. One expiring TODAY closes
    at the square-off time, and until then is governed by the normal stop and
    target like any other intraday position.
    """
    try:
        exp = date.fromisoformat(str(expiry)[:10])
    except (TypeError, ValueError):
        return False
    if exp < today:
        return True
    if exp > today:
        return False
    # expiry day: hold until square-off, then flatten
    return str(now_hhmm or "99:99") >= INDEX_OPT_SQUAREOFF


def evaluate_exit(p, lq, sess_row, today, today_s, market, now_hhmm=None):
    """Decide whether one open position exits, and at what price.

    Extracted VERBATIM from exit_monitor's loop so the exit rules can be tested
    without a live book. Same comparisons, same ordering, same precedence — the
    caller keeps every side effect (trade insert, position delete, Telegram).

    Returns (peak, eff_stop, exit_price, reason); exit_price and reason are None
    when the position is held. `now_hhmm` is injectable for testing the intraday
    square-off; production omits it.

    Precedence matters and is deliberate: stop is checked BEFORE the btst
    next-open exit, so a bad overnight down-gap is booked as a stop rather than
    a gap capture.
    """
    # WHICH EXTREMES ARE LEGITIMATE for this position, decided once and applied
    # to both the lock arming (peak) and the exit test (lo_ref/hi_ref). It used
    # to be decided only for the exit test, so `peak` still swept in the day
    # high and the session high — arming a breakeven lock off a price the trade
    # never saw, then exiting against a low it never saw either.
    same_day = str(p.get("edate"))[:10] == today_s
    use_live = p["strategy"] in MIDSESSION_STRATS and same_day
    # `p["peak"]` starts at the entry price and is persisted, so it already
    # carries every price observed SINCE entry — on a mid-session entry day that
    # is the only honest high available.
    peak = max(p["peak"], lq["price"])
    if not use_live:
        peak = max(peak, lq["high"], sess_row[1] if sess_row else 0.0)
    eff = p["stop"]
    if p["trail"]:
        eff = max(eff, peak * (1 - p["trail"]))
    # breakeven lock on a big winner only (recover ATR from the initial stop)
    atr_stop = PLAN.get(p["strategy"], {}).get("atr_stop", 2.0)
    atr_est = (p["entry"] - p["stop"]) / atr_stop if atr_stop else 0.0
    be = breakeven_price(market, p["entry"], p.get("shares") or 1.0, p["strategy"])
    if BE_TRIGGER_ATR and atr_est > 0 and peak >= p["entry"] + BE_TRIGGER_ATR * atr_est:
        eff = max(eff, be)
    # intraday lanes: once up >= lock%, stop rises to breakeven — a green
    # trade is never allowed to go red (user spec, matches the backtest)
    if p["strategy"] in INTRADAY_STRATS and peak >= p["entry"] * (1 + INTRA["lock"]):
        eff = max(eff, be)
    # TRADING days, not calendar days — the backtest validated an 8
    # trading-bar hold; calendar counting was force-selling ~2 sessions
    # early around weekends, truncating the bounce.
    held = trading_days_held(p["edate"], today, market)
    # IN gap names spike then mean-revert -> take profit at a fixed target
    # (validated: cuts give-back, holds the edge). US gap trends -> trail only.
    # Computed dynamically so it also protects positions opened before this rule.
    eff_tgt = p["target"] or 0.0
    if p["strategy"] == "gap_momentum" and GAP_TARGET.get(market):
        eff_tgt = max(eff_tgt, p["entry"] * (1 + GAP_TARGET[market]))
    ex = reason = None
    # swing/gap/momentum enter at the OPEN, so their day low/high are valid from
    # the first tick and `use_live` is False for them. Everything in
    # MIDSESSION_STRATS gets the LIVE price as its only reference on the entry
    # day; on any later held day the day extremes are legitimate again.
    lo_ref = lq["price"] if use_live else lq["low"]
    hi_ref = lq["price"] if use_live else lq["high"]
    if lo_ref <= eff or lq["price"] <= eff:
        ex, reason = min(eff, lq["price"]), ("trail" if p["trail"] and eff > p["stop"] else "stop")
    elif eff_tgt and (hi_ref >= eff_tgt or lq["price"] >= eff_tgt):
        ex, reason = max(eff_tgt, lq["price"]), "target"
    # AN OPTION MUST NOT BE CARRIED INTO ITS EXPIRY. Nothing closed one: there
    # is no expiry rule anywhere in this path, and HOLD_DAYS fell through to 10.
    # The NIFTY CE bought 2026-07-31 expires 08-04 while its time-exit was not
    # due until ~08-14. After expiry the contract drops out of the ATM watch
    # list, so `lq` is either absent — and the loop does `if not lq: continue` —
    # or frozen, and the frozen guard skips it too. Either way the position is
    # never closed and marks at a stale price forever.
    elif _expired_or_expiring(p.get("expiry") or lq.get("expiry"), today,
                              now_hhmm or datetime.now(IST).strftime("%H:%M")):
        ex, reason = lq["price"], "expiry"
    # Index options square off intraday on their own clock (see HOLD_DAYS).
    elif p["strategy"] == "index_options" and (
            now_hhmm or datetime.now(IST).strftime("%H:%M")) >= INDEX_OPT_SQUAREOFF:
        ex, reason = lq["price"], "eod"
    elif p["strategy"] == "btst" and not same_day:
        # BTST: sell the morning after entry. exit_monitor first runs at the
        # 09:15 open, so this fills at ~the open — the overnight gap is the whole
        # edge; holding into the day gives it back (validated). Stop above still
        # protects a bad down-gap (checked first, off the live open price).
        ex, reason = lq["price"], "btst"
    elif p["strategy"] in INTRADAY_STRATS and (now_hhmm or datetime.now(IST).strftime("%H:%M")) >= INTRA["squareoff"]:
        ex, reason = lq["price"], "eod"           # intraday lanes are NEVER held overnight
    elif held >= HOLD_DAYS.get(p["strategy"], 10):
        ex, reason = lq["price"], "time"
    return peak, eff, ex, reason


def exit_monitor(market):
    """Cheap, fast exit pass: checks open positions against LIVE price/high/low
    for stop / target / trailing / time exit. No feature recompute, so it can
    run every few seconds for near-instant exits."""
    from datetime import date
    v2 = _rw()
    row = v2.execute("SELECT budget FROM v2_book WHERE market=?", (market,)).fetchone()
    if not row:
        v2.close(); return
    budget = row[0]
    cside = COST_SIDE[market]
    today = datetime.now(IST).date()
    today_s = today.isoformat()
    positions = {}
    try:
        rows = v2.execute("SELECT id,strategy,symbol,entry_price,shares,stop,target,trail,peak,"
                          "entry_date,expiry FROM v2_positions WHERE market=?", (market,)).fetchall()
    except Exception:                            # expiry column not migrated yet
        rows = [(*r, None) for r in v2.execute(
            "SELECT id,strategy,symbol,entry_price,shares,stop,target,trail,peak,entry_date "
            "FROM v2_positions WHERE market=?", (market,))]
    for r in rows:
        positions[r[2]] = dict(id=r[0], strategy=r[1], entry=r[3], shares=r[4], stop=r[5],
                               target=r[6], trail=r[7], peak=r[8], edate=r[9], expiry=r[10])
    if not positions:                            # nothing held -> nothing to monitor,
        # but still write a heartbeat snapshot (throttled) so the health check knows
        # the engine is ALIVE on an empty/fresh book (equity = cash = budget+realized).
        if time.time() - _EQ_SNAP.get(market, 0) >= 60:
            _EQ_SNAP[market] = time.time()
            realized = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
            eqv = budget + realized
            try:
                v2.execute("INSERT OR REPLACE INTO v2_equity(market,date,equity,cash,positions_value,n_positions) VALUES(?,?,?,?,?,?)",
                           (market, "LIVE_" + datetime.now(timezone.utc).isoformat()[:19], eqv, eqv, 0.0, 0))
                v2.commit()
            except Exception:
                pass
        v2.close(); return
    live = _live(market, positions.keys())       # only held symbols (cheap, not all 10k)
    # Option contracts are NOT in latest_quotes — they are deliberately kept in
    # nfo_quotes so they cannot leak into the equity lanes' universe. Without
    # this merge exit_monitor would find no price for an option position and
    # hold it forever: the engine could open an option trade and never close
    # one, which is worse than not trading it at all.
    live.update(_option_live(positions.keys()))
    sess = _session_opens(market, live)          # session high ratchets US trails between samples
    frozen = _stale_symbols(market)              # don't mark/exit off a stale (frozen) quote
    realised = v2.execute("SELECT COALESCE(SUM(pnl),0) FROM v2_trades WHERE market=?", (market,)).fetchone()[0] or 0.0
    cash = budget - sum(p["shares"] * p["entry"] for p in positions.values()) + realised
    exits = 0
    for sym, p in list(positions.items()):
        lq = live.get(sym)
        if not lq:
            continue
        # Frozen quote (e.g. GUJGASLTD): a stale HIGH would phantom-hit a stop
        # or target. An EXPIRED contract is the one exception — its quote stops
        # updating precisely because the contract is gone, so refusing to act on
        # a stale price would strand the position permanently. The last price we
        # saw is the only price that will ever exist for it.
        # NB: "23:59" deliberately, not the wall clock — a frozen quote on a
        # contract expiring today means the contract is already gone from the
        # feed, so this guard must stay all-day even though the EXIT rule is
        # now time-aware. Passing the real time here would strand it.
        if sym in frozen and not _expired_or_expiring(
                p.get("expiry") or lq.get("expiry"), today, "23:59"):
            continue
        # BACKFILL the expiry while the contract is still quoted. Positions
        # opened before the column existed carry None, and once a contract
        # expires it leaves the watch list — so the last chance to learn its
        # expiry is now, while a quote still arrives. Written once per position.
        if lq.get("expiry") and not p.get("expiry"):
            p["expiry"] = lq["expiry"]
            try:
                v2.execute("UPDATE v2_positions SET expiry=? WHERE id=?", (lq["expiry"], p["id"]))
            except Exception:
                pass
        peak, eff, ex, reason = evaluate_exit(p, lq, sess.get(sym), today, today_s, market)
        if ex is not None:
            cash += p["shares"] * ex * (1 - cside)
            # ONE writer, which computes the costs itself — see record_exit.
            # Its return feeds the log below, so the number recorded and the
            # number logged cannot drift apart.
            net, net_pct = record_exit(v2, market, p["id"], today_s, ex,
                                       p["shares"], reason)
            # Log the FULL decision, not just the outcome. On 2026-07-29
            # HINDUNILVR exited at its entry price with reason "stop" while its
            # day high was only +0.05% above entry — neither breakeven trigger
            # (+1.5% or +3 ATR) could have armed, and the -1.75% stop was 36
            # points away. Without the inputs there is no way to tell whether a
            # bad tick latched `peak` (which is persisted, so one spurious high
            # arms the lock permanently) or the rule itself is wrong. These
            # fields make the next occurrence answerable instead of arguable.
            _LOG.info("EXIT %s %s %s entry=%.2f exit=%.2f reason=%s | stop=%.2f "
                      "eff=%.2f peak=%.2f live=%.2f high=%.2f low=%.2f net=%.2f%%",
                      market, p["strategy"], sym, p["entry"], ex, reason,
                      p["stop"], eff, peak, lq["price"], lq.get("high") or 0.0,
                      lq.get("low") or 0.0, net_pct)
            v2.execute("DELETE FROM v2_positions WHERE id=?", (p["id"],))
            try:
                from . import telegram_bot
                telegram_bot.notify_trade("SELL", sym, (int(p["shares"]) if float(p["shares"]).is_integer() else round(p["shares"], 2)),
                                          round(ex, 2), market, pnl_pct=(ex / p["entry"] - 1) * 100, strategy=p["strategy"])
            except Exception:
                pass
            del positions[sym]; exits += 1
        else:
            v2.execute("UPDATE v2_positions SET peak=? WHERE id=?", (peak, p["id"]))
    pv = sum(p["shares"] * (live[s]["price"] if s in live else p["entry"]) for s, p in positions.items())
    # snapshot at most once/min (was every 8s -> 57k rows bloating every query)
    if time.time() - _EQ_SNAP.get(market, 0) >= 60:
        _EQ_SNAP[market] = time.time()
        v2.execute("INSERT OR REPLACE INTO v2_equity(market,date,equity,cash,positions_value,n_positions) VALUES(?,?,?,?,?,?)",
                   (market, "LIVE_" + datetime.now(timezone.utc).isoformat()[:19], cash + pv, cash, pv, len(positions)))
    v2.commit(); v2.close()
    if exits:
        _status[market] = (_status.get(market, "") + f" · -{exits} exit").strip(" ·")


def _equity_janitor(market):
    """At session close: persist ONE compact daily equity row, then prune LIVE_
    minute snapshots older than 7 days. Keeps v2_equity bounded (~375 rows/day
    would otherwise grow forever) while the hero chart keeps a clean daily
    history and a full week of intraday detail."""
    try:
        v2 = _rw()
        row = v2.execute("SELECT equity,cash,positions_value,n_positions FROM v2_equity "
                         "WHERE market=? AND date LIKE 'LIVE_%' ORDER BY date DESC LIMIT 1",
                         (market,)).fetchone()
        if row:
            ds = datetime.now(IST).date().isoformat()
            v2.execute("INSERT OR REPLACE INTO v2_equity(market,date,equity,cash,positions_value,n_positions)"
                       " VALUES(?,?,?,?,?,?)", (market, ds, row[0], row[1], row[2], row[3]))
        cutoff = "LIVE_" + (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()[:19]
        v2.execute("DELETE FROM v2_equity WHERE market=? AND date LIKE 'LIVE_%' AND date < ?",
                   (market, cutoff))
        v2.commit(); v2.close()
    except Exception:
        pass


_last_signal: dict = {}
_last_vs: dict = {}          # volume_surge throttle
_last_sw: dict = {}          # sector_watch throttle
_last_btst: dict = {}        # btst throttle
_last_preopen: dict = {}     # pre-open warm-up throttle
_last_idx: dict = {}         # index-options throttle
INDEX_OPT_INTERVAL = 60      # one directional bet; no need to scan faster
_last_ispot: dict = {}       # index 5m-candle sampler throttle
INDEX_SPOT_INTERVAL = 30     # a 5-minute bar needs ~10 samples, not one per 8s cycle
EXPIRY_LAST_ENTRY = "13:30"  # on expiry day, later than this is buying pure decay
_preopen_tried: dict = {}    # one restart-recovery attempt per session
VOLSURGE_INTERVAL = 10      # 10s (was 20s): operator wants faster entry on surges
INTRAMOM_INTERVAL = 30      # narrow entry window; no need to scan more often
_last_im: dict = {}       # scan for intraday movers every 20s (not every 8s cycle)
BTST_INTERVAL = 60           # btst only fires in the 15:05-15:25 window; check once/min
SECTOR_WATCH_INTERVAL = 180  # watch-only radar every 3min
SIGNAL_INTERVAL = 300   # heavy signal recompute cadence (s) — daily signals barely
                        # change intraday, so 5min keeps the GIL-heavy panel/feature
                        # compute from starving the web event loop (exits still run every cycle)


_EOD_SNAP: dict = {}         # market -> day the close snapshot was written


def _today_key():
    return datetime.now(IST).date().isoformat()


_WATCHDOG_DONE: dict = {}


def _market_open_watchdog(market):
    """Once, ~5-20 min after the 09:15 IST open, verify the system is actually
    working (feed fresh, entry window armed, catalysts loaded) and TELEGRAM-alert
    if anything is broken — so outages surface automatically instead of the user
    finding them. Wall-clock gated (not on _SESSION_OPENED_AT, since a mid-session
    restart leaves that empty — which is itself something to flag)."""
    if market != "IN":
        return
    now = datetime.now(IST)
    hm = now.strftime("%H:%M")
    if not ("09:20" <= hm <= "09:35") or not market_open(market):
        return
    today = now.date().isoformat()
    if _WATCHDOG_DONE.get(market) == today:
        return
    _WATCHDOG_DONE[market] = today
    problems = []
    try:
        con = _ro(MAIN_DB)
        age = con.execute("SELECT (julianday('now')-julianday(MAX(ts)))*86400 FROM latest_quotes WHERE source=?",
                          (LIVE_SOURCE[market],)).fetchone()[0]
        con.close()
        if age is None or age > 180:
            problems.append("live price feed is STALE (%ss old)" % int(age or -1))
    except Exception:
        problems.append("price feed unreadable")
    if not _SESSION_OPENED_AT.get(market):
        problems.append("entry window did NOT arm (daily lane won't buy today — engine likely restarted mid-session)")
    try:
        if not _nse_catalyst_symbols():
            problems.append("no NSE catalysts loaded (volume_surge lane blind)")
    except Exception:
        pass
    try:
        from . import telegram_bot
        if problems:
            telegram_bot.notify_summary("⚠️ <b>OpenStocks · morning check FAILED</b>\n"
                                        + "\n".join("• " + p for p in problems)
                                        + "\n\nSome auto-trading may not work today.")
        else:
            telegram_bot.notify_summary("✅ <b>OpenStocks · morning check OK</b>\n"
                                        "Feed live, engine armed, catalysts loaded. Hunting for setups.")
    except Exception:
        pass
    _status[market] = ("watchdog: " + ("; ".join(problems) if problems else "all systems go")) + " · " + _status.get(market, "")


def loop(interval):
    try:
        v2 = _rw(); ensure_schema(v2); v2.close()
    except Exception:
        pass
    prev_open = {m: None for m in ENABLED_MARKETS}   # None = unknown (fresh start);
                                           # arm the entry window ONLY on a real
                                           # closed->open flip, never on a restart
    while True:
        for m in ENABLED_MARKETS:
            try:
                is_open = market_open(m)
                if is_open and prev_open[m] is False:
                    _SESSION_OPENED_AT[m] = time.time()          # session just opened
                elif is_open and prev_open[m] is None and m not in _SESSION_OPENED_AT:
                    # fresh process start while the market is ALREADY open (a
                    # mid-session restart): arm from the REAL session-open time,
                    # not the restart time — so the 30-min daily entry window is
                    # honored correctly (still open if we restarted near the open,
                    # correctly closed if we restarted later) instead of a restart
                    # silently disarming the daily lanes for the whole day.
                    try:
                        ot = market_regions.market_session_for_region(m).get("open_time")
                        _SESSION_OPENED_AT[m] = datetime.fromisoformat(ot).timestamp()
                    except Exception:
                        _SESSION_OPENED_AT[m] = time.time()
                if prev_open[m] is True and not is_open:
                    _tg_daily_summary(m)                         # session just closed -> daily digest
                    _equity_janitor(m)                           # compact daily row + prune old minute rows
                    if m == "IN":
                        _bars5m_janitor()                        # flush last bar, prune, monthly VACUUM
                        _nse_reference_refresh()                 # delivery / FII-DII / bulk deals / sectors
                        _fo_refresh()                            # index F&O bhavcopy (OI, chains)
                        try:                                     # index candles: bounded retention
                            from . import index_spot
                            index_spot.prune()
                        except Exception:
                            _LOG.exception("index bar prune failed")
                prev_open[m] = is_open
                if is_open:
                    # Record 5-min bars for the Nifty 500 off the quote feed we
                    # are already reading. Cheap: dict updates each cycle, one
                    # 500-row write every 5 minutes. Isolated so a recorder
                    # fault can never stop the engine trading.
                    if m == "IN":
                        try:
                            from . import bars5m
                            written = bars5m.observe(_live(m))
                            if written:
                                _LOG.info("5m bars written: %d", written)
                        except Exception:
                            _LOG.exception("5m bar record failed")
                    # Restart recovery. A deploy at 09:33 IST on 2026-07-29 left
                    # the whole day with no auction data, because the fetch only
                    # ran 09:05-09:15 and the cache was memory-only. NSE serves
                    # the morning auction all day, so a process that missed the
                    # window can still recover it. Attempted once per session.
                    if m == "IN" and not _preopen_tried.get(m):
                        _preopen_tried[m] = True
                        _refresh_preopen()
                    # Intraday INDEX candles. The index has no live quote
                    # anywhere — latest_quotes is equities, nfo_quotes is
                    # contracts — so everything read yesterday's bhavcopy close
                    # and the dashboard had no series to draw. Derived from the
                    # option quotes already polled (put-call parity), so this
                    # costs no API quota. Throttled: a 5-minute bar does not
                    # need an 8-second sampler.
                    if m == "IN" and time.time() - _last_ispot.get(m, 0) >= INDEX_SPOT_INTERVAL:
                        _last_ispot[m] = time.time()
                        try:
                            from . import index_spot
                            index_spot.observe(INDEX_ALLOWED)
                        except Exception:
                            _LOG.exception("index spot sample failed")
                    exit_monitor(m)                              # fast exits every cycle (held symbols only — cheap)
                    _market_open_watchdog(m)                     # once ~09:20 IST: alert if feed/engine/catalysts broken
                    # These scan the WHOLE universe (heavy: full latest_quotes read),
                    # so they must NOT run every 8s — that pegged the CPU and starved
                    # the web app. Throttled: movers caught within 20s, radar every 3min.
                    if time.time() - _last_vs.get(m, 0) >= VOLSURGE_INTERVAL:
                        volume_surge_pass(m)                     # day-1 mover catcher (NSE catalyst + volume)
                        _last_vs[m] = time.time()
                    if time.time() - _last_im.get(m, 0) >= INTRAMOM_INTERVAL:
                        intraday_momentum_pass(m)
                        _last_im[m] = time.time()
                    if time.time() - _last_btst.get(m, 0) >= BTST_INTERVAL:
                        btst_pass(m)                             # buy-today-sell-tomorrow (near close, catalyst gap)
                        _last_btst[m] = time.time()
                    if time.time() - _last_idx.get(m, 0) >= INDEX_OPT_INTERVAL:
                        index_options_pass(m)                    # index CE/PE (auto_trade gated)
                        _last_idx[m] = time.time()
                    if time.time() - _last_sw.get(m, 0) >= SECTOR_WATCH_INTERVAL:
                        sector_watch_pass(m)                     # watch-only radar (sector co-move + NSE spurt)
                        _last_sw[m] = time.time()
                    # Price alerts. These used to be evaluated ONLY inside the
                    # SSE payload builder, so they fired only while a browser
                    # had the dashboard open — set an alert, close the tab, and
                    # it never triggered. Evaluated here they run server-side on
                    # the engine's own cycle. Cheap: internally throttled, and a
                    # no-op when no alert is active.
                    if time.time() - _last_alerts.get(m, 0) >= ALERT_INTERVAL:
                        try:
                            from . import v2_web            # lazy: avoids a circular import
                            fired = v2_web._check_alerts()
                            if fired:
                                _LOG.info("alerts fired: %s",
                                          ", ".join(f"{f['symbol']} {f['kind']} {f['value']}" for f in fired))
                        except Exception:
                            _LOG.exception("alert check failed")
                        _last_alerts[m] = time.time()
                    if time.time() - _last_signal.get(m, 0) >= SIGNAL_INTERVAL:
                        poll_market(m)                           # heavy signal gen periodically
                        _last_signal[m] = time.time()
                elif in_preopen(m):
                    # Warm the signals so the open is spent BUYING, not computing.
                    if time.time() - _last_preopen.get(m, 0) >= PREOPEN_INTERVAL:
                        poll_market(m)
                        if m == "IN":
                            _refresh_preopen()
                        _last_preopen[m] = time.time()
                    tops = ""
                    try:
                        from . import preopen as _pre
                        top = _pre.gappers(limit=3)
                        if top:
                            tops = " · gapping: " + ", ".join(
                                f"{r['symbol']} {r['gap_pct']:+.1f}%" for r in top)
                    except Exception:
                        pass
                    _status[m] = (f"pre-open {datetime.now(IST).strftime('%H:%M IST')} · "
                                  f"preparing candidates for the open{tops}")
                else:
                    _status[m] = "closed"
                    # END-OF-DAY EQUITY POINT for every personal book.
                    #
                    # The snapshot lived inside poll_market, which only runs
                    # while the market is open — so a book's "daily" value was
                    # whatever it happened to be MID-SESSION on the last cycle
                    # before 15:30, not its close. A curve built from arbitrary
                    # intraday moments is not a daily curve, it is noise with
                    # dates on it.
                    #
                    # Once per calendar day, after the close, keyed on the day
                    # so the INSERT OR REPLACE overwrites the intraday value
                    # with the settled one.
                    if _EOD_SNAP.get(m) != _today_key():
                        eod = None
                        try:
                            from . import books as _bk3, plans as _pl3
                            eod = _rw()
                            n = _bk3.snapshot_all(eod, _pl3, m, _live(m))
                            _EOD_SNAP[m] = _today_key()
                            if n:
                                _LOG.info("end-of-day equity written for %d book(s)", n)
                        except Exception:
                            _LOG.exception("end-of-day equity snapshot failed")
                        finally:
                            if eod is not None:
                                eod.close()
            except Exception as exc:
                _status[m] = f"err {str(exc)[:40]}"
        time.sleep(interval)


def start_background(interval=8):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=loop, args=(interval,), daemon=True, name="v2-live-engine").start()


def status():
    return dict(_status)
