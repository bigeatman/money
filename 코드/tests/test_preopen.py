from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from money.engine import Engine
from money.paper import PaperBroker
from money.server import make_server
from money.store import Store


class PreopenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.time = datetime(2026, 10, 14, 23, 35, tzinfo=timezone.utc)
        self.opening = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
        self.clock = lambda: self.time
        self.config = json.loads(Path("config.example.json").read_text())
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.broker = PaperBroker(self.clock)
        self.broker.set_quote("KR", "005930", "KRX", 0, 0)
        self.broker.quotes["KR:005930"].update(last="1000", reference_price="1000",
                                              lower_limit="700", upper_limit="1300")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buy(self, qty=10, **changes):
        return dict({"action": "BUY", "symbol": "005930", "exchange": "KRX", "quantity": qty,
            "buy_limit": "1300", "target": "1030", "preopen": True, "order_type": "MARKET",
            "reason": "synthetic auction fixture"}, **changes)

    def plan(self, actions=None, pid="auction"):
        return {"plan_id": pid, "market": "KR", "generated_at": self.time.isoformat(),
            "expires_at": (self.time + timedelta(minutes=40)).isoformat(),
            "session": {"opens_at": self.opening.isoformat(),
                "closes_at": (self.opening + timedelta(hours=6, minutes=30)).isoformat(),
                "calendar_sources": ["https://example.org/fixture-calendar"]},
            "actions": [self.buy()] if actions is None else actions}

    def orders(self, side="BUY"):
        return [o for o in self.store.state["orders"].values() if o["side"] == side]

    def test_market_auction_submits_without_bid_ask_and_acceptance_is_not_a_fill(self):
        self.engine.submit(self.plan())
        with patch.object(self.broker, "place", wraps=self.broker.place) as place:
            self.engine.tick()
            self.engine.tick()
            self.assertEqual(place.call_count, 1)
            self.assertIsNone(place.call_args.args[-1])
        order = self.orders()[0]
        self.assertEqual((order["price"], order["reservation_price"], order["order_type"]), (None, "1300", "MARKET"))
        self.assertEqual(self.store.state["cash"], "1000000")
        self.assertFalse(self.store.state["positions"])
        self.assertEqual(self.engine.available(), Decimal("986954.5"))

    def test_market_buy_reserves_upper_limit_and_fees_and_cannot_spend_another_orders_cash(self):
        self.engine.submit(self.plan([self.buy(767)]))
        self.engine.tick()
        self.assertFalse(self.orders())
        self.engine.submit(self.plan([self.buy(766)], "affordable"))
        self.engine.tick()
        self.assertEqual(len(self.orders()), 1)
        self.engine.submit(self.plan([self.buy(1)], "extra"))
        self.engine.tick()
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.engine.available(), Decimal("714.7"))

    def test_partial_fill_releases_only_filled_reservation_and_adjusts_gap_target(self):
        self.engine.submit(self.plan([self.buy(500)]))
        self.engine.tick()
        order = self.orders()[0]
        self.broker.fill(order["broker_id"], 100, "1250")
        self.engine.tick()
        position = self.store.state["positions"]["KR:005930"]
        self.assertEqual(position["qty"], 100)
        self.assertEqual(Decimal(self.store.state["cash"]), Decimal("874562.5"))
        self.assertEqual(self.engine.available(), Decimal("352742.5"))
        self.assertGreater(Decimal(position["target"]) * 100 * Decimal("0.9945"), Decimal(position["basis_krw"]))
        self.assertFalse(self.orders("SELL"))
        cash = self.store.state["cash"]
        self.engine.tick()
        self.assertEqual(self.store.state["cash"], cash)
        self.time = self.opening
        self.engine.tick()
        sell = self.orders("SELL")[0]
        self.assertEqual(sell["qty"], 100)
        self.assertEqual(sell["price"], position["target"])

    def test_preopen_limit_order_does_not_require_live_top_of_book(self):
        self.engine.submit(self.plan([self.buy(order_type="LIMIT", buy_limit="1000")]))
        self.engine.tick()
        self.assertEqual(self.orders()[0]["price"], "1000")

    def test_gap_to_daily_upper_limit_keeps_shares_at_close(self):
        self.engine.submit(self.plan())
        self.engine.tick()
        order = self.orders()[0]
        self.broker.fill(order["broker_id"], 10, 1300)
        self.time = self.opening
        self.engine.tick()
        self.engine.tick()
        self.assertFalse(self.orders("SELL"))
        self.assertEqual(self.store.state["positions"]["KR:005930"]["profit_order_blocked"], "target_above_daily_upper_limit")
        events = [e for e in self.store.state["events"] if e["type"] == "profit_order_blocked"]
        self.assertEqual(len(events), 1)
        self.time = self.opening + timedelta(hours=6, minutes=15)
        self.engine.tick()
        self.assertFalse(self.orders("SELL"))
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 10)
        self.assertFalse(self.store.state["positions"]["KR:005930"]["liquidating"])

    def test_regular_limit_orders_still_wait_until_open_and_require_bid_ask(self):
        action = self.buy(preopen=False, order_type="LIMIT", buy_limit="1000")
        self.engine.submit(self.plan([action]))
        self.engine.tick()
        self.assertFalse(self.orders())
        self.time = self.opening
        self.engine.tick()
        self.assertFalse(self.orders())
        self.broker.quotes["KR:005930"].update(bid="999", ask="1000")
        self.engine.tick()
        self.assertEqual(len(self.orders()), 1)

    def test_price_ceiling_above_authorized_cap_rejects_market_order(self):
        self.engine.submit(self.plan([self.buy(buy_limit="1200")]))
        self.engine.tick()
        self.assertFalse(self.orders())
        self.assertEqual(self.store.state["commands"]["auction:0"]["state"], "rejected")

    def test_missing_or_inconsistent_daily_limits_never_fall_back_to_last_price(self):
        for field, value in [("reference_price", "0"), ("upper_limit", "0"),
                             ("lower_limit", "0"), ("upper_limit", "999")]:
            with self.subTest(field=field, value=value):
                q = self.broker.quotes["KR:005930"]
                original = q[field]
                q[field] = value
                self.engine.submit(self.plan(pid=field + value))
                self.engine.tick()
                self.assertFalse(self.orders())
                q[field] = original

    def test_stale_and_future_reference_responses_block_order(self):
        original = self.broker.quote
        for seconds in [-31, 2]:
            with self.subTest(seconds=seconds):
                self.engine.submit(self.plan(pid="age" + str(seconds)))
                q = original("KR", "005930", "KRX")
                q["fetched_at"] = (self.time + timedelta(seconds=seconds)).isoformat()
                with patch.object(self.broker, "quote", return_value=q):
                    self.engine.tick()
                self.assertFalse(self.orders())

    def test_no_auction_instruction_outside_window_or_for_us(self):
        for when in [self.opening - timedelta(minutes=30, seconds=1), self.opening]:
            self.time = when
            with self.assertRaisesRegex(ValueError, "opening auction"):
                self.engine.submit(self.plan())
        self.time = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
        plan = self.plan()
        plan["market"] = "US"
        plan["actions"][0].update(symbol="TEST", exchange="ND")
        plan["session"] = {"opens_at": "2026-10-14T13:30:00+00:00", "closes_at": "2026-10-14T20:00:00+00:00",
                           "calendar_sources": ["https://example.org/fixture"]}
        with self.assertRaisesRegex(ValueError, "KRX only"):
            self.engine.submit(plan)

    def test_market_buy_requires_explicit_preopen_and_never_applies_to_sell(self):
        with self.assertRaisesRegex(ValueError, "preopen=true"):
            self.engine.submit(self.plan([self.buy(preopen=False)]))
        for action in [self.buy(preopen="true"), self.buy(action="SELL"), self.buy(order_type="UNKNOWN")]:
            with self.assertRaises(ValueError):
                self.engine.submit(self.plan([action]))

    def test_unsent_auction_buy_expires_at_open_even_if_plan_lives_longer(self):
        self.engine.submit(self.plan())
        self.time = self.opening
        self.engine.tick()
        self.assertFalse(self.orders())
        self.assertEqual(self.store.state["commands"]["auction:0"]["state"], "expired")

    def test_slow_reference_query_crossing_open_cannot_send_market_order(self):
        self.time = self.opening - timedelta(seconds=1)
        self.engine.submit(self.plan())
        original = self.broker.quote
        def slow_quote(*args):
            result = original(*args)
            self.time += timedelta(seconds=2)
            return result
        with patch.object(self.broker, "quote", side_effect=slow_quote):
            self.engine.tick()
        self.assertFalse(self.orders())

    def test_restart_preserves_market_reservation_and_does_not_repeat_order(self):
        self.engine.submit(self.plan())
        self.engine.tick()
        available = self.engine.available()
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.engine.tick()
        self.assertEqual(self.engine.available(), available)
        self.assertEqual(len(self.orders()), 1)

    def test_unknown_market_order_keeps_cash_reserved_without_resubmitting(self):
        self.engine.submit(self.plan())
        with patch.object(self.broker, "place", side_effect=TimeoutError("ambiguous")) as place:
            self.engine.tick()
            self.engine = Engine(self.store, self.broker, self.config, self.clock)
            self.engine.tick()
            self.assertEqual(place.call_count, 1)
        self.assertTrue(self.engine.unknown())
        self.assertEqual(self.engine.available(), Decimal("986954.5"))

    def test_own_auction_order_can_be_cancelled_before_open(self):
        self.engine.submit(self.plan())
        self.engine.tick()
        ref, order = next(iter(self.store.state["orders"].items()))
        cancel = {"action": "CANCEL", "symbol": "005930", "exchange": "KRX", "order_ref": ref, "reason": "cancel fixture"}
        self.engine.submit(self.plan([cancel], "cancel"))
        self.engine.tick()
        self.assertEqual(order["status"], "cancel_requested")
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.available(), Decimal("1000000"))

    def test_reference_target_and_limit_band_checks_prevent_invalid_orders(self):
        for i, action in enumerate([self.buy(target="1005"), self.buy(order_type="LIMIT", buy_limit="1400", target="1450")]):
            self.engine.submit(self.plan([action], "invalid" + str(i)))
            self.engine.tick()
            self.assertFalse(self.orders())
            self.assertEqual(self.store.state["commands"]["invalid" + str(i) + ":0"]["state"], "rejected")

    def test_broker_account_failure_prevents_preopen_order(self):
        self.engine.submit(self.plan())
        with patch.object(self.broker, "account", side_effect=TimeoutError("account")):
            self.engine.tick()
        self.assertFalse(self.orders())

    def test_mcp_accepts_new_fields_and_returns_reserved_market_order(self):
        token = "x" * 64
        server = make_server(self.engine, token, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def rpc(name, args):
            data = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            req = Request(f"http://127.0.0.1:{server.server_port}/mcp/{token}", json.dumps(data).encode(),
                          {"Content-Type": "application/json"})
            with urlopen(req) as response:
                return json.load(response)["result"]
        try:
            result = rpc("submit_trading_plan", {"plan": self.plan()})
            self.assertFalse(result["isError"])
            self.engine.tick()
            result = rpc("get_execution_status", {})["structuredContent"]
            self.assertEqual(len(result["orders"]), 1)
            self.assertIsNone(next(iter(result["orders"].values()))["price"])
            snapshot = rpc("get_trading_snapshot", {"market": "KR", "instruments": [], "include_charts": False})
            self.assertTrue(snapshot["structuredContent"]["execution_capabilities"]["kr_preopen_buys"]["supported"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
