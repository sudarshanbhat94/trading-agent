"""Explicit operator allocation for empty, untraded NSE paper books only.

This is not a general deposit API. A new accounting epoch prevents added capital
from appearing as a trading return; old trades, publications and risk records
remain intact. Existing daily chart rows replaced at cutover are archived in the
immutable allocation receipt. No broker or strategy configuration is changed.
"""
from datetime import datetime, timezone
import json
import math

from . import account_safety, books, paper_ledger


def allocate(con, user_id, capital, *, request_key, reference, now=None):
    if isinstance(capital, bool) or not math.isfinite(float(capital)) or float(capital) <= 0:
        raise ValueError('Capital must be finite and positive')
    uid=int(user_id); capital=float(capital)
    if uid<=0 or not request_key or not reference:
        raise ValueError('An owned account, request identity and operator reference are required')
    now=now or datetime.now(timezone.utc)
    if now.utcoffset() is None:raise ValueError('An aware allocation timestamp is required')
    epoch=now.astimezone(timezone.utc).isoformat()
    day=now.astimezone(books.IST).date().isoformat()
    with account_safety.atomic(con):
        con.execute('CREATE TABLE IF NOT EXISTS paper_capital_allocations('
                    'request_key TEXT PRIMARY KEY,user_id INTEGER NOT NULL,capital REAL NOT NULL,'
                    'reference TEXT NOT NULL,receipt TEXT NOT NULL,created_at TEXT NOT NULL)')
        for action in ('UPDATE','DELETE'):
            con.execute('CREATE TRIGGER IF NOT EXISTS immutable_capital_'+action.lower()+
                        ' BEFORE '+action+" ON paper_capital_allocations BEGIN SELECT RAISE(ABORT,'immutable capital allocation'); END")
        old=con.execute('SELECT user_id,capital,reference,receipt FROM paper_capital_allocations WHERE request_key=?',
                        (request_key,)).fetchone()
        if old:
            if (old[0],old[1],old[2])!=(uid,capital,reference):raise ValueError('Allocation identity cannot be rebound')
            return json.loads(old[3])
        house=con.execute("SELECT budget,started_at FROM v2_book WHERE market='IN'").fetchone()
        own=con.execute("SELECT budget,started_at FROM user_book WHERE user_id=? AND market='IN'",(uid,)).fetchone()
        if not house or not own:raise ValueError('Both existing paper books are required')
        if capital<=max(float(house[0]),float(own[0])):raise ValueError('Allocation must increase both empty books')
        if con.execute("SELECT COUNT(*) FROM v2_positions WHERE market='IN'").fetchone()[0] or con.execute(
                "SELECT COUNT(*) FROM user_positions WHERE user_id=? AND market='IN'",(uid,)).fetchone()[0]:
            raise ValueError('Open positions prevent an empty-book allocation')
        if con.execute("SELECT COUNT(*) FROM v2_trades WHERE market='IN' AND julianday(closed_at)>=julianday(?)",(house[1],)).fetchone()[0] or con.execute(
                "SELECT COUNT(*) FROM user_trades WHERE user_id=? AND market='IN' AND COALESCE(book_epoch,?)=?",
                (uid,books.LEGACY_EPOCH,own[1])).fetchone()[0]:
            raise ValueError('Current-epoch trades require a cash-flow-aware allocation instead')
        if con.execute("SELECT COUNT(*) FROM paper_order_intents i LEFT JOIN paper_order_state s ON s.order_id=i.id "
                       "WHERE i.user_id IN (0,?) AND json_extract(i.payload,'$.plan.market')='IN' "
                       "AND (s.status IS NULL OR s.status NOT IN ('filled','rejected','expired','cancelled'))",(uid,)).fetchone()[0]:
            raise ValueError('Unresolved paper orders prevent allocation')
        # Independent owned-cash and ledger checks prevent a funding operation
        # from hiding a discrepancy or a loss in an apparently empty book.
        if abs(books.cash(con,uid)-float(own[0]))>.005:raise ValueError('Owned cash is not a clean capital baseline')
        ledger=paper_ledger.report(con,uid,'IN',own[1],books.cash(con,uid))
        if not ledger['balanced'] or ledger['status']=='mismatch':raise ValueError('Owned ledger requires reconciliation')
        from .v2_live import _epoch_pnl
        if abs(float(_epoch_pnl(con,'IN')))>.005:raise ValueError('House cash is not a clean capital baseline')
        if epoch<=max(str(house[1]),str(own[1])):raise ValueError('New allocation must follow existing epochs')
        archived=[list(r) for r in con.execute('SELECT * FROM user_equity WHERE user_id=? AND market=? AND date=?',(uid,'IN',day))]
        receipt=dict(user_id=uid,market='IN',capital=capital,epoch=epoch,reference=reference,
                     previous=dict(house=dict(capital=house[0],epoch=house[1]),owned=dict(capital=own[0],epoch=own[1])),
                     archived_same_day_equity=archived,kind='EMPTY_BOOK_CAPITAL_ALLOCATION',trading_pnl=0)
        con.execute("UPDATE v2_book SET budget=?,started_at=? WHERE market='IN'",(capital,epoch))
        con.execute("UPDATE user_book SET budget=?,started_at=? WHERE user_id=? AND market='IN'",(capital,epoch,uid))
        con.execute("INSERT INTO v2_equity(market,date,equity,cash,positions_value,n_positions) VALUES('IN',?,?,?,0,0)",
                    ('LIVE_'+epoch,capital,capital))
        con.execute('INSERT OR REPLACE INTO user_equity VALUES(?,?,?,?,?,?,?)',(uid,'IN',day,capital,capital,0,0))
        for kind,owner in (('house',0),('paper',uid)):
            account_safety.observe(con,kind,owner,'IN',epoch,capital,capital,day,session_open=capital)
        paper_ledger.anchor(con,uid,'IN',epoch,capital,0)
        con.execute('INSERT INTO paper_capital_allocations VALUES(?,?,?,?,?,?)',
                    (request_key,uid,capital,reference,json.dumps(receipt,sort_keys=True),epoch))
        return receipt
