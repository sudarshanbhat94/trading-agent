"""What we actually put on the wire to Upstox.

Every other test mocks place_order, which proves the decision logic and nothing
about the request. This one stands a real HTTP server in front of the real
httpx call and asserts the bytes.

Grounded in probes against the live Upstox API (no orders placed):

    invalid instrument   -> 400 UDAPI100011 "Invalid Instrument key"
    RBLBANK, quantity 0  -> 400 UDAPI1052  "quantity cannot be zero"

The second is the important one: it proves NSE_EQ|INE976G01028 resolves and
that only the deliberate zero quantity stopped it. Everything up to Upstox
accepting a valid order is therefore covered.
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import broker as _broker_mod

UID = 7


class _Upstox(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Upstox.seen.append(dict(path=self.path, body=body,
                                 auth=self.headers.get("Authorization"),
                                 ctype=self.headers.get("Content-Type")))
        # EXACT path: "/nope/order/place" also ends with "/order/place",
        # so a suffix check let a wrong base URL look like a filled order.
        if self.path != "/v2/order/place":
            out = json.dumps({"status": "error",
                              "errors": [{"errorCode": "UDAPI404"}]}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        out = json.dumps({"status": "success", "data": {"order_id": "TEST123"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class WirePayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Upstox)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self) -> None:
        _Upstox.seen.clear()
        tmp = tempfile.mkdtemp()
        os.environ["BROKER_STATE_DIR"] = os.path.join(tmp, "brokers")
        os.environ["BROKER_STATE_PATH"] = os.path.join(tmp, "legacy.json")
        os.environ["UPSTOX_ORDER_BASE_URL"] = f"http://127.0.0.1:{self.port}/v2"
        self.b = importlib.reload(_broker_mod)
        self.b.save_token(UID, "TESTTOKEN")

    def tearDown(self) -> None:
        os.environ.pop("UPSTOX_ORDER_BASE_URL", None)

    def _place(self, **kw):
        res = self.b.place_order(UID, "NSE_EQ|INE976G01028", 7, "BUY", **kw)
        return res, _Upstox.seen[-1]

    def test_it_hits_the_order_endpoint(self) -> None:
        _res, sent = self._place()
        self.assertEqual(sent["path"], "/v2/order/place")

    def test_it_sends_the_bearer_token(self) -> None:
        _res, sent = self._place()
        self.assertEqual(sent["auth"], "Bearer TESTTOKEN")
        self.assertEqual(sent["ctype"], "application/json")

    def test_the_body_matches_the_documented_schema(self) -> None:
        _res, sent = self._place()
        expected = {"quantity", "product", "validity", "price", "tag",
                    "instrument_token", "order_type", "transaction_type",
                    "disclosed_quantity", "trigger_price", "is_amo",
                    "market_protection"}
        self.assertEqual(set(sent["body"]), expected)

    def test_the_values_are_the_ones_upstox_accepts(self) -> None:
        """Probed live: this exact shape reached instrument validation, and with
        a valid key reached quantity validation."""
        _res, sent = self._place()
        b = sent["body"]
        self.assertEqual(b["instrument_token"], "NSE_EQ|INE976G01028")
        self.assertEqual(b["quantity"], 7)
        self.assertEqual(b["transaction_type"], "BUY")
        self.assertEqual(b["order_type"], "MARKET")
        self.assertEqual(b["product"], "D")
        self.assertEqual(b["validity"], "DAY")
        self.assertEqual(b["is_amo"], False)

    def test_a_market_order_carries_market_protection(self) -> None:
        """Live rejection that exposed this:
            UDAPI1158 "Market orders are not allowed. Try placing an order with
                       market protection."
        market_protection is a PERCENTAGE band around LTP, so 0 reads as
        "fill at any price" and Upstox refuses it."""
        _res, sent = self._place()
        self.assertGreater(sent["body"]["market_protection"], 0)
        self.assertEqual(sent["body"]["order_type"], "MARKET")

    def test_the_band_is_not_absurdly_wide(self) -> None:
        """It is the only thing standing between a thin book and a fill far
        from the price the decision was made at."""
        _res, sent = self._place()
        self.assertLessEqual(sent["body"]["market_protection"], 10)

    def test_a_limit_order_does_not_send_protection(self) -> None:
        """The field is meaningless when the price is already bounded."""
        self.b.place_order(UID, "NSE_EQ|INE976G01028", 7, "BUY",
                           order_type="LIMIT", price=380.0)
        self.assertEqual(_Upstox.seen[-1]["body"]["market_protection"], 0)

    def test_quantity_is_an_int_not_a_float(self) -> None:
        """Upstox rejects a float quantity, and Python division produces one."""
        res, sent = self.b.place_order(UID, "NSE_EQ|INE976G01028", 7.0, "BUY"), _Upstox.seen[-1]
        self.assertIsInstance(sent["body"]["quantity"], int)

    def test_a_sell_is_marked_as_one(self) -> None:
        self.b.place_order(UID, "NSE_EQ|INE976G01028", 7, "sell")
        self.assertEqual(_Upstox.seen[-1]["body"]["transaction_type"], "SELL")

    def test_the_order_id_is_read_back(self) -> None:
        res, _sent = self._place()
        self.assertTrue(res["ok"])
        self.assertEqual(res["order_id"], "TEST123")

    def test_a_rejection_is_reported_not_swallowed(self) -> None:
        """A failed order must never look like a placed one."""
        os.environ["UPSTOX_ORDER_BASE_URL"] = f"http://127.0.0.1:{self.port}/nope"
        b = importlib.reload(_broker_mod)
        b.save_token(UID, "TESTTOKEN")
        res = b.place_order(UID, "NSE_EQ|INE976G01028", 7, "BUY")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["order_id"])


class OrderHostTest(unittest.TestCase):
    def test_orders_use_the_documented_hft_host(self) -> None:
        os.environ.pop("UPSTOX_ORDER_BASE_URL", None)
        b = importlib.reload(_broker_mod)
        self.assertIn("api-hft.upstox.com", b.ORDER_BASE)

    def test_reads_still_use_the_general_host(self) -> None:
        b = importlib.reload(_broker_mod)
        self.assertIn("api.upstox.com", b.API_BASE)
        import inspect
        self.assertIn("API_BASE", inspect.getsource(b.funds))
        self.assertIn("ORDER_BASE", inspect.getsource(b.place_order))


if __name__ == "__main__":
    unittest.main()
