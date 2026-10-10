"""Bounded-memory encrypted recovery for large market databases.

Version 2 authenticates every ordered chunk and the end marker. All staging is
private; restored trees are execution-disabled and never replace production.
Declared coverage still requires a supervised quiescence/restore drill.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tarfile
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.fernet import Fernet
from .recovery_bundle import RecoveryError,KINDS,REQUIRED_ROLES,MAX_MEMBERS,_name,_sqlite_snapshot,_safe_payload

MAGIC=b'OSTOCKSBK2\n';BLOCK=1024*1024;MAX_TOTAL=50*1024**3
ROLE_TABLES={'paper_book':{'v2_book','user_book','v2_positions','user_positions'},
             'accounts':{'users','auth_sessions'},'market_data':{'universe','candles'},
             'idea_tracking':{'publications','samples','assessment_events'},
             'instrument_catalogue':{'instrument_snapshots','instrument_contracts','execution_contract_evidence'}}


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(BLOCK),b''):value.update(chunk)
    return value.hexdigest()


def _cipher(key,salt):
    Fernet(key)  # Validate the independently escrowed master key format.
    raw=base64.urlsafe_b64decode(key)
    derived=HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=b'openstocks-recovery-v2').derive(raw)
    return AESGCM(derived)


def _encrypt(source,destination,key):
    salt=os.urandom(32);prefix=os.urandom(8);header=MAGIC+salt+prefix;cipher=_cipher(key,salt)
    with Path(source).open('rb') as source,Path(destination).open('xb') as out:
        os.chmod(destination,0o600);out.write(header);counter=0
        while True:
            plain=source.read(BLOCK);final=not plain
            frame=struct.pack('>IIB',counter,len(plain)+16,int(final))
            out.write(frame);out.write(cipher.encrypt(prefix+struct.pack('>I',counter),plain,header+frame))
            if final:break
            counter+=1
        out.flush();os.fsync(out.fileno())


def _decrypt(source,destination,key):
    with Path(source).open('rb') as source,Path(destination).open('xb') as out:
        os.chmod(destination,0o600)
        header=source.read(len(MAGIC)+40)
        if len(header)!=len(MAGIC)+40 or not header.startswith(MAGIC):raise RecoveryError('Unknown recovery stream format')
        salt,prefix=header[len(MAGIC):len(MAGIC)+32],header[-8:];cipher=_cipher(key,salt)
        expected=0;total=0
        while True:
            frame=source.read(9)
            if len(frame)!=9:raise RecoveryError('Truncated recovery stream')
            ordinal,size,final=struct.unpack('>IIB',frame)
            if ordinal!=expected or not 16<=size<=BLOCK+16 or final not in (0,1) or (final and size!=16):
                raise RecoveryError('Recovery chunk sequence invalid')
            ciphertext=source.read(size)
            if len(ciphertext)!=size:raise RecoveryError('Truncated recovery chunk')
            plain=cipher.decrypt(prefix+struct.pack('>I',ordinal),ciphertext,header+frame)
            total+=len(plain)
            if total>MAX_TOTAL:raise RecoveryError('Recovery exceeds configured capacity')
            out.write(plain)
            if final:
                if source.read(1):raise RecoveryError('Unexpected bytes after authenticated recovery end')
                break
            expected+=1


def _inspect(path,name,kind,key):
    if Path(name).suffix.lower() in {'.key','.pem','.p12','.pfx'}:raise RecoveryError('Escrow cannot enter backup')
    if kind in {'protocol','broker-state'}:
        if Path(path).stat().st_size>16*BLOCK:raise RecoveryError('Structured recovery member too large')
        _safe_payload(name,Path(path).read_bytes(),kind,key)
    else:
        previous=b''
        with Path(path).open('rb') as handle:
            for chunk in iter(lambda:handle.read(BLOCK),b''):
                _safe_payload(name,previous+chunk,kind,key);previous=chunk[-256:]


def _roles(path,kind,roles):
    tables=None
    for role in roles:
        if role not in REQUIRED_ROLES:raise RecoveryError('Unknown deployment role')
        if role in ROLE_TABLES:
            if kind!='sqlite':raise RecoveryError('Database deployment role declared as a non-database')
            if tables is None:
                con=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
                try:tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                finally:con.close()
            if not ROLE_TABLES[role]<=tables:raise RecoveryError('Declared database role lacks expected schemas')
        elif role=='forward_protocol' and kind!='protocol':raise RecoveryError('Forward experiment must be preserved as protocol bytes')
        elif role in {'permissions','runtime_config'} and kind not in {'file','sqlite'}:
            raise RecoveryError('Runtime configuration/permissions role kind is invalid')


def create(sources,destination,key,*,source_commit,quiescence_reference):
    if not isinstance(source_commit,str) or not re.fullmatch('[a-f0-9]{40}',source_commit) or \
            not isinstance(quiescence_reference,str) or not quiescence_reference.strip() or \
            not isinstance(sources,dict) or not 0<len(sources)<MAX_MEMBERS:
        raise RecoveryError('Exact build, complete source declaration and supervised quiescence required')
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise RecoveryError('Backup destination already exists')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent,prefix='.backup-private-') as temporary:
        root=Path(temporary);records={};roles=set();total=0;staged={};originals={}
        for name,spec in sources.items():
            _name(name)
            if not isinstance(spec,dict) or spec.get('kind') not in KINDS:raise RecoveryError('Invalid source declaration')
            source=Path(spec['path']);kind=spec['kind'];declared=spec.get('roles',[])
            if not isinstance(declared,list) or any(not isinstance(r,str) for r in declared):raise RecoveryError('Invalid source roles')
            if source.is_symlink() or not source.is_file():raise RecoveryError('Missing or linked source')
            roles.update(declared);target=root/('member-'+str(len(records)))
            before=digest(source)
            wal=Path(str(source)+'-wal');wal_before=digest(wal) if kind=='sqlite' and wal.exists() else None
            originals[name]=(source,before,wal,wal_before,kind)
            if kind=='sqlite':_sqlite_snapshot(source,target)
            else:
                import shutil
                shutil.copyfile(source,target);target.chmod(0o600)
            if digest(source)!=before or (digest(wal) if kind=='sqlite' and wal.exists() else None)!=wal_before:
                raise RecoveryError('Source changed during declared quiescence')
            _inspect(target,name,kind,key);_roles(target,kind,declared);size=target.stat().st_size;total+=size
            if total>MAX_TOTAL:raise RecoveryError('Recovery sources exceed configured capacity')
            staged[name]=target;records[name]=dict(kind=kind,sha256=digest(target),bytes=size,roles=declared)
        # Detect changes across the whole set, not only during one member copy.
        # This is not a substitute for externally supervised quiescence.
        for source,before,wal,wal_before,kind in originals.values():
            if digest(source)!=before or (digest(wal) if kind=='sqlite' and wal.exists() else None)!=wal_before:
                raise RecoveryError('Source changed across complete declared backup')
        if REQUIRED_ROLES-roles:raise RecoveryError('Complete deployment source roles required')
        manifest=dict(format='openstocks-recovery-v2',source_commit=source_commit,quiescence_reference=quiescence_reference,
                      files=records,required_roles_covered=True,automatic_execution=False,broker_key_included=False)
        manifest_path=root/'manifest.json';manifest_path.write_text(json.dumps(manifest,sort_keys=True));manifest_path.chmod(0o600)
        archive=root/'plain.tar'
        with tarfile.open(archive,'w:') as tar:
            for name,path in staged.items():tar.add(path,arcname=name,recursive=False)
            tar.add(manifest_path,arcname='manifest.json',recursive=False)
        encrypted=root/'encrypted.bundle';_encrypt(archive,encrypted,key)
        os.link(encrypted,destination)
    return dict(format='openstocks-recovery-v2',files=len(records),bytes=total,encrypted=True,
                source_commit=source_commit,sha256=digest(destination),restore_disabled=True,required_roles_covered=True)


def restore(bundle,destination,key,*,broker_escrow_key=None):
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise RecoveryError('New restore destination required')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent,prefix='.restore-private-') as temporary:
        root=Path(temporary);archive=root/'plain.tar';staged=root/'tree';staged.mkdir(mode=0o700)
        try:
            _decrypt(bundle,archive,key)
            with tarfile.open(archive,'r:') as tar:
                members=tar.getmembers();names=[m.name for m in members]
                if not 0<len(members)<=MAX_MEMBERS or len(set(names))!=len(names) or names.count('manifest.json')!=1 or \
                        any(not m.isfile() or m.size<0 for m in members) or sum(m.size for m in members)>MAX_TOTAL:
                    raise RecoveryError('Invalid recovery archive members')
                manifest_member=next(m for m in members if m.name=='manifest.json')
                if manifest_member.size>BLOCK:raise RecoveryError('Recovery manifest too large')
                manifest=json.load(tar.extractfile(manifest_member));files=manifest.get('files')
                if manifest.get('format')!='openstocks-recovery-v2' or not isinstance(files,dict) or set(files)!=set(names)-{'manifest.json'} or \
                        manifest.get('automatic_execution') is not False or manifest.get('broker_key_included') is not False or \
                        not isinstance(manifest.get('source_commit'),str) or not re.fullmatch('[a-f0-9]{40}',manifest['source_commit']) or \
                        not manifest.get('quiescence_reference'):raise RecoveryError('Invalid recovery manifest')
                roles=set()
                for member in members:
                    if member.name=='manifest.json':continue
                    _name(member.name);record=files[member.name];path=staged/member.name
                    if not isinstance(record,dict) or record.get('kind') not in KINDS or type(record.get('bytes')) is not int or \
                            record['bytes']!=member.size or not isinstance(record.get('roles'),list) or \
                            any(not isinstance(r,str) for r in record['roles']):raise RecoveryError('Invalid recovery member declaration')
                    roles.update(record['roles']);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    import shutil
                    with tar.extractfile(member) as source,path.open('xb') as out:shutil.copyfileobj(source,out,length=BLOCK)
                    path.chmod(0o600)
                    if digest(path)!=record['sha256']:raise RecoveryError('Recovery member digest mismatch')
                    _inspect(path,member.name,record['kind'],key)
                    _roles(path,record['kind'],record['roles'])
                    if record['kind']=='sqlite':
                        con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                        try:
                            if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RecoveryError('Recovered database integrity failed')
                        finally:con.close()
                    if record['kind']=='broker-state':
                        if broker_escrow_key is None:raise RecoveryError('Separate broker escrow key required')
                        envelope=json.loads(path.read_text());state=json.loads(Fernet(broker_escrow_key).decrypt(envelope['ciphertext'].encode()))
                        if not path.stem.isdigit() or type(state.get('owner')) is not int or state['owner']!=int(path.stem) or \
                                not isinstance(state.get('state'),dict):raise RecoveryError('Recovered credential owner mismatch')
                if REQUIRED_ROLES-roles or manifest.get('required_roles_covered') is not True:raise RecoveryError('Incomplete recovery coverage')
            (staged/'manifest.json').write_text(json.dumps(manifest,indent=2));(staged/'manifest.json').chmod(0o600)
            (staged/'RESTORE_DISABLED').write_text('Execution disabled; supervised reconciliation and cutover approval required.\n')
            (staged/'RESTORE_DISABLED').chmod(0o600)
            # Reserve a fresh directory, install the guard first, then move
            # private members. A partial copy remains permanently disabled.
            destination.mkdir(mode=0o700)
            os.replace(staged/'RESTORE_DISABLED',destination/'RESTORE_DISABLED')
            for path in staged.iterdir():os.replace(path,destination/path.name)
        except RecoveryError:raise
        except Exception as exc:raise RecoveryError('Recovery authentication/validation failed') from exc
    return dict(format='openstocks-recovery-v2',files=len(files),source_commit=manifest['source_commit'],restore_disabled=True)
