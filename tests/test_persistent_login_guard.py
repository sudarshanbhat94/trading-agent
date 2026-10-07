import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import HTTPException, Request, Response
from app import auth, login_guard
from app.config import Settings
from app.db import Database


class PersistentLoginGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'accounts.db'
        self.db = Database(self.path); self.db.init()
        self.user = self.db.create_user('test-owner', auth.hash_password('a-long-test-password'), role='user', active=True)
        self.settings = Settings(auth_session_secret='isolated-secret-for-this-test')

    def test_concurrent_reservations_are_shared_and_survive_restart(self):
        barrier = Barrier(8)
        def attempt(_):
            with sqlite3.connect(self.path, timeout=10) as con:
                barrier.wait(); return login_guard.reserve(con, 'client|owner', 1000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(bool(ticket) for ticket, _ in results), 5)
        with sqlite3.connect(self.path) as con:
            self.assertGreater(login_guard.reserve(con, 'client|owner', 1001)[1], 0)
            self.assertTrue(login_guard.reserve(con, 'client|other-owner', 1001)[0])
            self.assertTrue(login_guard.reserve(con, 'client|owner', 1901)[0])
            self.assertNotIn('client|owner', str(con.execute('SELECT * FROM auth_login_reservations').fetchall()))

    def test_success_does_not_clear_other_pending_or_failed_attempts(self):
        with sqlite3.connect(self.path) as con:
            tickets = [login_guard.reserve(con, 'client|owner', 1000)[0] for _ in range(5)]
            login_guard.finish(con, tickets[0], True, 1001)
            for ticket in tickets[1:]: login_guard.finish(con, ticket, False, 1001)
            last, _ = login_guard.reserve(con, 'client|owner', 1001)
            login_guard.finish(con, last, False, 1001)
            login_guard.finish(con, tickets[0], True, 1002)
            self.assertGreater(login_guard.reserve(con, 'client|owner', 1002)[1], 0)

    def test_real_login_checks_shared_store_before_password_work(self):
        with self.db.connect() as con:
            for _ in range(5): login_guard.reserve(con, '|test-owner', 1000)
        with patch.object(auth.time, 'time', return_value=1001), patch.object(auth, 'verify_password') as verify:
            with self.assertRaises(HTTPException) as error:
                auth.login_user('test-owner', 'wrong', Response(), self.settings, self.db)
            self.assertEqual(error.exception.status_code, 429); verify.assert_not_called()

    def test_password_role_and_deactivation_revoke_all_sessions_atomically(self):
        for update in ({'password_hash': auth.hash_password('changed-test-password')}, {'role':'admin'}, {'active':False}):
            tokens = [auth._make_token(self.user, self.settings, self.db) for _ in range(2)]
            payloads = [auth._verify_token(token, self.settings, self.db) for token in tokens]
            self.assertTrue(all(auth._active_session(self.db, p) for p in payloads))
            self.db.update_user(self.user['id'], **update)
            self.assertTrue(all(not auth._active_session(self.db, p) for p in payloads))

    def test_logout_all_revokes_only_owned_sessions(self):
        other = self.db.create_user('other-owner', auth.hash_password('another-long-test-password'), role='user', active=True)
        own = auth._make_token(self.user, self.settings, self.db)
        extra = auth._make_token(self.user, self.settings, self.db)
        other_token = auth._make_token(other, self.settings, self.db)
        request = Request({'type':'http','headers':[(b'cookie', ('openstocks_session='+own).encode())]})
        self.assertFalse(auth.logout_all(Response(), request, self.settings, self.db)['authenticated'])
        self.assertFalse(auth._active_session(self.db, auth._verify_token(extra, self.settings, self.db)))
        self.assertTrue(auth._active_session(self.db, auth._verify_token(other_token, self.settings, self.db)))

    def test_missing_security_store_fails_closed(self):
        with self.db.connect() as con: con.execute('DROP TABLE auth_login_reservations')
        with self.assertRaises(HTTPException) as error:
            auth.login_user('test-owner', 'a-long-test-password', Response(), self.settings, self.db)
        self.assertEqual(error.exception.status_code, 503)
