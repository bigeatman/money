"""Local stock folders preserve raw bytes and expose a complete paged tree."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from money.dart_client import DartClient, DartError
from money.fundamentals import Fundamentals
from test_dart_archive import bundle


class StockFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.responses = []
        def opener(*args, **kwargs):
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return io.BytesIO(r)
        self.client = DartClient("b" * 40, interval=0, opener=opener, sleep=lambda _: None)
        self.f = Fundamentals(Path(self.tmp.name) / "fundamentals.sqlite", client=self.client)
        self.corp = "01639508"

    def tearDown(self):
        self.f.close()
        self.tmp.cleanup()

    def call(self, raw, endpoint="company.json", params=None, refresh=False):
        self.responses.append(raw)
        return self.f.call_api(endpoint, params if params is not None else {"corp_code": self.corp}, refresh=refresh)

    def folder(self, **kwargs):
        return self.f.query_data("files", corp_code=self.corp, **kwargs)

    def test_json_bytes_are_copied_exactly_and_no_api_is_called_by_listing(self):
        raw = b'{ "status": "000", "value": 1 }\r\n'
        self.call(raw)
        with patch.object(self.client, "get", side_effect=AssertionError("unexpected network")):
            out = self.folder()
        self.assertEqual(out["network_requests"], 0)
        root = Path(out["folder_root"])
        self.assertEqual(root.name, self.corp)
        self.assertEqual(out["total_response_files"], 1)
        self.assertEqual((root / out["rows"][0]["response_file"]).read_bytes(), raw)
        tree = json.loads(Path(out["full_tree_file"]).read_text())
        self.assertEqual(set(tree["paths"]), {out["rows"][0]["response_file"], "MANIFEST.json", "TREE.json"})
        evidence = self.f.evidence(out["tree_source_key"], limit=8000)
        self.assertIn('"provenance":"local_file_inventory"', evidence["untrusted_source_text"])
        fields = self.f.query_data("fields", source_key=out["tree_source_key"], field="/paths")
        self.assertEqual(len(fields["rows"]), 3)

    def test_zip_and_error_xml_are_kept_as_their_actual_response_types(self):
        payload = bundle([("../original.xml", b"<root>document</root>")])
        plan = {"companies": [{"symbol": "463020", "corp_code": self.corp}], "requests": [
            {"symbol": "463020", "corp_code": self.corp, "endpoint": "document.xml", "params": {"rcept_no": r}}
            for r in ["20260318000699", "20260814000864"]]}
        self.f.jobs.create(plan, "folder")
        self.call(payload, "document.xml", {"rcept_no": "20260318000699"})
        error = b"<result><status>014</status></result>"
        with self.assertRaisesRegex(DartError, "014"):
            self.call(error, "document.xml", {"rcept_no": "20260814000864"})
        out = self.folder()
        self.assertEqual(out["total_response_files"], 2)
        files = [(Path(out["folder_root"]) / x["response_file"]) for x in out["rows"]]
        self.assertEqual({p.suffix for p in files}, {".zip", ".xml"})
        self.assertEqual({p.read_bytes() for p in files}, {payload, error})
        self.assertFalse((Path(out["folder_root"]).parent / "original.xml").exists())

    def test_duplicate_response_attempts_keep_separate_files_and_pagination(self):
        raw = b'{"status":"000"}'
        self.call(raw)
        self.call(raw, refresh=True)
        first = self.folder(limit=1)
        second = self.folder(offset=first["next_offset"], limit=1)
        self.assertEqual(first["total_response_files"], 2)
        paths = first["tree_page"] + second["tree_page"]
        self.assertEqual(len(set(paths)), 2)
        self.assertIsNone(second["next_offset"])
        again = self.folder()
        self.assertEqual(again["total_response_files"], 2)
        self.assertEqual(set(paths), set(again["tree_page"]))

    def test_another_issuer_is_not_included(self):
        self.call(b'{"status":"000","company":1}')
        self.call(b'{"status":"000","company":2}', params={"corp_code": "00126380"})
        out = self.folder()
        self.assertEqual(out["total_response_files"], 1)
        body = (Path(out["folder_root"]) / out["rows"][0]["response_file"]).read_bytes()
        self.assertIn(b'"company":1', body)
        self.assertNotIn(b'"company":2', body)

    def test_attempts_without_a_body_are_explicit_in_manifest(self):
        self.responses.extend([URLError("private URL") for _ in range(3)])
        with self.assertRaises(DartError):
            self.f.call_api("company.json", {"corp_code": self.corp})
        out = self.folder()
        self.assertEqual((out["total_response_files"], out["attempts_without_body"]), (0, 3))
        manifest = json.loads(Path(out["manifest_file"]).read_text())
        self.assertEqual(len(manifest["attempts_without_body"]), 3)
        self.assertTrue(all(r["response_file"] is None for r in manifest["attempts_without_body"]))
        self.assertNotIn("private URL", Path(out["manifest_file"]).read_text())

    def test_corrupt_source_does_not_publish_a_verified_manifest(self):
        out = self.call(b'{"status":"000"}')
        source = self.f.archive.root / out["archive"]["path"]
        source.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.folder()
        self.assertFalse((self.f.stock_folders.root / self.corp / "MANIFEST.json").exists())

    def test_invalid_filter_and_folder_symlink_are_rejected(self):
        for corp in ["../etc", "abc", "01639508/other"]:
            with self.assertRaises(ValueError):
                self.f.query_data("files", corp_code=corp)
        with self.assertRaises(ValueError):
            self.folder(text="anything")
        with self.assertRaises(ValueError):
            self.folder(endpoint="../private")
        root = self.f.stock_folders.root
        root.mkdir()
        (root / self.corp).symlink_to(Path(self.tmp.name))
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.folder()

    def test_legacy_reconstructed_json_is_not_claimed_as_an_http_original(self):
        with self.f.db() as db:
            self.f._save(db, "company:" + self.corp, self.corp, {"corp_code": self.corp})
        out = self.folder()
        self.assertEqual(out["total_response_files"], 1)
        row = out["rows"][0]
        self.assertTrue(row["legacy"])
        self.assertTrue(row["response_file"].startswith("legacy/"))
        self.assertNotEqual(row["provenance"], "http_response")

    def test_endpoint_filter_changes_listing_without_losing_full_tree(self):
        self.call(b'{"status":"000"}')
        self.call(b'{"status":"013"}', "alotMatter.json")
        out = self.folder(endpoint="company.json")
        self.assertEqual((out["total_response_files"], out["filtered_response_files"]), (2, 1))
        paths = json.loads(Path(out["full_tree_file"]).read_text())["paths"]
        self.assertEqual(len(paths), 4)


if __name__ == "__main__":
    unittest.main()
