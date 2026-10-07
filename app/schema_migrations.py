"""Transactional startup migrations, including legacy executescript callers.

SQLite executescript normally commits before executing DDL. The restricted
proxy below deliberately executes complete statements inside the caller's
transaction and defers nested commit calls. It is only used during schema
setup; it must never wrap trading or external I/O.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from .account_safety import atomic


class SchemaConnection:
    def __init__(self,connection):self.connection=connection
    def __getattr__(self,name):return getattr(self.connection,name)
    def commit(self):pass  # Only the outer migration owns commit.
    def rollback(self):raise RuntimeError('Nested schema rollback requires outer migration failure')
    def executescript(self,script):
        pending='';cursor=None
        for character in script:
            pending+=character
            if character==';' and sqlite3.complete_statement(pending):
                cursor=self.connection.execute(pending);pending=''
        if pending.strip():cursor=self.connection.execute(pending)
        return cursor


def apply(connection,version,contract,migrate,verify):
    """Versioned migration + validation + receipt are one serialized commit."""
    if not isinstance(version,str) or not version or not isinstance(contract,dict):
        raise ValueError('Named version and explicit schema contract required')
    checksum=hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with atomic(connection):
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migration_receipts(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)')
        connection.execute("CREATE TRIGGER IF NOT EXISTS schema_receipts_no_update BEFORE UPDATE ON schema_migration_receipts BEGIN SELECT RAISE(ABORT,'migration receipts immutable'); END")
        connection.execute("CREATE TRIGGER IF NOT EXISTS schema_receipts_no_delete BEFORE DELETE ON schema_migration_receipts BEGIN SELECT RAISE(ABORT,'migration receipts immutable'); END")
        old=connection.execute('SELECT checksum FROM schema_migration_receipts WHERE version=?',(version,)).fetchone()
        if old and old[0]!=checksum:raise RuntimeError('Migration contract changed without a new version')
        proxy=SchemaConnection(connection)
        migrate(proxy)
        verify(proxy)
        if not old:
            connection.execute('INSERT INTO schema_migration_receipts VALUES(?,?,?)',(version,checksum,datetime.now(timezone.utc).isoformat()))


def validate_trading(connection):
    """Refuse torn accounting/ownership structures, not just a version label."""
    required={
        'user_positions':{'user_id','book_epoch','instrument_id','plan_id','model_version','exit_policy','shares'},
        'user_trades':{'user_id','book_epoch','instrument_id','plan_id','model_version','pnl'},
        'v2_live_orders':{'user_id','intent_key','semantic_key','request_fingerprint','filled_qty','average_price'},
        'broker_ledger_events':{'user_id','event_key','fingerprint'},
        'entry_contract_records':{'scope','user_id','position_id','payload'},
        'approved_execution_plans':{'id','user_id','epoch','payload'},
        'manual_plan_bindings':{'user_id','epoch','request_key','fingerprint','plan_id'},
        'paper_order_intents':{'id','user_id','epoch','plan_id','payload'},
        'paper_order_state':{'order_id','status','result'},
        'paper_order_events':{'order_id','kind','payload','observed_at'},
    }
    for table,columns in required.items():
        missing=columns-{r[1] for r in connection.execute('PRAGMA table_info('+table+')')}
        if missing:raise RuntimeError('Required migration contract incomplete: '+table+' '+str(sorted(missing)))
    for table in ('broker_ledger_events','broker_ledger_postings','entry_contract_records','approved_execution_plans','approved_execution_events','manual_plan_bindings','paper_order_intents','paper_order_events'):
        triggers=[(r[0] or '').upper() for r in connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=?',('trigger',table))]
        if any(not any(('BEFORE '+operation) in sql and 'RAISE(ABORT' in sql for sql in triggers) for operation in ('UPDATE','DELETE')):
            raise RuntimeError('Required immutable table protection unavailable: '+table)
    if connection.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('Migration foreign-key integrity failed')


TRADING_CONTRACT={'version':3,'scope':'canonical-entry-actual-broker-ledger',
                  'preserves':['capital','epoch','positions','realised-history'],
                  'required':['owned-canonical-provenance','append-only-actual-fill-ledger','approved-paper-plans','immutable-manual-plan-binding']}


def validate_accounts(connection):
    for table,fields in {'users':{'id','username','active','account_plan'},
                         'auth_sessions':{'user_id','session_hash','revoked_at'},
                         'auth_login_reservations':{'id','key_hash','started_at','outcome'},
                         'auth_login_locks':{'key_hash','locked_until'},
                         'market_execution_quotes':{'instrument_key','snapshot_at','payload'},
                         'subscription_receipts':{'user_id','request_id','payment_reference','amount_minor'}}.items():
        missing=fields-{r[1] for r in connection.execute('PRAGMA table_info('+table+')')}
        if missing:raise RuntimeError('Account migration incomplete: '+table+' '+str(sorted(missing)))
    if connection.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('Account foreign-key integrity failed')
