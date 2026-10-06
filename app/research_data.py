"""Point-in-time research facts; missing coverage never becomes a quality score.

Membership, fundamentals, prices, delivery and news share publication and
observation dates. This is an evidence store, not a model promotion service.
Adjusted bars must carry the provider's action-methodology reference; an
unadjusted series cannot silently enter an after-cost validation cohort.
"""
from datetime import datetime,timezone
import hashlib
import json
from .account_safety import atomic
from .execution_contracts import _moment

KINDS={'membership','fundamentals','bar','delivery','news','benchmark','corporate-action'}


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS research_facts(
      id TEXT PRIMARY KEY,instrument_id TEXT NOT NULL,kind TEXT NOT NULL,
      fact_key TEXT NOT NULL,effective_at TEXT NOT NULL,published_at TEXT NOT NULL,
      observed_at TEXT NOT NULL,source TEXT NOT NULL,rights_reference TEXT NOT NULL,payload TEXT NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS ix_research_fact_time ON research_facts(instrument_id,kind,effective_at,published_at,observed_at)')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_research_fact_{action.lower()} BEFORE {action} ON research_facts "
                    "BEGIN SELECT RAISE(ABORT,'immutable point-in-time research'); END")


def record(con,facts,*,now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(facts,list) or not facts or now.tzinfo is None:raise ValueError('Dated research batch required')
    normalized=[]
    for row in facts:
        if not isinstance(row,dict) or row.get('kind') not in KINDS or \
                not isinstance(row.get('instrument_id'),str) or not row['instrument_id'].startswith('ins_') or \
                not all(isinstance(row.get(k),str) and row[k].strip() for k in ('fact_key','source','rights_reference')) or \
                not isinstance(row.get('payload'),dict):raise ValueError('Canonical, sourced research identity required')
        effective,published,observed=map(_moment,(row['effective_at'],row['published_at'],row['observed_at']))
        if published>observed or observed>now:raise ValueError('Research chronology invalid')
        payload=row['payload']
        if row['kind'] in {'bar','benchmark'}:
            required={'open','high','low','close','volume','currency','adjustment_methodology','action_coverage_reference'}
            if not required<=set(payload) or not payload['adjustment_methodology'] or not payload['action_coverage_reference']:
                raise ValueError('Action-aware OHLCV methodology and coverage required')
            from decimal import Decimal
            prices=[Decimal(str(payload[k])) for k in ('open','high','low','close')]
            if not all(v.is_finite() and v>0 for v in prices) or not prices[2]<=min(prices[0],prices[3])<=max(prices[0],prices[3])<=prices[1] or \
                    type(payload['volume']) is not int or payload['volume']<0:raise ValueError('Invalid research bar')
            if effective>published:raise ValueError('A completed bar cannot be published before it closes')
        elif row['kind']=='membership':
            if not payload.get('index') or type(payload.get('member')) is not bool:raise ValueError('Explicit point-in-time membership required')
        elif row['kind']=='fundamentals':
            required={'roe','debt_to_equity','earnings_periods','filing_reference','accounting_period_end'}
            if not required<=set(payload) or not payload['filing_reference'] or not isinstance(payload['earnings_periods'],list) or \
                    len(payload['earnings_periods'])<4:raise ValueError('Dated quality metrics and earnings history required')
            from decimal import Decimal,InvalidOperation
            try:
                metrics=[Decimal(str(payload[k])) for k in ('roe','debt_to_equity')]
                if not all(v.is_finite() for v in metrics) or metrics[1]<0:raise ValueError('Invalid quality metric')
            except InvalidOperation as exc:raise ValueError('Invalid quality metric') from exc
        text=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)
        identity=hashlib.sha256(text.encode()).hexdigest()
        normalized.append((identity,row['instrument_id'],row['kind'],row['fact_key'],effective.isoformat(),
                           published.isoformat(),observed.isoformat(),row['source'],row['rights_reference'],json.dumps(payload,sort_keys=True,allow_nan=False)))
    ensure_schema(con)
    with atomic(con):con.executemany('INSERT OR IGNORE INTO research_facts VALUES(?,?,?,?,?,?,?,?,?,?)',normalized)
    return [r[0] for r in normalized]


def known(con,instrument_id,kind,decision_at):
    at=_moment(decision_at)
    if kind not in KINDS:raise ValueError('Unknown research fact kind')
    rows=con.execute('SELECT id,fact_key,effective_at,published_at,observed_at,source,rights_reference,payload FROM research_facts '
                     'WHERE instrument_id=? AND kind=? AND julianday(effective_at)<=julianday(?) '
                     'AND julianday(published_at)<=julianday(?) AND julianday(observed_at)<=julianday(?) '
                     'ORDER BY julianday(effective_at),julianday(observed_at)',(instrument_id,kind,at.isoformat(),at.isoformat(),at.isoformat())).fetchall()
    # Corrections known later supersede a fact for future decisions only.
    latest={}
    for row in rows:
        previous=latest.get(row[1])
        if previous and row[4]==previous[4] and row[7]!=previous[7]:raise ValueError('Conflicting contemporaneous research facts')
        latest[row[1]]=row
    fields=('id','fact_key','effective_at','published_at','observed_at','source','rights_reference')
    return [dict(zip(fields,row[:7]),payload=json.loads(row[7])) for row in latest.values()]


def coverage(con,instrument_ids,decision_at):
    result=[]
    for identity in instrument_ids:
        groups={kind:known(con,identity,kind,decision_at) for kind in ('membership','fundamentals','bar','delivery','news')}
        result.append(dict(instrument_id=identity,counts={k:len(v) for k,v in groups.items()},
                           research_complete=bool(groups['membership'] and groups['fundamentals'] and groups['bar']),
                           execution_approved=False))
    return dict(decision_at=decision_at,instruments=result,model_promoted=False,
                note='Coverage is not independent after-cost strategy validation')
