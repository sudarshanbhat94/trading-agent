"""Exposure survives selection removal, partial schemas and alias conflicts."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import v2_quote_feed as feed


class ExposureQuoteCoverageTest(unittest.TestCase):
    def test_missing_broker_schema_cannot_erase_house_and_personal_exposure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'paper.db'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE v2_positions(market,symbol)')
                con.execute('CREATE TABLE user_positions(market,symbol)')
                con.execute("INSERT INTO v2_positions VALUES('IN','HOUSE')")
                con.execute("INSERT INTO user_positions VALUES('IN','PERSONAL')")
            before = path.read_bytes()
            with patch.object(feed, 'V2_DB', str(path)), patch.object(feed, '_exposure_warning') as warning:
                got = feed._held()
            self.assertEqual(got['IN'], {'HOUSE', 'PERSONAL'})
            self.assertIn('broker,protection', warning.call_args.args[0])
            self.assertEqual(path.read_bytes(), before)

    def test_pending_unknown_and_confirmed_residual_inventory_remain_hot(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'paper.db'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE v2_positions(market,symbol)')
                con.execute('CREATE TABLE user_positions(market,symbol)')
                con.execute('CREATE TABLE v2_live_protection(symbol)')
                con.execute('CREATE TABLE v2_live_orders(user_id,market,symbol,instrument_key,status,side,filled_qty)')
                con.executemany('INSERT INTO v2_live_orders VALUES(?,?,?,?,?,?,?)', [
                    (2,'IN','WAIT','NSE_EQ|WAIT','unknown','BUY',0),
                    (2,'IN','LEFT','NSE_EQ|A','filled','BUY',10),
                    (2,'IN','LEFT','NSE_EQ|A','filled','SELL',5),
                    # A separate contract cannot cancel the residual exposure.
                    (2,'IN','LEFT','NSE_EQ|B','filled','SELL',5),
                    (2,'IN','DONE','NSE_EQ|DONE','filled','BUY',10),
                    (2,'IN','DONE','NSE_EQ|DONE','filled','SELL',10)])
                con.execute("INSERT INTO v2_live_protection VALUES('STOP')")
            with patch.object(feed, 'V2_DB', str(path)), patch.object(feed, '_exposure_warning'):
                self.assertEqual(feed._held()['IN'], {'WAIT','LEFT','STOP'})

    def test_disabled_equity_is_quote_only_and_does_not_expand_full_universe(self):
        active = dict(symbol='ACTIVE', exchange='NSE', enabled=1, upstox_instrument_key='NSE_EQ|ACTIVE')
        retired = dict(symbol='HELD', exchange='NSE', enabled=0, upstox_instrument_key='NSE_EQ|HELD')
        db = Mock(); db.runtime_settings.return_value = {}
        db.get_universe.side_effect = lambda enabled_only, market_region: [active] if enabled_only else [active, retired]
        with patch.object(feed,'Database',return_value=db), patch.object(feed,'settings_from_overrides',side_effect=lambda s,o:s), \
             patch.object(feed,'MARKETS',{'IN':'upstox'}), patch.object(feed,'build_market_data_provider'), \
             patch.object(feed,'WATCH_HOT',{}):
            _, _, full, mappings = feed._build()
            hot = feed._hot_rows(mappings, {'IN':{'HELD'}}, set())
        self.assertEqual(full['IN'], [active])
        self.assertEqual(hot['IN'], [retired])
        self.assertEqual(retired['enabled'], 0)

    def test_conflicting_venues_or_aliases_are_not_silently_chosen(self):
        row = dict(symbol='SAME', exchange='NSE', upstox_instrument_key='NSE_EQ|A', isin='A')
        for other in (dict(row,exchange='BSE'),dict(row,upstox_instrument_key='NSE_EQ|B')):
            self.assertEqual(feed._quote_map([row,other]), {})
        self.assertEqual(feed._quote_map([row,dict(row)]), {'SAME':row})

    def test_quote_map_refresh_sees_new_disabled_holdings_and_retains_prior_on_failure(self):
        db = Mock(); new = dict(symbol='NEW',exchange='NSE',enabled=0,upstox_instrument_key='NSE_EQ|NEW')
        db.get_universe.return_value = [new]
        with patch.object(feed,'MARKETS',{'IN':'upstox'}):
            updated = feed._refresh_quote_map(db, {'IN':{}})
            self.assertEqual(updated['IN'], {'NEW':new})
            db.get_universe.side_effect = sqlite3.OperationalError('fixture unavailable')
            self.assertEqual(feed._refresh_quote_map(db, updated), updated)
        db.get_universe.assert_called_with(enabled_only=False,market_region='IN')


if __name__ == '__main__': unittest.main()
