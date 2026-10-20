"""Explicit resumable DART request plans and attributed research records.

No universe expansion, trading, automatic schedules or investment decisions.
"""
import fcntl
import json
import re
import threading
import uuid

from .dart_client import DartError, validate_request
from .dart_data import dumps

STOP = {"missing_key", "010", "011", "012", "020", "901", "stopping"}
STATES = {"pending", "found", "partial", "raw_only", "external_required",
          "not_disclosed", "not_applicable", "fetch_failed", "calculation_required"}


class DartJobs:
    def __init__(self, fundamentals):
        self.f = fundamentals
        self.thread = None
        self.active_id = None
        self.guard = threading.Lock()
        with self.f.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dart_jobs (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL,
                    status TEXT NOT NULL, plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS dart_job_requests (
                    job TEXT NOT NULL, ordinal INTEGER NOT NULL, symbol TEXT NOT NULL,
                    corp TEXT NOT NULL, endpoint TEXT NOT NULL, params TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, error TEXT, updated TEXT NOT NULL,
                    PRIMARY KEY(job,ordinal));
                CREATE INDEX IF NOT EXISTS dart_job_state ON dart_job_requests(job,state,ordinal);
                CREATE TABLE IF NOT EXISTS dart_reviews (
                    job TEXT NOT NULL, symbol TEXT NOT NULL, item TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL,
                    sha256 TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(job,symbol,item));
                CREATE TABLE IF NOT EXISTS dart_review_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                    symbol TEXT NOT NULL, item TEXT NOT NULL, sha256 TEXT NOT NULL, updated TEXT NOT NULL);
            """)

    def _plan(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ValueError("invalid collection job ID")
        with self.f.db() as db:
            row = db.execute("SELECT * FROM dart_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("collection job not found")
        return row, json.loads(row["plan"])

    @staticmethod
    def validate(plan):
        if not isinstance(plan, dict) or not isinstance(plan.get("companies"), list) or not plan["companies"]:
            raise ValueError("plan.companies is required")
        companies = {}
        for company in plan["companies"]:
            symbol, corp = company.get("symbol"), company.get("corp_code")
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
                raise ValueError("invalid plan stock symbol")
            if not isinstance(corp, str) or not re.fullmatch(r"[0-9]{8}", corp) or symbol in companies:
                raise ValueError("invalid or duplicate plan issuer")
            companies[symbol] = corp
        requests = plan.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 20000:
            raise ValueError("plan.requests must contain 1..20000 explicit requests")
        seen = set()
        for request in requests:
            symbol = request.get("symbol")
            if symbol not in companies or request.get("corp_code") != companies[symbol]:
                raise ValueError("request is outside the explicit company plan")
            endpoint, params = request.get("endpoint"), request.get("params", {})
            validate_request(endpoint, params)
            if params.get("corp_code", companies[symbol]) != companies[symbol]:
                raise ValueError("request issuer does not match its stock")
            identity = (symbol, endpoint, dumps(params))
            if identity in seen:
                raise ValueError("duplicate request in plan")
            seen.add(identity)
        catalog = plan.get("catalog", [])
        if not isinstance(catalog, list) or any(not isinstance(x, dict) or not x.get("id") for x in catalog):
            raise ValueError("invalid review catalog")
        if len({x["id"] for x in catalog}) != len(catalog):
            raise ValueError("duplicate review catalog item")
        # Fail closed for NaN, unsupported values and accidental oversized plans.
        if len(dumps(plan).encode()) > 1800000:
            raise ValueError("collection plan exceeds 1.8 MB")

    def create(self, plan, job_id=None):
        self.validate(plan)
        job_id = job_id or "dart_" + uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ValueError("invalid collection job ID")
        body, at = dumps(plan), self.f.clock().isoformat()
        with self.f.db() as db:
            old = db.execute("SELECT plan FROM dart_jobs WHERE id=?", (job_id,)).fetchone()
            if old:
                if old[0] != body:
                    raise ValueError("job ID already belongs to a different plan")
                return job_id
            checksum = self.f.archive.store(db, body.encode(), "user_collection_plan")
            db.execute("INSERT INTO dart_jobs VALUES (?,?,?,?,?,?,NULL)",
                       (job_id, at, at, "pending", body, checksum))
            for ordinal, request in enumerate(plan["requests"]):
                db.execute("INSERT INTO dart_job_requests VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
                           (job_id, ordinal, request["symbol"], request["corp_code"], request["endpoint"],
                            dumps(request.get("params", {})), "pending", at))
        return job_id

    def start(self, job_id=None, plan=None, retry_failed=False):
        with self.guard:
            if self.thread and self.thread.is_alive():
                raise ValueError("a DART collection job is already running")
            if self.f.stop.is_set():
                raise ValueError("DART collector is stopping")
            if not self.f.config["enabled"] or not self.f.client.api_key:
                raise ValueError("OpenDART must be enabled and configured")
            lock = self.f.path.with_suffix(".jobs.lock").open("a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise ValueError("another DART job process is active") from None
            try:
                if plan is not None:
                    job_id = self.create(plan, job_id)
                self._plan(job_id)
                with self.f.db() as db:
                    db.execute("UPDATE dart_jobs SET status='interrupted',error='previous_process_stopped' WHERE status='running' AND id!=?", (job_id,))
                    states = ("running", "failed") if retry_failed else ("running",)
                    for state in states:
                        db.execute("UPDATE dart_job_requests SET state='pending',error=NULL WHERE job=? AND state=?", (job_id, state))
                    db.execute("UPDATE dart_jobs SET status='running',error=NULL,updated=? WHERE id=?",
                               (self.f.clock().isoformat(), job_id))
            except Exception:
                lock.close()
                raise
            self.thread = threading.Thread(target=self._worker, args=(job_id, lock),
                                           name="dart-explicit-plan", daemon=True)
            self.active_id = job_id
            self.thread.start()
        return self.report(job_id)

    def _worker(self, job_id, lock):
        status, error = "complete", None
        try:
            while not self.f.stop.is_set():
                with self.f.db() as db:
                    row = db.execute("SELECT * FROM dart_job_requests WHERE job=? AND state='pending' ORDER BY ordinal LIMIT 1", (job_id,)).fetchone()
                    if row is None:
                        break
                    db.execute("UPDATE dart_job_requests SET state='running' WHERE job=? AND ordinal=?", (job_id, row["ordinal"]))
                result, failure, state = None, None, "failed"
                try:
                    # Every request passes through the same exact-body archive.
                    result = self.f.call_api(row["endpoint"], json.loads(row["params"]), False, False)
                    state = result["availability"]
                    with self.f.db() as db:
                        # Binary APIs identify a receipt, rather than a corp_code.
                        # Preserve the plan's explicit issuer association for queries.
                        if row["endpoint"] in {"document.xml", "fnlttXbrl.xml"}:
                            db.execute("UPDATE cache SET corp=? WHERE key=? AND corp=''", (row["corp"], result["source_key"]))
                except DartError as exc:
                    failure = exc.code
                    if failure in STOP:
                        status, error, state = "paused", failure, "pending"
                    elif failure in {"013", "014"}:
                        state = "unavailable"
                except ValueError:
                    status, error, state = "paused", "request_busy_or_invalid", "pending"
                except Exception:
                    failure = "collection_error"
                at = self.f.clock().isoformat()
                with self.f.db() as db:
                    db.execute("UPDATE dart_job_requests SET state=?,result=?,error=?,updated=? WHERE job=? AND ordinal=?",
                               (state, dumps(result) if result is not None else None, failure, at, job_id, row["ordinal"]))
                    db.execute("UPDATE dart_jobs SET updated=? WHERE id=?", (at, job_id))
                if status == "paused":
                    break
            if self.f.stop.is_set():
                status, error = "interrupted", "stopping"
            with self.f.db() as db:
                failed = db.execute("SELECT COUNT(*) FROM dart_job_requests WHERE job=? AND state='failed'", (job_id,)).fetchone()[0]
            if status == "complete" and failed:
                status = "incomplete"
        except Exception:
            status, error = "interrupted", "job_error"
        finally:
            with self.f.db() as db:
                db.execute("UPDATE dart_jobs SET status=?,error=?,updated=? WHERE id=?",
                           (status, error, self.f.clock().isoformat(), job_id))
            lock.close()

    def report(self, job_id, offset=0, limit=20, view="requests", symbol=None):
        self.f._page(offset, limit)
        row, plan = self._plan(job_id)
        if view not in {"requests", "reviews"}:
            raise ValueError("view must be requests or reviews")
        if symbol is not None and symbol not in {c["symbol"] for c in plan["companies"]}:
            raise ValueError("symbol outside job")
        with self.f.db() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM dart_job_requests WHERE job=? GROUP BY state", (job_id,)))
            reviews = dict(db.execute("SELECT status,COUNT(*) FROM dart_reviews WHERE job=? GROUP BY status", (job_id,)))
            table = "dart_job_requests" if view == "requests" else "dart_reviews"
            where, args = "job=?", [job_id]
            if symbol is not None:
                where += " AND symbol=?"; args.append(symbol)
            order = "ordinal" if view == "requests" else "symbol,item"
            values = db.execute("SELECT * FROM " + table + " WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                                (*args, limit + 1, offset)).fetchall()
        rows = [dict(r) for r in values[:limit]]
        for item in rows:
            for field in ("params", "result", "body"):
                if item.get(field) is not None:
                    item[field] = json.loads(item[field])
            if view == "requests":
                item["source_key"] = self.f.archive.source_key(item["endpoint"], item["params"])
        running = bool(self.active_id == job_id and self.thread and self.thread.is_alive())
        if row["status"] == "running" and not running:
            # A separate CLI reader has no reference to the server's thread.
            # Inspect the process-wide lock before declaring an interruption.
            with self.f.path.with_suffix(".jobs.lock").open("a+") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running = True
        status = "interrupted" if row["status"] == "running" and not running else row["status"]
        return {"job_id": job_id, "status": status, "last_error": row["error"], "updated": row["updated"],
                "counts": counts, "total": len(plan["requests"]), "review_counts": reviews,
                "expected_review_items": len(plan.get("catalog", [])) * len(plan["companies"]),
                "plan_sha256": row["plan_sha256"], "view": view, "rows": rows,
                "next_offset": offset + limit if len(values) > limit else None,
                "collection_complete": sum(counts.get(s, 0) for s in ("pending", "running", "failed")) == 0,
                "investment_review_complete": False, "read_only": True}

    def record_reviews(self, job_id, symbol, items):
        _, plan = self._plan(job_id)
        companies = {c["symbol"]: c for c in plan["companies"]}
        catalog = {x["id"]: x for x in plan.get("catalog", [])}
        if symbol not in companies or not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("invalid review company or item batch")
        if any(not isinstance(item, dict) for item in items):
            raise ValueError("review items must be objects")
        for item in items:
            dumps(item)  # Reject NaN/unsupported values before writing any file.
        if len({x.get("item_id") for x in items}) != len(items):
            raise ValueError("duplicate review item in batch")
        at, checksums = self.f.clock().isoformat(), []
        with self.f.db() as db:
            for item in items:
                item_id, state, reason = item.get("item_id"), item.get("status"), item.get("reason", "")
                if item_id not in catalog or state not in STATES or not isinstance(reason, str):
                    raise ValueError("review item is outside the catalog or has invalid status")
                refs = item.get("source_keys", [])
                if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
                    raise ValueError("invalid review source keys")
                if state == "found" and (not refs or not item.get("facts")):
                    raise ValueError("found requires actual facts and stored evidence")
                if state != "found" and not reason.strip():
                    raise ValueError("an explicit reason is required for every unresolved/inapplicable item")
                for source in refs:
                    exists = db.execute("SELECT 1 FROM archive_sources WHERE key=? UNION ALL SELECT 1 FROM archive_heads WHERE source_key=? UNION ALL SELECT 1 FROM archive_requests WHERE source_key=? LIMIT 1", (source, source, source)).fetchone()
                    if exists is None:
                        raise ValueError("review refers to evidence not stored in Termux")
            for item in items:
                item_id, state, reason = item["item_id"], item["status"], item.get("reason", "")
                body = {"job_id": job_id, "symbol": symbol, "corp_code": companies[symbol]["corp_code"],
                        "catalog_item": catalog[item_id], "assessment": item, "provenance": "assistant_review",
                        "provider_fact": False}
                encoded = dumps(body)
                checksum = self.f.archive.store(db, encoded.encode(), "assistant_review")
                old = db.execute("SELECT sha256 FROM dart_reviews WHERE job=? AND symbol=? AND item=?", (job_id, symbol, item_id)).fetchone()
                if old is None or old[0] != checksum:
                    db.execute("INSERT INTO dart_review_history(job,symbol,item,sha256,updated) VALUES (?,?,?,?,?)", (job_id, symbol, item_id, checksum, at))
                db.execute("INSERT OR REPLACE INTO dart_reviews VALUES (?,?,?,?,?,?,?,?)",
                           (job_id, symbol, item_id, state, reason, encoded, checksum, at))
                checksums.append({"item_id": item_id, "sha256": checksum})
        return {"job_id": job_id, "symbol": symbol, "stored": len(items), "rows": checksums,
                "provenance": "assistant_review", "network_requests": 0}

    def close(self):
        if self.thread:
            self.thread.join(35)
