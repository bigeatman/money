import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from money.common import clean
from money.engine import Engine
from money.paper import PaperBroker
from money.server import make_server
from money.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db", "1000000")
        config = json.loads(Path("config.example.json").read_text())
        self.engine = Engine(self.store, PaperBroker(), config)
        self.token = "a" * 64
        self.server = make_server(self.engine, self.token, port=0, allowed_origins=["https://chatgpt.com"])
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def rpc(self, data, path=None, headers=None):
        req = Request(self.origin + (path or "/mcp/" + self.token), json.dumps(data).encode(),
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
        with urlopen(req) as response:
            return response.status, json.load(response) if response.status != 202 else None

    def test_mcp_initialize_tools_and_read_status(self):
        _, data = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(data["result"]["protocolVersion"], "2025-06-18")
        _, data = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({t["name"] for t in data["result"]["tools"]},
                         {"get_trading_snapshot", "submit_trading_plan", "get_execution_status",
                          "start_market_scan", "get_market_scan", "get_market_context"})
        _, data = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "get_execution_status", "arguments": {}}})
        self.assertFalse(data["result"]["isError"])
        self.assertEqual(data["result"]["structuredContent"]["mode"], "offline-paper")

    def test_notifications_are_accepted_without_body(self):
        code, body = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((code, body), (202, None))

    def test_unauthorized_and_untrusted_origin_denied(self):
        for path, headers, expected in [("/mcp", {}, 401),
            (None, {"Origin": "https://evil.example"}, 403)]:
            with self.assertRaises(HTTPError) as cm:
                self.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path, headers)
            self.assertEqual(cm.exception.code, expected)

    def test_read_status_with_bearer(self):
        with urlopen(Request(self.origin + "/status", headers={"Authorization": "Bearer " + self.token})) as r:
            self.assertEqual(json.load(r)["capital_krw"], "1000000")

    def test_secret_and_account_fields_are_recursively_removed(self):
        self.assertEqual(clean({"acnt_no": "123", "nested": {"secretkey": "x", "qty": 2},
                                "token": "secret"}), {"nested": {"qty": 2}})

    def test_diagnostic_endpoint_authenticates_and_exposes_read_result(self):
        payload = {"part": "balance"}
        with patch.object(self.engine, "diagnostic", return_value={"data": {"tdy_pl_amt": "-5"}}) as diagnose:
            _, data = self.rpc(payload, path="/diagnostics", headers={"Authorization": "Bearer " + self.token})
            self.assertTrue(data["ok"])
            self.assertEqual(data["result"]["data"]["tdy_pl_amt"], "-5")
            diagnose.assert_called_once_with("balance", None)
        with self.assertRaises(HTTPError) as caught:
            self.rpc(payload, path="/diagnostics")
        self.assertEqual(caught.exception.code, 401)

    def test_diagnostic_failure_is_returned_without_breaking_other_reads(self):
        with patch.object(self.engine, "diagnostic", side_effect=TimeoutError("ust21120")):
            _, data = self.rpc({"part": "rates"}, path="/diagnostics",
                               headers={"Authorization": "Bearer " + self.token})
            self.assertFalse(data["ok"])
            self.assertIn("ust21120", data["message"])
        self.assertFalse(self.store.state["orders"])

    def test_new_scan_and_context_tools_route_read_only_calls(self):
        cases = [
            ("start_market_scan", {"market": "KR", "force": True}, "market_scan", ("KR", True)),
            ("get_market_scan", {"market": "KR", "offset": 100, "limit": 10}, "market_scan_report",
             ("KR", None, "all", 100, 10)),
            ("get_market_context", {"market": "US", "instruments": [{"symbol": "MSFT", "exchange": "ND"}], "interval": 30},
             "market_context", ("US", [{"symbol": "MSFT", "exchange": "ND"}], 30))]
        for name, args, method, expected in cases:
            with self.subTest(name=name), patch.object(self.engine, method, return_value={"read_only": True}) as read:
                _, result = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
                self.assertFalse(result["result"]["isError"])
                read.assert_called_once_with(*expected)
        self.assertFalse(self.store.state["orders"])
        self.assertFalse(self.store.state["plans"])


if __name__ == "__main__":
    unittest.main()
