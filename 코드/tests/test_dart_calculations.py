"""Fixed formulas and rebuilding old jobs without more provider requests."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from money.dart_calculations import DEFINITIONS
from money.dart_client import DartClient, DartError
from money.dart_data import dumps
from money.dart_structured import calculated, source_quality
from money.fundamentals import Fundamentals
from tests.test_dart_structured import Provider

PARAMS = {"corp_code": "00126380", "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS"}


def financial():
    values = {"revenue": "200", "operating_profit": "40", "net_profit": "30", "liabilities": "50",
              "equity": "100", "current_assets": "60", "current_liabilities": "30",
              "operating_cash_flow": "70", "ppe_purchases": "(20)", "intangible_purchases": "5"}
    return {"status": "000", "list": [
        {**PARAMS, "account_id": account, "sj_div": statements[0], "currency": "KRW",
         "thstrm_nm": "current", "frmtrm_nm": "previous", "thstrm_amount": values[name],
         "frmtrm_amount": "100", "frmtrm_q_amount": "20", "thstrm_add_amount": "300", "frmtrm_add_amount": "250"}
        for name, (account, statements) in DEFINITIONS.items()]}


class CalculationTests(unittest.TestCase):
    def test_all_fixed_formulas_preserve_original_signs_and_inputs(self):
        payload = financial()
        before = deepcopy(payload)
        rows, derived = calculated(payload, "fnlttSinglAcntAll.json", PARAMS)
        self.assertEqual(payload, before)
        self.assertEqual(derived["ratios"], {"operating_margin_pct": "20.0000", "net_margin_pct": "15.0000",
            "liabilities_to_equity_pct": "50.0000", "current_ratio_pct": "200.0000"})
        self.assertEqual(derived["amounts"]["cash_after_asset_acquisitions"], "45")
        self.assertEqual(derived["calculation_inputs"]["ppe_purchases"]["value"], "-20")
        comparison = rows[0]["calculated"]["comparisons"][0]
        self.assertEqual((comparison["change_amount"], comparison["change_pct"]), ("100", "100.0000"))

    def test_interim_and_cumulative_never_use_prior_full_year_as_fallback(self):
        params = {**PARAMS, "reprt_code": "11012"}
        payload = financial()
        for row in payload["list"]:
            row["reprt_code"] = "11012"
        rows, _ = calculated(payload, "fnlttSinglAcntAll.json", params)
        regular, cumulative = rows[0]["calculated"]["comparisons"]
        self.assertEqual((regular["previous_field"], regular["change_amount"]), ("frmtrm_q_amount", "180"))
        self.assertEqual((cumulative["current_field"], cumulative["change_amount"]), ("thstrm_add_amount", "50"))
        self.assertEqual(rows[3]["calculated"]["comparisons"][0]["previous_field"], "frmtrm_amount")
        payload["list"][0].pop("frmtrm_q_amount")
        rows, _ = calculated(payload, "fnlttSinglAcntAll.json", params)
        self.assertIsNone(rows[0]["calculated"]["comparisons"][0]["change_amount"])

    def test_zero_negative_and_missing_inputs_are_explicit(self):
        payload = financial()
        payload["list"][0]["frmtrm_amount"] = "0"
        rows, _ = calculated(payload, "fnlttSinglAcntAll.json", PARAMS)
        comparison = rows[0]["calculated"]["comparisons"][0]
        self.assertEqual(comparison["change_amount"], "200")
        self.assertEqual(comparison["pct_status"], "zero_denominator")
        payload["list"][0]["frmtrm_amount"] = "-100"
        payload["list"][-1]["thstrm_amount"] = "-"
        rows, derived = calculated(payload, "fnlttSinglAcntAll.json", PARAMS)
        self.assertEqual(rows[0]["calculated"]["comparisons"][0]["change_pct"], "-300.0000")
        self.assertTrue(rows[0]["calculated"]["comparisons"][0]["negative_denominator"])
        self.assertIsNone(derived["amounts"]["cash_after_asset_acquisitions"])
        self.assertEqual(derived["fixed_calculations"]["cash_after_asset_acquisitions"]["missing_inputs"],
                         {"intangible_purchases": "amount_missing"})

    def test_ambiguous_accounts_currency_period_and_statement_cannot_mix(self):
        for field, value in [("currency", "USD"), ("thstrm_nm", "other"), ("sj_div", "CIS")]:
            payload = financial()
            payload["list"][2][field] = value
            _, derived = calculated(payload, "fnlttSinglAcntAll.json", PARAMS)
            self.assertNotIn("net_margin_pct", derived["ratios"])
        payload = financial()
        payload["list"].append(deepcopy(payload["list"][2]))
        _, derived = calculated(payload, "fnlttSinglAcntAll.json", PARAMS)
        self.assertEqual(derived["fixed_calculations"]["net_margin_pct"]["missing_inputs"], {"net_profit": "ambiguous_account"})

    def test_blank_audit_issuance_and_no_data_are_unknown_not_zero(self):
        for endpoint, record in [
                ("accnutAdtorNmNdAdtOpinion.json", {"bsns_year": "-", "adtor": "-", "adt_reprt_spcmnt_matter": "-"}),
                ("irdsSttus.json", {"isu_dcrs_qy": "-"})]:
            payload = {"status": "000", "list": [record]}
            rows, derived = calculated(payload, endpoint)
            self.assertEqual(source_quality(payload, rows, derived)["status"], "fields_missing")
            self.assertEqual(rows[0]["provider"], record)
        rows, derived = calculated({"status": "013"}, "irdsSttus.json")
        self.assertEqual(source_quality({"status": "013"}, rows, derived)["status"], "api_no_data")
        rows, _ = calculated({"status": "000", "list": [{"thstrm": "0", "frmtrm": "bad", "lwfr": "-"}]}, "alotMatter.json")
        self.assertEqual(rows[0]["numeric"]["thstrm"], "0")
        self.assertEqual(rows[0]["data_quality"]["invalid_numeric_fields"], ["frmtrm"])

    def test_resume_migrates_old_calculations_and_recovers_audit_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = Provider()
            def response(endpoint, params):
                if endpoint == "company.json":
                    return None
                if endpoint == "fnlttSinglAcntAll.json":
                    return financial()
                return {"status": "000", "list": [{"corp_code": PARAMS["corp_code"], "bsns_year": "-", "adtor": "-"}]}
            provider.override = response
            f = Fundamentals(Path(folder) / "fundamentals.sqlite", client=DartClient("a"*40, interval=0, opener=provider))
            try:
                plan = {"mode": "structured", "companies": [{"symbol": "005930", "corp_code": PARAMS["corp_code"]}],
                        "years": [2025], "apis": ["D1", "D4", "D10"], "report_codes": ["11011"], "workers": 1}
                persist = f.structured._persist
                def old_persist(db, corp, endpoint, params, fetched, symbol=None):
                    if endpoint == "accnutAdtorNmNdAdtOpinion.json":
                        raise DartError("response_scope_mismatch")
                    return persist(db, corp, endpoint, params, fetched, symbol)
                with patch.object(f.structured, "_persist", side_effect=old_persist):
                    out = f.collection_job("start", plan=plan)
                    f.structured.thread.join(10)
                with f.db() as db:
                    db.execute("UPDATE structured_requests SET result='{}' WHERE state='available'")
                    db.execute("DELETE FROM structured_calculations")
                before = len(provider.calls)
                provider.override = lambda e, p: self.fail("resume must reuse saved responses")
                f.collection_job("start", out["job_id"], retry_failed=True)
                f.structured.thread.join(10)
                final = f.collection_job("status", out["job_id"], limit=100)
                self.assertTrue(final["collection_complete"], final)
                self.assertFalse(final["data_complete"])
                self.assertEqual(len(provider.calls), before)
                self.assertTrue(all(r["result"]["network_requests"] == 0 for r in final["rows"]))
                with f.db() as db:
                    derived = json.loads(db.execute("SELECT body FROM structured_calculations").fetchone()[0])
                self.assertEqual(derived["ratios"]["net_margin_pct"], "15.0000")
                self.assertEqual(f.report("005930")["rows"][0]["data"]["ratios"]["net_margin_pct"], "15.0000")
                # A later legacy refresh must not expose old structured maths.
                changed = financial()
                changed["list"][0]["thstrm_amount"] = "400"
                with f.db() as db:
                    f._save(db, "financial:00126380:2025:11011:CFS", "00126380", changed)
                self.assertEqual(f.report("005930")["rows"][0]["data"]["ratios"]["operating_margin_pct"], "10.0000")
            finally:
                f.close()


if __name__ == "__main__":
    unittest.main()
