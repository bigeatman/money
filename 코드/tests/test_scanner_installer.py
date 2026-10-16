"""Installer integrity/rollback tests use an isolated dummy project and no daemon."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BUILD_PATH = Path(__file__).resolve().parent.parent / "scripts/build_scanner_installer.py"
spec = importlib.util.spec_from_file_location("scanner_installer_builder_fixture", BUILD_PATH)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "money").mkdir()
        (self.root / "runtime").mkdir()
        self.old, self.new = b"VALUE = 1\n", b"VALUE = 2\n"
        (self.root / "money/example.py").write_bytes(self.old)
        (self.root / ".env").write_text("PRIVATE_SENTINEL=never-change\n")
        (self.root / "config.json").write_text('{"port": 8765, "broker": "kiwoom-demo"}')
        self.ledger = json.dumps({"capital": "1000000", "cash": "960658.890604", "commands": {}})
        db = sqlite3.connect(self.root / "runtime/state.sqlite")
        db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY,body TEXT)")
        db.execute("INSERT INTO state VALUES(1,?)", (self.ledger,));db.commit();db.close()
        records = {"money/example.py": builder.record(self.old, self.new),
                   "money/new_module.py": builder.record(None, b"NEW = True\n")}
        template = BUILD_PATH.with_name("scanner_installer_template.py").read_text()
        text = builder.render(template, records)
        self.ns = {"__name__": "installer_fixture"}
        exec(compile(text, "installer_fixture.py", "exec"), self.ns)
        self.protected = {p: (self.root / p).read_bytes() for p in (".env", "config.json")}

    def tearDown(self):
        self.tmp.cleanup()

    def protected_unchanged(self):
        self.assertEqual(self.ns["read_ledger"](self.root)[0], self.ledger)
        for path, data in self.protected.items():
            self.assertEqual((self.root / path).read_bytes(), data)

    def test_prepare_is_read_only_and_source_application_is_idempotent(self):
        changes = self.ns["prepare"](self.root)
        self.assertEqual(len(changes), 2)
        self.assertEqual((self.root / "money/example.py").read_bytes(), self.old)
        self.assertFalse((self.root / "money/new_module.py").exists())
        for path, before, after in changes:
            self.ns["atomic_write"](path, after)
        self.assertEqual(self.ns["prepare"](self.root), [])
        self.protected_unchanged()

    def test_unknown_source_is_rejected_before_any_write(self):
        (self.root / "money/example.py").write_bytes(b"USER_EDIT = 3\n")
        with self.assertRaisesRegex(RuntimeError, "덮어쓰지"):
            self.ns["prepare"](self.root)
        self.assertFalse((self.root / "money/new_module.py").exists())
        self.protected_unchanged()

    def test_payload_hash_and_symlink_are_checked(self):
        self.ns["FILES"]["money/example.py"]["updated"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "검증 실패"):
            self.ns["prepare"](self.root)
        self.ns["FILES"]["money/example.py"] = builder.record(self.old, self.new)
        (self.root / "money/example.py").unlink()
        (self.root / "money/example.py").symlink_to(self.root / ".env")
        with self.assertRaisesRegex(RuntimeError, "심볼릭"):
            self.ns["prepare"](self.root)
        self.protected_unchanged()

    def test_successful_install_keeps_keys_configuration_and_ledger(self):
        changes = self.ns["prepare"](self.root)
        self.ns.update(has_session=lambda: False, port_open=lambda port: False,
                       start_server=lambda root: None, verify_server=lambda port, token: {"mode": "kiwoom-demo"})
        with patch.object(self.ns["shutil"], "which", return_value="tmux"), \
             patch.object(self.ns["subprocess"], "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.ns["install"](self.root, {"port": 8765}, changes, "private-token")
        self.assertEqual((self.root / "money/example.py").read_bytes(), self.new)
        self.assertTrue((self.root / "money/new_module.py").exists())
        self.protected_unchanged()

    def test_failed_self_test_rolls_back_modified_and_new_source(self):
        changes = self.ns["prepare"](self.root)
        self.ns.update(has_session=lambda: False, port_open=lambda port: False)
        with patch.object(self.ns["shutil"], "which", return_value="tmux"), \
             patch.object(self.ns["subprocess"], "run", return_value=subprocess.CompletedProcess([], 1, "", "synthetic failure")):
            with self.assertRaisesRegex(RuntimeError, "자체 테스트 실패"):
                self.ns["install"](self.root, {"port": 8765}, changes, "private-token")
        self.assertEqual((self.root / "money/example.py").read_bytes(), self.old)
        self.assertFalse((self.root / "money/new_module.py").exists())
        self.protected_unchanged()

    def test_old_runtime_without_scanner_cannot_be_reported_as_applied(self):
        self.ns.update(port_open=lambda port: True,
                       read_status=lambda port, token: {"mode": "kiwoom-demo", "execution_capabilities": {}})
        with self.assertRaisesRegex(RuntimeError, "수집기"):
            self.ns["verify_server"](8765, "private-token")


if __name__ == "__main__":
    unittest.main()
