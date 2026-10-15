import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from money.kiwoom import KiwoomBroker, MOCK_URL, Rejected, order_key


class FixtureBroker(KiwoomBroker):
    def __init__(self, responses):
        super().__init__()
        self.responses = responses
        self.calls = []

    def call(self, market, api, path, body, **kw):
        self.calls.append((market, api, path, body, kw))
        return self.responses[api]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        current = patch("money.kiwoom.now", return_value=datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc))
        current.start()
        self.addCleanup(current.stop)

    def test_order_routes_only_use_mock_endpoint_and_official_order_types(self):
        self.assertEqual(MOCK_URL, "https://mockapi.kiwoom.com")
        b = FixtureBroker({"kt10000": {"ord_no": "0000123"}, "ust20001": {"ord_no": "000000456"}})
        b.place("KR", "005930", "KRX", "BUY", 3, 1000)
        b.place("US", "TEST", "ND", "SELL", 1, None)
        self.assertEqual(b.calls[0][3], {"stk_cd": "005930", "ord_qty": "3", "ord_uv": "1000", "dmst_stex_tp": "KRX", "trde_tp": "0"})
        self.assertEqual(b.calls[1][3]["trde_tp"], "03")
        self.assertTrue(b.calls[1][4]["mutation"])

    def test_kr_execution_ids_deduplicate_fills_and_keep_pending_quantity(self):
        base = {"ord_no": "0000123", "stk_cd": "A005930", "ord_qty": "10", "io_tp_nm": "+매수",
                "cntr_no": "77", "cntr_qty": "2", "cntr_uv": "1000"}
        second = dict(base, cntr_no="78", cntr_qty="3", cntr_uv="999")
        b = FixtureBroker({"kt00009": {"acnt_ord_cntr_prst_array": [base, base, second]},
            "ka10075": {"oso": [{"ord_no": "00000123", "stk_cd": "A005930", "ord_qty": "10", "oso_qty": "5", "io_tp_nm": "+매수"}]}})
        with patch("money.kiwoom.now", return_value=datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)):
            h = b.history("KR", "20261014")["123"]
        self.assertEqual((h["filled"], h["remaining"], h["amount"]), (5, 5, "4997"))

    def test_kr_market_buy_uses_market_type_and_no_order_price(self):
        b = FixtureBroker({"kt10000": {"ord_no": "123"}})
        b.place("KR", "010140", "KRX", "BUY", 1, None)
        self.assertEqual(b.calls[0][3], {"stk_cd": "010140", "ord_qty": "1", "ord_uv": "", "dmst_stex_tp": "KRX", "trde_tp": "3"})
        self.assertTrue(b.calls[0][4]["mutation"])

    def test_kr_prior_day_order_uses_dated_history_and_ignores_reused_pending_id(self):
        row = {"ord_no": "123", "stk_cd": "A005930", "ord_qty": "10", "io_tp_nm": "-매도",
               "cntr_no": "77", "cntr_qty": "3", "cntr_uv": "1030"}
        b = FixtureBroker({"kt00009": {"acnt_ord_cntr_prst_array": [row]},
            "ka10075": {"oso": [{"ord_no": "123", "stk_cd": "A000660", "ord_qty": "1",
                                    "oso_qty": "1", "io_tp_nm": "+매수"}]}})
        h = b.history("KR", "20261014")["123"]
        self.assertEqual((h["filled"], h["remaining"], h["amount"], h["status"]), (3, 0, "3090", "expired"))
        self.assertEqual(h["symbol"], "005930")
        self.assertEqual([c[1] for c in b.calls], ["kt00009"])
        self.assertEqual(b.calls[0][3]["ord_dt"], "20261014")

    def test_kr_missing_prior_day_order_is_not_invented_as_expired(self):
        b = FixtureBroker({"kt00009": {"acnt_ord_cntr_prst_array": []}})
        self.assertEqual(b.history("KR", "20261014"), {})

    def test_kr_quote_exposes_signed_daily_limits_with_zero_auction_book(self):
        b = FixtureBroker({"ka10001": {"cur_prc": "18950", "base_pric": "18950", "upl_pric": "+24600", "lst_pric": "-13270"},
                          "ka10004": {"buy_fpr_bid": "0", "sel_fpr_bid": "0"}})
        q = b.quote("KR", "010140", "KRX")
        self.assertEqual((q["bid"], q["ask"], q["reference_price"], q["upper_limit"], q["lower_limit"]),
                         ("0", "0", "18950", "24600", "13270"))

    def test_us_legacy_collection_spelling_and_open_order_confirmation(self):
        row = {"ord_no": "00012", "ord_qty": "10", "cntr_qty": "3", "ord_remnq": "0",
               "cntr_uv": "100", "stk_cd": "TEST", "slby_tp_nm": "매수"}
        b = FixtureBroker({"ust21510": {"result_lsit": [row]},
            "ust21050": {"result_list": [dict(row, ord_no="12", ord_remnq="7", cntr_uv="100.0000")]}})
        h = b.history("US", "20261014")["12"]
        self.assertEqual((h["filled"], h["remaining"]), (3, 7))

    def test_missing_collection_is_not_interpreted_as_no_positions_or_orders(self):
        b = FixtureBroker({"ust21510": {"return_code": 0}})
        with self.assertRaises(ValueError):
            b.history("US", "20261014")

    def us_row(self, **changes):
        row = {"ord_no": "000004430", "orig_ord_no": "000000000", "stk_cd": "CRM",
               "slby_tp": "1", "slby_tp_nm": "매도", "ord_qty": "000000000100",
               "cntr_qty": "000000000100", "cntr_uv": "234.6360", "cnfm_qty": "000000000100",
               "ord_remnq": "000000000000", "ord_stat": "체결완료"}
        return dict(row, **changes)

    def test_live_demo_crm_fixture_uses_supported_today_api_and_reported_fill(self):
        row = self.us_row()
        b = FixtureBroker({"ust21510": {"result_list": [row]}, "ust21050": {"result_list": []}})
        h = b.history("US", "20261014")["4430"]
        self.assertEqual((h["filled"], h["remaining"], h["side"], h["qty"]), (100, 0, "SELL", 100))
        self.assertEqual(h["reported_fill_price"], "234.6360")
        self.assertEqual(h["amount"], "23463.6000")
        self.assertIn("rounding", h["amount_basis"])
        self.assertEqual([c[1] for c in b.calls], ["ust21510", "ust21050"])
        self.assertEqual(b.calls[1][3]["ord_dt"], "")
        self.assertEqual(b.execution_capabilities()["us_history_verified"]["trade_date"], "20261014")

    def test_order_confirmation_quantity_is_not_fill_quantity(self):
        row = self.us_row(cntr_qty="0", cntr_uv="0", ord_remnq="100", ord_stat="접수")
        b = FixtureBroker({"ust21510": {"result_list": [row]}, "ust21050": {"result_list": [row]}})
        h = b.history("US", "20261014")["4430"]
        self.assertEqual((h["filled"], h["remaining"], h["amount"]), (0, 100, "0"))

    def test_later_pending_fill_updates_cumulative_quantity_and_price_once(self):
        first = self.us_row(ord_qty="10", cntr_qty="3", cntr_uv="100", ord_remnq="7", ord_stat="부분체결")
        later = dict(first, cntr_qty="5", cntr_uv="99.8", ord_remnq="5")
        b = FixtureBroker({"ust21510": {"result_list": [first]}, "ust21050": {"result_list": [later]}})
        h = b.history("US", "20261014")["4430"]
        self.assertEqual((h["filled"], h["remaining"], h["amount"]), (5, 5, "499.0"))

    def test_duplicate_rows_are_not_double_counted_and_conflicts_fail(self):
        row = self.us_row()
        b = FixtureBroker({"ust21510": {"result_list": [row, row]}, "ust21050": {"result_list": []}})
        self.assertEqual(b.history("US", "20261014")["4430"]["filled"], 100)
        b.responses["ust21510"]["result_list"][1] = dict(row, cntr_qty="99")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            b.history("US", "20261014")
        self.assertIsNone(b.execution_capabilities()["us_history_verified"])

    def test_older_date_and_missing_pending_collection_cannot_verify_today_history(self):
        b = FixtureBroker({"ust21510": {"result_list": [self.us_row()]}, "ust21050": {"return_code": 0}})
        with self.assertRaisesRegex(ValueError, "today-only"):
            b.history("US", "20261013")
        self.assertFalse(b.calls)
        with self.assertRaises(ValueError):
            b.history("US", "20261014")
        self.assertIsNone(b.us_history_verified)

    def test_pending_fingerprint_or_invalid_price_does_not_confirm_a_fill(self):
        row = self.us_row(cntr_qty="3", ord_remnq="97")
        b = FixtureBroker({"ust21510": {"result_list": [row]},
            "ust21050": {"result_list": [dict(row, stk_cd="MSFT")]}})
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            b.history("US", "20261014")
        b.responses["ust21510"] = {"result_list": [dict(row, cntr_uv="0")]}
        with self.assertRaisesRegex(ValueError, "positive"):
            b.history("US", "20261014")

    def test_broker_business_rejection_with_http_success_is_rejected(self):
        b = KiwoomBroker()
        with patch.object(b, "_token", return_value="redacted"), patch.object(b, "_http", return_value=({"return_code": 3}, {})):
            with self.assertRaises(Rejected):
                b.call("KR", "kt10000", "/api/dostk/ordr", {}, mutation=True)

    def test_nonzero_code_preserves_detail_and_redacts_keys_and_token(self):
        b = KiwoomBroker()
        b.tokens["US"] = ("test-private-token", None)
        response = {"return_code": 20,
                    "return_msg": "diagnostic: test-app-key test-secret test-private-token"}
        with patch.dict("os.environ", {"KIWOOM_US_APPKEY": "test-app-key", "KIWOOM_US_SECRET": "test-secret"}), \
                patch.object(b, "_token", return_value="test-private-token"), \
                patch.object(b, "_http", return_value=(response, {})):
            with self.assertRaises(Rejected) as caught:
                b.call("US", "ust21070", "/api/us/acnt", {})
        message = str(caught.exception)
        self.assertIn("ust21070: code 20; diagnostic:", message)
        for private in ("test-app-key", "test-secret", "test-private-token"):
            self.assertNotIn(private, message)

    def test_timeout_identifies_api_without_retrying_a_mutating_request(self):
        b = KiwoomBroker()
        with patch.object(b, "_token", return_value="private-token"), \
                patch("money.kiwoom.urlopen", side_effect=TimeoutError("read timed out")) as request:
            with self.assertRaisesRegex(TimeoutError, "ust20001"):
                b.call("US", "ust20001", "/api/us/ordr", {}, mutation=True)
        self.assertEqual(request.call_count, 1)

    def test_read_timeout_retries_once_and_keeps_the_same_query(self):
        b = KiwoomBroker()
        response = ({"return_code": 0, "result_list": []}, {})
        with patch.object(b, "_token", return_value="private"), \
                patch.object(b, "_http", side_effect=[TimeoutError("read failed"), response]) as request, \
                patch("money.kiwoom.time.sleep"):
            self.assertEqual(b.call("US", "ust21070", "/api/us/acnt", {}), response[0])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])

    def test_permanent_business_error_is_not_retried(self):
        b = KiwoomBroker()
        with patch.object(b, "_token", return_value="private"), \
                patch.object(b, "_http", return_value=({"return_code": 20, "return_msg": "unknown business error"}, {})) as request:
            with self.assertRaises(Rejected):
                b.call("US", "ust21070", "/api/us/acnt", {})
        self.assertEqual(request.call_count, 1)

    def test_partial_account_retains_balance_and_signed_pnl_on_fx_timeout(self):
        b = FixtureBroker({
            "ust21070": {"crnc_code": "USD", "result_list": [], "tdy_pl_amt": "-505.4050", "tdy_pl_amt_krw": "-00000000676535"},
            "ust21110": {"result_list": [{"crnc_code": "USD", "fc_ord_alowa": "99000.291"}]}})
        original = b.call
        def call(*args, **kwargs):
            if args[1] == "ust21120":
                raise TimeoutError("ust21120 read failed")
            return original(*args, **kwargs)
        b.call = call
        report = b.account_report("US")
        self.assertFalse(report["complete"])
        self.assertEqual(report["positions"], {})
        self.assertEqual(report["available_cash"], "99000.291")
        self.assertIsNone(report["fx"])
        self.assertEqual(report["pnl"]["values"]["tdy_pl_amt"], "-505.4050")
        self.assertEqual(report["pnl"]["values"]["tdy_pl_amt_krw"], "-676535")
        with self.assertRaisesRegex(ValueError, "no new buy"):
            b.account("US")

    def test_missing_balance_collection_cannot_be_zero_holdings(self):
        b = FixtureBroker({"ust21070": {"return_code": 0},
            "ust21110": {"result_list": [{"crnc_code": "USD", "fc_ord_alowa": "100"}]},
            "ust21120": {"result_list": [{"crnc_code": "USD", "crnc_rt": "1300"}]}})
        report = b.account_report("US")
        self.assertIsNone(report["positions"])
        self.assertFalse(report["complete"])
        self.assertIn("balance", report["errors"])

    def test_missing_return_code_is_not_assumed_success(self):
        b = KiwoomBroker()
        with patch.object(b, "_token", return_value="redacted"), patch.object(b, "_http", return_value=({"ord_no": "1"}, {})):
            with self.assertRaises(ValueError):
                b.call("KR", "kt10000", "/api/dostk/ordr", {}, mutation=True)

    def test_order_padding_normalization(self):
        self.assertEqual(order_key("0000000123"), "123")
        self.assertEqual(order_key("p1"), "p1")


if __name__ == "__main__":
    unittest.main()
