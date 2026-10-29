"""Owned, read-only paper lifecycle status for the product workspace."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def owned_book_snapshot(con, user_id, quotes, now=None, market='IN'):
    """Unknown held marks cannot become a flat-at-entry equity estimate."""
    from . import books
    from .sleeves.feeds import fresh_quotes
    now = now or datetime.now(timezone.utc)
    marks = fresh_quotes(quotes or {}, now)
    result = books.stats(con, user_id, market, marks)
    missing = [p['symbol'] for p in books.positions(con, user_id, market) if p['symbol'] not in marks]
    result.update(valuation_complete=not missing, missing_marks=missing, snapshot_at=now.isoformat())
    if missing:
        for key in ('equity', 'deployed', 'deploy_pct', 'unrealised', 'overall_pnl'):
            result[key] = None
    return result


def market_data_status(path):
    """Last ingestion receipts, without opening a write transaction."""
    result=dict(status='unavailable',candles=None,identities=None)
    try:
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=3)) as con:
            values={k:json.loads(v) for k,v in con.execute("SELECT key,value FROM agent_state WHERE key IN ('daily_candle_ingestion','market_identity_refresh')")}
        result['candles']=values.get('daily_candle_ingestion')
        identity=values.get('market_identity_refresh')
        if identity:result['identities']={k:identity.get(k) for k in ('status','observed_at','updated','added','issues')}
        if result['candles']:
            markets=result['candles'].get('markets',[])
            result['status']='observed' if markets and identity and identity.get('status')=='complete' and all(r.get('status')=='complete' for r in markets) else 'partial'
    except (sqlite3.Error,ValueError,TypeError):pass
    return result


def execution_data_status(now=None):
    """Source availability only; it never certifies an instrument or an order."""
    from .entry_contracts import open_catalogue
    from . import execution_contracts
    from .exchange_session_feed import CALENDAR
    now=now or datetime.now(timezone.utc)
    result=dict(status='unavailable', rules=None, session='unavailable', reason='Execution source catalogue unavailable')
    try:
        with open_catalogue() as (con,_):
            rows=con.execute("SELECT DISTINCT identity FROM execution_contract_evidence WHERE kind='rules' "
                             "AND julianday(effective_until)>julianday(?) LIMIT 501",(now.isoformat(),)).fetchall()
            count=0
            for (identity,) in rows:
                try:
                    execution_contracts._latest(con,'rules',identity,now)
                    count+=1
                except ValueError:
                    pass
            try:
                _,session=execution_contracts._latest(con,'session',CALENDAR,now)
                result['session']='open' if session['open'] else 'closed'
            except ValueError:
                pass
            result.update(status='observed',rules=count,
                          reason='No current sourced instrument rules; new orders cannot pass' if not count
                          else 'Sourced rule records are available; each order still requires exact instrument and account validation')
    except (ValueError,sqlite3.Error,OSError):
        pass
    return result


def paper_status(con, user_id, now=None):
    now = now or datetime.now(timezone.utc)
    result = dict(status='unavailable', epoch=None, orders=None, exits=None,
                  completed=None, worker='unknown', last_order=None)
    try:
        row = con.execute("SELECT started_at FROM user_book WHERE user_id=? AND market='IN'", (user_id,)).fetchone()
        if not row:
            return result
        epoch = row[0]
        rows = con.execute('SELECT s.status,COUNT(*) FROM paper_order_intents i '
            'JOIN paper_order_state s ON s.order_id=i.id WHERE i.user_id=? AND i.epoch=? '
            "AND json_extract(i.payload,'$.plan.market')='IN' "
            "AND COALESCE(json_extract(i.payload,'$.plan.side'),'BUY')='BUY' GROUP BY s.status", (user_id,epoch)).fetchall()
        exits = con.execute("SELECT COUNT(*) FROM paper_order_intents i JOIN paper_order_state s ON s.order_id=i.id "
            "WHERE i.user_id=? AND i.epoch=? AND json_extract(i.payload,'$.plan.side')='SELL' AND s.status='pending'",
            (user_id,epoch)).fetchone()[0]
        completed = con.execute("SELECT COUNT(*) FROM user_trades WHERE user_id=? AND market='IN' AND book_epoch=?",
                                (user_id,epoch)).fetchone()[0]
        latest = con.execute('SELECT i.payload,i.submitted_at,s.status,s.reason FROM paper_order_intents i '
            'JOIN paper_order_state s ON s.order_id=i.id WHERE i.user_id=? AND i.epoch=? '
            'ORDER BY i.submitted_at DESC,i.id DESC LIMIT 1',(user_id,epoch)).fetchone()
        worker = con.execute("SELECT expires FROM worker_leases WHERE name='paper'").fetchone()
        result.update(status='ok', epoch=epoch, orders=dict(rows), exits=exits, completed=completed,
                      worker='running' if worker and worker[0]>now.timestamp() else 'not observed',
                      worker_valid_until=datetime.fromtimestamp(worker[0],timezone.utc).isoformat() if worker else None)
        if latest:
            plan = json.loads(latest[0])['plan']
            result['last_order'] = dict(symbol=plan['symbol'], side=plan.get('side','BUY'), at=latest[1], status=latest[2], reason=latest[3])
        return result
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return result  # Missing schema/invalid state must not look like zero activity.
