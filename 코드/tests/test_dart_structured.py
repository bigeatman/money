"""Offline structured collection: real transport/archive/SQLite, no credentials."""
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import zipfile

from money.dart_client import DartClient
from money.dart_structured import PAY_APIS, PERIOD_APIS, benchmark_capacity, capacity
from money.fundamentals import Fundamentals
from money.server import make_server


class Provider:
    def __init__(self):
        self.calls = []
        self.guard = threading.Lock()
        self.active = self.peak = 0
        self.delay = 0
        self.override = None
        self.block = None
        self.entered = threading.Event()

    def __call__(self, url, timeout):
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[1]
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if k != "crtfc_key"}
        with self.guard:
            self.calls.append((endpoint, params))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.entered.set()
            if self.block is not None:
                self.block.wait(5)
            time.sleep(self.delay)
            result = self.override(endpoint, params) if self.override else None
            if result is None:
                result = self.response(endpoint, params)
            return io.BytesIO(result if isinstance(result, bytes) else json.dumps(result).encode())
        finally:
            with self.guard:
                self.active -= 1

    def response(self, endpoint, p):
        if endpoint == "corpCode.xml":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as z:
                z.writestr("CORPCODE.xml", "<result>" + "".join(
                    "<list><corp_code>" + corp + "</corp_code><stock_code>" + symbol
                    + "</stock_code><corp_name>Fixture</corp_name><modify_date>20260101</modify_date></list>"
                    for symbol, corp in [("005930", "00126380"), ("000660", "00164779")]) + "</result>")
            return buffer.getvalue()
        if endpoint == "company.json":
            return {"status": "000", "corp_code": p["corp_code"], "corp_name": "Fixture",
                    "stock_code": "005930" if p["corp_code"] == "00126380" else "000660"}
        if endpoint == "list.json":
            return {"status": "013", "message": "No data"}
        base = {k: v for k, v in p.items() if k in {"corp_code", "bsns_year", "reprt_code", "fs_div"}}
        base["rcept_no"] = "20260318000001"
        if endpoint == "fnlttSinglAcntAll.json":
            rows = [{**base, "sj_div": sj, "account_id": account, "thstrm_amount": value, "currency": "KRW"}
                    for sj, account, value in [("IS", "ifrs-full_Revenue", "1,000"),
                        ("IS", "dart_OperatingIncomeLoss", "200"), ("BS", "ifrs-full_Liabilities", "50"),
                        ("BS", "ifrs-full_Equity", "100"), ("BS", "ifrs-full_CurrentAssets", "30"),
                        ("BS", "ifrs-full_CurrentLiabilities", "15")]]
            return {"status": "000", "list": rows}
        if endpoint == "stockTotqySttus.json":
            base.update(istc_totqy="1,000", tesstk_co="(20)", stock_knd="보통주")
        else:
            base.update(sm="-", jan_salary_am="0", nm="Provider fact")
        return {"status": "000", "list": [base]}


class StructuredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.client = DartClient("a" * 40, interval=0, opener=self.provider)
        self.f = Fundamentals(Path(self.tmp.name) / "fundamentals.sqlite", client=self.client)
        self.plan = {"mode": "structured", "companies": [{"symbol": "005930", "corp_code": "00126380"}],
                     "years": [2025], "workers": 4, "request_interval_seconds": 0}

    def tearDown(self):
        if self.provider.block:
            self.provider.block.set()
        self.f.close()
        self.tmp.cleanup()

    def run_job(self, plan=None, job_id=None, retry_failed=False):
        out = self.f.collection_job("start", job_id, plan, retry_failed)
        self.f.structured.thread.join(20)
        self.assertFalse(self.f.structured.thread.is_alive())
        return self.f.collection_job("status", out["job_id"], limit=100)

    def test_all_structured_apis_raw_rows_calculations_and_exact_cache(self):
        out = self.run_job(self.plan)
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual(out["total"], 46)
        self.assertEqual(set(e for e, _ in self.provider.calls),
                         {"company.json", "list.json", "fnlttSinglAcntAll.json", *PERIOD_APIS.values(), PAY_APIS[0]})
        self.assertNotIn("document.xml", {e for e, _ in self.provider.calls})
        self.assertTrue(all(p["corp_code"] == "00126380" for _, p in self.provider.calls))
        with self.f.db() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM structured_rows WHERE endpoint='stockTotqySttus.json'")]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["provider"]["istc_totqy"], "1,000")
            self.assertEqual(rows[0]["numeric"]["istc_totqy"], "1000")
            self.assertEqual(rows[0]["calculated"]["shares_excluding_treasury"], "1020")
            self.assertEqual(rows[0]["provider"]["rcept_no"], "20260318000001")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM structured_calculations").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM issuers").fetchone()[0], 1)
        self.assertTrue(self.f.verify_archive(limit=100)["all_ok"])
        self.assertEqual(len(self.f.report("005930")["rows"]), 4)
        calls = len(self.provider.calls)
        again = self.run_job(self.plan)
        self.assertEqual(len(self.provider.calls), calls)
        self.assertTrue(all(r["result"]["cache_hit"] for r in again["rows"]))
        self.assertTrue(self.run_job(job_id=out["job_id"])["collection_complete"])
        self.assertEqual(len(self.provider.calls), calls)

    def test_symbols_bootstrap_once_and_only_selected_issuer_is_inserted(self):
        self.plan.pop("companies")
        self.plan.update(symbols=["000660"], apis=["D1"])
        out = self.run_job(self.plan)
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual([e for e, _ in self.provider.calls], ["corpCode.xml", "company.json"])
        with self.f.db() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT symbol FROM issuers")], ["000660"])
        self.run_job(self.plan)
        self.assertEqual(len(self.provider.calls), 2)

    def test_actual_parallel_workers_and_one_second_responses(self):
        self.plan.update(apis=["D4"], workers=4)
        self.provider.delay = 1
        started = time.monotonic()
        out = self.run_job(self.plan)
        elapsed = time.monotonic() - started
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual(self.provider.peak, 4)
        self.assertEqual(out["execution"]["observed_peak_fetches"], 4)
        self.assertLess(elapsed, 3, out["execution"])
        timing = out["execution"]["timings_by_symbol"]["005930"]
        self.assertLess(timing["last_response_to_final_commit_ms"], 2000)
        print("\nSTRUCTURED_TIMING " + json.dumps(out["execution"], sort_keys=True))

    def test_worker_limit_multi_stock_fairness_and_resource_clamping(self):
        self.plan["companies"].append({"symbol": "000660", "corp_code": "00164779"})
        self.plan.update(apis=["D4"], workers=8)
        self.provider.delay = .04
        info = {**capacity(), "worker_ceiling": 2, "default_workers": 2}
        with patch("money.dart_structured.capacity", return_value=info):
            out = self.run_job(self.plan)
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual(self.provider.peak, 2)
        self.assertEqual({p["corp_code"] for _, p in self.provider.calls[:2]}, {"00126380", "00164779"})
        self.assertEqual(out["execution"]["effective_workers"], 2)

    def test_no_write_transaction_during_network_and_exclusive_legacy_gate(self):
        self.plan.update(apis=["D1"])
        self.provider.block = threading.Event()
        self.f.collection_job("start", plan=self.plan)
        self.assertTrue(self.provider.entered.wait(2))
        with self.f.db() as db:
            db.execute("INSERT INTO meta VALUES ('external_write','true')")
        with self.assertRaises(ValueError):
            self.f.call_api("company.json", {"corp_code": "00126380"})
        self.provider.block.set()
        self.f.structured.thread.join(5)

    def test_scope_mismatch_rolls_back_then_explicit_retry(self):
        self.plan.update(apis=["D4"], report_codes=["11011"])
        self.provider.override = lambda e, p: {"status": "000", "list": [{"corp_code": "99999999"}]}
        out = self.run_job(self.plan)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["rows"][0]["error"], "response_scope_mismatch")
        with self.f.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM structured_rows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_requests").fetchone()[0], 1)
        self.provider.override = None
        self.assertTrue(self.run_job(job_id=out["job_id"], retry_failed=True)["collection_complete"])
        self.assertEqual(len(self.provider.calls), 2)

    def test_empty_success_is_a_failure_and_no_data_is_not_zero(self):
        self.plan.update(apis=["D5"], report_codes=["11011"])
        self.provider.override = lambda e, p: {"status": "000"}
        out = self.run_job(self.plan)
        self.assertFalse(out["collection_complete"])
        self.assertEqual(out["rows"][0]["error"], "invalid_structured_rows")
        self.provider.override = None
        out = self.run_job(job_id=out["job_id"], retry_failed=True)
        self.assertTrue(out["collection_complete"], out)
        with self.f.db() as db:
            row = json.loads(db.execute("SELECT body FROM structured_rows").fetchone()[0])
        self.assertIsNone(row["numeric"]["sm"])
        self.assertEqual(row["numeric"]["jan_salary_am"], "0")

    def test_quota_pauses_and_resume_reuses_durable_successes(self):
        self.plan.update(apis=["D4"], workers=1)
        def fail_second(e, p):
            if p["reprt_code"] == "11012":
                return {"status": "020"}
        self.provider.override = fail_second
        out = self.run_job(self.plan)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(out["counts"], {"available": 1, "pending": 3})
        self.provider.override = None
        final = self.run_job(job_id=out["job_id"])
        self.assertTrue(final["collection_complete"], final)
        self.assertEqual(len(self.provider.calls), 5)
        self.assertEqual(self.provider.calls[0][1]["reprt_code"], "11013")

    def test_pagination_all_pages_and_changed_totals_are_not_complete(self):
        self.plan.update(apis=["D2"])
        def page(e, p):
            start, stop = (0, 100) if p["page_no"] == "1" else (100, 101)
            return {"status": "000", "total_count": 101, "total_page": 2, "list": [
                {"corp_code": p["corp_code"], "rcept_no": "20250601" + str(i).zfill(6),
                 "rcept_dt": "20250601", "report_nm": "Fixture"} for i in range(start, stop)]}
        self.provider.override = page
        out = self.run_job(self.plan)
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual(out["total"], 2)
        with self.f.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 101)
        def changed(e, p):
            result = page(e, p)
            if p["page_no"] == "2":
                result["total_count"] = 102
            return result
        self.provider.override = changed
        self.plan["refresh"] = True
        out = self.run_job(self.plan)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["last_error"], "pagination_changed")

    def test_d14_new_schema_no_data_checks_other_once_and_preserves_no_data(self):
        self.plan.update(apis=["D14"], years=[2026], report_codes=["11012"])
        self.provider.override = lambda e, p: {"status": "013"}
        out = self.run_job(self.plan)
        self.assertTrue(out["collection_complete"], out)
        self.assertEqual([e for e, _ in self.provider.calls], [PAY_APIS[1], PAY_APIS[0]])
        self.assertEqual(out["counts"], {"no_data": 2})

    def test_invalid_plans_rejected_before_any_network(self):
        for changes in [{"apis": ["D3"]}, {"apis": [[]]}, {"workers": True}, {"workers": 33},
                        {"request_interval_seconds": float("nan")}, {"years": []},
                        {"companies": [{"symbol": "005930;bad"}]}, {"years": [2025], "unexpected": 1},
                        {"companies": [{"symbol": "005930", "corp_code": "00126380"},
                                       {"symbol": "000660", "corp_code": "00126380"}]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.f.collection_job("start", plan={**self.plan, **changes})
        self.assertEqual(self.provider.calls, [])

    def test_capacity_is_diagnostic_not_a_claimed_measured_maximum(self):
        out = self.f.collection_job("status")
        self.assertEqual(out["mode"], "capacity")
        self.assertIsNone(out["capacity"]["maximum_tested_on_this_device"])
        self.assertTrue(1 <= out["capacity"]["worker_ceiling"] <= 32)
        self.assertEqual(out["capacity"]["sqlite_concurrent_writers"], 1)
        self.assertEqual(self.provider.calls, [])

    def test_local_device_benchmark_runs_real_pipeline_and_persists_summary_only(self):
        with patch("money.fundamentals.benchmark_capacity", side_effect=lambda directory: benchmark_capacity(directory, [2])):
            out = self.f.collection_job("status", plan={"mode": "capacity", "benchmark": True})
        measured = out["benchmark"]
        self.assertEqual(measured["maximum_verified_workers"], 2)
        self.assertEqual(measured["real_DART_requests"], 0)
        self.assertTrue(measured["results"][0]["complete"])
        self.assertEqual(self.provider.calls, [])
        with self.f.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM structured_rows").fetchone()[0], 0)
        self.assertEqual(self.f.collection_job("status")["capacity"]["maximum_tested_on_this_device"], 2)

    def test_reopen_can_read_and_resume_job_without_repeating_successes(self):
        self.plan.update(apis=["D1"])
        out = self.run_job(self.plan)
        self.f.close()
        self.f = Fundamentals(Path(self.tmp.name) / "fundamentals.sqlite", client=self.client)
        self.assertTrue(self.run_job(job_id=out["job_id"])["collection_complete"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_mcp_accepts_symbol_list_and_reports_capacity_without_schema_change(self):
        server = make_server(SimpleNamespace(manage_dart_collection=self.f.collection_job), "a" * 64, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def rpc(args):
            data = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "manage_dart_collection", "arguments": args}}
            request = Request("http://127.0.0.1:" + str(server.server_port) + "/mcp/" + "a" * 64,
                              json.dumps(data).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)["result"]["structuredContent"]
        try:
            self.assertEqual(rpc({"action": "status"})["mode"], "capacity")
            self.plan.update(apis=["D1"])
            job = rpc({"action": "start", "plan": self.plan})
            self.f.structured.thread.join(5)
            final = rpc({"action": "status", "job_id": job["job_id"]})
            self.assertTrue(final["collection_complete"], final)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
