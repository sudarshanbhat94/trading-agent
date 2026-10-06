import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from cryptography.fernet import Fernet
from app import recovery_bundle as recovery


class RecoveryBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.key=Fernet.generate_key()
        self.db=self.root/'source.db'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE book(cash INTEGER)');con.execute('INSERT INTO book VALUES(10000)')
        con.close()
        self.protocol=self.root/'protocol.json';self.protocol.write_text('{"version":"fixed","cohort":"untouched"}\n')
        self.sources={'var/book.db':{'path':str(self.db),'kind':'sqlite'},
                      'var/protocol.json':{'path':str(self.protocol),'kind':'protocol'}}
        self.bundle=self.root/'backup.enc'

    def create(self,**kwargs):
        return recovery.create(self.sources,self.bundle,self.key,source_commit='a'*40,
                               quiescence_reference='isolated fixture, no writers',**kwargs)

    def test_restore_exact_book_protocol_permissions_without_starting_execution(self):
        before=self.db.read_bytes();result=self.create()
        self.assertFalse(result['required_roles_covered'])
        self.assertNotIn(b'cohort',self.bundle.read_bytes())
        restored=self.root/'restored';out=recovery.restore(self.bundle,restored,self.key)
        self.assertFalse(out['execution_enabled']);self.assertTrue((restored/'RESTORE_DISABLED').exists())
        self.assertEqual((restored/'var/protocol.json').read_bytes(),self.protocol.read_bytes())
        self.assertEqual(self.db.read_bytes(),before)
        with sqlite3.connect(restored/'var/book.db') as con:
            self.assertEqual(con.execute('SELECT cash FROM book').fetchone()[0],10000)
            from app.recovery_guard import assert_database_execution_allowed
            with self.assertRaises(RuntimeError):assert_database_execution_allowed(con)
        con.close()
        from app import v2_live
        from unittest.mock import patch
        with patch.object(v2_live,'V2_DB',str(restored/'var/book.db')):
            with self.assertRaises(RuntimeError):v2_live.start_background()
        for file in restored.rglob('*'):
            self.assertFalse(file.stat().st_mode & 0o077)
        with self.assertRaises(recovery.RecoveryError):recovery.restore(self.bundle,restored,self.key)

    def test_incomplete_full_declaration_refuses_before_publication(self):
        with self.assertRaises(recovery.RecoveryError):self.create(require_complete=True)
        self.assertFalse(self.bundle.exists())
        self.sources['var/book.db']['roles']=sorted(recovery.REQUIRED_ROLES-{'forward_protocol'})
        self.sources['var/protocol.json']['roles']=['forward_protocol']
        self.assertTrue(self.create(require_complete=True)['required_roles_covered'])

    def test_missing_key_wrong_key_corruption_never_creates_restore(self):
        self.create();target=self.root/'restore'
        with self.assertRaises(recovery.RecoveryError):recovery.restore(self.bundle,target,Fernet.generate_key())
        data=bytearray(self.bundle.read_bytes());data[len(data)//2]^=1;self.bundle.write_bytes(data)
        with self.assertRaises(recovery.RecoveryError):recovery.restore(self.bundle,target,self.key)
        self.assertFalse(target.exists())

    def test_encrypted_credentials_need_independent_escrow_and_matching_owner(self):
        broker_key=Fernet.generate_key()
        state=self.root/'2.json'
        envelope={'format':'fernet-account-v1','ciphertext':Fernet(broker_key).encrypt(
            json.dumps({'owner':2,'state':{'fixture':True}}).encode()).decode()}
        state.write_text(json.dumps(envelope))
        self.sources['var/brokers/2.json']={'path':str(state),'kind':'broker-state'}
        self.create()
        for escrow in (None,Fernet.generate_key()):
            with self.assertRaises(recovery.RecoveryError):
                recovery.restore(self.bundle,self.root/'no',self.key,broker_escrow_key=escrow)
        out=recovery.restore(self.bundle,self.root/'yes',self.key,broker_escrow_key=broker_key)
        self.assertTrue(out['separate_broker_escrow_verified'])
        self.bundle.unlink();self.sources['var/brokers/3.json']=self.sources.pop('var/brokers/2.json');self.create()
        with self.assertRaises(recovery.RecoveryError):
            recovery.restore(self.bundle,self.root/'mismatch',self.key,broker_escrow_key=broker_key)

    def test_private_key_and_plaintext_credential_refusal(self):
        keyfile=self.root/'key';keyfile.write_bytes(self.key);keyfile.chmod(0o644)
        with self.assertRaises(recovery.RecoveryError):recovery.private_key(keyfile)
        keyfile.chmod(0o600);self.assertEqual(recovery.private_key(keyfile),self.key)
        self.sources['var/key']={'path':str(keyfile),'kind':'file'}
        with self.assertRaises(recovery.RecoveryError):self.create()
        self.sources.pop('var/key');state=self.root/'plain.json';state.write_text('{"access_token":"fixture"}')
        self.sources['var/brokers/2.json']={'path':str(state),'kind':'broker-state'}
        with self.assertRaises(recovery.RecoveryError):self.create()

    def test_traversal_links_duplicate_members_and_bad_hash_refuse(self):
        self.create();plain=Fernet(self.key).decrypt(self.bundle.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(plain),mode='r:') as archive:
            original=[(m.name,archive.extractfile(m).read()) for m in archive.getmembers()]
        for mutation in ('traversal','link','duplicate','hash','coverage'):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w') as archive:
                for name,data in original:
                    if mutation=='coverage' and name=='manifest.json':
                        manifest=json.loads(data);manifest['required_roles_covered']=True;manifest['missing_roles']=[]
                        data=json.dumps(manifest).encode()
                    if mutation=='hash' and name=='var/book.db':data+=b'x'
                    info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
                if mutation not in {'hash','coverage'}:
                    info=tarfile.TarInfo('../escape' if mutation=='traversal' else 'var/book.db')
                    if mutation=='link':info.type=tarfile.SYMTYPE;info.linkname='/outside'
                    archive.addfile(info,io.BytesIO(b''))
            hostile=self.root/(mutation+'.enc');hostile.write_bytes(Fernet(self.key).encrypt(stream.getvalue()))
            with self.assertRaises(recovery.RecoveryError):recovery.restore(hostile,self.root/mutation,self.key)
            self.assertFalse((self.root/mutation).exists())
