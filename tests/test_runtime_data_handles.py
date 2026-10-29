"""Failed/empty source reads must release their actual SQLite handles."""
import sqlite3
import unittest
from unittest.mock import patch

from app import v2_live, v2_web, index_history


class RuntimeDataHandlesTest(unittest.TestCase):
    def assert_closed(self, con):
        with self.assertRaises(sqlite3.ProgrammingError):con.execute('SELECT 1')

    def test_failed_quote_read_closes_before_propagating_source_error(self):
        con=sqlite3.connect(':memory:')
        with patch.object(v2_live,'_ro',return_value=con):
            with self.assertRaises(sqlite3.OperationalError):v2_live._live('IN',['TEST'])
        self.assert_closed(con)

    def test_empty_quote_watch_closes_its_handle(self):
        con=sqlite3.connect(':memory:')
        with patch.object(v2_live,'_ro',return_value=con):self.assertEqual(v2_live._live('IN',[]),{})
        self.assert_closed(con)

    def test_web_missing_schema_and_empty_watch_release_their_handles(self):
        for symbols in (['TEST'], []):
            con=sqlite3.connect(':memory:')
            with patch.object(v2_web,'_ro',return_value=con):
                self.assertEqual(v2_web._live_map('IN',symbols),{})
            self.assert_closed(con)

    def test_actual_readers_reject_nonfinite_prices_and_preserve_source(self):
        for module, reader in ((v2_live,v2_live._live),(v2_web,v2_web._live_map)):
            con=sqlite3.connect(':memory:')
            con.execute('CREATE TABLE latest_quotes(symbol,price,open,high,low,close,volume,ts,source)')
            con.executemany('INSERT INTO latest_quotes VALUES(?,?,?,?,?,?,?,?,?)',[
                ('INVALID',float('inf'),100,100,100,100,100,'fixture','upstox-live'),
                ('TEST',100,float('inf'),float('inf'),90,99,float('inf'),'fixture','upstox-live')])
            with patch.object(module,'_ro',return_value=con):quotes=reader('IN')
            self.assertNotIn('INVALID',quotes)
            self.assertEqual(quotes['TEST']['source'],'upstox-live')
            self.assertEqual(quotes['TEST']['high'],100)
            self.assertEqual(quotes['TEST']['open'],100)
            if module is v2_live:self.assertEqual(quotes['TEST']['vol'],0)
            self.assert_closed(con)

    def test_price_helpers_reject_boolean_and_nonfinite_values(self):
        for helper in (v2_live._f,v2_web._n):
            for value in (True,float('inf'),float('nan'),-1):
                self.assertEqual(helper(value,97),97)

    def test_failed_optional_index_and_options_reads_release_handles(self):
        for fn in (v2_live._sleeve_index_bars,v2_live._sleeve_option_chain):
            with self.subTest(reader=fn.__name__):
                con=sqlite3.connect(':memory:')
                with patch.object(v2_live,'_ro',return_value=con):fn('NIFTY')
                self.assert_closed(con)

    def test_index_history_closes_on_missing_evidence_and_success(self):
        for success in (False,True):
            with self.subTest(success=success):
                con=sqlite3.connect(':memory:')
                with patch.object(index_history.sqlite3,'connect',return_value=con),patch.object(index_history,'frames',return_value={} if success else None) as frames:
                    if not success:frames.side_effect=sqlite3.OperationalError('fixture missing evidence')
                    self.assertEqual(index_history.load(path='/nonexistent-fixture.db'),{})
                self.assert_closed(con)
