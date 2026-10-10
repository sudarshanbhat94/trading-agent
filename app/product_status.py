"""Owned, read-only paper lifecycle status for the product workspace."""
import json
import sqlite3
from datetime import datetime, timezone


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
