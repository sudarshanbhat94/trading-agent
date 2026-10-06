"""Encrypted, integrity-checked recovery of a declared, quiesced deployment.

The caller must quiesce all writers before capture; SQLite snapshots alone do
not make several databases/files one transaction. Restoration only creates a
new private directory and never starts a worker or replaces production files.
Backup encryption and broker-vault keys must be escrowed separately.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

from cryptography.fernet import Fernet

KINDS = {'sqlite', 'file', 'protocol', 'broker-state'}
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 256
REQUIRED_ROLES = {'paper_book', 'accounts', 'market_data', 'idea_tracking',
                  'forward_protocol', 'runtime_config', 'permissions', 'instrument_catalogue'}


class RecoveryError(ValueError):
    pass


def private_key(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RecoveryError("Escrow key must be a private regular file")
    key = path.read_bytes().strip()
    Fernet(key)
    return key


def _name(value):
    if not isinstance(value, str):
        raise RecoveryError("Invalid recovery member name")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or \
            str(path) != value or value in {'manifest.json', 'RESTORE_DISABLED'}:
        raise RecoveryError("Unsafe or reserved recovery member name")
    return value


def _safe_payload(name, data, kind, key):
    # Escrow is independent of the archive. Refuse obvious key files even if
    # incorrectly declared as ordinary configuration; never print their data.
    if Path(name).suffix.lower() in {'.key', '.pem', '.p12', '.pfx'} or \
            key.strip() in data or b'PRIVATE KEY-----' in data or \
            re.search(rb'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=])', data):
        raise RecoveryError("Escrow/private key material is prohibited in recovery members")
    if kind == 'broker-state':
        envelope = json.loads(data)
        if not isinstance(envelope, dict) or envelope.get('format') != 'fernet-account-v1' or \
                not isinstance(envelope.get('ciphertext'), str):
            raise RecoveryError("Broker credentials must already be encrypted")
    elif kind == 'protocol':
        if not isinstance(json.loads(data), dict):
            raise RecoveryError("Invalid forward protocol")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _sqlite_snapshot(source, destination):
    source = Path(source)
    con = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    out = sqlite3.connect(destination)
    try:
        con.backup(out)
        if out.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RecoveryError("Database integrity check failed")
    finally:
        out.close()
        con.close()


def create(sources, destination, key, *, source_commit, quiescence_reference,
           require_complete=False):
    """Capture explicitly named DB/config/protocol/encrypted-state files.

    sources maps safe relative member names to {path, kind}; kind is sqlite,
    file, protocol or broker-state. Credentials must already be encrypted.
    """
    if not isinstance(source_commit,str) or not re.fullmatch('[a-f0-9]{40}', source_commit) or \
            not isinstance(quiescence_reference,str) or not quiescence_reference.strip():
        raise RecoveryError("Exact build and reviewed writer-quiescence evidence required")
    if not isinstance(sources,dict) or not 0 < len(sources) < MAX_MEMBERS:
        raise RecoveryError("Recovery sources are required")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("Backup destination already exists")
    cipher = Fernet(key)
    payloads, records, roles = {}, {}, set()
    with tempfile.TemporaryDirectory() as tmp:
        for name, spec in sources.items():
            _name(name)
            if not isinstance(spec,dict):raise RecoveryError("Invalid recovery source declaration")
            source = Path(spec['path'])
            kind = spec.get('kind')
            declared_roles=spec.get('roles',[])
            if not isinstance(declared_roles,list) or any(not isinstance(r,str) for r in declared_roles):
                raise RecoveryError("Invalid source roles")
            roles.update(declared_roles)
            if source.is_symlink() or not source.is_file() or kind not in KINDS or \
                    source.stat().st_size > MAX_BUNDLE_BYTES:
                raise RecoveryError("Missing, linked or unsupported recovery source")
            # Detect source changes during this capture. This complements, but
            # never substitutes for, external writer quiescence.
            before = source.read_bytes()
            if kind == 'sqlite':
                target = Path(tmp)/str(len(records))
                _sqlite_snapshot(source, target)
                data = target.read_bytes()
            else:
                data = before
            if source.read_bytes() != before:
                raise RecoveryError("Source changed during recovery capture")
            _safe_payload(name,data,kind,key)
            payloads[name] = data
            records[name] = dict(kind=kind,sha256=_digest(data),bytes=len(data),roles=declared_roles)
            if sum(len(v) for v in payloads.values()) > MAX_BUNDLE_BYTES:
                raise RecoveryError("Recovery sources exceed the bundle size limit")
    missing_roles=sorted(REQUIRED_ROLES-roles)
    if require_complete and missing_roles:
        raise RecoveryError("Recovery declaration lacks required deployment roles")
    manifest = dict(format='openstocks-recovery-v1',source_commit=source_commit,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    quiescence_reference=quiescence_reference,files=records,
                    automatic_execution=False,broker_key_included=False,
                    required_roles_covered=not missing_roles,missing_roles=missing_roles)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        for name, data in dict(payloads,**{'manifest.json':json.dumps(manifest,sort_keys=True).encode()}).items():
            info = tarfile.TarInfo(name); info.size=len(data); info.mode=0o600
            archive.addfile(info,io.BytesIO(data))
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd, staged = tempfile.mkstemp(dir=destination.parent,prefix='.backup-')
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(cipher.encrypt(stream.getvalue()));handle.flush();os.fsync(handle.fileno())
        os.link(staged,destination)  # never replace another writer's backup
    finally:
        os.unlink(staged)
    return dict(files=len(records),source_commit=source_commit,encrypted=True,
                required_roles_covered=not missing_roles,missing_roles=missing_roles,
                restore_disabled=True,sha256=_digest(destination.read_bytes()))


def restore(bundle, destination, key, *, broker_escrow_key=None):
    """Validate the whole archive, then publish a new disabled restore tree."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("Restore requires a new destination")
    try:
        if Path(bundle).stat().st_size > MAX_BUNDLE_BYTES * 2:
            raise RecoveryError("Recovery bundle exceeds the size limit")
        plain = Fernet(key).decrypt(Path(bundle).read_bytes())
        with tarfile.open(fileobj=io.BytesIO(plain),mode='r:') as archive:
            members = archive.getmembers()
            names = [m.name for m in members]
            if not 0 < len(members) <= MAX_MEMBERS or len(names)!=len(set(names)) or names.count('manifest.json')!=1:
                raise RecoveryError("Duplicate or missing recovery manifest")
            if any(not m.isfile() or m.size < 0 for m in members) or sum(m.size for m in members)>MAX_BUNDLE_BYTES:
                raise RecoveryError("Links and non-files are prohibited")
            payloads={m.name:archive.extractfile(m).read() for m in members}
        manifest=json.loads(payloads.pop('manifest.json'))
        if not isinstance(manifest,dict) or manifest.get('format')!='openstocks-recovery-v1' or \
                not isinstance(manifest.get('files'),dict) or set(manifest['files'])!=set(payloads) or \
                manifest.get('automatic_execution') is not False or manifest.get('broker_key_included') is not False or \
                not re.fullmatch('[a-f0-9]{40}',manifest.get('source_commit','')) or not manifest.get('quiescence_reference'):
            raise RecoveryError("Recovery manifest does not match archive")
        for name,data in payloads.items():
            _name(name);record=manifest['files'][name]
            if not isinstance(record,dict) or record.get('kind') not in KINDS or \
                    isinstance(record.get('bytes'),bool) or record.get('sha256')!=_digest(data) or record.get('bytes')!=len(data):
                raise RecoveryError("Recovery member integrity mismatch")
            _safe_payload(name,data,record['kind'],key)
            if record['kind']=='broker-state':
                if broker_escrow_key is None:
                    raise RecoveryError("Separate broker key escrow is required")
                envelope=json.loads(data)
                decoded=json.loads(Fernet(broker_escrow_key).decrypt(envelope['ciphertext'].encode()))
                if not Path(name).stem.isdigit() or type(decoded['owner']) is not int or \
                        decoded['owner']!=int(Path(name).stem) or not isinstance(decoded['state'],dict):
                    raise RecoveryError("Recovered broker credential owner mismatch")
        roles=set()
        for record in manifest['files'].values():
            declared=record.get('roles',[])
            if not isinstance(declared,list) or any(not isinstance(role,str) for role in declared):
                raise RecoveryError("Invalid recovered source roles")
            roles.update(declared)
        missing=sorted(REQUIRED_ROLES-roles)
        if manifest.get('missing_roles')!=missing or manifest.get('required_roles_covered')!=(not missing):
            raise RecoveryError("Recovery role coverage contradicts source declarations")
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("Recovery bundle cannot be authenticated/decoded") from exc
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    staged=Path(tempfile.mkdtemp(dir=destination.parent,prefix='.restore-'))
    try:
        for name,data in payloads.items():
            target=staged/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            target.write_bytes(data);target.chmod(0o600)
            if manifest['files'][name]['kind']=='sqlite':
                con=sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise RecoveryError("Restored database integrity check failed")
                finally:con.close()
        (staged/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        (staged/'manifest.json').chmod(0o600)
        (staged/'RESTORE_DISABLED').write_text('No execution: reconcile and explicitly authorize cutover.\n')
        (staged/'RESTORE_DISABLED').chmod(0o600)
        # Reserve the final directory exclusively. A racing process can never
        # have its existing tree replaced. A failed copy leaves no usable tree.
        destination.mkdir(mode=0o700)
        try:
            shutil.copyfile(staged/'RESTORE_DISABLED',destination/'RESTORE_DISABLED')
            (destination/'RESTORE_DISABLED').chmod(0o600)
            for child in staged.iterdir():
                if child.name=='RESTORE_DISABLED':continue
                child.rename(destination/child.name)
        except Exception:
            shutil.rmtree(destination)
            raise
    finally:
        if staged.exists():shutil.rmtree(staged)
    return dict(files=len(payloads),integrity='ok',execution_enabled=False,
                required_roles_covered=manifest.get('required_roles_covered') is True,
                source_commit=manifest['source_commit'],separate_broker_escrow_verified=broker_escrow_key is not None)
