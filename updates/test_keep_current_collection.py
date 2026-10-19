import contextlib
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import keep_current_collection as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / "test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.archive = self.project / "runtime" / "fundamentals.files"
        self.folder = self.archive / "companies" / cleanup.CORP
        self.folder.mkdir(parents=True)
        (self.project / "money").mkdir()
        self.before = Path(__file__).with_name("dart_folders.before.py").read_bytes()
        (self.project / "money" / "dart_folders.py").write_bytes(self.before)
        (self.folder / "legacy").mkdir()
        (self.folder / "legacy" / "old.json").write_bytes(b"old")
        (self.folder / "obsolete.xml").write_bytes(b"obsolete")
        self.unrelated = self.archive / "companies" / "00000001"
        self.unrelated.mkdir()
        (self.unrelated / "keep.json").write_bytes(b"unrelated")
        self.entries = copy.deepcopy(cleanup.ENTRIES)
        for i, entry in enumerate(self.entries):
            raw = ("fresh-%s" % i).encode()
            entry.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                         object_path="objects/test-%s.%s" % (i, entry["media"]))
            source = self.archive / entry["object_path"]
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(raw)
        self.published = []

    def apply(self, **kwargs):
        return cleanup.apply_current(self.project, self.entries, self.published.append, **kwargs)

    def test_replaces_only_issuer_folder_with_exact_six_responses(self):
        result = self.apply()
        self.assertEqual(result["total_files"], 8)
        self.assertEqual(result["current_responses"], 6)
        self.assertEqual(result["previous_files_removed"], 2)
        self.assertEqual(sum(p.is_file() for p in self.folder.rglob("*")), 8)
        self.assertFalse((self.folder / "legacy").exists())
        self.assertEqual((self.unrelated / "keep.json").read_bytes(), b"unrelated")
        self.assertEqual(len(list((self.archive / "objects").iterdir())), 6)
        self.assertEqual(len(self.published[0]["paths"]), 8)
        self.assertEqual(cleanup.sha((self.project / "money" / "dart_folders.py").read_bytes()),
                         cleanup.UPDATED_SHA)

    def test_check_does_not_change_files_or_publish(self):
        self.assertTrue(self.apply(check=True)["checked"])
        self.assertEqual((self.folder / "legacy" / "old.json").read_bytes(), b"old")
        self.assertEqual((self.project / "money" / "dart_folders.py").read_bytes(), self.before)
        self.assertEqual(self.published, [])

    def test_corrupt_source_aborts_before_deletion(self):
        (self.archive / self.entries[0]["object_path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.apply()
        self.assertTrue((self.folder / "obsolete.xml").exists())
        self.assertEqual(self.published, [])

    def test_unknown_source_code_aborts_before_deletion(self):
        (self.project / "money" / "dart_folders.py").write_bytes(b"# user changes")
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.apply()
        self.assertTrue((self.folder / "obsolete.xml").exists())

    def test_publish_failure_restores_old_folder_and_code(self):
        def fail(value):
            raise RuntimeError("database write failed")
        with self.assertRaisesRegex(RuntimeError, "database"):
            cleanup.apply_current(self.project, self.entries, fail)
        self.assertEqual((self.folder / "legacy" / "old.json").read_bytes(), b"old")
        self.assertEqual((self.project / "money" / "dart_folders.py").read_bytes(), self.before)
        self.assertFalse(list(self.folder.parent.glob("." + cleanup.CORP + "-*")))

    def test_repeat_is_idempotent(self):
        self.apply()
        self.apply()
        actual = [p for p in self.folder.rglob("*") if p.is_file()]
        self.assertEqual(len(actual), 8)
        self.assertFalse(list(self.folder.parent.glob("." + cleanup.CORP + "-*")))

    def test_deletion_guard_rejects_unrelated_directory(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            cleanup.remove_staging(self.unrelated, self.folder.parent)
        self.assertTrue((self.unrelated / "keep.json").exists())

    def test_traversal_source_aborts(self):
        self.entries[0]["object_path"] = "../outside.json"
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.apply()
        self.assertTrue((self.folder / "obsolete.xml").exists())

    def test_symlink_target_aborts(self):
        real_is_symlink = Path.is_symlink
        folder = self.folder
        with patch.object(Path, "is_symlink", lambda p: p == folder or real_is_symlink(p)):
            with self.assertRaisesRegex(ValueError, "plain"):
                self.apply()
        self.assertTrue((self.folder / "obsolete.xml").exists())

    def test_future_report_respects_selected_requests(self):
        self.apply()
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.LOCK_EX = 1
        fake_fcntl.LOCK_NB = 2
        fake_fcntl.flock = lambda *args: None
        package = types.ModuleType("_cleanup_fake")
        package.__path__ = []
        data = types.ModuleType("_cleanup_fake.dart_data")
        data.dumps = lambda x: json.dumps(x, ensure_ascii=False)
        namespace = {"__name__": "_cleanup_fake.dart_folders",
                     "__package__": "_cleanup_fake"}
        with patch.dict(sys.modules, {"fcntl": fake_fcntl, "_cleanup_fake": package,
                                     "_cleanup_fake.dart_data": data}):
            exec(compile(cleanup.UPDATED_SOURCE, "dart_folders.py", "exec"), namespace)
        requests, objects = [], {}
        for entry in self.entries:
            requests.append({"id": entry["request_id"], "source_key": entry["source_key"],
                             "endpoint": entry["endpoint"], "sha256": entry["sha256"],
                             "params": json.dumps(entry["parameters"])})
            objects[entry["sha256"]] = {"path": entry["object_path"], "sha256": entry["sha256"],
                                       "size": entry["size"], "media": entry["media"],
                                       "provenance": "http_response"}
        old = dict(requests[0], id=12)
        requests.append(old)
        class FakeArchive:
            root = self.archive
            def store(self, *args):
                pass
        captured = []
        class FakeFundamentals:
            archive = FakeArchive()
            @contextlib.contextmanager
            def db(self):
                yield object()
            def _save(self, db, key, corp, inventory):
                captured.append(inventory)
        folders = namespace["DartFolders"](FakeFundamentals())
        folders._records = lambda corp: (requests, objects, [])
        report = folders.report(cleanup.CORP, limit=100)
        self.assertEqual(report["total_response_files"], 6)
        self.assertEqual(len(captured[0]["paths"]), 8)
        self.assertEqual(json.loads((self.folder / "MANIFEST.json").read_text())
                         ["selection"]["request_ids"],
                         sorted(entry["request_id"] for entry in self.entries))
        self.assertFalse(any("request_00000012" in str(p) for p in self.folder.rglob("*")))


if __name__ == "__main__":
    unittest.main()

