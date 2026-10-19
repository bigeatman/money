"""Offline persistence/integrity tests using the real HTTP parsing path."""
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile
import zlib

from money.dart_archive import content_hash
from money.dart_client import DartClient, DartError
from money.fundamentals import Fundamentals


def bundle(entries, year=2025):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, body in entries:
            info = zipfile.ZipInfo(name, date_time=(year, 1, 1, 0, 0, 0))
            archive.writestr(info, body)
    return stream.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fundamentals.sqlite"
        self.responses, self.urls = [], []
        self.client = DartClient("b" * 40, interval=0, opener=self.open, sleep=lambda _: None)
        self.cache = Fundamentals(self.path, client=self.client)
        self.params = {"corp_code": "00126380"}

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def open(self, url, timeout):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response) if isinstance(response, bytes) else response

    def call(self, raw, endpoint="company.json", params=None, refresh=False):
        self.responses.append(raw)
        return self.cache.call_api(endpoint, self.params if params is None else params, refresh)

    def requests(self):
        return self.cache.query_data("requests", limit=100)["rows"]

    def file(self, checksum):
        with self.cache.db() as db:
            row = db.execute("SELECT path FROM archive_files WHERE sha256=?", (checksum,)).fetchone()
        return self.cache.archive.root / row[0]

    def test_exact_json_bytes_all_fields_and_unchanged_refresh_are_persistent(self):
        raw = b'{ "status": "000", "nested": {"not_a_financial_metric": [null, false, "", {}, 12]}, "name": "test" }\n'
        first = self.call(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        self.assertEqual(first["archive"]["sha256"], checksum)
        self.assertEqual(self.file(checksum).read_bytes(), raw)
        self.assertEqual(self.file(checksum).stat().st_mode & 0o777, 0o600)
        fields = self.cache.query_data("fields", first["source_key"], field="not_a_financial_metric")["rows"]
        self.assertEqual([row["value"] for row in fields], [None, False, "", {}, 12])
        seq = self.cache.status()["latest_change_seq"]
        before = len(self.urls)
        cached = self.cache.call_api("company.json", self.params)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(len(self.urls), before)
        different_format = json.dumps(json.loads(raw), ensure_ascii=False).encode()
        again = self.call(different_format, refresh=True)
        self.assertNotEqual(again["archive"]["sha256"], checksum)
        self.assertEqual(self.cache.status()["latest_change_seq"], seq)
        rows = self.requests()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["changed"] for row in rows], [1, 0])
        self.assertEqual(rows[1]["previous_content_sha256"], rows[1]["content_sha256"])
        self.assertEqual(self.file(checksum).read_bytes(), raw)
        reopened = Fundamentals(self.path, client=DartClient("", interval=0))
        self.assertEqual(reopened.query_data("fields", first["source_key"], field="not_a_financial_metric")["rows"], fields)
        with self.cache.db() as db:
            for row in db.iterdump():
                self.assertNotIn("b" * 40, row)

    def test_updated_data_retains_each_version_and_change_diff(self):
        first = self.call(b'{"status":"000","value":1}')
        before = self.cache.status()["latest_change_seq"]
        self.call(b'{"status":"000","value":2}', refresh=True)
        self.assertEqual([row["changed"] for row in self.requests()], [1, 1])
        self.assertTrue(self.file(first["archive"]["sha256"]).exists())
        changes = self.cache.report(after_seq=before)["rows"]
        self.assertEqual(len(changes), 1)
        self.assertIn('"before":1', self.cache.evidence(changes[0]["diff_key"])["untrusted_source_text"])

    def test_zip_packaging_ignored_but_attributes_and_binary_members_count(self):
        entries = [("../report.xml", b'<report unit="KRW"><value>10</value><value>20</value></report>'),
                   ("images/logo.bin", b"\x00\xff\x00")]
        first = self.call(bundle(entries), "document.xml", {"rcept_no": "20261015000001"})
        seq = self.cache.status()["latest_change_seq"]
        self.call(bundle(list(reversed(entries)), year=2026), "document.xml", {"rcept_no": "20261015000001"}, True)
        self.assertEqual(self.cache.status()["latest_change_seq"], seq)
        self.assertEqual(self.requests()[-1]["changed"], 0)
        fields = self.cache.query_data("fields", first["source_key"], field="attributes/unit")["rows"]
        self.assertEqual(fields[0]["value"], "KRW")
        self.assertEqual(fields[0]["member"], "../report.xml")
        self.assertFalse((self.cache.archive.root.parent / "report.xml").exists())
        entries[0] = (entries[0][0], entries[0][1].replace(b"KRW", b"USD"))
        self.call(bundle(entries), "document.xml", {"rcept_no": "20261015000001"}, True)
        self.assertGreater(self.cache.status()["latest_change_seq"], seq)
        seq = self.cache.status()["latest_change_seq"]
        entries[1] = (entries[1][0], b"\x01\xff\x00")
        self.call(bundle(entries), "document.xml", {"rcept_no": "20261015000001"}, True)
        self.assertGreater(self.cache.status()["latest_change_seq"], seq)
        binary_sha = hashlib.sha256(entries[1][1]).hexdigest()
        self.assertEqual(self.file(binary_sha).read_bytes(), entries[1][1])
        files = self.cache.query_data("files", first["source_key"])["rows"]
        self.assertEqual(len(files), 3)

    def test_duplicate_zip_member_names_are_not_lost(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            raw = bundle([("same.xml", b"<x>one</x>"), ("same.xml", b"<x>two</x>")])
        out = self.call(raw, "document.xml", {"rcept_no": "20261015000001"})
        values = self.cache.query_data("fields", out["source_key"], field="/text")["rows"]
        self.assertEqual([row["value"] for row in values], ["one", "two"])

    def test_xml_api_status_attributes_and_repeated_nodes_are_indexed(self):
        raw = '<result><status>000</status><list unit="주">100</list><list unit="원">200</list></result>'.encode()
        out = self.call(raw, "company.xml")
        self.assertEqual(out["media"], "xml")
        self.assertEqual(self.file(out["archive"]["sha256"]).read_bytes(), raw)
        values = self.cache.query_data("fields", out["source_key"], field="attributes/unit")["rows"]
        self.assertEqual([row["value"] for row in values], ["주", "원"])
        self.assertIn('unit="원"', self.cache.evidence(out["source_key"])["untrusted_source_text"])

    def test_xml_no_data_is_explicit_and_generic_json_lists_keep_every_scalar(self):
        xml = self.call(b"<result><status>013</status><message>none</message></result>", "company.xml")
        self.assertEqual((xml["availability"], xml["api_status"]), ("no_data", "013"))
        out = self.call(b'{"status":"000","list":["item",0,false,null]}')
        fields = self.cache.query_data("fields", out["source_key"], field="/list/")["rows"]
        self.assertEqual([row["value"] for row in fields], ["item", 0, False, None])

    def test_financial_message_and_order_still_update_full_query_data(self):
        first = b'{"status":"000","message":"first","list":[{"account_id":"a"},{"account_id":"b"}]}'
        second = b'{"status":"000","message":"second","list":[{"account_id":"b"},{"account_id":"a"}]}'
        out = self.call(first, "fnlttSinglAcntAll.json")
        seq = self.cache.status()["latest_change_seq"]
        self.call(second, "fnlttSinglAcntAll.json", refresh=True)
        self.assertEqual(self.cache.status()["latest_change_seq"], seq)
        current = self.cache.call_api("fnlttSinglAcntAll.json", self.params, include_data=True)
        self.assertEqual(current["data"]["message"], "second")
        self.assertEqual(self.cache.query_data("fields", out["source_key"], field="/message")["rows"][0]["value"], "second")
        self.assertEqual(self.requests()[-1]["changed"], 1)

    def test_master_and_each_listing_page_are_archived_before_aggregation(self):
        master = b"<result><list><corp_code>00126380</corp_code><corp_name>Fixture</corp_name><stock_code>005930</stock_code></list></result>"
        self.responses.append(bundle([("CORPCODE.xml", master)]))
        self.assertEqual(len(self.client.corporations()), 1)
        rows = [{"rcept_no": "20261015" + str(i).zfill(6), "rcept_dt": "20261015"} for i in range(101)]
        self.responses += [json.dumps({"status": "000", "total_count": 101, "total_page": 2, "list": rows[:100]}).encode(),
                           json.dumps({"status": "000", "total_count": 101, "total_page": 2, "list": rows[100:]}).encode()]
        self.assertEqual(len(self.client.filings("20261015", "20261016", "Y")), 101)
        self.assertEqual([row["endpoint"] for row in self.requests()], ["corpCode.xml", "list.json", "list.json"])
        sources = self.cache.query_data(endpoint="corpCode.xml")["rows"]
        self.assertEqual(len(sources), 1)
        self.assertTrue(self.cache.query_data("fields", sources[0]["source_key"])["rows"])
        self.assertIn("00126380", self.cache.evidence(sources[0]["source_key"])["untrusted_source_text"])

    def test_unchanged_save_uses_db_checksum_and_does_not_decode_old_payload(self):
        self.call(b'{"status":"000","value":1}')
        with self.cache.db() as db:
            key = self.cache.archive.source_key("company.json", self.params)
            with patch.object(self.cache, "_stored", side_effect=AssertionError("old body decoded")):
                self.cache._save(db, key, "00126380", {"status": "000", "value": 1})

    def test_provider_errors_invalid_json_and_http_retry_bodies_are_all_saved(self):
        good = self.call(b'{"status":"000","value":1}')
        error = b'<result><status>020</status><message>limit</message></result>'
        with self.assertRaisesRegex(DartError, "020"):
            self.call(error, "document.xml", {"rcept_no": "20261015000001"})
        with self.assertRaisesRegex(DartError, "invalid_json"):
            self.call(b"{broken", refresh=True)
        http_body = b"provider unavailable"
        self.responses.extend([HTTPError("https://invalid/", 503, "failure", {}, io.BytesIO(http_body)),
                               b'{"status":"000","value":1}'])
        self.cache.call_api("company.json", self.params, refresh=True)
        rows = self.requests()
        self.assertEqual([row["error"] for row in rows], [None, "020", "invalid_json", "http_503", None])
        self.assertEqual(self.file(rows[1]["sha256"]).read_bytes(), error)
        self.assertEqual(self.file(rows[3]["sha256"]).read_bytes(), http_body)
        self.assertEqual(self.cache.status()["cached_sources"], 1)
        self.assertEqual(self.cache.call_api("company.json", self.params)["source_key"], good["source_key"])

    def test_no_data_and_bodyless_network_attempts_are_distinct(self):
        out = self.call(b'{"status":"013","message":"none"}')
        self.assertEqual(out["availability"], "no_data")
        self.responses.extend([URLError("private url with key") for _ in range(3)])
        with self.assertRaisesRegex(DartError, "network_error"):
            self.cache.call_api("company.json", self.params, refresh=True)
        rows = self.requests()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["sha256"] is None and row["complete"] == 0 for row in rows[1:]))

    def test_partial_response_is_preserved_and_never_becomes_cache(self):
        class Partial(io.BytesIO):
            def read(self, size=-1):
                raise IncompleteRead(b'{"status":', 20)
        self.responses.extend([Partial() for _ in range(3)])
        with self.assertRaisesRegex(DartError, "incomplete_response"):
            self.cache.call_api("company.json", self.params)
        self.assertEqual(self.cache.status()["cached_sources"], 0)
        self.assertEqual(len(self.requests()), 3)
        self.assertTrue(all(row["complete"] == 0 for row in self.requests()))
        self.assertEqual(self.file(self.requests()[0]["sha256"]).read_bytes(), b'{"status":')

    def test_content_length_mismatch_is_not_a_complete_response(self):
        class Short(io.BytesIO):
            def getheader(self, name):
                return "1000"
        self.responses.extend([Short(b'{"status":"000"}') for _ in range(3)])
        with self.assertRaisesRegex(DartError, "incomplete_response"):
            self.cache.call_api("company.json", self.params)
        self.assertTrue(all(row["complete"] == 0 for row in self.requests()))
        self.assertEqual(self.cache.status()["cached_sources"], 0)

    def test_nested_archives_and_binary_nuls_do_not_hide_text(self):
        inner = bundle([("part.xml", b"<p>all_nested_data</p>")])
        outer = bundle([("first.bin", b"GIF89a\x00bytes"), ("inner.zip", inner), ("last.xml", b"<p>last_document</p>")])
        out = self.call(outer, "document.xml", {"rcept_no": "20261015000001"})
        fields = self.cache.query_data("fields", out["source_key"], text="all_nested_data")["rows"]
        self.assertEqual(fields[0]["member"], "inner.zip!/part.xml")
        self.assertEqual(len(self.cache.query_data("files", out["source_key"])["rows"]), 5)
        self.assertIn("last_document", self.cache.evidence(out["source_key"])["untrusted_source_text"])

    def test_index_failure_and_cache_write_failure_do_not_discard_http_body(self):
        raw = b'{"status":"000","value":123}'
        with patch.object(self.cache.archive, "_index", side_effect=RuntimeError("fixture")):
            out = self.call(raw)
        self.assertEqual(out["archive"]["index_error"], "index_failed")
        self.assertEqual(self.file(out["archive"]["sha256"]).read_bytes(), raw)
        newer = b'{"status":"000","value":456}'
        with patch.object(self.cache, "_save", side_effect=sqlite3.OperationalError("fixture")):
            with self.assertRaisesRegex(DartError, "request_error"):
                self.call(newer, refresh=True)
        self.assertEqual(self.file(self.requests()[-1]["sha256"]).read_bytes(), newer)

    def test_cli_can_query_verify_and_migrate_without_remote_calls(self):
        out = self.call(b'{"status":"000","value":123}')
        root = str(self.path.parent)
        subprocess.run([sys.executable, "-m", "money.cli", "init", "--root", root], check=True, capture_output=True)
        self.cache.export(Path(root) / "runtime" / "fundamentals.sqlite")
        for args in (["dart-query", "--view", "fields", "--source-key", out["source_key"]],
                     ["dart-verify"], ["dart-migrate", "--all"]):
            result = subprocess.run([sys.executable, "-m", "money.cli", *args, "--root", root],
                                    check=True, capture_output=True, text=True)
            self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertEqual(len(self.urls), 1)

    def test_oversize_response_is_fully_saved_but_not_loaded_into_cache(self):
        raw = b'{"status":"000","value":"' + b"x" * 200 + b'"}'
        with patch("money.dart_archive.MAX_BYTES", 20):
            with self.assertRaisesRegex(DartError, "response_size"):
                self.call(raw)
        row = self.requests()[0]
        self.assertEqual(row["error"], "response_size")
        self.assertEqual(self.file(row["sha256"]).read_bytes(), raw)
        self.assertEqual(self.cache.status()["cached_sources"], 0)

    def test_verification_detects_corruption_and_refresh_repairs_file(self):
        raw = b'{"status":"000","value":1}'
        out = self.call(raw)
        path = self.file(out["archive"]["sha256"])
        path.write_bytes(b"damaged")
        self.assertEqual(self.cache.verify_archive()["rows"][0]["status"], "checksum_mismatch")
        self.call(raw, refresh=True)
        self.assertTrue(self.cache.verify_archive()["all_ok"])
        path.unlink()
        self.assertEqual(self.cache.verify_archive()["rows"][0]["status"], "missing")

    def test_legacy_migration_is_resumable_preserves_sequences_and_needs_no_key(self):
        # Build a real schema-1-shaped cache without any archive rows.
        payloads = [b'{"status":"000","value":1}', b'{"status":"000","value":2}']
        with self.cache.db() as db:
            db.execute("PRAGMA user_version=1")
            for index, raw in enumerate(payloads):
                digest = content_hash(raw)
                db.execute("INSERT INTO versions VALUES (?,?,?)", (digest, "json", zlib.compress(raw)))
                db.execute("INSERT INTO cache VALUES (?,?,?,?,?)", ("legacy:" + str(index), "00126380", digest, "available", "2026-10-19"))
        migrated = Fundamentals(self.path, client=DartClient("", interval=0))
        self.assertEqual(migrated.migrate_archive(1)["remaining"], 1)
        self.assertTrue(migrated.migrate_archive(1)["complete"])
        self.assertEqual(migrated.migrate_archive(1)["processed"], 0)
        self.assertEqual(len(migrated.query_data()["rows"]), 2)
        self.assertEqual(migrated.status()["latest_change_seq"], 0)
        self.assertEqual(migrated.status()["archive"]["legacy_versions_pending"], 0)
        self.assertEqual(len(self.urls), 0)
        with migrated.db() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_snapshot_contains_exact_files_and_reopens_after_original_removed(self):
        raw = b'{ "status" : "000", "value" : 123 }\n'
        out = self.call(raw)
        destination = Path(self.tmp.name) / "snapshot.sqlite"
        snapshot = self.cache.export(destination)
        self.assertTrue(Path(snapshot["archive_directory"]).is_dir())
        restored = Fundamentals(destination, client=DartClient("", interval=0))
        self.assertTrue(restored.verify_archive()["all_ok"])
        self.file(out["archive"]["sha256"]).unlink()
        with restored.db() as db:
            meta = restored.archive.metadata(db, out["source_key"])
        self.assertEqual((restored.archive.root / meta["path"]).read_bytes(), raw)
        self.assertIn("123", restored.evidence(out["source_key"])["untrusted_source_text"])


if __name__ == "__main__":
    unittest.main()
