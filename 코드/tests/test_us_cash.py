"""US mock cash validation: no network or broker orders in these fixtures."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from money.engine import Engine
from money.kiwoom import KiwoomBroker, Rejected
from money.paper import PaperBroker
from money.store import Store


def cash_response(**changes):
    usd = {"crnc_code": "USD", "fc_entra": "1000", "fc_pymn_alowa": "1000",
           "fc_ord_alowa": "2000", "fc_ch_uncla": "0", "fc_etc_loana": "0",
           "futr_repl_profa": "0", "futr_profa_booka": "0"}
    usd.update(changes)
    return {"ch_uncla": "0", "etc_loana": "0", "result_list": [usd]}


def pending_row(**changes):
    row = {"ord_no": "00012", "stk_cd": "TEST", "ord_qty": "10", "cntr_qty": "4",
           "ord_remnq": "6", "cntr_uv": "100", "ord_uv": "100", "slby_tp": "2",
           "slby_tp_nm": "매수", "ord_stat": "부분체결", "crnc_code": "USD", "frgn_trde_tp": "00"}
    row.update(changes)
    return row


class CashFixture(KiwoomBroker):
    def __init__(self, cash=None, pending=None):
        super().__init__()
        self.responses = {"ust21110": cash if cash is not None else cash_response(),
                          "ust21050": {"result_list": pending or []}}
        self.calls = []

    def call(self, market, api, path, body, **kwargs):
        self.calls.append((market, api, path, body, kwargs))
        value = self.responses[api]  # Unexpected APIs, including ust31490, fail.
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class CashCapacityTests(unittest.TestCase):
    def test_supported_cash_queries_replace_unsupported_quantity_api(self):
        b = CashFixture(cash_response(fc_entra="75963.880", fc_pymn_alowa="75963.880", fc_ord_alowa="99192.839"))
        self.assertEqual(b.orderable("US", "OPK", "ND", "1.62", buy_fee="0.0025"), 46774)
        self.assertEqual([x[1] for x in b.calls], ["ust21050", "ust21110"])
        self.assertTrue(all(x[4]["pages"] and not x[4].get("mutation") for x in b.calls))
        self.assertEqual(b.execution_capabilities()["adapter_version"], "us-demo-v6")

    def test_smallest_cash_balance_and_buy_fee_bound_integer_quantity(self):
        b = CashFixture(cash_response(fc_pymn_alowa="800"))
        self.assertEqual(b.orderable("US", "TEST", "ND", "100", buy_fee="0.0025"), 7)
        self.assertEqual(b.orderable("US", "TEST", "ND", "100", buy_fee="0"), 8)

    def test_all_pending_buys_including_external_orders_reserve_remaining_cost(self):
        b = CashFixture(pending=[pending_row()])
        self.assertEqual(b.orderable("US", "OTHER", "NY", "100", buy_fee="0.0025"), 3)
        p = b.us_buying_power_verified
        self.assertEqual(Decimal(p["pending_buy_reserve_usd"]), Decimal("601.5000"))
        self.assertEqual(Decimal(p["available_usd"]), Decimal("398.5000"))

    def test_same_padded_pending_order_is_reserved_once(self):
        b = CashFixture(pending=[pending_row(), pending_row(ord_no="12")])
        self.assertEqual(b.orderable("US", "TEST", "ND", "100"), 4)
        b.responses["ust21050"]["result_list"][1]["ord_remnq"] = "5"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            b.us_cash_buying_power()

    def test_pending_sales_never_add_expected_proceeds(self):
        b = CashFixture(pending=[pending_row(slby_tp="1", slby_tp_nm="매도")])
        self.assertEqual(b.orderable("US", "TEST", "ND", "100", buy_fee="0.0025"), 9)

    def test_debt_or_futures_collateral_blocks_cash_only_buys(self):
        for field in ("fc_ch_uncla", "fc_etc_loana", "futr_repl_profa", "futr_profa_booka"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "zero debt"):
                    CashFixture(cash_response(**{field: "1"})).us_cash_buying_power()
        for field in ("ch_uncla", "etc_loana"):
            cash = cash_response()
            cash[field] = "1"
            with self.assertRaisesRegex(ValueError, "zero debt"):
                CashFixture(cash).us_cash_buying_power()

    def test_missing_or_bad_cash_data_never_reuses_old_verified_capacity(self):
        for field in ("fc_entra", "fc_pymn_alowa", "fc_ord_alowa", "fc_ch_uncla", "fc_etc_loana"):
            for value in (None, "", "NaN", "Infinity"):
                with self.subTest(field=field, value=value):
                    b = CashFixture()
                    b.us_cash_buying_power()
                    b.responses["ust21110"]["result_list"][0][field] = value
                    with self.assertRaises((ValueError, KeyError)):
                        b.us_cash_buying_power()
                    self.assertIsNone(b.us_buying_power_verified)
        b = CashFixture()
        del b.responses["ust21110"]["result_list"][0]["fc_entra"]
        with self.assertRaises(KeyError):
            b.us_cash_buying_power()

    def test_missing_or_duplicate_usd_or_missing_order_collection_blocks(self):
        for cash in ({"result_list": []}, {"result_list": [cash_response()["result_list"][0]] * 2}):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                CashFixture(cash).us_cash_buying_power()
        b = CashFixture()
        b.responses["ust21050"] = {"return_code": 0}
        with self.assertRaisesRegex(ValueError, "array"):
            b.us_cash_buying_power()

    def test_unbounded_or_unconfirmed_pending_order_blocks(self):
        for changes in ({"frgn_trde_tp": "03"}, {"crnc_code": "JPY"}, {"ord_uv": "0"},
                        {"ord_uv": "-1"}, {"ord_no": ""}, {"ord_remnq": "0", "ord_stat": "확인중"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CashFixture(pending=[pending_row(**changes)]).us_cash_buying_power()

    def test_negative_cash_is_zero_not_absolute_and_reserves_can_exhaust_cash(self):
        self.assertEqual(CashFixture(cash_response(fc_pymn_alowa="-20")).orderable("US", "TEST", "ND", "1"), 0)
        self.assertEqual(CashFixture(cash_response(fc_entra="500"), [pending_row()]).orderable("US", "TEST", "ND", "1"), 0)

    def test_query_failure_is_not_a_permissive_fallback(self):
        for api in ("ust21050", "ust21110"):
            b = CashFixture()
            b.responses[api] = Rejected("RC9002: demo query delayed")
            with self.assertRaises(Rejected):
                b.orderable("US", "TEST", "ND", "100")
            self.assertIsNone(b.us_buying_power_verified)

    def test_invalid_price_or_fee_and_non_demo_are_blocked_without_query(self):
        for price, fee in (("0", "0"), ("-1", "0"), ("NaN", "0"), ("1", "-0.1"), ("1", "1")):
            b = CashFixture()
            with self.assertRaises(ValueError):
                b.orderable("US", "TEST", "ND", price, buy_fee=fee)
            self.assertFalse(b.calls)
        b = CashFixture()
        b.mode = "other"
        with self.assertRaises(ValueError):
            b.orderable("US", "TEST", "ND", "1")
        self.assertFalse(b.calls)
        self.assertIsNone(CashFixture().orderable("KR", "005930", "KRX", "1000"))


class CashEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.time = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
        self.config = json.loads(Path("config.example.json").read_text())
        self.store = Store(Path(self.tmp.name) / "state.sqlite", "1000000")
        self.broker = PaperBroker(lambda: self.time)
        self.broker.set_quote("US", "TEST", "ND", "99.99", "100", "1350")
        self.cash = CashFixture()
        self.broker.orderable = self.cash.orderable
        self.engine = Engine(self.store, self.broker, self.config, lambda: self.time)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buy(self, qty):
        plan = {"plan_id": "cash-test", "market": "US", "generated_at": self.time.isoformat(),
                "expires_at": (self.time + timedelta(minutes=5)).isoformat(),
                "session": {"opens_at": "2026-10-15T09:30:00-04:00", "closes_at": "2026-10-15T16:00:00-04:00",
                            "calendar_sources": ["https://example.org/fixture"]},
                "actions": [{"action": "BUY", "symbol": "TEST", "exchange": "ND", "quantity": qty,
                             "buy_limit": "100", "target": "103", "reason": "synthetic fixture"}]}
        self.engine.submit(plan)
        self.engine.tick()
        return self.store.state["commands"]["cash-test:0"]

    def test_valid_cash_still_requires_shared_strategy_budget(self):
        command = self.buy(8)  # $800 exceeds the shared KRW 1m capital.
        self.assertEqual(command["state"], "waiting_cash")
        self.assertFalse(self.broker.orders)

    def test_exact_buy_fee_reaches_cash_check_and_valid_buy_can_be_sent(self):
        self.cash.responses["ust21110"] = cash_response(fc_entra="100.20")
        command = self.buy(1)
        self.assertEqual(command["state"], "waiting_broker_cash")
        self.assertFalse(self.broker.orders)
        self.cash.responses["ust21110"] = cash_response(fc_entra="100.25")
        self.engine.tick()
        self.assertEqual(command["state"], "done")
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.cash.us_buying_power_verified["buy_fee"], "0.0025")

    def test_cash_read_failure_prevents_broker_order(self):
        self.cash.responses["ust21110"] = TimeoutError("cash read failed")
        self.buy(1)
        self.assertFalse(self.broker.orders)
        self.assertEqual(self.store.state["events"][-1]["message"], "cash read failed")

    def test_slow_cash_check_cannot_send_after_quote_age_or_plan_expiry(self):
        original = self.broker.orderable
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            self.time += timedelta(seconds=31)
            return result
        self.broker.orderable = slow
        command = self.buy(1)
        self.assertEqual(command["state"], "waiting_quote")
        self.assertFalse(self.broker.orders)
        self.time += timedelta(minutes=5)
        self.engine.tick()
        self.assertEqual(command["state"], "expired")
        self.assertFalse(self.broker.orders)

    def test_read_only_diagnostic_never_submits_a_plan_or_order(self):
        self.broker.mode = "kiwoom-demo"
        self.broker.us_cash_buying_power = self.cash.us_cash_buying_power
        result = self.engine.diagnostic("buying_power")
        self.assertEqual(result["data"]["available_usd"], "1000")
        self.assertFalse(self.store.state["commands"])
        self.assertFalse(self.store.state["orders"])


if __name__ == "__main__":
    unittest.main()
