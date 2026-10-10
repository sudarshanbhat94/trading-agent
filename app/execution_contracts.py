"""Decision-time contract rules and exchange sessions; unknown means refusal.

Rules supplement discovery masters with separately sourced units, settlement,
circuits, restrictions and reviewed corporate actions. They are append-only;
a later correction cannot rewrite what an earlier decision could know.
"""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import json

from .account_safety import atomic
from .instrument_catalog import InstrumentError,resolve,_positive_decimal


def _moment(value):
    try:
        moment=datetime.fromisoformat(value.replace('Z','+00:00'))
        if moment.tzinfo is None:raise ValueError()
        return moment
    except (ValueError,TypeError,AttributeError) as exc:
        raise InstrumentError('aware source/effective timestamp required') from exc


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS execution_contract_evidence(
      id TEXT PRIMARY KEY,kind TEXT NOT NULL,identity TEXT NOT NULL,observed_at TEXT NOT NULL,
      effective_from TEXT NOT NULL,effective_until TEXT NOT NULL,source TEXT NOT NULL,
      payload TEXT NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS ix_execution_contract_evidence ON execution_contract_evidence(kind,identity,observed_at)')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_contract_{action.lower()} BEFORE {action} "
                    "ON execution_contract_evidence BEGIN SELECT RAISE(ABORT,'immutable contract evidence'); END")


def record(con,kind,identity,payload,*,source,observed_at,effective_from,effective_until,now=None):
    now=now or datetime.now(timezone.utc)
    observed,start,end=map(_moment,(observed_at,effective_from,effective_until))
    if kind not in {'rules','session'} or not identity or not isinstance(source,str) or not source.strip() or \
            observed>now or start>=end or not isinstance(payload,dict):
        raise InstrumentError('invalid/unattributed contract evidence')
    if kind=='rules':
        for key in ('lot_size','freeze_quantity'):
            if type(payload.get(key)) is not int or payload[key]<1:raise InstrumentError('missing/invalid '+key)
        for key in ('tick_size','lower_circuit','upper_circuit'):_positive_decimal(payload.get(key),key)
        if _positive_decimal(payload['lower_circuit'],'lower circuit')>=_positive_decimal(payload['upper_circuit'],'upper circuit') or \
                payload['freeze_quantity']<payload['lot_size'] or not payload.get('settlement') or payload['settlement']=='UNKNOWN' or \
                not payload.get('calendar') or type(payload.get('banned')) is not bool or \
                type(payload.get('corporate_action_pending')) is not bool or \
                _moment(payload.get('actions_reviewed_at'))>observed:
            raise InstrumentError('incomplete restrictions, calendar or corporate-action evidence')
    else:
        if type(payload.get('open')) is not bool:raise InstrumentError('session status unknown')
        if payload['open'] and not start<=_moment(payload.get('opens_at'))<_moment(payload.get('closes_at'))<=end:
            raise InstrumentError('session times outside effective interval')
        if 'fresh_until' in payload and not observed < _moment(payload['fresh_until']) <= end:
            raise InstrumentError('invalid sourced session freshness window')
    text=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
    values=(kind,identity,observed.isoformat(),start.isoformat(),end.isoformat(),source,text)
    evidence_id=hashlib.sha256(json.dumps(values).encode()).hexdigest()
    ensure_schema(con)
    with atomic(con):
        con.execute('INSERT OR IGNORE INTO execution_contract_evidence VALUES(?,?,?,?,?,?,?,?)',(evidence_id,*values))
    return evidence_id


def _latest(con,kind,identity,now):
    # SQLite julianday rounds sub-millisecond source times. Use it only for a
    # padded candidate search, then compare exact aware timestamps in Python.
    before,after=now-timedelta(seconds=1),now+timedelta(seconds=1)
    rows=con.execute('SELECT id,observed_at,payload,effective_from,effective_until FROM execution_contract_evidence WHERE kind=? AND identity=? '
                     'AND julianday(observed_at)<=julianday(?) AND julianday(effective_from)<=julianday(?) '
                     'AND julianday(effective_until)>julianday(?) ORDER BY julianday(observed_at) DESC',
                     (kind,identity,after.isoformat(),after.isoformat(),before.isoformat())).fetchall()
    rows=[r for r in rows if _moment(r[1])<=now and _moment(r[3])<=now<_moment(r[4])]
    if not rows:raise InstrumentError('dated '+kind+' evidence unavailable')
    latest=max(_moment(r[1]) for r in rows);tied=[r for r in rows if _moment(r[1])==latest]
    if len({r[2] for r in tied})!=1:raise InstrumentError('conflicting '+kind+' evidence')
    if kind=='rules' and now-latest>timedelta(hours=25):raise InstrumentError('contract rules are stale')
    payload=json.loads(tied[0][2])
    if kind=='session' and 'fresh_until' in payload and now>=_moment(payload['fresh_until']):
        raise InstrumentError('sourced exchange status is stale')
    return tied[0][0],payload


def protection_contract(con,*,instrument_id,quantity,price,provider='upstox',now=None):
    """Dated protective-trigger rules; an after-hours trigger is not a fill.

    Do not require an open exchange session to establish a broker GTT. Actual
    exchange execution remains subject to broker/exchange restrictions.
    """
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise InstrumentError('aware decision time required')
    spec,key=resolve(con,instrument_id=instrument_id,provider=provider,now=now)
    rules_id,rules=_latest(con,'rules',spec.id,now)
    if rules['banned'] or rules['corporate_action_pending'] or now-_moment(rules['actions_reviewed_at'])>timedelta(hours=25):
        raise InstrumentError('restriction or corporate-action review blocks new orders')
    spec=replace(spec,lot_size=rules['lot_size'],tick_size=str(rules['tick_size']),
                 freeze_quantity=rules['freeze_quantity'],settlement=rules['settlement'])
    px=_positive_decimal(price,'order price')
    if not _positive_decimal(rules['lower_circuit'],'lower circuit')<=px<=_positive_decimal(rules['upper_circuit'],'upper circuit'):
        raise InstrumentError('reference price outside current circuit bounds')
    cutoff=_moment(rules['expiry_cutoff']) if spec.expiry and rules.get('expiry_cutoff') else None
    spec.validate_order(quantity,price,now=now,expiry_cutoff=cutoff)
    return spec,key,dict(rules_id=rules_id,observed_at=now.isoformat()),rules['calendar']


def order_contract(con,*,instrument_id,quantity,price,provider='upstox',now=None):
    now=now or datetime.now(timezone.utc)
    spec,key,evidence,calendar=protection_contract(con,instrument_id=instrument_id,quantity=quantity,price=price,provider=provider,now=now)
    session_id,session=_latest(con,'session',calendar,now)
    if not session['open'] or not _moment(session['opens_at'])<=now<_moment(session['closes_at']):
        raise InstrumentError('exchange session is closed')
    return spec,key,dict(evidence,session_id=session_id,calendar=calendar)
