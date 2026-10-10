"""Copied startup migration cannot reset books or silently lose old values."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app import books, v2_live, upgrade_rehearsal as upgrade
from app.db import Database


class UpgradeRehearsalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.paper=self.root/'paper.db'; self.accounts=self.root/'accounts.db'; self.protocol=self.root/'protocol.json'
        with closing(sqlite3.connect(self.paper)) as con:
            v2_live.ensure_schema(con); books.ensure_book(con,2)
            con.execute('CREATE TABLE legacy_evidence(id,payload BLOB)')
            con.execute('INSERT INTO legacy_evidence VALUES(?,?)',(1,b'untouched legacy observation'))
            con.commit()
        Database(self.accounts).init()
        self.protocol.write_text('{"registered":"original"}\n')

    def run_copy(self, destination, migration=None):
        def paper(path):
            with closing(sqlite3.connect(path)) as con: v2_live.ensure_schema(con)
        return upgrade.rehearse(self.paper,self.accounts,self.protocol,destination,user_id=2,
            migrate_paper=migration or paper,migrate_accounts=lambda path:Database(path).init())

    def test_real_schema_callbacks_preserve_originals_epoch_capital_and_protocol(self):
        original=[p.read_bytes() for p in (self.paper,self.accounts,self.protocol)]
        destination=self.root/'copy'; result=self.run_copy(destination)
        self.assertEqual(result['status'],'passed')
        self.assertEqual((result['paper']['capital'],result['paper']['cash'],result['paper']['equity'],result['paper']['open_positions']),
                         (10000,10000,10000,0))
        self.assertEqual(original,[p.read_bytes() for p in (self.paper,self.accounts,self.protocol)])
        self.assertTrue((destination/'RESTORE_DISABLED').exists())
        self.assertFalse(result['coordinated_backup']); self.assertFalse(result['deployment_authorized'])
        self.assertFalse(result['release_certified']); self.assertEqual(result['real_orders'],0)
        for path in destination.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077,0)
        self.assertNotIn('untouched legacy observation',(destination/'rehearsal.json').read_text())

    def test_loss_of_historical_data_fails_without_changing_source(self):
        original=self.paper.read_bytes()
        def corrupt(path):
            with closing(sqlite3.connect(path)) as con:
                con.execute('DELETE FROM legacy_evidence'); con.commit()
        destination=self.root/'bad'
        with self.assertRaisesRegex(ValueError,'pre-existing'):
            self.run_copy(destination,corrupt)
        self.assertEqual(self.paper.read_bytes(),original)
        self.assertEqual(json.loads((destination/'rehearsal.json').read_text())['status'],'failed')
        self.assertTrue((destination/'RESTORE_DISABLED').exists())

    def test_same_row_count_with_different_values_and_duplicate_rows_are_detected(self):
        with closing(sqlite3.connect(':memory:')) as con:
            con.execute('CREATE TABLE sample(a,b)')
            con.executemany('INSERT INTO sample VALUES(?,?)',[(None,b'A'),(None,b'A'),(1,'1')])
            before=upgrade.preservation_snapshot(con)
            con.execute("UPDATE sample SET b='changed' WHERE a=1")
            with self.assertRaises(ValueError): upgrade.verify_preservation(con,before)

    def test_existing_destination_links_and_bad_protocol_refuse_before_migration(self):
        destination=self.root/'exists'; destination.mkdir()
        with self.assertRaises(ValueError): self.run_copy(destination)
        linked=self.root/'link.db'; linked.symlink_to(self.paper)
        with self.assertRaises(ValueError):
            upgrade.rehearse(linked,self.accounts,self.protocol,self.root/'linked',user_id=2,
                migrate_paper=lambda _:self.fail('must not migrate'),migrate_accounts=lambda _:None)
        self.protocol.write_text('[]')
        with self.assertRaises(ValueError): self.run_copy(self.root/'bad-protocol')
        self.assertFalse((self.root/'bad-protocol').exists())


if __name__=='__main__': unittest.main()
