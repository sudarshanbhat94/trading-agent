"""Conditional stock idea plans. Never fund or execute an order.

The screen ranks businesses; this layer makes an explicit pullback plan.
Price targets are risk multiples, not forecasts of where prices will go.
All quantities are independent alternatives, using the existing paper allocator.
"""
import math
from dataclasses import replace
from datetime import datetime, timezone

from ..sleeves.base import Candidate
from ..sleeves.config import SLEEVES
from ..sleeves.feeds import fresh_quotes
from ..sleeves.risk import BookState, RiskManager, SLIPPAGE
from ..costs import round_trip
from .confirmation import POLICY


def account_state(con, uid, quotes, now):
    """Read only this user's current epoch. Never create a book during a read."""
    from .. import books
    marks = fresh_quotes(quotes, now)
    stats = books.stats(con, uid, "IN", marks)
    positions = books.positions(con, uid, "IN")
    epoch = books.current_epoch(con, uid, "IN")
    counts, notionals = {}, {}
    deployed = risk = strategic = 0.
    missing = []
    from ..sleeves.risk import stop_loss_including_costs
    for p in positions:
        sleeve = p.get("sleeve") or p["strategy"]
        value = p["shares"]*p["entry_price"]
        counts[sleeve] = counts.get(sleeve, 0)+1
        notionals[sleeve] = notionals.get(sleeve, 0)+value
        deployed += value
        if p["symbol"] not in marks or not p.get("stop"):
            missing.append(p["symbol"])
            continue
        price = marks[p["symbol"]]["price"]
        loss = stop_loss_including_costs(price, min(price,p["stop"]),p["shares"])
        risk += loss
        if sleeve == "index_directional":strategic += loss
    day = now.astimezone(books.IST).date().isoformat()
    realised = con.execute("SELECT COALESCE(SUM(pnl),0) FROM user_trades WHERE user_id=? "
        "AND market='IN' AND COALESCE(book_epoch,?)=? AND substr(exit_date,1,10)=?",
        (uid,books.LEGACY_EPOCH,epoch,day)).fetchone()[0] or 0.
    # Conservative: all currently open losses count; profits never replenish
    # today's allowance. A missing held quote blocks sizing altogether.
    day_pnl = realised + min(0.,stats["unrealised"])
    peak = con.execute("SELECT MAX(equity) FROM user_equity WHERE user_id=? "
                       "AND market='IN' AND date>=?",(uid,epoch[:10])).fetchone()[0]
    book = BookState(stats["budget"],stats["cash"],deployed,len(positions),counts,
        stats["equity"],max(peak or 0.,stats["budget"],stats["equity"]),day_pnl,notionals,risk,strategic)
    return book, ("Held-position quote or stop unavailable" if missing else "")


def _net(entry, target, qty):
    buy = entry*qty*(1+SLIPPAGE)
    sell = target*qty*(1-SLIPPAGE)
    return sell-buy-round_trip(buy,sell,"D")


def shortlist(screen, book, quotes=None, now=None, book_error="", limit=10):
    now = now or datetime.now(timezone.utc)
    allocator = RiskManager(replace(SLEEVES,capital=book.capital))
    marks = fresh_quotes(quotes or {},now)
    rows, rejected, seen = [], [], set()
    for row in screen.get("equities",[]):
        if row.get("symbol") in seen:
            continue
        seen.add(row.get("symbol"))
        if book_error:
            rejected.append(dict(symbol=row.get("symbol"), reason=book_error))
            continue
        if row.get("flags"):
            rejected.append(dict(symbol=row["symbol"],reason="; ".join(row["flags"])))
            continue
        m = row.get("metrics") or {}
        try:
            if not (m.get("above50") and float(m.get("rs_vs_nifty20_pct") or 0)>0
                    and float(m.get("return126_pct") or 0)>0
                    and float(m.get("sector_rs20_pct") or 0)>0):
                rejected.append(dict(symbol=row["symbol"],reason="Stock or sector momentum confirmation missing"))
                continue
            close = float(m["price"])
            atr = close*float(m["atr_pct"])/100
            # Wait for a pullback; never prescribe buying the last close or a
            # fresh high just because the company has a high screen score.
            upper = round(close-atr,2)
            lower = round(upper-.25*atr,2)
            stop = round(close-2*atr,2)
            if not all(math.isfinite(v) for v in (lower,upper,stop,atr)) or not 0 < stop < lower <= upper:
                raise ValueError("invalid ATR plan")
            r = upper-stop
            targets = [round(upper+multiple*r,2) for multiple in (2,3,4)]
            c = Candidate(row["symbol"],"quality_momentum",float(row["score"])/100,
                upper,stop,target=targets[2],max_hold_days=40)
            allocation = allocator.size(c,book)
        except (KeyError,ValueError,TypeError):
            rejected.append(dict(symbol=row.get("symbol"),reason="Entry and stop cannot be calculated"))
            continue
        if not allocation.ok:
            rejected.append(dict(symbol=row["symbol"],reason=allocation.reason))
            continue
        qty = allocation.shares
        quote = marks.get(row["symbol"])
        current = float(quote["price"]) if quote else None
        state = ("INVALIDATED" if current is not None and current<=stop else
                 "IN ZONE · CONFIRMATION NEEDED" if current is not None and lower<=current<=upper else
                 "BELOW ENTRY ZONE" if current is not None and current<lower else
                 "WAIT FOR PULLBACK" if current is not None else "LIVE QUOTE UNAVAILABLE")
        if screen.get("stale") or screen.get("price_stale"):state="STALE PLAN"
        if book_error:state="ACCOUNT CHECK NEEDED"
        returns = [round(_net(upper,t,qty),2) for t in targets]
        if any(value <= 0 for value in returns):
            rejected.append(dict(symbol=row["symbol"],
                reason="First target is not profitable after fees and slippage at this quantity"))
            continue
        rows.append(dict(symbol=row["symbol"],sector=row["sector"],score=row["score"],
            entry_low=lower,entry_high=upper,stop=stop,t1=targets[0],t2=targets[1],t3=targets[2],
            qty=qty,notional=round(qty*upper,2),estimated_stop_loss=round(allocation.risk_amount,2),
            estimated_net_at_targets=returns,
            net_r_at_targets=[round(p/allocation.risk_amount,2) for p in returns],
            price_asof=screen.get("price_asof"),quote_price=current,
            quote_at=quote.get("ts") if quote else None,state=state,
            why=f"20-session strength versus NIFTYBEES (Nifty ETF proxy) +{m['rs_vs_nifty20_pct']:.1f}pp; sector +{m['sector_rs20_pct']:.1f}pp; profitable business with delivery evidence",
            data_contract_version=screen.get("data_contract_version", "screening-data-v1"),
            model_version="conditional-pullback-v2",
            confirmation_policy=dict(POLICY),
            buy_condition=f"After an observed zone touch, require a completed daily rebound (close above open and prior close, in the top 40% of the range, volume at least 1.5x the prior 20-session mean); consider only a fresh next-session quote between Rs {lower:.2f} and Rs {upper:.2f} with market, news and risk checks passed",
            invalidation=f"Cancel if price falls below Rs {stop:.2f}, adverse news appears, or the market/risk gate blocks entry",
            horizon="4–8 weeks; reassess after 40 sessions",
            target_method="T1/T2/T3 = 2/3/4 times price risk from the upper entry price; scenarios, not price forecasts",
            actionable=False,execution="Conditional paper preview; stock execution remains unpromoted",
            evidence=row))
    rows.sort(key=lambda r:(-r["score"],r["symbol"]))
    rows=rows[:min(max(limit,0),10)]
    for i,row in enumerate(rows,1):row["rank"]=i
    return dict(ideas=rows,count=len(rows),requested=10,capital=book.capital,cash=book.cash,
        risk_cap=round(max(0.,min(book.capital*SLEEVES.risk_per_trade,
            book.capital*SLEEVES.daily_loss_limit+min(book.day_pnl,0.)-book.open_risk+book.strategic_open_risk,
            book.capital*SLEEVES.max_drawdown-book.open_risk)),2),
        max_positions=SLEEVES.max_positions_total,book_error=book_error,
        rejected=rejected,generated_at=screen.get("generated_at"),price_asof=screen.get("price_asof"),
        note="Ten alternatives, not ten simultaneous purchases. Quantities use your paper account and include estimated stop costs. Entry confirmation and production promotion are still required.")
