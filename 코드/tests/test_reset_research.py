"""Offline regression checks for the destructive, inventory-preserving reset."""
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from money.dart_client import DartClient
from money.fundamentals import Fundamentals
from money.scanner import MarketScanner

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "reset-research.py"
spec = importlib.util.spec_from_file_location("reset_research", MODULE)
resetter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resetter)


class ResetResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.database = self.runtime / "fundamentals.sqlite"
        client = DartClient("a" * 40, interval=0, opener=lambda *args, **kwargs: io.BytesIO(
            b'{"status":"000","corp_code":"01492651","corp_name":"example"}'))
        self.f = Fundamentals(self.database, client=client)
        with self.f.db() as db:
            db.execute("INSERT INTO issuers VALUES (?,?,?,?,?,?)",
                       ("01492651", "372320", "test issuer", "20261020", 1, '{"master":true}'))
        self.f._set_meta("master_at", "2026-10-19T00:00:00+00:00")
        self.f._set_meta("indexed_through", "2026-10-18")
        self.f.call_api("company.json", {"corp_code": "01492651"})
        self.f.jobs.create({"companies": [{"symbol": "372320", "corp_code": "01492651"}],
                           "requests": [{"symbol": "372320", "corp_code": "01492651",
                                         "endpoint": "company.json", "params": {"corp_code": "01492651"}}]})
        self.f.close()
        self.scans = self.runtime / "market-scans"
        self.scans.mkdir()
        job = {"scan_id": "KR-test", "market": "KR", "started_at": "2026-10-20T00:00:00+00:00",
               "finished_at": "2026-10-20T00:01:00+00:00", "inventory_complete": True,
               "rows": [{"key": "KRX:372320", "symbol": "372320", "exchange": "KRX", "name": "issuer",
                         "asset_type": "cash_equity", "segment": "10", "reference_close": "100",
                         "observation": {"last": "200"}, "features": {"return": "2"},
                         "status": "candidate", "signals": ["signal"]}]}
        (self.scans / "KR-test.json").write_text(json.dumps(job))
        (self.scans / "KR-index.json").write_text(json.dumps({"latest": "KR-test", "completed": None}))
        self.protected = {self.root / ".env": b"test-only credential", self.root / "config.json": b"{}",
                          self.runtime / "state.sqlite": b"test-only trading ledger"}
        for path, body in self.protected.items():
            path.write_bytes(body)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reset_removes_research_retains_exact_master_and_loadable_inventory(self):
        with sqlite3.connect(self.database) as db:
            before = db.execute("SELECT * FROM issuers").fetchall()
            usage = db.execute("SELECT * FROM usage").fetchall()
        result = resetter.reset(self.root)
        self.assertEqual(result["issuer_rows_preserved"], 1)
        self.assertEqual(result["network_requests"], 0)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT * FROM issuers").fetchall(), before)
            self.assertEqual(db.execute("SELECT * FROM usage").fetchall(), usage)
            for table in resetter.TABLES - {"issuers", "usage", "meta"}:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT * FROM meta WHERE key='indexed_through'").fetchone())
        self.assertEqual(list((self.runtime / "fundamentals.files").iterdir()), [])
        scanner = MarketScanner(None, self.scans)
        report = scanner.report("KR")
        self.assertFalse(report["usable_for_new_buys"])
        row = report["rows"][0]
        self.assertEqual(row["symbol"], "372320")
        self.assertIsNone(row["observation"])
        self.assertNotIn("features", row)
        self.assertNotIn("reference_close", row)
        scanner.close()
        f = Fundamentals(self.database)
        self.assertEqual(f.status()["companies"], 1)
        self.assertEqual(f.status()["cached_sources"], 0)
        self.assertEqual(f.status()["pending_sources"], 0)
        f.close()
        for path, body in self.protected.items():
            self.assertEqual(path.read_bytes(), body)
        self.assertTrue(resetter.reset(self.root)["research_reset"])

    def test_running_server_blocks_all_deletions(self):
        with (self.runtime / "executor.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Stop the money server"):
                resetter.reset(self.root)
        self.assertGreater(self.f.status()["cached_sources"], 0)
        self.assertTrue((self.scans / "KR-test.json").exists())

    def test_unknown_table_blocks_all_deletions(self):
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE unexpected_account_state (body TEXT)")
        with self.assertRaisesRegex(ValueError, "Unrecognized research schema"):
            resetter.reset(self.root)
        self.assertGreater(self.f.status()["cached_sources"], 0)
        self.assertTrue((self.scans / "KR-test.json").exists())


if __name__ == "__main__":
    unittest.main()
