"""Check that the standalone updater verifies DART without starting collection."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dart_installer_builder_fixture", ROOT / "scripts/build_dart_installer.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DartInstallerTests(unittest.TestCase):
    def setUp(self):
        self.ns = {"__name__": "dart_installer_fixture"}
        exec(compile(builder.template(ROOT), "dart_installer_fixture.py", "exec"), self.ns)
        self.status = {"execution_capabilities": {"fundamentals": {
            "supported": True, "version": "opendart-v3", "schema_version": 2,
            "configured": True, "enabled": True,
            "archive": {"capture_exact_http_bodies": True, "legacy_versions_pending": 10}}}}
        self.ns["verify_existing_server"] = lambda port, token: self.status
        self.calls = []

        def post(port, token, path, payload):
            self.calls.append(payload["params"])
            return {"result": {"structuredContent": {"rows": []}}}

        self.ns["post"] = post

    def test_checks_dart_runtime_and_local_query_without_collection(self):
        self.assertEqual(self.ns["diagnose"]({"port": 8765}, "private-token"), 0)
        self.assertEqual([x["name"] for x in self.calls], ["query_dart_data"])

    def test_rejects_old_runtime_disabled_capture_or_missing_key(self):
        dart = self.status["execution_capabilities"]["fundamentals"]
        for key, wrong in (("version", "opendart-v2"), ("schema_version", 1),
                           ("configured", False), ("supported", False), ("enabled", False)):
            with self.subTest(key=key):
                correct = dart[key]
                dart[key] = wrong
                with self.assertRaisesRegex(RuntimeError, "확인하지 못"):
                    self.ns["verify_server"](8765, "private-token")
                dart[key] = correct
        dart["archive"]["capture_exact_http_bodies"] = False
        with self.assertRaisesRegex(RuntimeError, "확인하지 못"):
            self.ns["verify_server"](8765, "private-token")
        self.assertEqual(self.calls, [])

    def test_rejects_missing_query_tool(self):
        self.ns["post"] = lambda *args: {"result": {"isError": True}}
        with self.assertRaisesRegex(RuntimeError, "query_dart_data"):
            self.ns["verify_server"](8765, "private-token")

    def test_default_project_and_check_mode_do_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money-work/money/코드"
            root.mkdir(parents=True)
            (root / "config.json").write_text(
                '{"broker":"kiwoom-demo","capital_krw":"1000000","host":"127.0.0.1"}')
            seen = []
            self.ns["prepare"] = lambda project: seen.append(project) or []
            self.ns["install"] = lambda *args: self.fail("check must not install")
            with patch.object(self.ns["Path"], "home", return_value=Path(tmp)), \
                 patch.object(self.ns["sys"], "argv", ["updater.py", "--check"]):
                self.assertEqual(self.ns["main"](), 0)
            self.assertEqual(seen, [root])

    def test_unknown_source_cannot_be_overwritten_and_reapply_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, updated = b"VALUE = 1\n", b"VALUE = 2\n"
            self.ns["FILES"] = {"item.py": builder.scanner.record(original, updated)}
            (root / "item.py").write_text("LOCAL_EDIT = True\n")
            with self.assertRaisesRegex(RuntimeError, "덮어쓰지"):
                self.ns["prepare"](root)
            (root / "item.py").write_bytes(original)
            changes = self.ns["prepare"](root)
            for path, before, after in changes:
                self.ns["atomic_write"](path, after)
            self.assertEqual(self.ns["prepare"](root), [])

    def test_database_backup_includes_wal_and_restores_old_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            backup_dir = root / "backup"
            backup_dir.mkdir()
            path = root / "runtime/fundamentals.sqlite"
            db = sqlite3.connect(path)
            db.executescript("PRAGMA journal_mode=WAL; PRAGMA user_version=1; CREATE TABLE cache(value TEXT);")
            db.execute("INSERT INTO cache VALUES ('original')")
            db.commit()
            backup = self.ns["backup_dart_database"](root, backup_dir)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            db.executescript("PRAGMA user_version=2; UPDATE cache SET value='changed'; CREATE TABLE archive_test(id INTEGER);")
            db.close()
            self.ns["restore_dart_database"](root, backup)
            with sqlite3.connect(path) as restored:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(restored.execute("SELECT value FROM cache").fetchone()[0], "original")
                self.assertIsNone(restored.execute("SELECT name FROM sqlite_master WHERE name='archive_test'").fetchone())

    def test_failed_runtime_rolls_back_source_and_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            ledger = json.dumps({"capital": "1000000", "commands": {}})
            with sqlite3.connect(root / "runtime/state.sqlite") as db:
                db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY, body TEXT)")
                db.execute("INSERT INTO state VALUES(1,?)", (ledger,))
            with sqlite3.connect(root / "runtime/fundamentals.sqlite") as db:
                db.executescript("PRAGMA user_version=1; CREATE TABLE cache(value TEXT);")
            (root / ".env").write_text("PRIVATE_SENTINEL=never-change\n")
            (root / "config.json").write_text('{"port":8765}')
            (root / "item.py").write_text("VALUE = 1\n")
            self.ns["FILES"] = {"item.py": builder.scanner.record(b"VALUE = 1\n", b"VALUE = 2\n")}
            changes = self.ns["prepare"](root)
            self.ns.update(has_session=lambda: False, port_open=lambda port: False, start_server=lambda root: None)

            def fail_verify(*args):
                with sqlite3.connect(root / "runtime/fundamentals.sqlite") as db:
                    db.execute("PRAGMA user_version=2")
                raise RuntimeError("synthetic DART failure")

            self.ns["verify_server"] = fail_verify
            with patch.object(self.ns["shutil"], "which", return_value="tmux"), \
                 patch.object(self.ns["subprocess"], "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with self.assertRaisesRegex(RuntimeError, "이전 소스를 복원"):
                    self.ns["install"](root, {"port": 8765}, changes, "private-token")
            self.assertEqual((root / "item.py").read_text(), "VALUE = 1\n")
            with sqlite3.connect(root / "runtime/fundamentals.sqlite") as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(self.ns["read_ledger"](root)[0], ledger)
            self.assertEqual((root / ".env").read_text(), "PRIVATE_SENTINEL=never-change\n")


if __name__ == "__main__":
    unittest.main()
