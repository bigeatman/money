"""Bounded, resumable structured OpenDART ingestion. No D3 or investment judgments."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import fcntl
import itertools
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .dart_client import DartError, unpack, xml_root
from .dart_data import amount, dumps, metrics

PERIOD_APIS = {
    "D5": "empSttus.json", "D6": "exctvSttus.json", "D7": "hyslrSttus.json",
    "D8": "stockTotqySttus.json", "D9": "tesstkAcqsDspsSttus.json",
    "D10": "accnutAdtorNmNdAdtOpinion.json", "D11": "otrCprInvstmntSttus.json",
    "D12": "alotMatter.json", "D13": "irdsSttus.json"}
DEFAULT_APIS = ["D1", "D2", "D4", *PERIOD_APIS, "D14"]
REPORTS = ["11013", "11012", "11014", "11011"]
PAY_APIS = ("hmvAuditIndvdlBySttus.json", "hmvAuditIndvdlBySttusV2.json")
STOP = {"missing_key", "010", "011", "012", "020", "901", "stopping"}
NUMERIC_FIELDS = {
    "thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_q_amount",
    "frmtrm_add_amount", "bfefrmtrm_amount", "sm", "fyer_salary_totamt", "jan_salary_am",
    "istc_totqy", "tesstk_co", "distb_stock_co", "isu_stock_totqy",
    "now_to_isu_stock_totqy", "now_to_dcrs_stock_totqy", "bsis_posesn_stock_co",
    "bsis_posesn_stock_qota", "trmend_posesn_stock_co", "trmend_posesn_stock_qota"}


def capacity():
    """Observed resources and a documented software ceiling, not a claimed optimum."""
    cpu = os.cpu_count() or 1
    try:
        cpu = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pass
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                memory[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    fd = None
    try:
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft > 0:
            fd = soft
    except (ImportError, OSError, ValueError):
        pass
    bounds = [32, max(1, cpu * 4)]
    if memory.get("MemAvailable"):
        bounds.append(max(1, memory["MemAvailable"] // (128 * 1024 * 1024)))
    if fd is not None:
        bounds.append(max(1, (fd - 64) // 4))
    ceiling = min(bounds)
    return {"cpu_available": cpu, "memory_total_bytes": memory.get("MemTotal"),
            "memory_available_bytes": memory.get("MemAvailable"), "file_descriptor_soft_limit": fd,
            "worker_ceiling": ceiling, "default_workers": min(8, ceiling),
            "ceiling_basis": "min(32, CPU*4, available_RAM/128MiB, (FD_limit-64)/4); resource estimate",
            "maximum_tested_on_this_device": None, "sqlite_concurrent_writers": 1,
            "network_during_write_transaction": False}


def normalize(plan):
    if not isinstance(plan, dict) or plan.get("mode") != "structured":
        raise ValueError("plan.mode must be structured")
    allowed = {"mode", "symbols", "companies", "years", "report_codes", "fs_divs", "apis",
               "disclosure_from", "disclosure_to", "workers", "request_interval_seconds", "refresh"}
    if set(plan) - allowed:
        raise ValueError("unknown structured plan option")
    if "companies" in plan and "symbols" in plan:
        raise ValueError("provide companies or symbols, not both")
    values = plan.get("companies", plan.get("symbols"))
    if not isinstance(values, list) or not values:
        raise ValueError("a nonempty explicit company list is required")
    companies, seen, corporations = [], set(), set()
    for value in values:
        value = {"symbol": value} if isinstance(value, str) else value
        if not isinstance(value, dict) or set(value) - {"symbol", "corp_code"}:
            raise ValueError("invalid company")
        symbol, corp = value.get("symbol"), value.get("corp_code")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol) or symbol in seen:
            raise ValueError("invalid or duplicate stock symbol")
        if corp is not None and (not isinstance(corp, str) or not re.fullmatch(r"[0-9]{8}", corp)):
            raise ValueError("invalid corp_code")
        if corp is not None and corp in corporations:
            raise ValueError("multiple symbols for one corporation require separate plans")
        companies.append({"symbol": symbol, "corp_code": corp})
        seen.add(symbol)
        if corp is not None:
            corporations.add(corp)
    years = plan.get("years")
    if not isinstance(years, list) or not years or any(
            isinstance(y, bool) or not isinstance(y, int) or not 2015 <= y <= datetime.now().year for y in years):
        raise ValueError("years must explicitly list business years from 2015 through this year")
    years = sorted(set(years))
    reports = plan.get("report_codes", REPORTS)
    bases = plan.get("fs_divs", ["CFS"])
    apis = plan.get("apis", DEFAULT_APIS)
    for values, options, label in [(reports, REPORTS, "report_codes"), (bases, ["CFS", "OFS"], "fs_divs"),
                                   (apis, DEFAULT_APIS, "apis (D3 is excluded)")]:
        if (not isinstance(values, list) or not values
                or any(not isinstance(x, str) or x not in options for x in values)
                or len(set(values)) != len(values)):
            raise ValueError("invalid " + label)
    begin = plan.get("disclosure_from", str(min(years)) + "0101")
    end = plan.get("disclosure_to", str(max(years)) + "1231")
    for value in (begin, end):
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
            raise ValueError("invalid disclosure receipt date")
        datetime.strptime(value, "%Y%m%d")
    if begin > end:
        raise ValueError("disclosure_from must not exceed disclosure_to")
    workers = plan.get("workers")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32):
        raise ValueError("workers must be 1..32")
    interval = plan.get("request_interval_seconds")
    if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float))
                                 or not math.isfinite(interval) or not 0 <= interval <= 30):
        raise ValueError("request_interval_seconds must be 0..30")
    refresh = plan.get("refresh", False)
    if not isinstance(refresh, bool):
        raise ValueError("refresh must be boolean")
    result = {"mode": "structured", "companies": companies, "years": years, "report_codes": reports,
              "fs_divs": bases, "apis": apis, "disclosure_from": begin, "disclosure_to": end,
              "workers": workers, "request_interval_seconds": interval, "refresh": refresh}
    if len(dumps(result).encode()) > 1800000:
        raise ValueError("structured plan exceeds 1.8 MB")
    return result


def calculated(payload, endpoint):
    """Keep provider rows intact and add only named numeric conversions/formulas."""
    rows = payload.get("list", []) if isinstance(payload, dict) else []
    if endpoint == "company.json" and payload.get("status") == "000":
        rows = [payload]
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise DartError("invalid_structured_rows")
    if payload.get("status") == "000" and not rows:
        raise DartError("invalid_structured_rows")
    if payload.get("status") == "013" and rows:
        raise DartError("invalid_structured_rows")
    output = []
    for record in rows:
        numbers = {key: str(number) if (number := amount(value)) is not None else None
                   for key, value in record.items() if key in NUMERIC_FIELDS}
        values = {"provider": record, "numeric": numbers}
        if endpoint == "stockTotqySttus.json":
            issued, own = amount(record.get("istc_totqy")), amount(record.get("tesstk_co"))
            values["calculated"] = {"shares_excluding_treasury": str(issued - own) if issued is not None and own is not None else None,
                                    "formula": "istc_totqy - tesstk_co"}
        output.append(values)
    derived = metrics(payload) if endpoint == "fnlttSinglAcntAll.json" else None
    return output, derived


class StructuredCollector:
    def __init__(self, fundamentals):
        self.f = fundamentals
        self.thread = None
        self.active_id = None
        self.guard = threading.Lock()
        self.fetch_guard = threading.Lock()
        self.fetch_active = self.fetch_peak = 0
        with self.f.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS structured_jobs (
                    id TEXT PRIMARY KEY, plan TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    created TEXT NOT NULL, updated TEXT NOT NULL, execution TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS structured_requests (
                    job TEXT NOT NULL, ordinal INTEGER NOT NULL, symbol TEXT NOT NULL,
                    corp TEXT NOT NULL, code TEXT NOT NULL, endpoint TEXT NOT NULL, params TEXT NOT NULL,
                    source_key TEXT NOT NULL, state TEXT NOT NULL, result TEXT, error TEXT,
                    PRIMARY KEY(job,source_key), UNIQUE(job,ordinal));
                CREATE INDEX IF NOT EXISTS structured_pending ON structured_requests(job,state,ordinal);
                CREATE TABLE IF NOT EXISTS structured_rows (
                    source_key TEXT NOT NULL, ordinal INTEGER NOT NULL, corp TEXT NOT NULL,
                    endpoint TEXT NOT NULL, year TEXT, report TEXT, basis TEXT,
                    receipt TEXT, body TEXT NOT NULL, PRIMARY KEY(source_key,ordinal));
                CREATE INDEX IF NOT EXISTS structured_scope ON structured_rows(corp,year,report,basis);
                CREATE TABLE IF NOT EXISTS structured_calculations (
                    source_key TEXT PRIMARY KEY, corp TEXT NOT NULL, year TEXT,
                    report TEXT, basis TEXT, body TEXT NOT NULL);
            """)

    def _row(self, job):
        if not isinstance(job, str) or not re.fullmatch(r"structured_[A-Za-z0-9_-]{1,80}", job):
            raise ValueError("invalid structured job ID")
        with self.f.db() as db:
            row = db.execute("SELECT * FROM structured_jobs WHERE id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("structured job not found")
        return row

    def start(self, plan=None, job_id=None, retry_failed=False):
        validated = normalize(plan) if plan is not None else None
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise ValueError("a structured job is already running")
            if self.f.stop.is_set() or not self.f.config["enabled"] or not self.f.client.api_key:
                raise ValueError("OpenDART must be enabled, configured and running")
            if validated is None and job_id is None:
                raise ValueError("plan or job_id is required")
            job = job_id or "structured_" + uuid.uuid4().hex
            if not re.fullmatch(r"structured_[A-Za-z0-9_-]{1,80}", job):
                raise ValueError("structured job ID must start with structured_")
            locks = []
            try:
                for suffix in (".jobs.lock", self.f.path.suffix + ".lock"):
                    lock = self.f.path.with_suffix(suffix).open("a+")
                    locks.append(lock)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise ValueError("another OpenDART collection is active") from None
                at = self.f.clock().isoformat()
                with self.f.db() as db:
                    old = db.execute("SELECT * FROM structured_jobs WHERE id=?", (job,)).fetchone()
                    if old is not None:
                        if validated is not None and old["plan"] != dumps(validated):
                            raise ValueError("job ID belongs to a different plan")
                        validated = json.loads(old["plan"])
                    elif validated is None:
                        raise ValueError("structured job not found")
                    else:
                        db.execute("INSERT INTO structured_jobs VALUES (?,?,?,NULL,?,?,?)",
                                   (job, dumps(validated), "pending", at, at, "{}"))
                    db.execute("UPDATE structured_requests SET state='pending',error=NULL WHERE job=? AND state='running'", (job,))
                    if retry_failed:
                        db.execute("UPDATE structured_requests SET state='pending',error=NULL WHERE job=? AND state='failed'", (job,))
                    db.execute("UPDATE structured_jobs SET status='running',error=NULL,updated=? WHERE id=?", (at, job))
                self.active_id = job
                self.thread = threading.Thread(target=self._worker, args=(job, validated, locks),
                                               name="dart-structured", daemon=True)
                self.thread.start()
            except Exception:
                for lock in locks:
                    lock.close()
                raise
        return self.report(job)

    def _fetch(self, endpoint, params, refresh):
        started = time.monotonic()
        key = self.f.archive.source_key(endpoint, params)
        with self.f.db() as db:
            stored, payload = self.f._stored(db, key)
        hit = stored is not None and not refresh
        if not hit:
            with self.fetch_guard:
                self.fetch_active += 1
                self.fetch_peak = max(self.fetch_peak, self.fetch_active)
            try:
                payload = self.f.client.get(endpoint, params)
            finally:
                with self.fetch_guard:
                    self.fetch_active -= 1
        local = getattr(self.f.client, "local", None)
        received = getattr(local, "received_at", None) if not hit else None
        return {"key": key, "payload": payload, "cache_hit": hit,
                "started": started, "received": received, "fetched": time.monotonic(),
                "network_requests": 0 if hit else getattr(local, "attempts", 1)}

    def _persist(self, db, corp, endpoint, params, fetched, symbol=None):
        payload, key = fetched["payload"], fetched["key"]
        status = payload.get("status") if isinstance(payload, dict) else None
        availability = "no_data" if status == "013" else "available"
        self.f._save(db, key, corp, payload, availability, request_key=key)
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("request:" + key,
                   dumps({"endpoint": endpoint, "parameters": params, "checked_at": self.f.clock().isoformat()})))
        if isinstance(payload, dict):
            rows, derived = calculated(payload, endpoint)
            db.execute("DELETE FROM structured_rows WHERE source_key=?", (key,))
            db.execute("DELETE FROM structured_calculations WHERE source_key=?", (key,))
            for index, value in enumerate(rows):
                record = value["provider"]
                for field in ("corp_code", "bsns_year", "reprt_code", "fs_div"):
                    if field in params and field in record and str(record[field]) != str(params[field]):
                        raise DartError("response_scope_mismatch")
                if symbol and record.get("stock_code") and record["stock_code"] != symbol:
                    raise DartError("response_scope_mismatch")
                db.execute("INSERT INTO structured_rows VALUES (?,?,?,?,?,?,?,?,?)",
                           (key, index, corp, endpoint, params.get("bsns_year"), params.get("reprt_code"),
                            params.get("fs_div"), record.get("rcept_no"), dumps(value)))
            if derived is not None:
                db.execute("INSERT OR REPLACE INTO structured_calculations VALUES (?,?,?,?,?,?)",
                           (key, corp, params.get("bsns_year"), params.get("reprt_code"), params.get("fs_div"), dumps(derived)))
            if endpoint == "list.json":
                for value in rows:
                    r = value["provider"]
                    if not re.fullmatch(r"\d{14}", r.get("rcept_no", "")):
                        raise DartError("invalid_receipt")
                    if not params["bgn_de"] <= r.get("rcept_dt", "") <= params["end_de"]:
                        raise DartError("invalid_receipt_date")
                    db.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?)",
                               (r["rcept_no"], corp, r["rcept_dt"], r.get("report_nm", ""), r.get("flr_nm", ""), dumps(r)))
            if endpoint == "company.json" and rows and symbol and payload.get("corp_name"):
                db.execute("INSERT OR REPLACE INTO issuers VALUES (?,?,?,NULL,1,?)",
                           (corp, symbol, payload["corp_name"], dumps(payload)))
            # Keep the established get_fundamentals financial/company reader
            # usable while retaining the exact request key for deduplication.
            if endpoint == "company.json":
                self.f._save(db, "company:" + corp, corp, payload, availability, request_key=key)
            elif endpoint == "fnlttSinglAcntAll.json":
                alias = "financial:" + ":".join(str(params[k]) for k in ("corp_code", "bsns_year", "reprt_code", "fs_div"))
                self.f._save(db, alias, corp, payload, availability, request_key=key)
            return availability, len(rows), derived
        return availability, 0, None

    def _resolve(self, plan):
        companies = [dict(c) for c in plan["companies"]]
        unknown = []
        with self.f.db() as db:
            for company in companies:
                rows = db.execute("SELECT corp FROM issuers WHERE symbol=? AND active=1", (company["symbol"],)).fetchall()
                if company["corp_code"] is None:
                    if len(rows) == 1:
                        company["corp_code"] = rows[0][0]
                    else:
                        unknown.append(company)
                elif rows and any(r[0] != company["corp_code"] for r in rows):
                    raise DartError("issuer_mapping_conflict")
        if unknown:
            fetched = self._fetch("corpCode.xml", {}, False)
            xmls = [body for name, body in unpack(fetched["payload"]) if name.lower().endswith(".xml")]
            if len(xmls) != 1:
                raise DartError("invalid_master")
            mapping = {}
            wanted = {c["symbol"] for c in unknown}
            for node in xml_root(xmls[0]).findall("list"):
                item = {c.tag: (c.text or "").strip() for c in node}
                symbol = item.get("stock_code")
                if symbol in wanted:
                    if symbol in mapping or not re.fullmatch(r"\d{8}", item.get("corp_code", "")):
                        raise DartError("invalid_master")
                    mapping[symbol] = item
            if any(c["symbol"] not in mapping for c in unknown):
                raise DartError("stock_not_in_master")
            with self.f.db() as db:
                self._persist(db, "", "corpCode.xml", {}, fetched)
                for company in unknown:
                    item = mapping[company["symbol"]]
                    company["corp_code"] = item["corp_code"]
                    db.execute("INSERT OR REPLACE INTO issuers VALUES (?,?,?,?,1,?)",
                               (item["corp_code"], company["symbol"], item["corp_name"], item.get("modify_date"), dumps(item)))
        if len({c["corp_code"] for c in companies}) != len(companies):
            raise DartError("issuer_mapping_conflict")
        return companies

    def _add(self, db, job, company, code, endpoint, params):
        key = self.f.archive.source_key(endpoint, params)
        ordinal = db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM structured_requests WHERE job=?", (job,)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO structured_requests VALUES (?,?,?,?,?,?,?,?,'pending',NULL,NULL)",
                   (job, ordinal, company["symbol"], company["corp_code"], code, endpoint, dumps(params), key))

    def _compile(self, db, job, plan, companies):
        queues = []
        for company in companies:
            corp, requests = company["corp_code"], []
            if "D1" in plan["apis"]:
                requests.append(("D1", "company.json", {"corp_code": corp}))
            if "D2" in plan["apis"]:
                requests.append(("D2", "list.json", {"corp_code": corp, "bgn_de": plan["disclosure_from"],
                    "end_de": plan["disclosure_to"], "last_reprt_at": "N", "page_count": 100, "page_no": 1}))
            for year in plan["years"]:
                for report in plan["report_codes"]:
                    base = {"corp_code": corp, "bsns_year": str(year), "reprt_code": report}
                    if "D4" in plan["apis"]:
                        for basis in plan["fs_divs"]:
                            requests.append(("D4", "fnlttSinglAcntAll.json", {**base, "fs_div": basis}))
                    for code, endpoint in PERIOD_APIS.items():
                        if code in plan["apis"]:
                            requests.append((code, endpoint, base))
                    if "D14" in plan["apis"]:
                        # This selects a first request, never invents a fact. A no-data
                        # response schedules the other published schema exactly once.
                        requests.append(("D14", PAY_APIS[year >= 2026], base))
            queues.append([(company, *request) for request in requests])
        # Fair scheduling across stocks, with one global HTTP-worker bound.
        for group in itertools.zip_longest(*queues):
            for item in group:
                if item is not None:
                    self._add(db, job, *item)

    def _followups(self, db, job, row, payload):
        company = {"symbol": row["symbol"], "corp_code": row["corp"]}
        params = json.loads(row["params"])
        if row["code"] == "D2" and payload.get("status") == "000":
            try:
                pages, count = int(payload["total_page"]), int(payload["total_count"])
            except (KeyError, TypeError, ValueError):
                raise DartError("invalid_pagination") from None
            if pages < 1 or count < 1 or pages != (count + 99) // 100 or params["page_no"] > pages:
                raise DartError("invalid_pagination")
            if not payload.get("list"):
                raise DartError("incomplete_pagination")
            if params["page_no"] < pages:
                self._add(db, job, company, "D2", "list.json", {**params, "page_no": params["page_no"] + 1})
        if row["code"] == "D14" and payload.get("status") == "013":
            endpoint = PAY_APIS[1] if row["endpoint"] == PAY_APIS[0] else PAY_APIS[0]
            self._add(db, job, company, "D14", endpoint, params)

    def _verify_pages(self, db, job):
        groups = {}
        for row in db.execute("SELECT * FROM structured_requests WHERE job=? AND code='D2'", (job,)):
            if row["state"] not in {"available", "no_data"}:
                continue
            _, payload = self.f._stored(db, row["source_key"])
            group = groups.setdefault(row["corp"], [])
            group.append(payload)
        for pages in groups.values():
            if len(pages) == 1 and pages[0].get("status") == "013":
                continue
            expected = {(p.get("total_count"), p.get("total_page")) for p in pages}
            receipts = [r.get("rcept_no") for p in pages for r in p.get("list", [])]
            if len(expected) != 1 or any(p.get("status") != "000" for p in pages):
                raise DartError("pagination_changed")
            count, total = next(iter(expected))
            if len(pages) != int(total) or len(receipts) != int(count) or len(set(receipts)) != len(receipts):
                raise DartError("incomplete_pagination")

    def _worker(self, job, plan, locks):
        begin = time.monotonic()
        state, error, active = "complete", None, {}
        previous_interval = self.f.client.interval
        info = capacity()
        workers = min(plan["workers"] or info["default_workers"], info["worker_ceiling"])
        execution = {"requested_workers": plan["workers"], "effective_workers": workers,
                     "capacity": info, "bootstrap_ms": 0, "elapsed_ms": None,
                     "measurement_scope": "this attempt", "observed_peak_fetches": 0}
        stock_times = {}
        self.fetch_peak = 0
        try:
            if plan["request_interval_seconds"] is not None:
                self.f.client.interval = plan["request_interval_seconds"]
            companies = self._resolve(plan)
            execution["bootstrap_ms"] = round((time.monotonic() - begin) * 1000, 3)
            execution["request_interval_seconds"] = self.f.client.interval
            with self.f.db() as db:
                self._compile(db, job, plan, companies)
                db.execute("UPDATE structured_jobs SET execution=? WHERE id=?", (dumps(execution), job))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dart-http") as pool:
                while True:
                    if self.f.stop.is_set() and state == "complete":
                        state, error = "interrupted", "stopping"
                    if state == "complete":
                        with self.f.db() as db:
                            pending = db.execute("SELECT * FROM structured_requests WHERE job=? AND state='pending' ORDER BY ordinal LIMIT ?",
                                                 (job, workers - len(active))).fetchall()
                            for row in pending:
                                db.execute("UPDATE structured_requests SET state='running' WHERE job=? AND source_key=?", (job, row["source_key"]))
                        for row in pending:
                            active[pool.submit(self._fetch, row["endpoint"], json.loads(row["params"]), plan["refresh"])] = row
                    if not active:
                        break
                    ready, _ = wait(active, return_when=FIRST_COMPLETED, timeout=.2)
                    for future in ready:
                        row = active.pop(future)
                        outcome, failure = None, None
                        try:
                            fetched = future.result()
                            process_start = time.monotonic()
                            with self.f.db() as db:
                                availability, count, derived = self._persist(db, row["corp"], row["endpoint"], json.loads(row["params"]), fetched, row["symbol"])
                                self._followups(db, job, row, fetched["payload"])
                                outcome = {"source_key": row["source_key"], "cache_hit": fetched["cache_hit"],
                                    "network_requests": fetched["network_requests"], "normalized_rows": count,
                                    "calculations": derived, "fetch_and_archive_ms": round((fetched["fetched"] - fetched["started"]) * 1000, 3)}
                                db.execute("UPDATE structured_requests SET state=?,result=?,error=NULL WHERE job=? AND source_key=?",
                                           (availability, dumps(outcome), job, row["source_key"]))
                            # This timestamp is after the successful SQLite COMMIT.
                            committed = time.monotonic()
                            timing = stock_times.setdefault(row["symbol"], {"first_fetch": fetched["started"],
                                "last_received": None, "last_commit": committed, "completed_requests": 0})
                            timing["first_fetch"] = min(timing["first_fetch"], fetched["started"])
                            timing["last_commit"] = committed
                            timing["completed_requests"] += 1
                            if fetched["received"] is not None:
                                timing["last_received"] = max(timing["last_received"] or 0, fetched["received"])
                            outcome["normalize_and_commit_ms"] = round((committed - process_start) * 1000, 3)
                            outcome["received_to_commit_ms"] = round((committed - fetched["received"]) * 1000, 3) if fetched["received"] is not None else None
                            with self.f.db() as db:
                                db.execute("UPDATE structured_requests SET result=? WHERE job=? AND source_key=?",
                                           (dumps(outcome), job, row["source_key"]))
                        except DartError as exc:
                            failure = exc.code
                        except Exception:
                            failure = "collection_error"
                        if failure:
                            pending_state = failure in STOP
                            if pending_state:
                                state, error = "paused", failure
                            with self.f.db() as db:
                                db.execute("UPDATE structured_requests SET state=?,error=? WHERE job=? AND source_key=?",
                                           ("pending" if pending_state else "failed", failure, job, row["source_key"]))
                        with self.f.db() as db:
                            db.execute("UPDATE structured_jobs SET updated=? WHERE id=?", (self.f.clock().isoformat(), job))
            with self.f.db() as db:
                failed = db.execute("SELECT COUNT(*) FROM structured_requests WHERE job=? AND state='failed'", (job,)).fetchone()[0]
                if state == "complete" and failed:
                    state = "incomplete"
                if state == "complete":
                    self._verify_pages(db, job)
        except DartError as exc:
            error = exc.code
            state = "paused" if error in STOP else "incomplete"
        except Exception:
            state, error = "interrupted", "structured_job_error"
        finally:
            execution["elapsed_ms"] = round((time.monotonic() - begin) * 1000, 3)
            execution["observed_peak_fetches"] = self.fetch_peak
            execution["timings_by_symbol"] = {symbol: {
                "completed_requests": t["completed_requests"],
                "fetch_through_final_commit_ms": round((t["last_commit"] - t["first_fetch"]) * 1000, 3),
                "last_response_to_final_commit_ms": round((t["last_commit"] - t["last_received"]) * 1000, 3)
                    if t["last_received"] is not None else None} for symbol, t in stock_times.items()}
            self.f.client.interval = previous_interval
            try:
                with self.f.db() as db:
                    db.execute("UPDATE structured_jobs SET status=?,error=?,updated=?,execution=? WHERE id=?",
                               (state, error, self.f.clock().isoformat(), dumps(execution), job))
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('structured_last_execution',?)", (dumps(execution),))
            finally:
                for lock in locks:
                    lock.close()

    def report(self, job, offset=0, limit=20, symbol=None):
        self.f._page(offset, limit)
        header = self._row(job)
        plan = json.loads(header["plan"])
        if symbol is not None and symbol not in {c["symbol"] for c in plan["companies"]}:
            raise ValueError("symbol outside structured plan")
        with self.f.db() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM structured_requests WHERE job=? GROUP BY state", (job,)))
            where, args = "job=?", [job]
            if symbol is not None:
                where += " AND symbol=?"; args.append(symbol)
            found = db.execute("SELECT * FROM structured_requests WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?",
                               (*args, limit + 1, offset)).fetchall()
            companies = [dict(r) for r in db.execute("SELECT symbol,state,COUNT(*) AS count FROM structured_requests WHERE job=? GROUP BY symbol,state", (job,))]
        rows = [dict(r) for r in found[:limit]]
        for row in rows:
            row["params"] = json.loads(row["params"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        status = header["status"]
        if status == "running" and not (self.thread and self.thread.is_alive() and self.active_id == job):
            with self.f.path.with_suffix(".jobs.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status = "interrupted"
                except BlockingIOError:
                    pass
        return {"job_id": job, "mode": "structured", "status": status, "last_error": header["error"],
                "counts": counts, "total": sum(counts.values()), "by_company": companies, "rows": rows,
                "next_offset": offset + limit if len(found) > limit else None,
                "execution": json.loads(header["execution"]), "collection_complete": status == "complete",
                "D3_excluded": True, "investment_review_complete": False, "read_only": True}

    def close(self):
        if self.thread:
            self.thread.join(35)


def benchmark_capacity(directory, levels=None):
    """Exercise this device's real archive/SQLite pipeline with local fake responses.

    No DART requests, credentials, issuer data or production cache writes occur.
    This proves concurrency for the stated fixture, not an optimal DART rate.
    """
    import io
    import tempfile
    from .dart_client import DartClient
    from .fundamentals import Fundamentals

    info = capacity()
    levels = levels or [n for n in (1, 2, 4, 8, 16, 32) if n <= info["worker_ceiling"]]
    results = []

    def respond(url, timeout):
        from urllib.parse import parse_qs, urlparse
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if k != "crtfc_key"}
        time.sleep(.02)
        rows = [{**params, "sj_div": "BS", "account_id": "fixture_" + str(i),
                 "thstrm_amount": str(i * 1000), "currency": "KRW"} for i in range(64)]
        return io.BytesIO(dumps({"status": "000", "list": rows}).encode())

    with tempfile.TemporaryDirectory(prefix="dart-benchmark-", dir=directory) as folder:
        for workers in levels:
            sample = Fundamentals(Path(folder) / (str(workers) + ".sqlite"),
                                  client=DartClient("offline-benchmark", interval=0, opener=respond))
            try:
                job = sample.collection_job("start", plan={"mode": "structured", "companies": [
                    {"symbol": "000000", "corp_code": "99999999"}], "years": list(range(2015, 2023)),
                    "apis": ["D4"], "workers": workers, "request_interval_seconds": 0})
                sample.structured.thread.join(30)
                if sample.structured.thread.is_alive():
                    raise ValueError("local benchmark timed out")
                out = sample.collection_job("status", job["job_id"], limit=100)
                timings = sorted(r["result"]["received_to_commit_ms"] for r in out["rows"] if r["result"])
                results.append({"workers": workers, "complete": out["collection_complete"],
                    "observed_peak_fetches": out["execution"]["observed_peak_fetches"],
                    "elapsed_ms": out["execution"]["elapsed_ms"],
                    "received_to_commit_p95_ms": timings[math.ceil(len(timings) * .95) - 1] if timings else None})
                if not out["collection_complete"]:
                    break
            finally:
                sample.close()
    completed = [r for r in results if r["complete"]]
    return {"kind": "local_fixture_on_this_device", "capacity": info,
            "requests_per_level": 32, "rows_per_response": 64, "simulated_latency_ms": 20,
            "real_DART_requests": 0, "results": results,
            "maximum_verified_workers": max((r["observed_peak_fetches"] for r in completed), default=0),
            "fastest_fixture_workers": min(completed, key=lambda r: r["elapsed_ms"])["workers"] if completed else None,
            "limitation": "Fixture result only; network latency, DART throttling and device load change real throughput."}
