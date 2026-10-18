"""Synthetic OpenDART data: pagination, resumability, corrections and token bounds.

No live credentials, broker calls, LLM requests, or orders are used.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zipfile

from money.cli import load_env
from money.cli import main as cli_main
from money.common import clean
from money.dart_client import DartClient, DartError, unpack, xml_root
from money.dart_data import metrics
from money.fundamentals import Fundamentals


def archive(text, filename="report.xml"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as out:
        out.writestr(filename, text.encode())
    return stream.getvalue()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def filing(receipt="20261015000001", corp="00126380", title="사업보고서 (2025.12)"):
    return {"rcept_no": receipt, "corp_code": corp, "corp_name": "Fixture", "stock_code": "005930",
            "rcept_dt": receipt[:8], "report_nm": title, "flr_nm": "Fixture", "corp_cls": "Y", "rm": ""}


class FixtureClient(DartClient):
    def __init__(self):
        super().__init__("a" * 40, interval=0)
        self.calls = []
        self.documents = [filing()]
        self.receipt = "20261015000001"
        self.revenue = "100"
        self.fail = {}
        self.master = ("<result><list><corp_code>00126380</corp_code><corp_name>기업 하나</corp_name>"
            "<stock_code>005930</stock_code><modify_date>20261015</modify_date></list>"
            "<list><corp_code>00164779</corp_code><corp_name>기업 둘</corp_name><stock_code>000660</stock_code>"
            "<modify_date>20261015</modify_date></list><list><corp_code>99999999</corp_code>"
            "<corp_name>비상장</corp_name><stock_code> </stock_code></list></result>")

    def get(self, endpoint, params):
        self.before_request()
        self.calls.append((endpoint, deepcopy(params)))
        if endpoint in self.fail:
            raise DartError(self.fail[endpoint])
        if endpoint == "corpCode.xml":
            return archive(self.master, "CORPCODE.xml")
        if endpoint == "company.json":
            return {"status": "000", "corp_code": params["corp_code"], "acc_mt": "12", "ceo_nm": "Fixture CEO"}
        if endpoint == "list.json":
            rows = [x for x in self.documents if x["corp_cls"] == params["corp_cls"]
                    and params["bgn_de"] <= x["rcept_dt"] <= params["end_de"]]
            if not rows:
                return {"status": "013"}
            page, count = params["page_no"], params["page_count"]
            return {"status": "000", "total_count": len(rows), "total_page": (len(rows)+count-1)//count,
                    "list": deepcopy(rows[(page-1)*count:page*count])}
        if endpoint == "fnlttSinglAcntAll.json":
            if params["corp_code"] != "00126380" or params["bsns_year"] != "2025" or params["reprt_code"] != "11011":
                return {"status": "013"}
            rows = []
            for statement, account, value in [("IS", "ifrs-full_Revenue", self.revenue),
                    ("IS", "dart_OperatingIncomeLoss", "20"), ("BS", "ifrs-full_Liabilities", "50"),
                    ("BS", "ifrs-full_Equity", "100"), ("BS", "ifrs-full_CurrentAssets", "30"),
                    ("BS", "ifrs-full_CurrentLiabilities", "15")]:
                rows.append({"corp_code": params["corp_code"], "bsns_year": "2025", "reprt_code": "11011",
                    "sj_div": statement, "account_id": account, "account_nm": account,
                    "thstrm_amount": value, "currency": "KRW", "rcept_no": self.receipt, "thstrm_nm": "제1기"})
            return {"status": "000", "list": rows}
        if endpoint == "document.xml":
            receipt = params["rcept_no"]
            value = self.revenue if receipt == self.receipt else "100"
            return archive("<DOCUMENT><TITLE>사업 내용</TITLE><P>매출 " + value + "</P><P>사업 위험 검토</P></DOCUMENT>")
        raise AssertionError(endpoint)


class FundamentalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fundamentals.sqlite"
        self.client, self.clock = FixtureClient(), Clock()
        self.config = {"disclosure_days": 2, "history_years": 2, "max_requests_per_run": 1000}
        self.cache = Fundamentals(self.path, self.config, self.client, self.clock)

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def sync(self):
        out = self.cache.sync()
        self.assertEqual(out["status"], "complete", out)
        self.assertEqual(out["pending_sources"], 0)
        return out

    def test_baseline_has_all_listed_issuers_both_bases_and_missing_is_not_zero(self):
        self.sync()
        page = self.cache.report(limit=1)
        self.assertEqual(page["companies"], 2)
        symbols = [page["rows"][0]["symbol"], self.cache.report(offset=page["next_offset"], limit=1)["rows"][0]["symbol"]]
        self.assertEqual(set(symbols), {"005930", "000660"})
        report = self.cache.report("005930", limit=100)
        rows = [x for x in report["rows"] if x["availability"] == "available"]
        self.assertEqual({x["request_basis"][-1] for x in rows}, {"CFS", "OFS"})
        self.assertEqual(rows[0]["data"]["ratios"]["operating_margin_pct"], "20.0000")
        self.assertTrue(all(x["data"] is None for x in self.cache.report("000660", limit=100)["rows"]))
        self.assertFalse(report["coverage"]["investment_review_complete"])

    def test_unchanged_run_only_rechecks_index_and_never_reloads_cached_sources(self):
        before = self.sync()["latest_change_seq"]
        self.client.calls.clear()
        self.assertEqual(self.sync()["latest_change_seq"], before)
        self.assertEqual({endpoint for endpoint, _ in self.client.calls}, {"list.json"})

    def test_correction_invalidates_only_affected_company_and_preserves_old_evidence(self):
        before = self.sync()["latest_change_seq"]
        self.client.calls.clear()
        receipt = "20261016000002"
        self.client.documents.append(filing(receipt, title="[기재정정]사업보고서 (2025.12)"))
        self.client.receipt, self.client.revenue = receipt, "200"
        after = self.sync()["latest_change_seq"]
        financial_calls = [p for e, p in self.client.calls if e == "fnlttSinglAcntAll.json"]
        self.assertTrue(financial_calls)
        self.assertEqual({p["corp_code"] for p in financial_calls}, {"00126380"})
        report = self.cache.report("005930", limit=100)
        available = [r for r in report["rows"] if r["availability"] == "available"]
        self.assertEqual(available[0]["data"]["ratios"]["operating_margin_pct"], "10.0000")
        result = self.cache.report(after_seq=before, through_seq=after, limit=100)
        doc = next(r for r in result["rows"] if r["source_key"] == "document:" + receipt)
        diff = self.cache.evidence(doc["diff_key"])["untrusted_source_text"]
        self.assertIn("20261015000001", diff)
        self.assertIn("200", diff)
        self.assertIn("100", self.cache.evidence("document:20261015000001")["untrusted_source_text"])

    def test_new_nonfinancial_disclosure_does_not_refetch_financial_statements(self):
        self.sync(); self.client.calls.clear()
        self.client.documents.append(filing("20261016000003", title="주요사항보고서(자기주식취득)"))
        self.sync()
        self.assertFalse(any(e == "fnlttSinglAcntAll.json" for e, _ in self.client.calls))
        self.assertTrue(any(e == "document.xml" for e, _ in self.client.calls))

    def test_next_day_overlap_and_long_offline_gap_do_not_drop_disclosures(self):
        self.sync()
        self.client.documents.append(filing("20261016000020", title="주요사항보고서(계약)"))
        self.clock.value += timedelta(days=1)
        self.sync()
        self.assertIn("20261016000020", self.cache.evidence("filing:20261016000020")["untrusted_source_text"])
        self.client.documents.append(filing("20261018000020", title="주요사항보고서(계약)"))
        self.clock.value += timedelta(days=10)
        self.sync()
        self.assertIn("20261018000020", self.cache.evidence("filing:20261018000020")["untrusted_source_text"])

    def test_wrong_company_financial_response_is_rejected_and_retried(self):
        original = self.client.get
        def wrong(endpoint, params):
            response = original(endpoint, params)
            if endpoint == "fnlttSinglAcntAll.json" and response["status"] == "000":
                response["list"][0]["corp_code"] = "99999999"
            return response
        self.client.get = wrong
        result = self.cache.sync()
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(any(r["availability"] == "available" for r in self.cache.report("005930", limit=100)["rows"]))
        with self.cache.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE error='invalid_financial_context'").fetchone()[0], 2)
        self.client.get = original
        self.clock.value += timedelta(hours=1)
        self.sync()

    def test_financial_row_order_and_transport_message_are_not_data_changes(self):
        self.sync()
        key = "financial:00126380:2025:11011:CFS"
        before = self.cache.status()["latest_change_seq"]
        with self.cache.db() as db:
            _, payload = self.cache._stored(db, key)
            payload["list"].reverse()
            payload["message"] = "OK"
            self.cache._save(db, key, "00126380", payload)
        self.assertEqual(self.cache.status()["latest_change_seq"], before)

    def test_unavailable_document_is_explicit_and_retried_after_ttl(self):
        self.client.fail["document.xml"] = "014"
        self.sync()
        with self.cache.db() as db:
            row, _ = self.cache._stored(db, "document:20261015000001")
            self.assertEqual(row["availability"], "document_unavailable")
        del self.client.fail["document.xml"]
        self.client.calls.clear()
        self.sync()
        self.assertFalse(any(e == "document.xml" for e, _ in self.client.calls))
        self.clock.value += timedelta(days=1)
        self.sync()
        self.assertIn("사업 내용", self.cache.evidence("document:20261015000001")["untrusted_source_text"])

    def test_background_start_and_second_process_share_one_writer(self):
        entered, release = threading.Event(), threading.Event()
        original = self.client.corporations
        def blocked():
            entered.set()
            if not release.wait(3):
                raise AssertionError("worker was not released")
            return original()
        self.client.corporations = blocked
        try:
            self.cache.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.cache.start()["status"], "running")
            other = Fundamentals(self.path, self.config, FixtureClient(), self.clock)
            self.assertEqual(other.sync()["status"], "already_running")
            self.assertEqual(other.status()["status"], "running")
        finally:
            release.set()
            self.cache.thread.join(5)
        self.assertFalse(self.cache.thread.is_alive())
        self.assertEqual(self.cache.status()["status"], "complete")

    def test_api_lag_keeps_old_financial_values_marked_stale_and_retryable(self):
        self.sync()
        self.client.documents.append(filing("20261016000004", title="[기재정정]사업보고서 (2025.12)"))
        result = self.cache.sync()
        self.assertEqual(result["status"], "incomplete")
        stale = [r for r in self.cache.report("005930", limit=100)["rows"] if r["availability"] == "available"]
        self.assertTrue(all(r["stale"] for r in stale))
        self.assertTrue(all(r["error"] == "financial_api_lag" for r in stale))
        self.client.receipt = "20261016000004"
        self.clock.value += timedelta(hours=1)
        self.sync()

    def test_legacy_local_run_and_daily_limits_do_not_stop_requests(self):
        self.cache = Fundamentals(self.path, {**self.config, "max_requests_per_run": 1,
            "max_requests_per_day": 1}, self.client, self.clock)
        with self.cache.db() as db:
            db.execute("INSERT INTO usage VALUES ('20261016',40000)")
        for _ in range(1001):
            self.client.get("company.json", {"corp_code": "00126380"})
        self.assertEqual(self.cache.requests, 1001)
        with self.cache.db() as db:
            self.assertEqual(db.execute("SELECT requests FROM usage").fetchone()[0], 41001)
        self.assertEqual(self.cache.status()["local_request_limits"], {"per_run": None, "per_day": None})

    def test_no_startup_or_periodic_open_dart_collection(self):
        root = Path(self.tmp.name)
        subprocess.run([sys.executable, "-m", "money.cli", "init", "--root", str(root)],
                       capture_output=True, text=True, check=True)
        with patch("sys.argv", ["money", "run", "--root", str(root)]), \
                patch("money.cli.Fundamentals", return_value=self.cache), \
                patch("money.cli.make_server"), patch("money.cli.signal.signal"), \
                patch("money.cli.Engine") as engine:
            engine.return_value.tick.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                cli_main()
            engine.return_value.tick.assert_called_once()
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.cache.status()["automatic_collection"])
        self.assertEqual(self.client.calls, [])

    def test_old_budget_pause_is_archived_and_never_blocks_new_api_requests(self):
        old = {"status": "paused", "error": "run_budget", "requests": 1000}
        self.cache._set_meta("run", old)
        resumed = Fundamentals(self.path, self.config, self.client, self.clock)
        self.assertEqual(resumed.status()["status"], "idle")
        self.assertEqual(resumed._meta("legacy_budget_pause"), old)
        self.assertEqual(resumed.call_api("company.json", {"corp_code": "00126380"})["api_status"], "000")

    def test_direct_api_only_fetches_requested_source_and_reuses_unchanged_data(self):
        params = {"corp_code": "00126380"}
        out = self.cache.call_api("company.json", params)
        self.assertFalse(out["cache_hit"])
        self.assertNotIn("data", out)
        self.assertEqual(self.client.calls, [("company.json", params)])
        seq = self.cache.status()["latest_change_seq"]
        again = self.cache.call_api("company.json", params, include_data=True)
        self.assertTrue(again["cache_hit"])
        self.assertEqual(again["data"]["acc_mt"], "12")
        self.assertEqual(len(self.client.calls), 1)
        self.cache.call_api("company.json", params, refresh=True)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.cache.status()["latest_change_seq"], seq)
        self.assertEqual(self.cache.status()["pending_sources"], 0)
        self.assertEqual(self.cache.status()["companies"], 0)
        self.assertIn("acc_mt", self.cache.evidence(out["source_key"])["untrusted_source_text"])

    def test_direct_api_different_parameters_and_corrections_preserve_versions(self):
        first = self.cache.call_api("company.json", {"corp_code": "00126380"})
        second = self.cache.call_api("company.json", {"corp_code": "00164779"})
        self.assertNotEqual(first["source_key"], second["source_key"])
        original = self.client.get
        def revised(endpoint, params):
            payload = original(endpoint, params)
            payload["acc_mt"] = "03"
            return payload
        self.client.get = revised
        self.cache.call_api("company.json", {"corp_code": "00126380"}, refresh=True)
        with self.cache.db() as db:
            versions = db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            provenance = db.execute("SELECT value FROM meta WHERE key=?", ("request:" + first["source_key"],)).fetchone()[0]
        self.assertEqual(versions, 3)
        self.assertNotIn("a" * 40, provenance)
        self.assertIn("03", self.cache.evidence(first["source_key"])["untrusted_source_text"])

    def test_direct_api_preserves_provider_pagination_and_never_queues_documents(self):
        params = {"bgn_de": "20261015", "end_de": "20261016", "corp_cls": "Y", "page_no": 1, "page_count": 1}
        out = self.cache.call_api("list.json", params, include_data=True)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(out["returned_rows"], 1)
        self.assertEqual(out["provider_pagination"]["total_page"], 1)
        with self.cache.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_direct_financial_row_order_is_not_a_data_change(self):
        params = {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS"}
        self.cache.call_api("fnlttSinglAcntAll.json", params)
        seq = self.cache.status()["latest_change_seq"]
        original = self.client.get
        def reordered(endpoint, params):
            payload = original(endpoint, params)
            payload["list"].reverse()
            payload["message"] = "Changed transport message"
            return payload
        self.client.get = reordered
        self.cache.call_api("fnlttSinglAcntAll.json", params, refresh=True)
        self.assertEqual(self.cache.status()["latest_change_seq"], seq)

    def test_direct_xbrl_zip_is_stored_and_evidence_can_be_read(self):
        self.client.get = lambda endpoint, params: archive("<xbrl><value>123</value></xbrl>", "report.xbrl")
        out = self.cache.call_api("fnlttXbrl.xml", {"rcept_no": "20261015000001", "reprt_code": "11011"})
        self.assertEqual(out["media"], "zip")
        self.assertIn("123", self.cache.evidence(out["source_key"])["untrusted_source_text"])

    def test_direct_api_records_no_data_and_keeps_provider_quota_error(self):
        out = self.cache.call_api("fnlttSinglAcntAll.json", {"corp_code": "00164779", "bsns_year": "2025", "reprt_code": "11011"})
        self.assertEqual(out["availability"], "no_data")
        self.client.fail["company.json"] = "020"
        with self.assertRaises(DartError) as caught:
            self.cache.call_api("company.json", {"corp_code": "00126380"})
        self.assertEqual(caught.exception.code, "020")
        self.assertEqual(self.cache.status()["last_error"], "020")

    def test_direct_api_rejects_urls_and_credentials_without_transport_or_storage(self):
        for endpoint, params in [("https://evil.test/company.json", {}), ("../company.json", {}),
                ("company.json?crtfc_key=x", {}), ("company.json", {"crtfc_key": "secret"}),
                ("company.json", {"corp_code": ["00126380"]})]:
            with self.subTest(endpoint=endpoint), self.assertRaises(DartError):
                self.cache.call_api(endpoint, params)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.cache.status()["cached_sources"], 0)

    def test_transient_failure_never_becomes_no_data_or_completed_baseline(self):
        self.client.fail["fnlttSinglAcntAll.json"] = "network_error"
        result = self.cache.sync()
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["pending_sources"], 0)
        self.assertEqual(self.cache.report("005930")["rows"], [])
        del self.client.fail["fnlttSinglAcntAll.json"]
        self.clock.value += timedelta(hours=1)
        self.sync()

    def test_quota_failure_does_not_advance_unfinished_index(self):
        self.client.fail["list.json"] = "020"
        out = self.cache.sync()
        self.assertEqual(out["status"], "paused")
        self.assertIsNone(out["indexed_through"])
        del self.client.fail["list.json"]
        self.sync()

    def test_change_cursor_covers_all_pages_and_is_immutable_during_new_writes(self):
        bound = self.sync()["latest_change_seq"]
        with self.cache.db() as db:
            self.cache._save(db, "extra:fixture", "00126380", {"value": 1})
        seq, seen = 0, []
        while seq is not None:
            page = self.cache.report(after_seq=seq, through_seq=bound, limit=3)
            seen.extend(x["seq"] for x in page["rows"])
            seq = page["next_after_seq"]
        self.assertEqual(seen, list(range(1, bound + 1)))
        self.assertEqual(page["completed_through_seq"], bound)
        self.assertEqual(self.cache.report(after_seq=bound)["rows"][0]["source_key"], "extra:fixture")
        with self.assertRaises(ValueError):
            self.cache.report(after_seq=bound + 100)

    def test_evidence_is_character_paged_without_llm_calls(self):
        self.sync()
        key = "document:20261015000001"
        full = self.cache.evidence(key)["untrusted_source_text"]
        offset, parts = 0, []
        while offset is not None:
            result = self.cache.evidence(key, offset, 5)
            self.assertLessEqual(len(result["untrusted_source_text"]), 5)
            parts.append(result["untrusted_source_text"])
            offset = result["next_offset"]
        self.assertEqual("".join(parts), full)
        self.assertIn("rcpNo=20261015000001", result["source_url"])

    def test_single_file_backup_reopens_without_key_and_keeps_diffs(self):
        before = self.sync()
        target = Path(self.tmp.name) / "snapshot.sqlite"
        self.cache.export(target)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT MAX(seq) FROM changes").fetchone()[0], before["latest_change_seq"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(("a" * 40).encode(), target.read_bytes())
        with self.assertRaises(ValueError):
            self.cache.export(target)

    def test_missing_key_is_explicit(self):
        missing = Fundamentals(Path(self.tmp.name) / "missing.sqlite", self.config,
                               DartClient(""), self.clock)
        self.assertEqual(missing.sync()["last_error"], "missing_key")

    def test_research_configuration_and_database_failure_do_not_break_execution_status(self):
        root = Path(self.tmp.name)
        def command(name):
            return subprocess.run([sys.executable, "-m", "money.cli", name, "--root", str(root)],
                                  capture_output=True, text=True, check=True)
        command("init")
        configfile = root / "config.json"
        config = json.loads(configfile.read_text())
        config["opendart"] = {"history_years": "invalid"}
        configfile.write_text(json.dumps(config))
        status = json.loads(command("status").stdout)
        self.assertEqual(status["execution_capabilities"]["fundamentals"]["status"], "unavailable")
        self.assertFalse(status["orders"])
        with patch.object(self.cache, "status", side_effect=sqlite3.OperationalError("bad storage")):
            self.assertEqual(self.cache.capabilities()["status"], "unavailable")


class DartTransportTests(unittest.TestCase):
    def test_other_json_read_endpoints_use_only_official_host_and_local_key(self):
        from urllib.parse import parse_qs, urlsplit
        requested = []
        def opener(url, timeout):
            requested.append(url)
            return io.BytesIO(b'{"status":"000","list":[]}')
        client = DartClient("b" * 40, interval=0, opener=opener, sleep=lambda _: None)
        client.get("alotMatter.json", {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011"})
        url = urlsplit(requested[0])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "opendart.fss.or.kr", "/api/alotMatter.json"))
        self.assertEqual(parse_qs(url.query)["crtfc_key"], ["b" * 40])

    def test_transport_does_not_leak_key_from_network_exception(self):
        key = "b" * 40
        def fail(url, timeout):
            raise URLError("sensitive url " + url)
        client = DartClient(key, interval=0, opener=fail, sleep=lambda _: None)
        with self.assertRaises(DartError) as caught:
            client.get("company.json", {"corp_code": "00126380"})
        self.assertNotIn(key, str(caught.exception))
        self.assertEqual(caught.exception.code, "network_error")

    def test_pagination_reads_more_than_100_and_rejects_repeated_receipts(self):
        client = FixtureClient()
        client.documents = [filing("20261015" + str(i).zfill(6)) for i in range(125)]
        self.assertEqual(len(client.filings("20261015", "20261016", "Y")), 125)
        client.documents[-1] = client.documents[0]
        with self.assertRaisesRegex(DartError, "duplicate_receipt"):
            client.filings("20261015", "20261016", "Y")

    def test_xml_entities_rejected_and_archive_error_not_cached_as_document(self):
        with self.assertRaises(DartError):
            xml_root(b'<!DOCTYPE x [<!ENTITY y "bad">]><x>&y;</x>')
        with self.assertRaises(DartError) as caught:
            unpack(b"<result><status>020</status><message>limit</message></result>")
        self.assertEqual(caught.exception.code, "020")

    def test_ambiguous_account_currency_and_empty_amounts_do_not_produce_ratios(self):
        fixture = FixtureClient().get("fnlttSinglAcntAll.json", {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011"})
        self.assertEqual(metrics(fixture)["ratios"]["operating_margin_pct"], "20.0000")
        fixture["list"][0]["currency"] = "USD"
        self.assertNotIn("operating_margin_pct", metrics(fixture)["ratios"])
        fixture["list"].append(deepcopy(fixture["list"][2]))
        self.assertNotIn("liabilities_to_equity_pct", metrics(fixture)["ratios"])
        fixture["list"][4]["thstrm_amount"] = "-"
        self.assertNotIn("current_ratio_pct", metrics(fixture)["ratios"])

    def test_local_env_load_and_output_redaction(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / ".env"
            path.write_text("OPENDART_API_KEY=" + "a" * 40 + "\nUNRELATED=ignore\n")
            load_env(path)
            self.assertEqual(os.environ["OPENDART_API_KEY"], "a" * 40)
            self.assertNotIn("UNRELATED", os.environ)
            self.assertEqual(clean({"api_key": "s", "crtfc_key": "s", "source_key": "financial:1"}),
                             {"source_key": "financial:1"})


if __name__ == "__main__":
    unittest.main()
