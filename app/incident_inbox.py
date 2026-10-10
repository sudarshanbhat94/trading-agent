"""Owned, durable acknowledgement of execution warnings; never risk clearance."""
import hashlib
import json
import time
from .account_safety import atomic


def ensure_schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS execution_incident_acknowledgements(user_id INTEGER NOT NULL,incident_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,acknowledged_at REAL NOT NULL,PRIMARY KEY(user_id,incident_id,fingerprint))')
    for action in ('UPDATE','DELETE'):
        con.execute('CREATE TRIGGER IF NOT EXISTS incident_ack_no_'+action.lower()+' BEFORE '+action+' ON execution_incident_acknowledgements BEGIN SELECT RAISE(ABORT,\'immutable incident acknowledgement\'); END')


def _fingerprint(row):return hashlib.sha256(json.dumps(list(row),separators=(',',':'),allow_nan=False).encode()).hexdigest()


def report(con,uid):
    rows=[]
    for row in con.execute('SELECT id,code,reference,detail,opened_at FROM execution_incidents WHERE user_id=? AND resolved_at IS NULL ORDER BY opened_at DESC,id DESC LIMIT 50',(uid,)):
        fingerprint=_fingerprint(row)
        ack=con.execute('SELECT acknowledged_at FROM execution_incident_acknowledgements WHERE user_id=? AND incident_id=? AND fingerprint=?',(uid,row[0],fingerprint)).fetchone()
        rows.append(dict(id=row[0],code=row[1],reference=row[2],detail=row[3],opened_at=row[4],fingerprint=fingerprint,acknowledged_at=ack[0] if ack else None))
    return rows


def acknowledge(con,uid,incident_id,fingerprint):
    if type(uid) is not int or uid<1 or type(incident_id) is not int or incident_id<1 or not isinstance(fingerprint,str):raise ValueError('Owned incident identity required')
    with atomic(con):
        row=con.execute('SELECT id,code,reference,detail,opened_at FROM execution_incidents WHERE id=? AND user_id=?',(incident_id,uid)).fetchone()
        if not row or fingerprint!=_fingerprint(row):raise ValueError('Incident unavailable or changed; review current evidence')
        con.execute('INSERT OR IGNORE INTO execution_incident_acknowledgements VALUES(?,?,?,?)',(uid,incident_id,fingerprint,time.time()))
    return dict(acknowledged=True,risk_cleared=False,incident_id=incident_id)
