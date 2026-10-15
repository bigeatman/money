"""Holding and order reconciliation across market sessions."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

import test_engine as fixtures
from money.engine import Engine
from money.kiwoom import KiwoomBroker
from money.store import Store


class OvernightTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    plan = fixtures.ExecutionTests.plan
    buy = fixtures.ExecutionTests.buy
    orders = fixtures.ExecutionTests.orders
    enter = fixtures.ExecutionTests.enter

    def next_plan(self, actions, pid="next", market="KR"):
        self.clock.time += timedelta(days=1)
        plan = self.plan(actions, pid, market)
        for field in ("opens_at", "closes_at"):
            old = datetime.fromisoformat(plan["session"][field])
            plan["session"][field] = (old + timedelta(days=1)).isoformat()
        return plan

    def hold(self, market="KR"):
        return {"action": "HOLD", "symbol": "005930" if market == "KR" else "TEST",
                "exchange": "KRX" if market == "KR" else "ND", "reason": "carry confirmed shares"}

    def test_cutoff_cancels_only_unfilled_buys_and_keeps_partial_shares(self):
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        buy = self.orders("BUY")[0]
        self.broker.fill(buy["broker_id"], 4, 1000)
        self.engine.tick()
        self.clock.time = datetime(2026, 10, 14, 6, 15, tzinfo=timezone.utc)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(buy["status"], "closed")
        self.assertEqual(buy["filled"], 4)
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 4)
        self.assertEqual([(o["qty"], o["price"]) for o in self.orders("SELL")], [(4, "1030")])

    def test_next_day_hold_keeps_shares_and_replaces_confirmed_cancelled_day_order(self):
        self.enter()
        cash = self.store.state["cash"]
        old = self.orders("SELL")[0]
        self.broker.cancel("KR", "005930", "KRX", old["broker_id"])
        self.engine.submit(self.next_plan([self.hold()]))
        for _ in range(3):
            self.engine.tick()
        position = self.store.state["positions"]["KR:005930"]
        self.assertEqual((position["qty"], position["liquidating"]), (10, False))
        self.assertEqual(self.store.state["cash"], cash)
        self.assertEqual(len(self.orders("SELL")), 2)
        self.assertEqual(self.orders("SELL")[-1]["price"], "1030")
        self.assertEqual(self.orders("SELL")[-1]["date"], "20261015")

    def test_new_session_buy_can_coexist_with_carried_holdings(self):
        self.enter()
        self.engine.submit(self.next_plan([self.buy(2)]))
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 2)
        self.assertFalse(self.store.state["positions"]["KR:005930"]["liquidating"])

    def test_restart_and_stale_calendar_cannot_force_or_create_orders(self):
        self.enter()
        cash, order_count = self.store.state["cash"], len(self.store.state["orders"])
        self.store.close()
        self.clock.time += timedelta(days=1)
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.engine.tick()
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 10)
        self.assertEqual(self.store.state["cash"], cash)
        self.assertEqual(len(self.store.state["orders"]), order_count)

    def test_explicit_sell_still_exits_at_entry_cutoff(self):
        self.enter()
        self.clock.time = datetime(2026, 10, 14, 6, 15, tzinfo=timezone.utc)
        action = dict(self.hold(), action="SELL", quantity=0)
        plan = self.plan([action], "explicit-exit")
        plan["expires_at"] = (self.clock() + timedelta(minutes=10)).isoformat()
        self.engine.submit(plan)
        for _ in range(3):
            self.engine.tick()
        self.assertIsNone(self.orders("SELL")[-1]["price"])
        self.assertEqual(self.orders("SELL")[-1]["qty"], 10)

    def test_kr_dated_history_accounts_for_late_fill_before_expiring_remainder(self):
        self.enter()
        sell = self.orders("SELL")[0]
        self.engine.submit(self.next_plan([self.hold()]))
        original = self.broker.history
        def history(market, date):
            result = original(market, date)
            result[sell["broker_id"]] = {"symbol": "005930", "side": "SELL", "qty": 10,
                "filled": 3, "remaining": 0, "amount": "3090", "status": "expired"}
            return result
        self.broker.history = history
        self.engine.tick()
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 7)
        self.assertEqual(len(self.orders("SELL")), 1)
        cash = self.store.state["cash"]
        self.engine.tick()
        self.assertEqual(sell["status"], "closed")
        self.assertEqual((self.orders("SELL")[-1]["qty"], self.orders("SELL")[-1]["price"]), (7, "1030"))
        self.assertEqual(self.store.state["cash"], cash)

    def test_missing_prior_day_history_never_releases_sell_reservation(self):
        self.enter()
        sell = self.orders("SELL")[0]
        self.engine.submit(self.next_plan([self.hold()]))
        self.broker.history = lambda *args: {}
        for _ in range(3):
            self.engine.tick()
        self.assertEqual((sell["status"], sell["remaining"]), ("open", 10))
        self.assertEqual(len(self.orders("SELL")), 1)

    def test_us_close_keeps_target_and_holdings(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        buy = {"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": 1,
               "buy_limit": "100", "target": "105", "reason": "fixture"}
        self.engine.submit(self.plan([buy], "us", "US"))
        self.engine.tick()
        order = self.orders("BUY")[0]
        self.broker.fill(order["broker_id"], 1, 100)
        self.engine.tick()
        self.clock.time = datetime(2026, 10, 14, 19, 55, tzinfo=timezone.utc)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual([(o["qty"], o["price"]) for o in self.orders("SELL")], [(1, "105")])
        self.assertEqual(self.store.state["positions"]["US:TEST"]["qty"], 1)

    def test_us_prior_unresolved_order_is_not_treated_as_cancelled(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        buy = dict(self.hold("US"), action="BUY", quantity=1, buy_limit="100", target="105")
        self.engine.submit(self.plan([buy], "us", "US"))
        self.engine.tick()
        self.broker.fill(self.orders("BUY")[0]["broker_id"], 1, 100)
        self.engine.tick()
        self.engine.submit(self.next_plan([self.hold("US")], "next-us", "US"))
        self.broker.history = lambda market, date: KiwoomBroker.history(self.broker, market, date)
        with patch("money.kiwoom.now", return_value=self.clock.time):
            self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 1)
        self.assertEqual(self.store.state["positions"]["US:TEST"]["qty"], 1)
        self.assertIn("today-only", self.store.state["events"][-1]["message"])

    def test_fresh_next_day_limits_replace_old_auction_ceiling(self):
        self.enter()
        old = self.orders("SELL")[0]
        self.broker.cancel("KR", "005930", "KRX", old["broker_id"])
        p = self.store.state["positions"]["KR:005930"]
        p["auction_upper_limit"] = "1000"
        self.engine.submit(self.next_plan([self.hold()]))
        self.broker.mode = "kiwoom-demo"
        self.broker.quotes["KR:005930"]["upper_limit"] = "1400"
        self.broker.account = lambda market: {"positions": {"005930": {"qty": 10, "available": 10}},
                                               "available_cash": "1000000"}
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(p["auction_upper_limit"], "1400")
        self.assertEqual(self.orders("SELL")[-1]["date"], "20261015")
        self.assertEqual(self.orders("SELL")[-1]["price"], "1030")

    def test_policy_is_reported_in_read_only_execution_status(self):
        policy = self.engine.status()["execution_capabilities"]["holding_policy"]
        self.assertFalse(policy["force_close_at_session_end"])
        self.assertFalse(policy["force_close_on_session_rollover"])
        self.assertTrue(policy["overnight_holdings"])
        self.assertFalse(self.store.state["orders"])

    def test_restart_removes_legacy_time_exit_flag_without_changing_shares_or_cash(self):
        self.enter()
        cash = self.store.state["cash"]
        self.store.state.pop("holding_policy_version")
        self.store.state["positions"]["KR:005930"]["liquidating"] = True
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        for _ in range(3):
            self.engine.tick()
        position = self.store.state["positions"]["KR:005930"]
        self.assertEqual((position["qty"], position["liquidating"]), (10, False))
        self.assertEqual(self.store.state["cash"], cash)
        self.assertTrue(all(o["price"] is not None for o in self.orders("SELL")))

    def test_restart_preserves_proven_legacy_explicit_sell_intent(self):
        self.enter()
        action = dict(self.hold(), action="SELL", quantity=0)
        self.engine.submit(self.plan([action], "explicit-exit"))
        self.engine.tick()
        position = self.store.state["positions"]["KR:005930"]
        self.store.state.pop("holding_policy_version")
        position.pop("liquidation_source")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.assertTrue(position["liquidating"])
        self.assertEqual(position["liquidation_source"], "explicit_SELL")
        for _ in range(3):
            self.engine.tick()
        self.assertIsNone(self.orders("SELL")[-1]["price"])

    def test_prior_buy_cash_stays_reserved_until_expiry_confirmation(self):
        self.engine.submit(self.plan([self.buy(100)]))
        self.engine.tick()
        purchase = self.orders("BUY")[0]
        plan = self.next_plan([self.hold()])
        self.engine.submit(plan)
        self.broker.history = lambda *args: {purchase["broker_id"]: {
            "symbol": "005930", "qty": 100, "side": "BUY", "filled": 0,
            "amount": "0", "remaining": 0, "status": "expired"}}
        self.engine.tick()
        self.assertLess(self.engine.available(), Decimal("900000"))
        self.engine.tick()
        self.assertEqual(purchase["status"], "closed")
        self.assertEqual(self.engine.available(), Decimal("1000000"))


if __name__ == "__main__":
    unittest.main()
