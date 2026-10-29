"""Conditional stock idea plans. Never fund or execute an order.

The screen ranks businesses; this layer makes an explicit pullback plan.
Price targets are risk multiples, not forecasts of where prices will go.
All quantities are independent alternatives, using the existing paper allocator.
"""
import math
from datetime import datetime, timezone

from ..sleeves.base import Candidate
from ..sleeves.config import SLEEVES
from ..sleeves.feeds import fresh_quotes
from ..sleeves.risk import BookState, RiskManager, SLIPPAGE
from ..costs import round_trip
from .confirmation import POLICY
from .selection import MODEL_VERSION, POLICY as SELECTION_POLICY, reject_reason
from .health import discovery_health


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
    from ..paper_exchange import integrity_reason, reserved_state
    issue = integrity_reason(con, allow_filling=False)
    if not issue:
        book = reserved_state(con, uid, epoch, book)
    return book, ("Held-position quote or stop unavailable" if missing else issue)


def _net(entry, target, qty):
    buy = entry*qty*(1+SLIPPAGE)
    sell = target*qty*(1-SLIPPAGE)
    return sell-buy-round_trip(buy,sell,"D")


def shortlist(screen, book, quotes=None, now=None, book_error="", limit=3, *, _stage='entry', settings=None):
    now = now or datetime.now(timezone.utc)
    settings = settings or SLEEVES
    # Sizing already uses book.capital. dataclasses.replace() would rerun
    # SleeveSettings.__post_init__ and erase context-specific sleeve limits.
    allocator = RiskManager(settings)
    marks = fresh_quotes(quotes or {},now)
    rows, rejected, seen, qualified = [], [], set(), []
    for row in screen.get("equities",[]):
        if row.get("symbol") in seen:
            continue
        seen.add(row.get("symbol"))
        if book_error:
            rejected.append(dict(symbol=row.get("symbol"), reason=book_error))
            continue
        reason = reject_reason(row, screen, now, stage=_stage)
        if reason:
            rejected.append(dict(symbol=row.get("symbol"),reason=reason))
            continue
        if _stage == 'watch' and not reject_reason(row, screen, now):
            # It belongs in Entry plans already, not twice in the same desk.
            continue
        qualified.append(row)
        m = row.get("metrics") or {}
        try:
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
            rejected.append(dict(symbol=row["symbol"], sector=row["sector"], score=float(row["score"]),
                reason=allocation.reason, rejection_stage='funding', qualification=_stage,
                entry_high=upper, stop=stop, funding=allocation.funding,
                actionable=False, execution_ready=False))
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
                reason="First target is not profitable after fees and slippage at this quantity",
                sector=row['sector'],score=float(row['score']),rejection_stage='economics',qualification=_stage,
                entry_high=upper,stop=stop,funding=allocation.funding,actionable=False,execution_ready=False))
            continue
        if _net(upper,targets[2],qty) < allocation.risk_amount*SELECTION_POLICY["min_net_r_at_final_target"]:
            rejected.append(dict(symbol=row["symbol"],
                reason="Final-target net reward is below the estimated stop loss after fees and slippage",
                sector=row['sector'],score=float(row['score']),rejection_stage='economics',qualification=_stage,
                entry_high=upper,stop=stop,funding=allocation.funding,actionable=False,execution_ready=False))
            continue
        if state in ("INVALIDATED", "BELOW ENTRY ZONE"):
            rejected.append(dict(symbol=row["symbol"],reason="Fresh price invalidates this entry plan"))
            continue
        rows.append(dict(symbol=row["symbol"],sector=row["sector"],score=float(row["score"]),
            entry_low=lower,entry_high=upper,stop=stop,t1=targets[0],t2=targets[1],t3=targets[2],
            qty=qty,notional=round(qty*upper,2),estimated_stop_loss=round(allocation.risk_amount,2),
            cash_required=round(allocation.cash_required,2), funding=allocation.funding,
            funding_status='fundable_conditional', execution_ready=False,
            estimated_net_at_targets=returns,
            net_r_at_targets=[round(p/allocation.risk_amount,2) for p in returns],
            price_asof=screen.get("price_asof"),quote_price=current,
            quote_at=quote.get("ts") if quote else None,state=state,
            why=f"20-session strength versus Nifty 50 +{float(m['rs_vs_nifty20_pct']):.1f}pp; sector +{float(m['sector_rs20_pct']):.1f}pp; volume {float(m['relative_volume']):.1f}x with delivery above average; quality and after-cost reward checks passed",
            data_contract_version=screen.get("data_contract_version", "screening-data-v1"),
            model_version=MODEL_VERSION,selection_policy=dict(SELECTION_POLICY),
            confirmation_policy=dict(POLICY),
            buy_condition=f"After an observed zone touch, require a completed daily rebound (close above open and prior close, in the top 40% of the range, volume at least 1.5x the prior 20-session mean); consider only a fresh next-session quote between Rs {lower:.2f} and Rs {upper:.2f} with market, news and risk checks passed",
            invalidation=f"Cancel if price falls below Rs {stop:.2f}, adverse news appears, or the market/risk gate blocks entry",
            horizon="4–8 weeks; reassess after 40 sessions",
            target_method="T1/T2/T3 = 2/3/4 times price risk from the upper entry price; scenarios, not price forecasts",
            actionable=False,execution="Conditional research preview; automatic paper entries use a separate confirmed trial plan",
            evidence=row))
        if _stage == 'watch':
            rows[-1].update(model_version='stock-research-watch-v1', state='RESEARCH WATCH',
                entry_blocker=reject_reason(row, screen, now) or 'Rebound confirmation and later executable quote required',
                why=f"Verified profitable business, positive stock and sector relative strength; volume {float(m['relative_volume']):.1f}x. Entry checks have not passed.",
                execution='Research candidate only; cannot submit an order or authorise automation',
                buy_condition='Wait for all entry participation and setup checks, then a new frozen plan and observed rebound confirmation')
    rows.sort(key=lambda r:(-r["score"],r["symbol"]))
    quality_rank = {r['symbol']:i for i,r in enumerate(sorted(qualified,
        key=lambda r:(-float(r['score']),r['symbol'])),1)}
    funding_rejections = sorted((r for r in rejected if r.get('rejection_stage') in ('funding','economics')),
                               key=lambda r:(-r['score'],r['symbol']))
    for row in funding_rejections:row['quality_rank']=quality_rank[row['symbol']]
    fundable_count = len(rows)
    selected, sectors = [], set()
    maximum = min(max(limit,0),SELECTION_POLICY["max_ideas"])
    for row in rows:
        reason = ("A higher-ranked qualifying idea already covers this sector" if row["sector"] in sectors else
                  "Selective shortlist cap reached; no extra publication" if len(selected)>=maximum else "")
        if reason:
            rejected.append(dict(symbol=row["symbol"],reason=reason))
        else:
            selected.append(row);sectors.add(row["sector"])
    rows = selected
    for i,row in enumerate(rows,1):
        row.update(rank=i, funding_rank=i, quality_rank=quality_rank[row['symbol']])
    return dict(ideas=rows,count=len(rows),requested=0,max_ideas=maximum,selection_policy=dict(SELECTION_POLICY),capital=book.capital,cash=book.cash,
        risk_cap=round(max(0.,min(book.capital*settings.risk_per_trade,
            book.capital*settings.daily_loss_limit+min(book.day_pnl,0.)-book.open_risk+book.strategic_open_risk,
            book.capital*settings.max_drawdown-book.open_risk)),2),
        max_positions=settings.max_positions_total,book_error=book_error,
        funding_rejections=funding_rejections,
        funding_summary=dict(version='account-funding-v1',qualified=len(qualified),fundable=fundable_count,
            unfunded=len(funding_rejections),minimum_ticket=settings.min_ticket,
            sleeve_position_limit=settings.quality_momentum.max_positions,
            sleeve_positions_committed=book.per_sleeve_positions.get('quality_momentum',0),
            ranking='Existing quality score among fundable alternatives; one per sector. These alternatives are not cumulative orders.',
            qualification=_stage,execution_ready=False),
        rejected=rejected,discovery_health=discovery_health(screen,rejected,now),
        generated_at=screen.get("generated_at"),price_asof=screen.get("price_asof"),
        note="Zero to three qualifying ideas; no daily quota and at most one per sector. Quantities are independent alternatives using your paper account, after estimated stop costs. Automatic paper entries require confirmed trial plans; independent validation remains required before live use.")


def research_candidates(screen, book, quotes=None, now=None, book_error=''):
    """Show strong businesses awaiting a setup without manufacturing buy plans.

    Shares/levels are conditional scenarios under the same account allocator.
    These rows are never published into the executable/forward plan journals.
    """
    return shortlist(screen, book, quotes, now, book_error, _stage='watch')['ideas']
