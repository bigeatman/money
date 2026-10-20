"""Offline explicit-plan, resumability, attribution and real-report regression tests."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_fundamentals as fixtures
import test_dart_archive as archive_fixtures
from money.dart_client import DartClient, DartError
from money.dart_xml import report_root
from money.fundamentals import Fundamentals

DART = '<DOCUMENT xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="dart4.xsd">'


class DartXmlTests(unittest.TestCase):
    def test_real_lg_literal_label_and_company_ampersand_are_preserved(self):
        text = DART + '<TABLE><TR><TD><종속기업></TD><TD>D&O CM NANJING CO., LTD.</TD></TR></TABLE></DOCUMENT>'
        raw, before = text.encode(), hashlib.sha256(text.encode()).hexdigest()
        root, note = report_root(raw, text)
        self.assertEqual([node.text for node in root.iter("TD")], ["<종속기업>", "D&O CM NANJING CO., LTD."])
        self.assertEqual(note["mode"], "dart_literal_text")
        self.assertEqual({x["kind"] for x in note["repairs"]}, {"literal_angle_label", "bare_ampersand"})
        self.assertEqual(hashlib.sha256(raw).hexdigest(), before)

    def test_strict_xml_paired_korean_nodes_and_existing_entities_do_not_change(self):
        text = '<root><항목>D&amp;O &lt;회사&gt;</항목></root>'
        root, note = report_root(text.encode(), text)
        self.assertEqual(root[0].text, "D&O <회사>")
        self.assertEqual(note, {"mode": "strict_xml", "repairs": []})

    def test_repair_does_not_touch_cdata_or_invent_unknown_entity_values(self):
        text = DART + '<P><![CDATA[<종속기업> D&O]]></P><TD><회사></TD></DOCUMENT>'
        root, _ = report_root(text.encode(), text)
        self.assertEqual(root[0].text, "<종속기업> D&O")
        self.assertEqual(root[1].text, "<회사>")
        for invalid in (DART + '<P>&unknown;</P></DOCUMENT>', '<root><P>D&O</P></root>',
                        '<!DOCTYPE root [<!ENTITY x SYSTEM "file:///private">]><root>&x;</root>',
                        DART + '<P><UNMATCHED></P></DOCUMENT>'):
            with self.subTest(invalid=invalid), self.assertRaises(DartError):
                report_root(invalid.encode(), invalid)

    def test_local_reindex_recovers_legacy_field_index_without_network_or_new_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = (DART + '<P><종속기업> D&O</P></DOCUMENT>').encode()
            bundle = archive_fixtures.bundle([("lg.xml", raw)])
            client = DartClient("a" * 40, interval=0, opener=lambda *args, **kwargs: io.BytesIO(bundle))
            f = Fundamentals(Path(tmp) / "fundamentals.sqlite", client=client)
            with patch("money.dart_archive.report_root", side_effect=DartError("invalid_xml")):
                result = f.call_api("document.xml", {"rcept_no": "20260318001025"})
            seq, usage = f.status()["latest_change_seq"], f.status()["archive"]["request_attempts"]
            self.assertEqual(f.query_data("fields", result["source_key"])["rows"], [])
            indexed = f.reindex_archive()
            self.assertTrue(indexed["all_ok"])
            rows = f.query_data("fields", result["source_key"], text="종속기업")["rows"]
            self.assertEqual(rows[0]["value"], "<종속기업> D&O")
            self.assertEqual(f.status()["latest_change_seq"], seq)
            self.assertEqual(f.status()["archive"]["request_attempts"], usage)
            with f.db() as db:
                note = db.execute("SELECT mode FROM archive_parse_notes WHERE sha256=?", (hashlib.sha256(raw).hexdigest(),)).fetchone()[0]
            self.assertEqual(note, "dart_literal_text")
            self.assertTrue(f.verify_archive()["all_ok"])
            f.close()

    def test_unrepairable_file_cursor_terminates_and_text_search_returns_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = archive_fixtures.bundle([("bad.xml", b"<root><wrong></root>")])
            client = DartClient("a" * 40, interval=0, opener=lambda *args, **kwargs: io.BytesIO(raw))
            f = Fundamentals(Path(tmp) / "fundamentals.sqlite", client=client)
            result = f.call_api("document.xml", {"rcept_no": "20260318001025"})
            out = f.reindex_archive(limit=1)
            self.assertFalse(out["all_ok"])
            self.assertIsNone(out["next_after_sha"])
            text = f.query_data("text", result["source_key"], text="wrong", limit=1)
            self.assertIn("wrong", text["rows"][0]["text"])
            f.close()


class DartJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fundamentals.sqlite"
        self.client = fixtures.FixtureClient()
        self.f = Fundamentals(self.path, client=self.client, clock=fixtures.Clock())
        self.plan = {"companies": [{"symbol": "005930", "corp_code": "00126380"}],
                     "requests": [{"symbol": "005930", "corp_code": "00126380", "endpoint": "company.json", "params": {"corp_code": "00126380"}}],
                     "catalog": [{"id": "common_01", "title": "사업 내용"}, {"id": "common_02", "title": "외부 금리"}]}

    def tearDown(self):
        self.f.close()
        self.tmp.cleanup()

    def run_job(self, **kwargs):
        out = self.f.jobs.start(**kwargs)
        self.f.jobs.thread.join(5)
        self.assertFalse(self.f.jobs.thread.is_alive())
        return self.f.jobs.report(out["job_id"])

    def test_exact_plan_only_cache_reuse_and_persistent_outcomes(self):
        out = self.run_job(plan=self.plan, job_id="sample")
        self.assertEqual(out["counts"], {"available": 1})
        self.assertTrue(out["collection_complete"])
        self.assertEqual(len(self.client.calls), 1)
        second = self.run_job(job_id="sample")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(second["expected_review_items"], 2)
        self.assertFalse(second["investment_review_complete"])
        self.assertTrue(self.f.verify_archive()["all_ok"])

    def test_no_data_missing_document_and_failures_are_distinct(self):
        self.plan["requests"] += [
            {"symbol": "005930", "corp_code": "00126380", "endpoint": "fnlttSinglAcntAll.json", "params": {"corp_code": "00126380", "bsns_year": "2024", "reprt_code": "11011", "fs_div": "CFS"}},
            {"symbol": "005930", "corp_code": "00126380", "endpoint": "document.xml", "params": {"rcept_no": "20260318001025"}}]
        self.client.fail["document.xml"] = "014"
        out = self.run_job(plan=self.plan)
        self.assertEqual(out["counts"], {"available": 1, "no_data": 1, "unavailable": 1})
        self.assertEqual(out["rows"][2]["error"], "014")
        self.assertTrue(out["collection_complete"])

    def test_quota_pause_does_not_mark_missing_or_loop_and_explicit_resume_works(self):
        self.client.fail["company.json"] = "020"
        out = self.run_job(plan=self.plan, job_id="quota")
        self.assertEqual(out["status"], "paused")
        self.assertEqual(out["last_error"], "020")
        self.assertEqual(out["counts"], {"pending": 1})
        self.assertEqual(len(self.client.calls), 1)
        del self.client.fail["company.json"]
        self.assertTrue(self.run_job(job_id="quota")["collection_complete"])

    def test_restart_resumes_running_row_and_uses_already_saved_response(self):
        job = self.f.jobs.create(self.plan, "resume")
        self.f.call_api("company.json", {"corp_code": "00126380"})
        with self.f.db() as db:
            db.execute("UPDATE dart_jobs SET status='running'")
            db.execute("UPDATE dart_job_requests SET state='running'")
        self.f.close()
        self.f = Fundamentals(self.path, client=self.client, clock=fixtures.Clock())
        self.assertEqual(self.f.jobs.report(job)["status"], "interrupted")
        before = len(self.client.calls)
        self.assertTrue(self.run_job(job_id=job)["collection_complete"])
        self.assertEqual(len(self.client.calls), before)

    def test_separate_cli_reader_recognizes_a_running_server_job(self):
        entered, release = threading.Event(), threading.Event()
        original = self.client.get
        def blocking_get(endpoint, params):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture not released")
            return original(endpoint, params)
        with patch.object(self.client, "get", side_effect=blocking_get):
            out = self.f.jobs.start(plan=self.plan, job_id="separate_reader")
            self.assertTrue(entered.wait(2))
            reader = Fundamentals(self.path, client=fixtures.FixtureClient(), clock=fixtures.Clock())
            try:
                self.assertEqual(reader.jobs.report(out["job_id"])["status"], "running")
            finally:
                reader.close()
                release.set()
                self.f.jobs.thread.join(5)

    def test_plan_rejects_duplicates_unknown_issuers_and_changed_identity(self):
        for mutation in (lambda p: p["requests"].append(deepcopy(p["requests"][0])),
                         lambda p: p["requests"][0].update(symbol="000660"),
                         lambda p: p["requests"][0]["params"].update(corp_code="00164779")):
            plan = deepcopy(self.plan); mutation(plan)
            with self.assertRaises(ValueError):
                self.f.jobs.create(plan)
        self.f.jobs.create(self.plan, "fixed")
        changed = deepcopy(self.plan); changed["note"] = "changed"
        with self.assertRaisesRegex(ValueError, "different plan"):
            self.f.jobs.create(changed, "fixed")

    def test_reviews_require_reasons_evidence_and_preserve_versions(self):
        result = self.run_job(plan=self.plan, job_id="review")
        source = result["rows"][0]["result"]["source_key"]
        items = [{"item_id": "common_01", "status": "found", "facts": {"corp_code": "00126380"}, "source_keys": [source]},
                 {"item_id": "common_02", "status": "external_required", "reason": "현재 시장금리는 DART 기업 공시만으로 확인할 수 없음", "source_keys": []}]
        saved = self.f.record_review("review", "005930", items)
        self.assertEqual(saved["stored"], 2)
        self.f.record_review("review", "005930", items)
        with self.f.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dart_review_history").fetchone()[0], 2)
        items[1]["reason"] = "Updated explicit reason"
        self.f.record_review("review", "005930", items)
        out = self.f.jobs.report("review", view="reviews")
        self.assertEqual(out["review_counts"], {"external_required": 1, "found": 1})
        self.assertEqual(out["rows"][0]["body"]["provenance"], "assistant_review")
        with self.f.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dart_review_history").fetchone()[0], 3)
        self.assertTrue(self.f.verify_archive(limit=100)["all_ok"])

    def test_invalid_review_batch_is_atomic_and_does_not_create_orphan_files(self):
        self.f.jobs.create(self.plan, "bad_review")
        before = set(self.f.archive.root.rglob("*.json"))
        for items in ([{"item_id": "common_01", "status": "partial", "reason": ""}],
                      [{"item_id": "common_01", "status": "found", "facts": {"x": 1}, "source_keys": ["made_up"]}],
                      [{"item_id": "common_01", "status": "external_required", "reason": "ok"},
                       {"item_id": "common_02", "status": "found", "facts": {}, "source_keys": []}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.f.record_review("bad_review", "005930", items)
        self.assertEqual(set(self.f.archive.root.rglob("*.json")), before)
        self.assertEqual(self.f.jobs.report("bad_review", view="reviews")["rows"], [])
