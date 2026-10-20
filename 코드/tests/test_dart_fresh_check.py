"""Deletion followed by three genuinely fresh collections through real SQLite."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from money.dart_client import DartClient
from money.fundamentals import Fundamentals
from tests.test_dart_structured import Provider

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-dart-fresh.py"
spec = importlib.util.spec_from_file_location("dart_fresh_check", SCRIPT)
fresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fresh)


class FreshCheckTests(unittest.TestCase):
    def test_deletes_old_material_and_fetches_hyundai_and_two_other_stocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime").mkdir()
            (root / "config.json").write_text('{}')
            provider = Provider()
            provider.override = lambda e, p: ({"status": "000", "corp_code": "00164742", "corp_name": "Hyundai",
                "stock_code": "005380"} if e == "company.json" and p.get("corp_code") == "00164742" else None)
            client = DartClient("offline-fixture", interval=0, opener=provider)
            f = Fundamentals(root / "runtime" / "fundamentals.sqlite", client=client)
            with f.db() as db:
                f._save(db, "old-sentinel", "00164742", {"old_material": "must disappear"})
            f.close()
            with redirect_stdout(io.StringIO()):
                result = fresh.run(root, client)
            self.assertTrue(result["processing_ok"], result)
            self.assertFalse(result["data_complete"])
            self.assertEqual(len(result["selected"]), 3)
            self.assertEqual(result["selected"][0]["symbol"], "005380")
            self.assertEqual(len({r["corp_code"] for r in result["selected"]}), 3)
            self.assertEqual(result["random_pool_size"], 2)
            self.assertTrue(all(r["cache_hits"] == 0 and r["network_requests"] == 50 for r in result["companies"]))
            self.assertEqual(result["D3_requests"], 0)
            self.assertEqual(len(provider.calls), 151)
            reopened = Fundamentals(root / "runtime" / "fundamentals.sqlite", client=client)
            try:
                with reopened.db() as db:
                    self.assertIsNone(db.execute("SELECT * FROM cache WHERE key='old-sentinel'").fetchone())
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_requests").fetchone()[0], 151)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM structured_jobs").fetchone()[0], 1)
                self.assertTrue(reopened.verify_archive(limit=100)["all_ok"])
                self.assertEqual(json.loads((root / 'runtime' / 'dart-fresh-check.json').read_text())["job_id"], fresh.JOB_ID)
            finally:
                reopened.close()

    def test_missing_credentials_stops_before_delete(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text('{}')
            with patch.dict('os.environ', {}, clear=True), patch.object(fresh.reset_module, "reset") as reset:
                with self.assertRaisesRegex(ValueError, "nothing was deleted"):
                    fresh.run(root)
                reset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
