import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app import v2_live,books,approved_execution,schema_migrations


class TransactionalMigrationTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.addCleanup(self.con.close)

    def test_fault_rolls_back_schema_and_receipt_not_partial_startup(self):
        def fail(con):
            con.executescript('CREATE TABLE transient_fixture(id INTEGER); INSERT INTO transient_fixture VALUES(1);')
            con.commit()
            raise OSError('fixture migration disk fault')
        with patch.object(approved_execution,'ensure_schema',side_effect=fail):
            with self.assertRaises(OSError):v2_live.ensure_schema(self.con)
        self.assertEqual(self.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[])
        self.assertFalse(self.con.in_transaction)
        v2_live.ensure_schema(self.con)
        self.assertEqual(self.con.execute('SELECT version FROM schema_migration_receipts').fetchone()[0],'trading-schema-v3')

    def test_existing_book_epoch_capital_inventory_and_nested_transaction_preserved(self):
        v2_live.ensure_schema(self.con);books.ensure_book(self.con,2);self.con.commit()
        before=self.con.execute('SELECT * FROM user_book').fetchall()
        house=self.con.execute('SELECT * FROM v2_book').fetchall()
        v2_live.ensure_schema(self.con)
        self.assertEqual(self.con.execute('SELECT * FROM user_book').fetchall(),before)
        self.assertEqual(self.con.execute('SELECT * FROM v2_book').fetchall(),house)
        self.con.execute('BEGIN IMMEDIATE')
        self.con.execute("CREATE TABLE parent_fixture(id INTEGER)")
        v2_live.ensure_schema(self.con)
        self.assertTrue(self.con.in_transaction);self.con.rollback()
        self.assertFalse(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='parent_fixture'").fetchone())
        with self.assertRaises(RuntimeError):schema_migrations.apply(self.con,'trading-schema-v3',{'changed':True},lambda c:None,lambda c:None)
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM schema_migration_receipts')

    def test_script_parses_trigger_and_literal_semicolons_without_commit(self):
        self.con.execute('BEGIN IMMEDIATE');proxy=schema_migrations.SchemaConnection(self.con)
        proxy.executescript("CREATE TABLE notes(value TEXT); CREATE TRIGGER deny_note BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT,'no;delete'); END; INSERT INTO notes VALUES('one;two');")
        self.assertEqual(self.con.execute('SELECT value FROM notes').fetchone()[0],'one;two')
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('DELETE FROM notes')
        self.con.rollback()
        self.assertFalse(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone())

    def test_account_startup_fault_is_atomic_and_preserves_existing_users(self):
        from app.db import Database
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/'accounts.db')
            with patch.object(db,'_seed_strategy_plans',side_effect=OSError('fixture migration fault')):
                with self.assertRaises(OSError):db.init()
            with db.connect() as con:self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[])
            db.init()
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT version FROM schema_migration_receipts').fetchone()[0],'account-schema-v2')
