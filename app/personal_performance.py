"""Actual personal-paper fills by epoch, sleeve and entry regime; read-only."""
from datetime import datetime
import math

from . import books
from .sleeves.feeds import fresh_quotes


def summary(rows):
    known=[r for r in rows if r[2] is not None and math.isfinite(float(r[2]))]
    net=sum(float(r[2]) for r in known)
    rs=[float(r[2])/float(r[4]) for r in known if r[4] is not None and math.isfinite(r[4]) and r[4]>0]
    return dict(trades=len(rows),net_pnl=round(net,2) if len(known)==len(rows) else None,
                pnl_observations=len(known),
                win_rate=round(sum(r[2]>0 for r in known)/len(known)*100,2) if known else None,
                average_r=round(sum(rs)/len(rs),4) if rs else None,r_observations=len(rs))


def report(con, user_id, market="IN", quotes=None, day=None, now=None):
    now=now or datetime.now(books.IST)
    day=day or now.astimezone(books.IST).date().isoformat()
    day=datetime.strptime(day,"%Y-%m-%d").date().isoformat()
    epoch=books.current_epoch(con,user_id,market)
    risk="risk_amt" if 'risk_amt' in {r[1] for r in con.execute('PRAGMA table_info(user_trades)')} else 'NULL'
    rows=con.execute("SELECT COALESCE(sleeve,strategy,'legacy'),COALESCE(regime,'UNSPECIFIED'),pnl,"
                     "shares*(exit_price-entry_price),"+risk+",exit_date FROM user_trades WHERE user_id=? "
                     "AND market=? AND COALESCE(book_epoch,?)=?",(user_id,market,books.LEGACY_EPOCH,epoch)).fetchall()
    daily=[r for r in rows if r[5]==day]
    pos=books.positions(con,user_id,market)
    marks=fresh_quotes(quotes or {},now)
    missing=[p['symbol'] for p in pos if p['symbol'] not in marks]
    book=books.stats(con,user_id,market,marks)
    if missing:
        for key in ('equity','unrealised','overall_pnl','deployed'):book[key]=None
    return dict(scope='personal-paper',epoch=epoch,day=day,book=book,snapshot_at=now.isoformat(),
                market=market,currency={"IN":"INR","US":"USD"}.get(market),
                valuation_complete=not missing,missing_marks=missing,
                current_epoch=summary(rows),daily=summary(daily),
                by_sleeve={key:summary([r for r in daily if r[0]==key]) for key in sorted({r[0] for r in daily})},
                by_regime={key:summary([r for r in daily if r[1]==key]) for key in sorted({'ON','NEUTRAL','OFF'}|{r[1] for r in daily})},
                by_sleeve_epoch={key:summary([r for r in rows if r[0]==key]) for key in sorted({r[0] for r in rows})},
                by_regime_epoch={key:summary([r for r in rows if r[1]==key]) for key in sorted({'ON','NEUTRAL','OFF'}|{r[1] for r in rows})},
                note='Actual paper trades only. Book valuation is the current snapshot; day filters closed trades. Missing historical initial risk is excluded from R. No strategy-profitability claim.')
