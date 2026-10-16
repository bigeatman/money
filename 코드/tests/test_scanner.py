"""Full-universe transport/integrity and order-gate regression tests, no network."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from money.common import symbol
from money.engine import Engine
from money.kiwoom import KiwoomBroker
from money.marketdata import MarketData
from money.paper import PaperBroker
from money.scanner import MarketScanner
from money.scanner import UnavailableScanner
from money.store import Store


class Clock:
    def __init__(self, value="2026-10-16T00:15:00+00:00"):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value


def kr_item(code, market="0"):
    return {"code": code, "name": "Stock " + code, "marketCode": market,
            "lastPrice": "100", "state": "정상"}


def observation(code, volume="1000", price="101"):
    return {"stk_cd": code, "stk_nm": "Stock " + code, "cur_prc": price,
            "open_pric": "100", "high_pric": "105", "low_pric": "95",
            "flu_rt": "1", "trde_qty": volume, "acc_trde_qty": volume,
            "dt": "20261016", "cntr_tm": "091500", "bid_tm": "091500"}


class FixtureBroker:
    def __init__(self, count=121):
        self.calls = []
        self.kr = [kr_item(f"{i + 1:06d}") for i in range(count)]
        self.us = [{"stex_tp": e, "stk_cd": c, "stk_nm": c, "isEtf": "0"}
                   for e, c in [("NY", "AAA"), ("ND", "BBB"), ("NA", "CCC")]]
        self.us.append({"stex_tp": "NY", "stk_cd": "FUND", "stk_nm": "Fund", "isEtf": "1"})
        self.missing = set()
        self.bad_cursor = False
        self.volume = "1000"
        self.delay = None

    def scan_page(self, market, api, path, body, cursor=""):
        self.calls.append((market, api, path, copy.deepcopy(body), cursor))
        if self.delay is not None:
            self.delay.wait(2)
        if api == "ka10099":
            segment = body["mrkt_tp"]
            rows = self.kr if segment == "0" else [kr_item("900001", "10")] if segment == "10" else []
            if segment == "50":
                rows = [kr_item("900002", "50")]
            if segment == "8":
                rows = [kr_item("888880", "8")]
            if self.bad_cursor and segment == "0":
                return {"list": rows[:1]}, {"cont-yn": "Y", "next-key": "repeat"}
            return {"list": rows}, {"cont-yn": "N"}
        if api == "ka10095":
            return {"atn_stk_infr": [observation(c, self.volume) for c in body["stk_cd"].split("|")
                                      if c not in self.missing]}, {"cont-yn": "N"}
        if api == "usa10099":
            return {"list": self.us}, {"cont-yn": "N"}
        if api == "usa20570":
            return {"result_list": [dict(observation("AAA"), stex_tp="NY")]}, {"cont-yn": "N"}
        if api == "usa20530":
            return {"result_list": [dict(observation("AAA"), stex_tp="NY")]}, {"cont-yn": "N"}
        if api == "usa20100":
            code = body["stk_cd"]
            if code in self.missing:
                raise TimeoutError("synthetic per-stock read timeout")
            return observation(code, self.volume), {"cont-yn": "N"}
        raise AssertionError("unexpected API " + api)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.broker = FixtureBroker()
        self.scanner = MarketScanner(self.broker, Path(self.tmp.name) / "scans", clock=self.clock)

    def tearDown(self):
        self.scanner.close()
        for thread in list(self.scanner.threads.values()):
            thread.join(3)
        self.tmp.cleanup()

    def scan(self, market="KR", force=False):
        started = self.scanner.start(market, force)
        self.scanner.threads[started["scan_id"]].join(3)
        result = self.scanner.report(market, limit=500)
        self.assertNotEqual(result["status"], "running")
        return result

    def test_entire_master_is_queried_in_batches_and_not_capped_to_watchlist(self):
        out = self.scan()
        self.assertTrue(out["data_complete"])
        self.assertEqual(out["counts"], {"N": 124, "E": 1, "U": 123, "V": 123, "D": 0})
        self.assertTrue(out["count_identities_verified"])
        master = [x for x in self.broker.calls if x[1] == "ka10099"]
        quotes = [x for x in self.broker.calls if x[1] == "ka10095"]
        self.assertEqual({x[3]["mrkt_tp"] for x in master}, {"0", "10", "50", "8", "60", "70", "90"})
        requested = [c for x in quotes for c in x[3]["stk_cd"].split("|")]
        self.assertEqual(len(requested), 123)
        self.assertEqual(len(set(requested)), 123)
        self.assertNotIn("888880", requested)
        self.assertFalse(out["candidate_review_complete"])

    def test_all_pages_are_retrievable_without_losing_candidates(self):
        self.scan()
        ids, offset = [], 0
        while offset is not None:
            page = self.scanner.report("KR", view="candidate", offset=offset, limit=7)
            ids.extend(r["key"] for r in page["rows"])
            offset = page["next_offset"]
        self.assertEqual(len(ids), 123)
        self.assertEqual(len(set(ids)), 123)

    def test_one_missing_symbol_blocks_new_buys_instead_of_becoming_excluded(self):
        self.broker.missing.add("000121")
        out = self.scan()
        self.assertFalse(out["data_complete"])
        self.assertEqual(out["counts"]["D"], 1)
        row = next(r for r in out["rows"] if r["symbol"] == "000121")
        self.assertEqual(row["status"], "data_missing")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.scanner.require_complete("KR", "000001", "KRX")

    def test_repeated_cursor_never_counts_a_partial_inventory_as_complete(self):
        self.broker.bad_cursor = True
        out = self.scan()
        self.assertFalse(out["inventory_complete"])
        self.assertIsNone(out["counts"]["N"])
        self.assertTrue(any("cursor" in e.get("message", "") for e in out["errors"]))
        self.assertFalse(any(x[1] == "ka10095" for x in self.broker.calls))

    def test_us_ranking_is_not_the_universe_and_missing_rows_get_individual_reads(self):
        self.clock.value = datetime(2026, 10, 16, 14, tzinfo=timezone.utc)
        out = self.scan("US")
        self.assertTrue(out["data_complete"])
        self.assertEqual(out["counts"], {"N": 4, "E": 1, "U": 3, "V": 3, "D": 0})
        calls = self.broker.calls
        self.assertEqual(calls[0][1], "usa10099")
        singles = {x[3]["stk_cd"] for x in calls if x[1] == "usa20100"}
        self.assertEqual(singles, {"BBB", "CCC"})
        self.assertIsNone(next(r for r in out["rows"] if r["symbol"] == "AAA")["observation"]["provider_timestamp"])

    def test_us_missing_exchange_fails_inventory_before_any_quote_selection(self):
        self.broker.us = self.broker.us[:2]
        out = self.scan("US")
        self.assertFalse(out["inventory_complete"])
        self.assertFalse(any(x[1] != "usa10099" for x in self.broker.calls))

    def test_capped_us_bulk_is_partitioned_by_official_sectors_before_single_fallback(self):
        self.broker.us = [{"stex_tp": ["NY", "ND", "NA"][i % 3], "stk_cd": "S" + str(i).zfill(5),
                           "stk_nm": str(i), "isEtf": "0"} for i in range(301)]
        original = self.broker.scan_page
        def page(market, api, path, body, cursor=""):
            if api == "usa10101":
                self.broker.calls.append((market, api, path, body, cursor))
                return {"list": [{"inds_cd": "TECH"}, {"inds_cd": "FIN"}]}, {"cont-yn": "N"}
            if api in {"usa20530", "usa20570"} and body["inds_cd"] != "000":
                self.broker.calls.append((market, api, path, body, cursor))
                subset = self.broker.us[:150] if body["inds_cd"] == "TECH" else self.broker.us[150:]
                return {"result_list": [dict(observation(x["stk_cd"]), stex_tp=x["stex_tp"]) for x in subset]}, {"cont-yn": "N"}
            return original(market, api, path, body, cursor)
        self.broker.scan_page = page
        result = self.scan("US")
        self.assertTrue(result["data_complete"])
        self.assertEqual(result["counts"]["V"], 301)
        self.assertFalse(any(x[1] == "usa20100" for x in self.broker.calls))

    def test_fallback_failure_is_missing_data_not_a_false_zero_price(self):
        self.broker.missing.add("CCC")
        out = self.scan("US")
        self.assertFalse(out["data_complete"])
        self.assertEqual(out["counts"]["D"], 1)
        row = next(r for r in out["rows"] if r["symbol"] == "CCC")
        self.assertIsNone(row["observation"])

    def test_same_hour_start_reuses_job_and_force_does_not_overlap_running_job(self):
        self.broker.delay = threading.Event()
        a = self.scanner.start("KR")
        b = self.scanner.start("KR", force=True)
        self.assertEqual(a["scan_id"], b["scan_id"])
        self.broker.delay.set()
        self.scanner.threads[a["scan_id"]].join(3)
        self.assertEqual(self.scanner.start("KR")["scan_id"], a["scan_id"])

    def test_new_hour_refreshes_whole_inventory(self):
        a = self.scan()
        self.clock.value += timedelta(hours=1)
        b = self.scan()
        self.assertNotEqual(a["scan_id"], b["scan_id"])
        self.assertEqual(sum(x[1] == "ka10099" for x in self.broker.calls), 14)

    def test_force_retries_finished_incomplete_scan(self):
        self.broker.missing.add("000001")
        a = self.scan()
        self.broker.missing.clear()
        b = self.scan(force=True)
        self.assertNotEqual(a["scan_id"], b["scan_id"])
        self.assertTrue(b["data_complete"])

    def test_completed_scan_expires_and_previous_date_never_permits_buy(self):
        out = self.scan()
        self.scanner.require_complete("KR", "000001", "KRX")
        self.clock.value += timedelta(minutes=31)
        self.assertFalse(self.scanner.report("KR")["usable_for_new_buys"])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.scanner.require_complete("KR", "000001", "KRX")
        self.clock.value += timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "another trading date"):
            self.scanner.require_complete("KR", "000001", "KRX")

    def test_unknown_or_excluded_stock_never_inherits_scan_permission(self):
        self.scan()
        for code in ("888880", "999999"):
            with self.assertRaisesRegex(ValueError, "eligible"):
                self.scanner.require_complete("KR", code, "KRX")

    def test_missing_source_timestamp_is_reported_not_fabricated(self):
        raw = observation("000001")
        for key in ("dt", "cntr_tm", "bid_tm"):
            raw.pop(key)
        data = MarketData(self.broker, self.clock, lambda: None, lambda **k: None)
        out = data.normalize_kr(raw, self.clock().isoformat())
        self.assertIsNone(out["provider_timestamp"])
        self.assertIn("absent", out["timestamp_basis"])

    def test_no_trade_rows_are_waiting_and_do_not_manufacture_price_or_rvol(self):
        raw = observation("000001", volume="0")
        raw.update(open_pric="0", high_pric="0", low_pric="0")
        data = MarketData(self.broker, self.clock, lambda: None, lambda **k: None)
        obs = data.normalize_kr(raw, self.clock().isoformat())
        self.assertTrue(obs["no_session_trades"])
        row = {"exchange": "KRX", "observation": obs, "signals": []}
        self.scanner._features(row, None)
        self.assertEqual(row["signals"], [])
        self.assertIsNone(row["features"]["rvol"])

    def test_share_volume_acceleration_requires_two_real_intervals(self):
        self.scan()
        self.clock.value += timedelta(minutes=5)
        self.broker.volume = "2000"
        second = self.scan(force=True)
        row = next(r for r in second["rows"] if r["symbol"] == "000001")
        self.assertIsNone(row["features"]["activity_acceleration"])
        self.clock.value += timedelta(minutes=5)
        self.broker.volume = "4000"
        third = self.scan(force=True)
        row = next(r for r in third["rows"] if r["symbol"] == "000001")
        self.assertIn("share_volume_acceleration", row["signals"])
        self.assertIsNone(row["features"]["rvol"])

    def test_restart_keeps_complete_data_and_never_treats_interrupted_job_as_complete(self):
        out = self.scan()
        reopened = MarketScanner(self.broker, self.scanner.directory, clock=self.clock)
        self.assertTrue(reopened.report("KR")["data_complete"])
        path = self.scanner.directory / (out["scan_id"] + ".json")
        job = json.loads(path.read_text())
        job.update(status="running", data_complete=False)
        path.write_text(json.dumps(job))
        interrupted = MarketScanner(self.broker, self.scanner.directory, clock=self.clock)
        self.assertEqual(interrupted.report("KR")["status"], "interrupted")
        with self.assertRaises(ValueError):
            interrupted.require_complete("KR", "000001", "KRX")

    def test_invalid_paging_and_path_traversal_are_rejected(self):
        self.scan()
        for args in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 501},
                     {"scan_id": "../../.env"}, {"view": "top10"}):
            with self.assertRaises(ValueError):
                self.scanner.report("KR", **args)

    def test_persistence_failure_never_allows_buy_and_heartbeat_does_not_raise(self):
        with patch.object(self.scanner, "_write", side_effect=OSError("synthetic disk full")):
            self.scanner.heartbeat()
        out = self.scanner.report("KR")
        self.assertEqual(out["status"], "failed")
        self.assertFalse(out["data_complete"])
        with self.assertRaises(ValueError):
            self.scanner.require_complete("KR", "000001", "KRX")

    def test_nonfinite_unused_raw_fields_do_not_leak_into_completed_result(self):
        original = self.broker.scan_page
        def page(market, api, path, body, cursor=""):
            data, headers = original(market, api, path, body, cursor)
            if api == "ka10095":
                data["atn_stk_infr"][0]["unused_field"] = float("nan")
            return data, headers
        self.broker.scan_page = page
        out = self.scan()
        self.assertFalse(out["data_complete"])
        json.dumps(out, allow_nan=False)


class BrokerPageTests(unittest.TestCase):
    def test_scan_transport_cannot_submit_an_order(self):
        broker = KiwoomBroker()
        with self.assertRaisesRegex(ValueError, "read-only"):
            broker.scan_page("KR", "kt10000", "/api/dostk/ordr", {})

    def test_continuation_headers_and_token_refresh_use_only_read_calls(self):
        broker = KiwoomBroker()
        with patch.object(broker, "_token", return_value="private"), patch.object(broker, "_request",
                return_value=({"return_code": 0, "list": []}, {"cont-yn": "N"})) as read:
            broker.scan_page("KR", "ka10099", "/api/dostk/stkinfo", {"mrkt_tp": "0"}, "next")
        headers = read.call_args.args[2]
        self.assertEqual((headers["cont-yn"], headers["next-key"]), ("Y", "next"))
        self.assertFalse(read.call_args.kwargs.get("mutation"))

    def test_catalog_wait_does_not_hold_broker_lock(self):
        broker = KiwoomBroker()
        broker._catalog_at = 100.0
        observed = []
        def wait(seconds):
            acquired = []
            def other():
                with broker.lock:
                    acquired.append(True)
            t = threading.Thread(target=other);t.start();t.join(1)
            observed.append((seconds, bool(acquired)))
        with patch("money.kiwoom.time.monotonic", return_value=101.0), patch("money.kiwoom.time.sleep", side_effect=wait), \
             patch.object(broker, "_scan_page", return_value=({}, {})):
            broker.scan_page("US", "usa10099", "/api/us/stkinfo", {"stex_tp": "%"})
        self.assertAlmostEqual(observed[0][0], 11.1)
        self.assertTrue(observed[0][1])

    def test_alphanumeric_krx_codes_from_master_are_supported(self):
        symbol("KR", "0009K0", "KRX")
        for code in ("0009K0_NX", "../KEY", "0009k0"):
            with self.assertRaises(ValueError):
                symbol("KR", code, "KRX")


class EngineGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.tmp.name) / "ledger.sqlite", "1000000")
        self.config = json.loads(Path("config.example.json").read_text())
        self.scanner = MarketScanner(FixtureBroker(1), Path(self.tmp.name) / "scans", clock=self.clock)
        self.engine = Engine(self.store, PaperBroker(), self.config, clock=self.clock, scanner=self.scanner)

    def tearDown(self):
        self.scanner.close()
        for t in list(self.scanner.threads.values()):t.join(3)
        self.store.close()
        self.tmp.cleanup()

    def plan(self, action):
        return {"plan_id": "gate-" + action, "market": "KR", "generated_at": self.clock().isoformat(),
                "expires_at": (self.clock() + timedelta(minutes=5)).isoformat(),
                "session": {"opens_at": "2026-10-16T09:00:00+09:00", "closes_at": "2026-10-16T15:30:00+09:00",
                            "calendar_sources": ["https://www.krx.co.kr/"]},
                "actions": [{"action": action, "symbol": "000001", "exchange": "KRX", "reason": "fixture review",
                             **({"quantity": 1, "buy_limit": "100", "target": "110"} if action == "BUY" else {})}]}

    def test_buy_requires_complete_scan_before_ledger_mutation_but_hold_and_sell_do_not(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.engine.submit(self.plan("BUY"))
        self.assertFalse(self.store.state["plans"])
        self.engine.submit(self.plan("HOLD"))
        self.engine.submit(self.plan("SELL"))
        self.assertFalse(self.store.state["orders"])

    def test_scan_id_is_bound_to_queued_buy_and_rechecked_at_execution(self):
        started = self.scanner.start("KR")
        self.scanner.threads[started["scan_id"]].join(3)
        self.engine.submit(self.plan("BUY"))
        command = self.store.state["commands"]["gate-BUY:0"]
        self.assertEqual(command["market_scan_id"], started["scan_id"])
        self.clock.value += timedelta(minutes=31)
        self.engine._buy(command)
        self.assertEqual(command["state"], "waiting_market_scan")
        self.assertFalse(self.store.state["orders"])

    def test_scanner_configuration_failure_keeps_account_and_exit_interface_available(self):
        self.engine.scanner = UnavailableScanner("synthetic scanner storage failure")
        out = self.engine.snapshot("KR", [], False)
        self.assertEqual(out["market_scan"]["status"], "unavailable")
        self.engine.submit(self.plan("SELL"))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.engine.submit(self.plan("BUY"))


if __name__ == "__main__":
    unittest.main()
