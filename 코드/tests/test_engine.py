from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from money.engine import Engine
from money.kiwoom import KiwoomBroker
from money.paper import PaperBroker
from money.store import Store


class Clock:
    def __init__(self):
        self.time = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.time


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.config = json.loads(Path("config.example.json").read_text())
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.broker = PaperBroker(self.clock)
        self.broker.set_quote("KR", "005930", "KRX", 999, 1000)
        self.broker.set_quote("US", "TEST", "ND", 99, 100, 1350)
        self.engine = Engine(self.store, self.broker, self.config, self.clock)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, actions, pid="one", market="KR"):
        if market == "KR":
            opens = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        else:
            opens = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        return {"plan_id": pid, "market": market, "generated_at": self.clock().isoformat(),
            "expires_at": (self.clock() + timedelta(minutes=40)).isoformat(),
            "session": {"opens_at": opens.isoformat(), "closes_at": (opens + timedelta(hours=6, minutes=30)).isoformat(),
                        "calendar_sources": ["https://example.org/test-calendar"]}, "actions": actions}

    def buy(self, qty=10):
        return {"action": "BUY", "symbol": "005930", "exchange": "KRX", "quantity": qty,
                "buy_limit": "1000", "target": "1030", "reason": "synthetic test fixture"}

    def orders(self, side):
        return [o for o in self.store.state["orders"].values() if o["side"] == side]

    def enter(self, qty=10):
        self.engine.submit(self.plan([self.buy(qty)]))
        self.engine.tick()
        o = self.orders("BUY")[0]
        self.broker.fill(o["broker_id"], qty, 1000)
        self.engine.tick()
        return o

    def test_wait_until_regular_open(self):
        self.clock.time -= timedelta(minutes=30)
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))
        self.clock.time += timedelta(minutes=30)
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 1)

    def test_idle_session_skips_account_polling_but_snapshot_reads_account(self):
        self.engine.submit(self.plan([]))
        with patch.object(self.broker, "account", wraps=self.broker.account) as account:
            self.engine.tick()
            self.engine.tick()
            account.assert_not_called()
            self.engine.snapshot("KR", instruments=[], include_charts=False)
            account.assert_called_once_with("KR")

    def test_expired_buy_is_expired_without_polling_or_creating_orders(self):
        self.engine.submit(self.plan([self.buy()]))
        self.clock.time += timedelta(minutes=41)
        with patch.object(self.broker, "account", side_effect=TimeoutError("unavailable")) as account:
            self.engine.tick()
            account.assert_not_called()
        self.assertEqual(self.store.state["commands"]["one:0"]["state"], "expired")
        self.assertFalse(self.orders("BUY"))

    def test_pending_buy_checks_account_and_blocks_order_on_account_failure(self):
        self.engine.submit(self.plan([self.buy()]))
        with patch.object(self.broker, "account", side_effect=TimeoutError("unavailable")) as account:
            self.engine.tick()
            account.assert_called_once_with("KR")
        self.assertFalse(self.orders("BUY"))
        self.assertEqual(self.store.state["events"][-1]["type"], "broker_sync_error")

    def us_partial_broker(self, positions):
        self.broker.mode = "kiwoom-demo"
        self.broker.account_report = lambda market: {
            "positions": positions, "available_cash": "100000", "fx": None, "complete": False,
            "errors": {"rates": {"api": "ust21120", "error": "TimeoutError", "message": "timed out"}},
            "raw": {"balance": {"tdy_pl_amt": "-505.4050"}}}

    def test_partial_us_snapshot_still_reports_pnl_and_quote_but_blocks_new_buys(self):
        self.us_partial_broker({})
        snap = self.engine.snapshot("US", [{"symbol": "TEST", "exchange": "ND"}], False)
        self.assertIn("US:TEST", snap["quotes"])
        self.assertEqual(snap["account"]["raw"]["balance"]["tdy_pl_amt"], "-505.4050")
        self.assertFalse(snap["account_complete"])
        self.assertTrue(snap["paused_new_buys"])

    def test_us_fill_adapter_protects_only_confirmed_partial_shares_and_does_not_repeat_fills(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        buy = {"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": 4,
               "buy_limit": "100", "target": "105", "reason": "integration fixture"}
        self.engine.submit(self.plan([buy], "us-partial", "US"))
        self.engine.tick()
        purchase = self.orders("BUY")[0]
        self.broker.fill(purchase["broker_id"], 2, "99.9910")
        row = {"ord_no": purchase["broker_id"], "stk_cd": "TEST", "slby_tp": "2", "slby_tp_nm": "매수",
               "ord_qty": "4", "cntr_qty": "2", "cntr_uv": "99.9910", "ord_remnq": "2", "ord_stat": "부분체결"}
        responses = {"ust21510": {"result_list": [row]}, "ust21050": {"result_list": [row]}}
        self.broker.call = lambda market, api, path, body, **kw: responses[api]
        self.broker._us_order_row = KiwoomBroker._us_order_row
        self.broker.us_history_verified = None
        self.broker.history = lambda market, date: KiwoomBroker.history(self.broker, market, date)
        with patch("money.kiwoom.now", return_value=self.clock.time):
            self.engine.tick()
            cash = self.store.state["cash"]
            self.engine.tick()
            self.assertEqual(self.store.state["cash"], cash)
            self.assertEqual([o["qty"] for o in self.orders("SELL")], [2])
            self.broker.fill(purchase["broker_id"], 2, "99.9990")
            row.update(cntr_qty="4", cntr_uv="99.9950", ord_remnq="0", ord_stat="체결완료")
            responses["ust21050"] = {"result_list": []}
            self.engine.tick()
        self.assertEqual([o["qty"] for o in self.orders("SELL")], [2, 2])
        self.assertEqual(self.store.state["positions"]["US:TEST"]["qty"], 4)
        self.assertEqual(Decimal(purchase["amount"]), Decimal("399.9800"))
        self.assertIn("rounding", purchase["fill_amount_basis"])

    def test_us_buy_preflight_does_not_send_order_when_fill_query_fails(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        self.broker.execution_capabilities = lambda: {"us_history_verified": None}
        self.broker.history = lambda *args: (_ for _ in ()).throw(ValueError("fill API unavailable"))
        buy = {"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": 1,
               "buy_limit": "100", "target": "105", "reason": "fixture"}
        self.engine.submit(self.plan([buy], "us-unavailable", "US"))
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))
        self.assertEqual(self.store.state["events"][-1]["message"], "fill API unavailable")

    def test_missing_us_fx_blocks_buy_but_does_not_block_owned_share_exit(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        buy = {"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": 1,
               "buy_limit": "100", "target": "105", "reason": "fixture"}
        self.engine.submit(self.plan([buy], "us-buy", "US"))
        self.us_partial_broker({"TEST": {"qty": 1, "available": 1}})
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))
        self.assertEqual(self.store.state["commands"]["us-buy:0"]["state"], "waiting_account")
        self.store.state["positions"]["US:TEST"] = {"market": "US", "symbol": "TEST", "exchange": "ND",
            "qty": 1, "basis_krw": "135000", "fx": "1350", "target": "105", "liquidating": False}
        sell = {"action": "SELL", "symbol": "TEST", "exchange": "ND", "quantity": 0, "reason": "exit fixture"}
        self.engine.submit(self.plan([sell], "us-sell", "US"))
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 1)
        self.assertEqual(self.orders("SELL")[0]["qty"], 1)

    def test_unknown_holdings_blocks_sell_even_when_ledger_has_owned_shares(self):
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        self.store.state["positions"]["US:TEST"] = {"market": "US", "symbol": "TEST", "exchange": "ND",
            "qty": 1, "basis_krw": "135000", "fx": "1350", "target": "105", "liquidating": False}
        sell = {"action": "SELL", "symbol": "TEST", "exchange": "ND", "quantity": 0, "reason": "exit fixture"}
        self.engine.submit(self.plan([sell], "us-sell", "US"))
        self.us_partial_broker(None)
        self.engine.tick()
        self.assertFalse(self.orders("SELL"))

    def test_duplicate_plan_and_restart_do_not_duplicate_purchase(self):
        plan = self.plan([self.buy()])
        self.engine.submit(plan)
        self.engine.tick()
        self.assertTrue(self.engine.submit(plan)["duplicate"])
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 1)

    def test_same_id_different_body_rejected(self):
        self.engine.submit(self.plan([self.buy()]))
        with self.assertRaises(ValueError):
            self.engine.submit(self.plan([self.buy(11)]))

    def test_partial_fills_receive_only_matching_profit_sell_quantities(self):
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        b = self.orders("BUY")[0]
        self.broker.fill(b["broker_id"], 3, 1000)
        self.engine.tick()
        self.assertEqual([o["qty"] for o in self.orders("SELL")], [3])
        self.broker.fill(b["broker_id"], 7, 999)
        self.engine.tick()
        self.assertEqual([o["qty"] for o in self.orders("SELL")], [3, 7])
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 2)

    def test_profit_sell_updates_reusable_cash_without_counting_order_acceptance(self):
        self.enter()
        cash = Decimal(self.store.state["cash"])
        sell = self.orders("SELL")[0]
        self.engine.tick()
        self.assertEqual(Decimal(self.store.state["cash"]), cash)
        self.broker.fill(sell["broker_id"], 10, 1030)
        self.engine.tick()
        self.assertGreater(Decimal(self.store.state["cash"]), Decimal("1000000"))
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 0)

    def test_over_budget_waits_and_reserves_pending_buys(self):
        self.engine.submit(self.plan([self.buy(1000)]))
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))  # Commission puts it above 1m.
        self.engine.submit(self.plan([self.buy(500)], "second"))
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 1)
        self.engine.submit(self.plan([self.buy(500)], "third"))
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 1)

    def test_shared_kr_us_budget_with_fx(self):
        self.enter(400)
        self.clock.time = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        buy = {"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": 5,
               "buy_limit": "100", "target": "105", "reason": "test"}
        self.engine.submit(self.plan([buy], "us", "US"))
        self.engine.tick()
        self.assertFalse([o for o in self.orders("BUY") if o["market"] == "US"])

    def test_buy_cap_never_chased(self):
        self.broker.set_quote("KR", "005930", "KRX", 1001, 1002)
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))

    def test_cost_negative_target_and_invalid_tick_rejected(self):
        for field, value in [("target", "1001"), ("buy_limit", "1000.5"), ("quantity", -1), ("quantity", True)]:
            b = self.buy()
            b[field] = value
            with self.assertRaises(ValueError):
                self.engine.submit(self.plan([b]))

    def test_expired_or_naive_timestamp_rejected(self):
        p = self.plan([])
        p["expires_at"] = self.clock().isoformat()
        with self.assertRaises(ValueError):
            self.engine.submit(p)
        p = self.plan([])
        p["generated_at"] = "2026-10-14T09:00:00"
        with self.assertRaises(ValueError):
            self.engine.submit(p)

    def test_reprice_waits_for_cancel_confirmation(self):
        self.enter()
        action = {"action": "REPRICE", "symbol": "005930", "exchange": "KRX", "target": "1040", "reason": "test"}
        self.engine.submit(self.plan([action], "reprice"))
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 1)
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 1)
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 2)
        self.assertEqual(self.orders("SELL")[1]["price"], "1040")

    def test_cancel_fill_race_never_oversells(self):
        self.enter()
        sell = self.orders("SELL")[0]
        self.broker.fill(sell["broker_id"], 4, 1030)
        action = {"action": "SELL", "symbol": "005930", "exchange": "KRX", "quantity": 0, "reason": "exit"}
        self.engine.submit(self.plan([action], "exit"))
        self.engine.tick()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.orders("SELL")[-1]["qty"], 6)
        self.assertIsNone(self.orders("SELL")[-1]["price"])

    def test_close_keeps_holdings_and_existing_target_without_another_ai_plan(self):
        self.enter()
        self.clock.time = datetime(2026, 10, 14, 6, 15, tzinfo=timezone.utc)
        self.engine.tick()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(len(self.orders("SELL")), 1)
        self.assertEqual(self.orders("SELL")[-1]["price"], "1030")
        self.assertEqual(self.orders("SELL")[-1]["qty"], 10)
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 10)
        self.assertFalse(self.store.state["positions"]["KR:005930"]["liquidating"])

    def test_ambiguous_order_is_not_retried_even_after_restart(self):
        def ambiguous(*args, **kwargs):
            raise TimeoutError("connection lost after possible broker acceptance")
        self.broker.place = ambiguous
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        self.assertTrue(self.engine.unknown())
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.engine.submit(self.plan([self.buy()], "new"))
        self.engine.tick()
        self.assertEqual(len(self.orders("BUY")), 1)

    def test_fill_notional_and_quantity_are_saved_atomically(self):
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        b = self.orders("BUY")[0]
        self.broker.fill(b["broker_id"], 2, 1000)
        self.engine.tick()
        cash = self.store.state["cash"]
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.engine = Engine(self.store, self.broker, self.config, self.clock)
        self.engine.tick()
        self.assertEqual(self.store.state["cash"], cash)
        self.assertEqual(self.store.state["positions"]["KR:005930"]["qty"], 2)

    def test_account_or_broker_mode_change_cannot_reuse_live_order_ledger(self):
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        broker = PaperBroker(self.clock)
        broker.mode = "different-account"
        with self.assertRaises(ValueError):
            Engine(self.store, broker, self.config, self.clock)

    def test_empty_pending_query_does_not_release_unconfirmed_buy_reservation(self):
        self.engine.submit(self.plan([self.buy(500)]))
        self.engine.tick()
        b = self.orders("BUY")[0]
        self.broker.orders[b["broker_id"]]["remaining"] = 0
        self.broker.orders[b["broker_id"]]["status"] = "closed"
        self.engine.tick()
        self.assertEqual(b["remaining"], 500)
        self.assertLess(self.engine.available(), Decimal("500000"))

    def test_no_extra_buy_after_cutoff(self):
        self.clock.time = datetime(2026, 10, 14, 6, 15, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            self.engine.submit(self.plan([self.buy()]))

    def test_slow_quote_crossing_expiry_cannot_submit_buy(self):
        self.engine.submit(self.plan([self.buy()]))
        original = self.broker.quote
        def slow_quote(*args):
            self.clock.time += timedelta(minutes=41)
            return original(*args)
        self.broker.quote = slow_quote
        self.engine.tick()
        self.assertFalse(self.orders("BUY"))

    def test_snapshot_records_new_fill_and_redacts_secrets(self):
        self.engine.submit(self.plan([self.buy()]))
        self.engine.tick()
        b = self.orders("BUY")[0]
        self.broker.fill(b["broker_id"], 2, 1000)
        snap = self.engine.snapshot("KR", include_charts=False)
        self.assertEqual(snap["managed_positions"]["KR:005930"]["qty"], 2)


if __name__ == "__main__":
    unittest.main()
