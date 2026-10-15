"""Atomic import of reviewed exchange/broker evidence, including exclusions.

Raw exchange layouts change. A versioned normalization manifest pins the raw
bytes, their actual observation time, and every rule's source. Unknown data is
quarantined, not substituted with a guessed lot/tick/holiday/settlement rule.
No scrape confers redistribution rights or enables an execution route.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .account_safety import atomic
from . import instrument_catalog as catalogue,execution_contracts as contracts

OFFICIAL_HOSTS={'nseindia.com','www.nseindia.com','nsearchives.nseindia.com','archives.nseindia.com',
                'bseindia.com','www.bseindia.com','assets.upstox.com','upstox.com',
                'smartapi.angelone.in','margincalculator.angelone.in','angelone.in','www.angelone.in',
                'mcxindia.com','www.mcxindia.com'}


def ensure_schema(con):
    catalogue.ensure_schema(con);contracts.ensure_schema(con)
    con.execute('''CREATE TABLE IF NOT EXISTS catalogue_imports(
      digest TEXT PRIMARY KEY,observed_at TEXT NOT NULL,payload TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS catalogue_quarantine(
      import_digest TEXT NOT NULL,identity TEXT NOT NULL,reason TEXT NOT NULL,
      PRIMARY KEY(import_digest,identity))''')
    for table in ('catalogue_imports','catalogue_quarantine'):
        for action in ('UPDATE','DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable catalogue evidence'); END")


def import_bundle(con,bundle,root,*,now=None):
    now=now or datetime.now(timezone.utc);root=Path(root).resolve()
    if not isinstance(bundle,dict) or bundle.get('schema')!='openstocks-contract-evidence-v1' or \
            not bundle.get('normalizer_version') or not bundle.get('review_reference'):
        raise ValueError('Versioned and reviewed source normalization required')
    artifacts={}
    for item in bundle.get('artifacts',[]):
        if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] in artifacts:
            raise ValueError('Unique source artifact identity required')
        uri=urlsplit(item.get('source',''))
        if uri.scheme!='https' or uri.hostname not in OFFICIAL_HOSTS or uri.username or uri.password:
            raise ValueError('Official source required; licensed sources need a separately reviewed connector')
        relative=Path(item.get('path',''))
        path=(root/relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or path==root or root not in path.parents or \
                not path.is_file() or (root/relative).is_symlink():raise ValueError('Unsafe source archive path')
        observed=contracts._moment(item.get('observed_at'))
        if observed>now or not item.get('rights_reference'):raise ValueError('Dated source and rights classification required')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item.get('sha256'):raise ValueError('Source archive digest mismatch')
        artifacts[item['id']]=item
    if not artifacts:raise ValueError('Archived source evidence required')
    observed=contracts._moment(bundle['observed_at'])
    if observed>now:raise ValueError('Future bundle observation')
    text=json.dumps(bundle,sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256(text.encode()).hexdigest()
    rows=bundle.get('instruments');sessions=bundle.get('sessions')
    if not isinstance(rows,list) or not rows or not isinstance(sessions,list):raise ValueError('Contracts and sourced sessions required')
    by_provider={};reviewed=[];quarantine=[];identities=set()
    def source(record):
        refs=record.get('source_artifacts')
        if not isinstance(refs,list) or not refs or any(r not in artifacts for r in refs):raise ValueError('Missing row source attribution')
        at=contracts._moment(record['observed_at'])
        if at>observed or any(contracts._moment(artifacts[r]['observed_at'])>at for r in refs):
            raise ValueError('Normalization cannot predate its raw evidence or postdate the bundle')
        return json.dumps([dict(id=r,sha256=artifacts[r]['sha256'],source=artifacts[r]['source']) for r in refs],sort_keys=True)
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Malformed contract row')
        attribution=source(row)
        spec=catalogue.Instrument(**row['contract'])
        if spec.id in identities:raise ValueError('Duplicate canonical contract')
        identities.add(spec.id)
        aliases=row.get('aliases')
        if not isinstance(aliases,dict) or not aliases or any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in aliases.items()):
            raise ValueError('Sourced broker aliases required')
        for provider,key in aliases.items():by_provider.setdefault(provider,[]).append((spec,key))
        rules=row.get('rules')
        if rules is None:quarantine.append((spec.id,'No reviewed execution rules; discovery only'));continue
        reviewed.append((spec.id,rules,attribution,row))
    ensure_schema(con)
    with atomic(con):
        previous=con.execute('SELECT payload FROM catalogue_imports WHERE digest=?',(digest,)).fetchone()
        if previous:return json.loads(previous[0])
        snapshots={provider:catalogue.import_snapshot(con,items,provider=provider,source='reviewed-bundle:'+digest,
                   source_day=bundle['source_day'],observed_at=bundle['observed_at'],now=now) for provider,items in by_provider.items()}
        for identity,rules,attribution,row in reviewed:
            contracts.record(con,'rules',identity,rules,source=attribution,observed_at=row['observed_at'],
                             effective_from=row['effective_from'],effective_until=row['effective_until'],now=now)
        for session in sessions:
            contracts.record(con,'session',session['calendar'],session['payload'],source=source(session),observed_at=session['observed_at'],
                             effective_from=session['effective_from'],effective_until=session['effective_until'],now=now)
        result=dict(digest=digest,source_day=bundle['source_day'],snapshots=snapshots,contracts=len(rows),rules=len(reviewed),
                    quarantined=len(quarantine),execution_enabled=False,commercial_rights_verified=False)
        con.execute('INSERT INTO catalogue_imports VALUES(?,?,?)',(digest,observed.isoformat(),json.dumps(result,sort_keys=True)))
        con.executemany('INSERT INTO catalogue_quarantine VALUES(?,?,?)',[(digest,*r) for r in quarantine])
        return result


def quarantine_legacy(con,catalogue_con,*,now=None):
    """Append identity assessments without rewriting ownership, capital or P&L.

    Ambiguous historical symbols stay quarantined. Current masters cannot
    establish which series/expiry an old position represented.
    """
    now=now or datetime.now(timezone.utc)
    con.execute('''CREATE TABLE IF NOT EXISTS historical_identity_reviews(
      scope TEXT NOT NULL,row_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
      observed_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,
      candidates TEXT NOT NULL,PRIMARY KEY(scope,row_id,observed_at))''')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_historical_identity_{action.lower()} BEFORE {action} "
                    "ON historical_identity_reviews BEGIN SELECT RAISE(ABORT,'immutable historical identity review'); END")
    with atomic(con):
        counts={'bound':0,'quarantined':0}
        for scope,table,owner in (('personal','user_positions','user_id'),('house','v2_positions','0')):
            cols={r[1] for r in con.execute('PRAGMA table_info('+table+')')}
            if not cols:continue
            query=f'SELECT id,{owner},market,symbol'+(',instrument_id' if 'instrument_id' in cols else ',NULL')+' FROM '+table
            for rid,uid,market,symbol,existing in con.execute(query).fetchall():
                candidates=[]
                try:
                    spec,key=catalogue.resolve(catalogue_con,instrument_id=existing,now=now) if existing else (None,None)
                    bound=bool(spec and market=='IN' and spec.symbol==symbol and spec.venue=='NSE')
                except ValueError:bound=False
                if not bound:
                    candidates=[r[0] for r in catalogue_con.execute('SELECT DISTINCT instrument_id FROM instrument_contracts WHERE symbol=?',(symbol,))]
                status='bound' if bound else 'quarantined';counts[status]+=1
                con.execute('INSERT OR IGNORE INTO historical_identity_reviews VALUES(?,?,?,?,?,?,?)',
                    (scope,rid,uid,now.isoformat(),status,'Persisted canonical identity verified' if bound else
                     'Historical ownership/contract cannot be inferred from a current display symbol',json.dumps(candidates)))
        return counts
