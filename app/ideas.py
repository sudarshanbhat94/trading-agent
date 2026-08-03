"""Published stock ideas: entry, stop, three targets, size — and a tracker.

WHAT MAKES THIS DIFFERENT FROM THE PAPER BOOK
The book decides what IT will do with ITS capital and its six position slots.
An idea is a recommendation to a subscriber who has their own money, their own
existing holdings and no slot limit. Same signal, different question, so the
sizing and the exits are stated explicitly instead of being implied by whatever
the engine happened to have room for.

Ideas are published FROM the engine's own ranked candidate list, after the meta
filter, in the engine's own order. They are not a second opinion computed
somewhere else — if the ideas page and the book ever disagreed about what looked
good, one of them would be lying, and there would be no way to tell which.

THE TARGETS, AND AN HONEST WARNING ABOUT THEM
The operator asked for T1/T2/T3 "calculated properly, not a blind target". So
they are R-multiples of each lane's OWN measured ATR stop, not percentages
someone liked the look of:

    R      = atr_stop x ATR       (the lane's measured stop distance)
    stop   = entry - R
    T1/2/3 = entry + 1R / 2R / 3R

Using the lane's own stop as the unit is what makes them comparable across a
Rs 200 stock and a Rs 5,000 one, and it means a target moves when the evidence
behind the stop moves.

But the evidence in PLAN says plainly that targets COST money on these lanes:

    swing_meanrev, same 19,510 entries, avg net % per trade
        hold-only                +0.775
        stop 3ATR                +0.680
        stop 2ATR + target 3.5   +0.506   <- what the lane used to run
    mom_breakout, 2,201 entries
        trail-only +0.896 | 8xATR +0.833 | 4xATR +0.565 | 3xATR +0.351

Both studies found the same thing independently: targets clip the winners that
pay for the losers. T1 at 1R is INSIDE the range those tests measured as
harmful. The ladder is built because it was asked for and because a subscriber
managing their own money reasonably wants defined exits — but `t1_costs_edge`
is exposed so the UI can say so next to the number rather than in a footnote
nobody reads. Do not quietly present T1 as the recommended exit.

TRACKING
The scoreboard closes an idea at the FIRST of stop or T1 (the operator's choice
when asked). `best_target` keeps recording T2/T3 afterwards because it is free
and because it is the evidence that would justify changing the T1 rule later.
"""
from __future__ import annotations

import math

# The reference account an idea is sized for. A subscriber's real capital is
# unknown and asking for it would be a KYC question we have no business asking,
# so every idea is sized for ONE stated number and the UI says which — a
# quantity with no capital attached to it is not actionable.
CAPITAL = 100000.0
RISK_PCT = 0.01                 # 1% of capital at risk per idea, i.e. Rs 1,000
# No single idea may commit more than this fraction of the account. Without it a
# very tight stop produces a mathematically correct but absurd size: a Rs 2 stop
# on a Rs 900 stock asks for 500 shares, Rs 450,000 of a Rs 100,000 account.
MAX_NOTIONAL_PCT = 0.25
# How long an unresolved idea stays open before it is retired at the last price.
# The swing lane's own hold is 8 trading days and the breakout's is 40; 10 is
# chosen to match the horizon the entry edge was actually measured over
# (+3.93% per 10 days for the top conviction decile) rather than either lane.
HORIZON_DAYS = 10

# How many ideas each tier sees per day. Rank 1 is the engine's best candidate,
# so a Starter subscriber gets the SAME top idea an Elite one does — the paid
# tiers add breadth, not a better first pick. Selling a "better" idea to the
# higher tier would mean deliberately publishing a worse one to everybody else.
PER_DAY = {"free": 0, "watch": 1, "paper": 3, "auto": 5}
MAX_PER_DAY = 5

STATUS_OPEN = "open"
STATUS_T1, STATUS_T2, STATUS_T3 = "t1", "t2", "t3"
STATUS_STOPPED = "stopped"
STATUS_EXPIRED = "expired"
RESOLVED = (STATUS_T1, STATUS_T2, STATUS_T3, STATUS_STOPPED, STATUS_EXPIRED)


def levels(entry: float, atr: float, atr_stop: float) -> dict:
    """Stop and the three targets, as R-multiples of the lane's measured stop.

    Returns None-free floats or raises nothing: a non-positive ATR or entry
    yields an empty dict, because an idea without a real stop distance cannot be
    sized and must not be published rather than being published with a guess.
    """
    entry, atr, atr_stop = float(entry or 0), float(atr or 0), float(atr_stop or 0)
    if entry <= 0 or atr <= 0 or atr_stop <= 0:
        return {}
    r = atr * atr_stop
    if r <= 0 or r >= entry:            # a stop at or below zero is not a stop
        return {}
    return dict(r=round(r, 4), stop=round(entry - r, 2),
                t1=round(entry + r, 2), t2=round(entry + 2 * r, 2),
                t3=round(entry + 3 * r, 2))


def size(entry: float, stop: float, capital: float = CAPITAL,
         risk_pct: float = RISK_PCT, max_notional_pct: float = MAX_NOTIONAL_PCT) -> int:
    """Whole shares such that (entry - stop) x qty is `risk_pct` of capital.

    Capped by notional so a tight stop cannot ask for more than the account.
    Returns 0 when the idea is unsizeable — the caller must not publish it.
    """
    entry, stop = float(entry or 0), float(stop or 0)
    risk_per_share = entry - stop
    if entry <= 0 or risk_per_share <= 0 or capital <= 0:
        return 0
    qty = math.floor(capital * risk_pct / risk_per_share)
    cap = math.floor(capital * max_notional_pct / entry)
    return max(0, min(qty, cap))


def tier_for_rank(rank: int) -> str:
    """The LOWEST tier that may see this idea.

    Derived from PER_DAY rather than hardcoded, so changing what a tier is worth
    is one edit and cannot leave the gate and the pricing page disagreeing.
    """
    for tier in ("watch", "paper", "auto"):
        if rank <= PER_DAY[tier]:
            return tier
    return "auto"


def allowance(plan: str) -> int:
    """How many of a day's ideas this plan sees."""
    return PER_DAY.get(str(plan or "").strip().lower(), 0)


def t1_costs_edge(strategy: str) -> bool:
    """True when this lane's own backtest says a target at 1R gives up edge.

    Both measured lanes say yes. It is a function rather than a constant so the
    day a lane is measured otherwise, the UI copy follows the evidence.
    """
    return str(strategy) in ("swing_meanrev", "mom_breakout", "gap_momentum")


def build(candidate: dict, atr_stop: float, rank: int,
          capital: float = CAPITAL) -> dict:
    """One publishable idea from one engine candidate, or {} if unsizeable."""
    entry = float(candidate.get("price") or 0)
    lv = levels(entry, candidate.get("atr"), atr_stop)
    if not lv:
        return {}
    qty = size(entry, lv["stop"], capital)
    if qty <= 0:
        return {}
    return dict(symbol=candidate.get("symbol"), strategy=candidate.get("strategy"),
                entry=round(entry, 2), atr=round(float(candidate.get("atr") or 0), 4),
                conviction=candidate.get("meta_p") if candidate.get("meta_p") is not None
                else candidate.get("score"),
                stop=lv["stop"], t1=lv["t1"], t2=lv["t2"], t3=lv["t3"],
                qty=qty, risk_amt=round(qty * (entry - lv["stop"]), 2),
                notional=round(qty * entry, 2),
                rank=rank, tier=tier_for_rank(rank),
                t1_costs_edge=t1_costs_edge(candidate.get("strategy")))


def resolve(idea: dict, high: float, low: float) -> dict:
    """Advance one idea against a bar. Returns the fields that changed.

    STOP IS CHECKED FIRST. Within a single bar we cannot know which came first,
    and assuming the target did would manufacture wins out of ambiguity — the
    same optimism that made the book's reported P&L disagree with its equity.
    Pessimistic on ties is the only defensible default for a number that is
    being sold.
    """
    if idea.get("status") in RESOLVED:
        return {}
    entry = float(idea.get("entry") or 0)
    if entry <= 0:
        return {}
    hi, lo = float(high or 0), float(low or 0)
    out: dict = {}
    if lo > 0 and lo <= float(idea["stop"]):
        return dict(status=STATUS_STOPPED, hit_price=float(idea["stop"]),
                    result_pct=round((float(idea["stop"]) / entry - 1) * 100, 3))
    # best_target keeps climbing after the scoreboard has closed at T1
    best = idea.get("best_target") or ""
    for name, level in (("t3", idea["t3"]), ("t2", idea["t2"]), ("t1", idea["t1"])):
        if hi >= float(level):
            if not best or _target_ord(name) > _target_ord(best):
                out["best_target"] = name
            break
    if idea.get("status") == STATUS_OPEN and hi >= float(idea["t1"]):
        out.update(status=STATUS_T1, hit_price=float(idea["t1"]),
                   result_pct=round((float(idea["t1"]) / entry - 1) * 100, 3))
    return out


def _target_ord(name: str) -> int:
    return {"t1": 1, "t2": 2, "t3": 3}.get(str(name), 0)


COLUMNS = ("id", "market", "symbol", "strategy", "published_date", "published_at",
           "tier", "rank", "entry", "atr", "conviction", "stop", "t1", "t2", "t3",
           "qty", "risk_amt", "notional", "status", "best_target", "hit_price",
           "result_pct", "mfe", "mae", "last_price", "last_at", "closed_at")


def row_to_dict(row) -> dict:
    return dict(zip(COLUMNS, row))


def publish(v2, market, candidates, atr_stop_for, today_s, now_iso,
            capital=CAPITAL, limit=MAX_PER_DAY):
    """Write today's ideas. Idempotent, and NEVER rewrites a published level.

    `INSERT OR IGNORE` rather than OR REPLACE, deliberately: poll_market runs
    every five minutes and the candidate list moves with the tape. Replacing
    would silently walk an entry, stop or target after a subscriber had already
    acted on it, and their screenshot would stop matching the page. First
    publication of the day wins; the rest of the day is tracking, not editing.

    Returns the number of NEW ideas written.
    """
    written = 0
    for rank, cand in enumerate(candidates[:limit], start=1):
        idea = build(cand, atr_stop_for(cand.get("strategy")), rank, capital)
        if not idea:
            continue                       # unsizeable — publishing a guess is worse
        cur = v2.execute(
            "INSERT OR IGNORE INTO v2_ideas(market,symbol,strategy,published_date,"
            "published_at,tier,rank,entry,atr,conviction,stop,t1,t2,t3,qty,risk_amt,"
            "notional,status,last_price,last_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (market, idea["symbol"], idea["strategy"], today_s, now_iso,
             idea["tier"], idea["rank"], idea["entry"], idea["atr"], idea["conviction"],
             idea["stop"], idea["t1"], idea["t2"], idea["t3"], idea["qty"],
             idea["risk_amt"], idea["notional"], STATUS_OPEN, idea["entry"], now_iso))
        written += cur.rowcount or 0
    v2.commit()
    return written


def track(v2, market, live, now_iso, today_s=None, horizon_days=HORIZON_DAYS):
    """Advance every open idea against the latest quote. Returns what changed.

    Uses the quote's session high/low when present and falls back to the last
    price, so a feed without OHLC degrades to close-only tracking rather than
    silently never resolving anything.
    """
    rows = v2.execute(
        "SELECT " + ",".join(COLUMNS) + " FROM v2_ideas WHERE market=? AND status=?",
        (market, STATUS_OPEN)).fetchall()
    changed = []
    for row in rows:
        idea = row_to_dict(row)
        q = (live or {}).get(idea["symbol"])
        if not q:
            continue
        price = float(q.get("price") or 0)
        if price <= 0:
            continue
        hi = float(q.get("high") or 0) or price
        lo = float(q.get("low") or 0) or price
        upd = dict(last_price=price, last_at=now_iso)
        upd.update(excursion(idea, hi, lo))
        upd.update(resolve(idea, hi, lo))
        if today_s and idea["status"] == STATUS_OPEN and "status" not in upd:
            if _sessions_since(idea["published_date"], today_s) >= horizon_days:
                upd.update(status=STATUS_EXPIRED, hit_price=price,
                           result_pct=round((price / float(idea["entry"]) - 1) * 100, 3))
        if upd.get("status") in RESOLVED:
            upd["closed_at"] = now_iso
        cols = ",".join(f"{k}=?" for k in upd)
        v2.execute(f"UPDATE v2_ideas SET {cols} WHERE id=?",
                   (*upd.values(), idea["id"]))
        if upd.get("status"):
            changed.append(dict(idea, **upd))
    v2.commit()
    return changed


def _sessions_since(published_date, today_s):
    """Calendar days, used only as the horizon clock.

    Deliberately NOT trading days: this is a "how stale is this idea" timer for
    a reader, not a hold-period accounting, and a calendar week is what somebody
    means when they ask how old a recommendation is.
    """
    from datetime import date
    try:
        a = date.fromisoformat(str(published_date)[:10])
        b = date.fromisoformat(str(today_s)[:10])
    except (TypeError, ValueError):
        return 0
    return (b - a).days


def visible(v2, market, plan, days=30, limit=200):
    """The ideas this plan may see, newest first.

    The tier gate is applied in SQL against the idea's OWN stored tier, not
    recomputed from its rank at read time — an idea a subscriber was shown must
    not vanish because the per-tier allowance changed afterwards.
    """
    allowed = [t for t in ("watch", "paper", "auto")
               if PER_DAY.get(t, 0) <= PER_DAY.get(str(plan or "").lower(), 0)]
    if not allowed:
        return []
    marks = ",".join("?" * len(allowed))
    rows = v2.execute(
        "SELECT " + ",".join(COLUMNS) + " FROM v2_ideas WHERE market=?"
        f" AND tier IN ({marks})"
        " AND published_date >= date('now', ?)"
        " ORDER BY published_date DESC, rank ASC LIMIT ?",
        (market, *allowed, f"-{int(days)} day", int(limit))).fetchall()
    return [row_to_dict(r) for r in rows]


def excursion(idea: dict, high: float, low: float) -> dict:
    """Running best/worst since publication, in percent of entry.

    Kept for every idea including the losers, because max adverse excursion is
    the number that says whether a stop sits inside normal noise — which is how
    the -35% option stop was shown to be worthless.
    """
    entry = float(idea.get("entry") or 0)
    if entry <= 0:
        return {}
    out = {}
    if high:
        mfe = round((float(high) / entry - 1) * 100, 3)
        if idea.get("mfe") is None or mfe > float(idea["mfe"]):
            out["mfe"] = mfe
    if low:
        mae = round((float(low) / entry - 1) * 100, 3)
        if idea.get("mae") is None or mae < float(idea["mae"]):
            out["mae"] = mae
    return out


def scoreboard(rows) -> dict:
    """Aggregate published ideas honestly.

    Counts EXPIRED ideas as the losers they usually are rather than dropping
    them, and always reports the sample size alongside the rate — the same rule
    the options tile follows. `open` are excluded from the rate because they
    have not happened yet, but they are counted so the reader can see how much
    of the record is still undecided.
    """
    rows = list(rows or [])
    closed = [r for r in rows if r.get("status") in RESOLVED]
    wins = [r for r in closed if (r.get("result_pct") or 0) > 0]
    rets = [float(r.get("result_pct") or 0) for r in closed]
    return dict(
        published=len(rows), open=len([r for r in rows if r.get("status") == STATUS_OPEN]),
        closed=len(closed), wins=len(wins),
        win_pct=(round(len(wins) / len(closed) * 100) if closed else None),
        avg_pct=(round(sum(rets) / len(rets), 2) if rets else None),
        hit_t1=len([r for r in rows if _target_ord(r.get("best_target")) >= 1]),
        hit_t2=len([r for r in rows if _target_ord(r.get("best_target")) >= 2]),
        hit_t3=len([r for r in rows if _target_ord(r.get("best_target")) >= 3]),
        stopped=len([r for r in rows if r.get("status") == STATUS_STOPPED]),
        expired=len([r for r in rows if r.get("status") == STATUS_EXPIRED]),
    )
