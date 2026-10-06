"""Append-only owned journal observations; not a broker-fee/cash ledger.

Only normalized fields from an already owned intent are retained. Duplicate
callbacks deduplicate by content. Exchange trade IDs/fees remain a separate
reconciliation contract, never inferred from acceptance or status snapshots.
"""
import hashlib
import json
from datetime import datetime,timezone


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS live_execution_events(
      id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,order_id INTEGER NOT NULL,
      kind TEXT NOT NULL,payload TEXT NOT NULL,observed_at TEXT NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS ix_live_execution_events_owned ON live_execution_events(user_id,order_id,observed_at)')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_live_execution_{action.lower()} BEFORE {action} ON live_execution_events "
                    "BEGIN SELECT RAISE(ABORT,'immutable execution observation'); END")


def record(con,user_id,order_id,kind):
    row=con.execute('SELECT market,symbol,instrument_key,side,qty,product,status,intent_key,broker_order_id,'
                    'filled_qty,average_price,origin_position_id,semantic_key FROM v2_live_orders WHERE id=? AND user_id=?',
                    (order_id,user_id)).fetchone()
    if not row:raise ValueError('Owned order required for execution observation')
    payload=dict(zip(('market','symbol','broker_alias','side','requested_qty','product','status','intent_key','broker_order_id',
                      'cumulative_filled_qty','cumulative_average_price','origin_position_id','semantic_key'),row))
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
    identity=hashlib.sha256(json.dumps([user_id,order_id,kind,encoded]).encode()).hexdigest()
    con.execute('INSERT OR IGNORE INTO live_execution_events VALUES(?,?,?,?,?,?)',
                (identity,user_id,order_id,kind,encoded,datetime.now(timezone.utc).isoformat()))
    return identity


def report(con,user_id,limit=50):
    limit=max(1,min(int(limit),100))
    rows=con.execute('SELECT id,order_id,kind,payload,observed_at FROM live_execution_events WHERE user_id=? '
                     'ORDER BY observed_at DESC,id DESC LIMIT ?',(user_id,limit)).fetchall()
    return dict(scope='owned-journal-observations',fee_ledger_certified=False,
                rows=[dict(id=r[0],order_id=r[1],kind=r[2],order=json.loads(r[3]),observed_at=r[4]) for r in rows])
