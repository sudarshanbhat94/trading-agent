from pathlib import Path
import sqlite3
import tempfile
import unittest
from cryptography.fernet import Fernet
from app import recovery_stream as recovery
from app.recovery_bundle import RecoveryError
from app.recovery_guard import assert_execution_allowed


class StreamingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.key=Fernet.generate_key();self.bundle=self.root/'backup.encrypted';self.sources={}
        for role,tables in recovery.ROLE_TABLES.items():
            path=self.root/(role+'.db')
            with sqlite3.connect(path) as con:
                for table in tables:con.execute('CREATE TABLE '+table+'(id INTEGER)')
            self.sources[path.name]={'path':str(path),'kind':'sqlite','roles':[role]}
        for name,kind,roles,content in [('protocol.json','protocol',['forward_protocol'],b'{"fixture":true}'),
             ('config.json','file',['runtime_config','permissions'],b'{"fixture":true}'),
             ('large.dat','file',[],b'x'*(recovery.BLOCK*2+31))]:
            path=self.root/name;path.write_bytes(content);self.sources[name]={'path':str(path),'kind':kind,'roles':roles}

    def create(self):return recovery.create(self.sources,self.bundle,self.key,source_commit='a'*40,quiescence_reference='Synthetic stopped writers only')

    def test_multichunk_round_trip_private_and_disabled_without_source_changes(self):
        before={n:recovery.digest(s['path']) for n,s in self.sources.items()};self.create()
        target=self.root/'restored';result=recovery.restore(self.bundle,target,self.key)
        self.assertTrue(result['restore_disabled']);self.assertEqual((target/'large.dat').read_bytes(),b'x'*(recovery.BLOCK*2+31))
        self.assertEqual(before,{n:recovery.digest(s['path']) for n,s in self.sources.items()})
        self.assertEqual(target.stat().st_mode&0o077,0)
        with self.assertRaises(RuntimeError):assert_execution_allowed(target/'paper_book.db')

    def test_truncation_wrong_key_reorder_and_extra_bytes_refuse_before_publication(self):
        self.create();original=self.bundle.read_bytes()
        for index,content in enumerate((original[:-1],original+b'x',original[:80]+bytes([original[80]^1])+original[81:])):
            source=self.root/('bad-'+str(index));source.write_bytes(content)
            with self.assertRaises(RecoveryError):recovery.restore(source,self.root/('target-'+str(index)),self.key)
            self.assertFalse((self.root/('target-'+str(index))).exists())
        with self.assertRaises(RecoveryError):recovery.restore(self.bundle,self.root/'wrong-key',Fernet.generate_key())

    def test_false_role_declaration_and_existing_destination_are_refused(self):
        self.sources['config.json']['roles'].append('paper_book')
        with self.assertRaises(RecoveryError):self.create()
        self.sources['config.json']['roles'].pop();self.create()
        with self.assertRaises(RecoveryError):recovery.restore(self.bundle,self.root,self.key)
