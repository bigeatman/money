#!/usr/bin/env python3
"""Delete research, fetch Hyundai plus two random DART issuers, and check storage."""
import argparse
from collections import Counter
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from money.cli import load_env
from money.dart_client import unpack, xml_root
from money.fundamentals import Fundamentals

JOB_ID = "structured_fresh_three"
SCRIPT = Path(__file__).with_name("reset-research.py")
spec = importlib.util.spec_from_file_location("reset_research", SCRIPT)
reset_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_module)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def run(root, client=None):
    begin = time.monotonic()
    root = Path(root).resolve()
    config = json.loads((root / "config.json").read_text())
    load_env(root / ".env")
    # Preflight before deleting anything. API credentials are never printed.
    if not config.get("opendart", {}).get("enabled", True):
        raise ValueError("OpenDART is disabled; nothing was deleted")
    if client is None and not os.environ.get("OPENDART_API_KEY"):
        raise ValueError("OpenDART API key is missing; nothing was deleted")
    emit({"stage": "delete_research", "result": reset_module.reset(root)})
    f = Fundamentals(root / "runtime" / "fundamentals.sqlite", config.get("opendart"), client=client)
    try:
        emit({"stage": "select_random_issuers", "source": "fresh corpCode.xml; two distinct non-Hyundai issuers with stock codes"})
        raw = f.client.get("corpCode.xml", {})
        xmls = [body for name, body in unpack(raw) if name.lower().endswith(".xml")]
        if len(xmls) != 1:
            raise ValueError("Invalid DART issuer dictionary")
        pool = {}
        for node in xml_root(xmls[0]).findall("list"):
            item = {child.tag: (child.text or "").strip() for child in node}
            corp, symbol = item.get("corp_code", ""), item.get("stock_code", "")
            if re.fullmatch(r"[A-Z0-9]{6}", symbol) and re.fullmatch(r"\d{8}", corp) and corp != "00164742" and symbol != "005380":
                if corp in pool and pool[corp]["symbol"] != symbol:
                    raise ValueError("Ambiguous issuer dictionary")
                pool[corp] = {"symbol": symbol, "corp_code": corp, "name": item.get("corp_name")}
        if len(pool) < 2:
            raise ValueError("Not enough distinct stock-coded issuers")
        selected = [{"symbol": "005380", "corp_code": "00164742", "name": "현대차"},
                    *secrets.SystemRandom().sample(list(pool.values()), 2)]
        setup_ms = round((time.monotonic() - begin) * 1000, 3)
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        plan = {"mode": "structured", "companies": [{k: c[k] for k in ("symbol", "corp_code")} for c in selected],
                "years": [2025], "report_codes": ["11013", "11012", "11014", "11011"], "fs_divs": ["CFS", "OFS"],
                "disclosure_from": "20250101", "disclosure_to": today,
                "workers": 8, "request_interval_seconds": 0, "refresh": True}
        emit({"stage": "fresh_collection", "job_id": JOB_ID, "selected": selected, "random_pool_size": len(pool), "refresh": True})
        f.collection_job("start", JOB_ID, plan)
        progress = time.monotonic()
        while f.structured.thread.is_alive():
            f.structured.thread.join(1)
            if time.monotonic() - progress >= 10:
                emit({"stage": "collection_progress", "counts": f.collection_job("status", JOB_ID, limit=1)["counts"]})
                progress = time.monotonic()
        result = f.collection_job("status", JOB_ID, limit=1)
        reports = []
        with f.db() as db:
            for company in selected:
                rows = [dict(r) for r in db.execute("SELECT * FROM structured_requests WHERE job=? AND symbol=? ORDER BY ordinal", (JOB_ID, company["symbol"]))]
                details = [json.loads(r["result"]) for r in rows if r["result"]]
                errors = [{"api": r["code"], "endpoint": r["endpoint"], "params": json.loads(r["params"]), "error": r["error"]}
                          for r in rows if r["state"] not in ("available", "no_data")]
                gaps = [{"api": r["code"], "report": json.loads(r["params"]).get("reprt_code"),
                         "status": d.get("data_quality", {}).get("status"),
                         "missing_fields": d.get("data_quality", {}).get("missing_fields", {})}
                        for r, d in ((r, json.loads(r["result"])) for r in rows if r["result"])
                        if d.get("data_quality", {}).get("status") != "available"]
                reports.append({**company, "counts": dict(Counter(r["state"] for r in rows)), "errors": errors,
                    "cache_hits": sum(bool(d["cache_hit"]) for d in details),
                    "network_requests": sum(d["network_requests"] for d in details),
                    "normalized_rows": sum(d["normalized_rows"] for d in details), "missing_data": gaps,
                    "timing": result["execution"].get("timings_by_symbol", {}).get(company["symbol"])})
            d3 = db.execute("SELECT COUNT(*) FROM archive_requests WHERE endpoint='document.xml'").fetchone()[0]
            provider_errors = [dict(r) for r in db.execute("SELECT endpoint,params,error FROM archive_requests WHERE error IS NOT NULL")]
        summary = {"job_id": JOB_ID, "fresh_collection": True, "selected": selected,
                   "random_selection_source": "stock-coded issuers in freshly downloaded DART corpCode.xml", "random_pool_size": len(pool),
                   "processing_ok": result["collection_complete"] and not provider_errors and not d3 and all(
                       not r["errors"] and not r["cache_hits"] and r["normalized_rows"] > 0 for r in reports),
                   "data_complete": result["data_complete"], "status": result["status"], "last_error": result["last_error"],
                   "elapsed_ms": result["execution"]["elapsed_ms"],
                   "reset_and_dictionary_ms": setup_ms,
                   "total_ms": round((time.monotonic() - begin) * 1000, 3), "D3_requests": d3,
                   "provider_errors": provider_errors, "companies": reports,
                   "missing_data_meaning": "Provider blanks/013 remain unknown; processing success is not complete DOCX coverage"}
        (root / "runtime" / "dart-fresh-check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        emit({"stage": "check_complete", **summary})
        return summary
    finally:
        f.stop.set()
        f.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--erase-research", action="store_true", required=True)
    args = parser.parse_args()
    try:
        sys.exit(0 if run(args.root)["processing_ok"] else 1)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
