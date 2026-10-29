"""Actual local WebSocket frames exercise the read-only collector contract."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import portfolio_stream,v2_live
from scripts import broker_portfolio_feed


class PortfolioCollectorWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_binding_real_frames_durable_gap_no_book_changes(self):
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosedOK
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'paper.db';con=sqlite3.connect(path);v2_live.ensure_schema(con)
            book=con.execute('SELECT * FROM v2_book').fetchall();con.close()
            messages=[dict(update_type='order',order_id='unknown-external-order',quantity=20,filled_quantity=0,
                           average_price=0,instrument_token='NSE_EQ|TEST',product='D',transaction_type='SELL',status='open'),
                      dict(update_type='gtt_order',type='SINGLE',instrument_token='NSE_EQ|TEST',product='D',quantity=20,
                           gtt_order_id='GTT-fixture',expires_at=1,rules=[dict(strategy='ENTRY',trigger_type='BELOW',
                           trigger_price=95,transaction_type='SELL',status='COMPLETED',order_id='fixture-child')])]
            async def send(socket):
                for message in messages:await socket.send(json.dumps(message))
                await socket.close()
            async with serve(send,'127.0.0.1',0) as server:
                uri='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
                with patch.object(broker_portfolio_feed.broker,'portfolio_stream_access',return_value=('fixture-profile',uri)), \
                        patch.object(broker_portfolio_feed.broker,'place_order',side_effect=AssertionError('collector cannot trade')):
                    with self.assertRaises(ConnectionClosedOK):await broker_portfolio_feed.collect(2,path,duration=2)
            con=sqlite3.connect(path)
            try:
                self.assertEqual(con.execute('SELECT * FROM v2_book').fetchall(),book)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM v2_live_orders').fetchone()[0],0)
                report=portfolio_stream.report(con,2)
                self.assertFalse(report['connected'])
                self.assertEqual(report['event_counts'],{'connection':1,'order':1,'gtt_order':1,'gap':1})
                self.assertEqual(portfolio_stream.report(con,3)['event_counts'],{})
            finally:con.close()
