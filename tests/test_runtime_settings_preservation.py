"""Restart normalization must not rewrite unaffected configuration history."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.db import Database
from app.llm_policy import runtime_overrides_without_llm


class RuntimeSettingsPreservationTest(unittest.TestCase):
    def test_noop_startup_preserves_every_value_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'accounts.db');db.init()
            values=runtime_overrides_without_llm({'paper_capital':10000,'risk_threshold':0.9})
            with patch('app.db.utc_now',return_value='2026-10-06T00:00:00Z'):
                db.update_runtime_settings(values)
            with db.connect() as con:
                original=[tuple(r) for r in con.execute('SELECT key,value,updated_at FROM runtime_settings ORDER BY key')]
            with patch('app.db.utc_now',return_value='2026-10-07T00:00:00Z'):
                db.update_runtime_settings(runtime_overrides_without_llm(db.runtime_settings()))
            with db.connect() as con:
                current=[tuple(r) for r in con.execute('SELECT key,value,updated_at FROM runtime_settings ORDER BY key')]
            self.assertEqual(current,original)
            with patch('app.db.utc_now',return_value='2026-10-07T00:00:00Z'):
                db.update_runtime_settings({'paper_capital':20000})
            with db.connect() as con:
                changed=[tuple(r) for r in con.execute('SELECT key,value,updated_at FROM runtime_settings ORDER BY key')]
            self.assertEqual(sum(a!=b for a,b in zip(original,changed)),1)
            self.assertEqual(next(r[2] for r in changed if r[0]=='paper_capital'),'2026-10-07T00:00:00Z')
