"""Rehearse startup migrations on private copies, never on a running book.

Each SQLite backup is consistent individually. This is NOT a coordinated
deployment backup, writer-quiescence proof, release authorization or cutover.
All output remains beneath RESTORE_DISABLED. Historical account/book values
are checked by streaming fingerprints; the report contains no row contents.
"""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time


ACCOUNT_TABLES = {'users', 'auth_sessions', 'subscriptions', 'subscription_requests',
                  'subscription_receipts', 'settings', 'runtime_settings',
                  'user_settings', 'user_preferences'}
MAX_DATABASE_BYTES = 2 * 1024**3


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)


def _tables(con):
    return sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def fingerprint(con, table, columns):
    """Compare the multiset of all original column values, including duplicates."""
    fields = ','.join(map(_identifier, columns))
    digest = hashlib.sha256(); count = 0
    for row in con.execute('SELECT ' + fields + ' FROM ' + _identifier(table) + ' ORDER BY ' + fields):
        values = [dict(blob=v.hex()) if isinstance(v, bytes) else v for v in row]
        encoded = json.dumps(values, separators=(',', ':'), ensure_ascii=True).encode()
        digest.update(len(encoded).to_bytes(8, 'big')); digest.update(encoded); count += 1
    return dict(rows=count, sha256=digest.hexdigest())


def preservation_snapshot(con, tables=None):
    result = {}
    for table in _tables(con):
        if tables is not None and table not in tables:
            continue
        columns = [r[1] for r in con.execute('PRAGMA table_info(' + _identifier(table) + ')')]
        result[table] = dict(columns=columns, **fingerprint(con, table, columns))
    return result


def verify_preservation(con, before):
    for table, previous in before.items():
        current = fingerprint(con, table, previous['columns'])
        if current != {k: previous[k] for k in ('rows', 'sha256')}:
            raise ValueError('Migration changed pre-existing values: ' + table)
    if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Migrated copy failed SQLite integrity')


def snapshot_database(source, destination, *, timeout_seconds=120):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_DATABASE_BYTES:
        raise ValueError('Missing, linked or oversized source database')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
    incoming = _connect(source); outgoing = sqlite3.connect(destination)
    deadline = time.monotonic() + timeout_seconds
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('Consistent database snapshot exceeded time bound')
    try:
        incoming.backup(outgoing, pages=256, progress=progress, sleep=.05)
        if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot failed SQLite integrity')
    finally:
        incoming.close(); outgoing.close()


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, sort_keys=True, indent=2); handle.write('\n')
        handle.flush(); os.fsync(handle.fileno())


def rehearse(paper, accounts, protocol, destination, *, user_id, migrate_paper, migrate_accounts):
    """Migrate only copied files; injected callbacks never receive source paths."""
    sources = [Path(v) for v in (paper, accounts, protocol)]
    paper, accounts, protocol = sources
    destination = Path(destination)
    if type(user_id) is not int or user_id < 1 or destination.exists() or destination.is_symlink():
        raise ValueError('Owned user and a new rehearsal destination required')
    if any(p.is_symlink() or not p.is_file() for p in sources) or len({p.resolve() for p in sources}) != 3:
        raise ValueError('Three distinct regular source files required')
    if any(destination.resolve() == p.resolve() or destination.resolve() in p.resolve().parents for p in sources):
        raise ValueError('Rehearsal destination must not contain the source runtime')
    if protocol.stat().st_size > 1024**2:
        raise ValueError('Forward protocol exceeds limit')
    raw_protocol = protocol.read_bytes()
    if not isinstance(json.loads(raw_protocol), dict):
        raise ValueError('Forward protocol must be an object')
    if shutil.disk_usage(destination.parent).free < sum(p.stat().st_size for p in sources)*2 + 50*1024**2:
        raise ValueError('Insufficient free space for snapshots and migrated copies')
    destination.mkdir(mode=0o700)
    _write(destination / 'RESTORE_DISABLED', dict(reason='Isolated startup-migration rehearsal; no cutover authorized'))
    backup = destination / 'original'; backup.mkdir(mode=0o700)
    upgraded = destination / 'candidate'; upgraded.mkdir(mode=0o700)
    for name, source in (('paper.db', paper), ('accounts.db', accounts)):
        snapshot_database(source, backup / name)
        shutil.copyfile(backup / name, upgraded / name); os.chmod(upgraded / name, 0o600)
    fd = os.open(backup / 'forward-protocol.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle: handle.write(raw_protocol)
    originals = {}
    for name, selected in (('paper.db', None), ('accounts.db', ACCOUNT_TABLES)):
        with closing(_connect(backup / name)) as con:
            originals[name] = preservation_snapshot(con, selected)
            if name == 'accounts.db' and 'users' not in originals[name]:
                raise ValueError('Account schema unavailable')
            if name == 'paper.db':
                if 'user_book' not in originals[name]: raise ValueError('Owned book unavailable')
                row = con.execute("SELECT budget,started_at FROM user_book WHERE user_id=? AND market='IN'", (user_id,)).fetchone()
                if row is None: raise ValueError('Owned paper epoch unavailable')
                capital, epoch = row
    try:
        migrate_accounts(upgraded / 'accounts.db')
        migrate_paper(upgraded / 'paper.db')
        for name in originals:
            with closing(_connect(upgraded / name)) as con: verify_preservation(con, originals[name])
        # Verify original snapshots were not changed by migration callbacks.
        for name in originals:
            with closing(_connect(backup / name)) as con: verify_preservation(con, originals[name])
        if protocol.read_bytes() != raw_protocol:
            raise ValueError('Forward protocol changed during rehearsal')
        from . import books
        with closing(_connect(upgraded / 'paper.db')) as con:
            # Unknown held marks are never reported as reliable equity.
            positions = books.positions(con, user_id, 'IN')
            summary = dict(capital=books.budget_of(con,user_id,'IN'), cash=books.cash(con,user_id,'IN'),
                           epoch=books.current_epoch(con,user_id,'IN'), open_positions=len(positions))
            summary['equity'] = summary['cash'] if not positions else None
        if (summary['capital'],summary['epoch']) != (capital,epoch):
            raise ValueError('Owned capital or epoch changed')
        result = dict(status='passed',checked_at=datetime.now(timezone.utc).isoformat(),
            paper=summary, protocol_sha256=hashlib.sha256(raw_protocol).hexdigest(),
            preserved_tables={name:len(rows) for name,rows in originals.items()},
            account_scope=sorted(ACCOUNT_TABLES), paper_scope='all pre-existing tables/columns/rows',
            production_modified=False, real_orders=0, execution_disabled=True,
            coordinated_backup=False, deployment_authorized=False, release_certified=False)
        _write(destination / 'rehearsal.json', result)
        return result
    except BaseException as exc:
        _write(destination / 'rehearsal.json', dict(status='failed',error_type=type(exc).__name__,execution_disabled=True))
        raise
