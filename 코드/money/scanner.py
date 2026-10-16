"""Persistent, asynchronous full-market collection, independent of order execution.

This module discovers and supplies evidence. It never submits a plan or order,
does not assign a profit probability, and does not choose a fixed top-N universe.
"""
import copy
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from .common import now, number, timestamp
from .marketdata import MarketData, SPEC_URL

VERSION = "whole-market-v1"
DEFAULTS = {"batch_size": 50, "max_pages": 10000, "max_collection_seconds": 1800,
            "max_quote_age_seconds": 1800, "activity_acceleration_ratio": "1.5",
            "retained_scans_per_market": 3}
TERMINAL = {"complete", "incomplete", "failed", "interrupted"}


class ScanStopped(Exception):
    pass


class MarketScanner:
    def __init__(self, broker, directory, config=None, clock=now):
        self.broker, self.directory, self.clock = broker, Path(directory), clock
        self.config = {**DEFAULTS, **(config or {})}
        for key in ("batch_size", "max_pages", "max_collection_seconds", "max_quote_age_seconds",
                    "retained_scans_per_market"):
            value = self.config[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("invalid market_scan option: " + key)
        if self.config["batch_size"] > 100 or self.config["max_collection_seconds"] > 3600:
            raise ValueError("batch_size <= 100 and max_collection_seconds <= 3600 are required")
        if number(self.config["activity_acceleration_ratio"], signed=True) <= 1:
            raise ValueError("activity acceleration ratio must exceed 1")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.jobs, self.latest, self.completed, self.threads = {}, {}, {}, {}
        for market in ("KR", "US"):
            pointer = self.directory / (market + "-index.json")
            if not pointer.exists():
                continue
            try:
                index = json.loads(pointer.read_text())
                for field, target in (("latest", self.latest), ("completed", self.completed)):
                    sid = index.get(field)
                    if not sid:
                        continue
                    self._valid_id(sid)
                    job = json.loads((self.directory / (sid + ".json")).read_text())
                    if job["market"] != market or job["scan_id"] != sid:
                        raise ValueError("scan index mismatch")
                    if job["status"] not in TERMINAL:
                        job.update(status="interrupted", data_complete=False,
                                   failure="executor restarted during collection")
                    self.jobs[sid], target[market] = job, sid
            except (ValueError, KeyError, TypeError, OSError):
                # An unreadable cache cannot become evidence permitting a buy.
                continue

    @staticmethod
    def _valid_id(sid):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid):
            raise ValueError("invalid scan_id")

    @staticmethod
    def _zone(market):
        if market not in {"KR", "US"}:
            raise ValueError("market must be KR or US")
        return ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")

    def _slot(self, market):
        local = self.clock().astimezone(self._zone(market))
        if market == "US":
            local -= timedelta(minutes=30)
        return local.strftime("%Y%m%d-%H")

    def capabilities(self):
        return {"version": VERSION, "supported": True, "data_only": True,
                "markets": ["KR", "US"], "universe": "full Kiwoom market inventory before any quote filtering",
                "max_collection_seconds": self.config["max_collection_seconds"],
                "max_quote_age_seconds": self.config["max_quote_age_seconds"],
                "rules": {"price_signal": "last > session open and volume > 0",
                          "activity_signal": "current share-volume rate / preceding interval rate >= configured ratio",
                          "activity_acceleration_ratio": self.config["activity_acceleration_ratio"],
                          "events": "unverified; absence of a price/activity signal is waiting, not exclusion",
                          "top_n_limit": None}, "specification": SPEC_URL}

    def start(self, market, force=False):
        self._zone(market)
        if not isinstance(force, bool):
            raise ValueError("force must be boolean")
        with self.lock:
            current = self.jobs.get(self.latest.get(market))
            if current and (current["status"] == "running" or not force and current["slot"] == self._slot(market)):
                return self._summary(current)
            if self.stop.is_set():
                raise ValueError("scanner is stopping")
            at = self.clock()
            sid = market + "-" + at.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
            job = {"scan_id": sid, "market": market, "status": "running", "slot": self._slot(market),
                   "trade_date": at.astimezone(self._zone(market)).strftime("%Y%m%d"),
                   "started_at": at.isoformat(), "finished_at": None, "inventory_complete": False,
                   "data_complete": False, "counts": {k: None for k in ("N", "E", "U", "V", "D")},
                   "progress": {}, "rows": [], "errors": [], "sources": [],
                   "candidate_review_complete": False, "selection_status": "data collection only; AI review still required"}
            self.jobs[sid], self.latest[market] = job, sid
            try:
                self._persist(job)
            except (OSError, ValueError) as exc:
                job.update(status="failed", failure="cannot persist scan: " + str(exc))
                raise
            thread = threading.Thread(target=self._collect, args=(sid,), daemon=True, name="market-scan-" + market)
            self.threads[sid] = thread
            thread.start()
            return self._summary(job)

    def heartbeat(self):
        # Nominal regular hours trigger read-only collection. This is NOT a
        # verified exchange calendar and never authorizes an order.
        for market in ("KR", "US"):
            local = self.clock().astimezone(self._zone(market))
            minute = local.hour * 60 + local.minute
            opening, closing = (540, 930) if market == "KR" else (570, 960)
            if local.weekday() < 5 and opening <= minute < closing:
                try:
                    self.start(market)
                except (OSError, ValueError):
                    # A read-only scan must never stop the execution/exit loop.
                    continue

    def _write(self, path, value):
        stage = path.with_suffix(path.suffix + ".tmp")
        with stage.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(stage, path)

    def _persist(self, job):
        self._write(self.directory / (job["scan_id"] + ".json"), job)
        market = job["market"]
        self._write(self.directory / (market + "-index.json"),
                    {"latest": self.latest.get(market), "completed": self.completed.get(market)})

    def _collect(self, sid):
        with self.lock:
            job = self.jobs[sid]
            market = job["market"]
            previous = self.jobs.get(self.completed.get(market), {})
            previous_rows = {r["key"]: r for r in previous.get("rows", [])}
        deadline = time.monotonic() + self.config["max_collection_seconds"]
        holder, inventory, sources, errors = {}, {}, [], []
        inventory_complete = False

        def check():
            if self.stop.is_set():
                raise ScanStopped("scanner stopped")
            if time.monotonic() >= deadline:
                raise ScanStopped("collection time budget exceeded; remaining symbols are missing, not rejected")

        def progress(**values):
            observed = values.pop("observations", None)
            if observed is not None:
                holder["observations"] = observed
                values["quote_rows_received"] = len(observed)
            with self.lock:
                job["progress"].update(values)

        data = MarketData(self.broker, self.clock, check, progress,
                          self.config["batch_size"], self.config["max_pages"])
        try:
            inventory, sources, errors = data.inventory(market)
            inventory_complete = not errors
            if not inventory_complete:
                raise ScanStopped("inventory incomplete; no narrowed subset was scanned")
            eligible = {k: item for k, item in inventory.items() if data.policy(market, item) is None}
            with self.lock:
                job["inventory_complete"] = True
                job["progress"].update(inventory_rows=len(inventory), eligible_rows=len(eligible))
            fn = data.kr_observations if market == "KR" else data.us_observations
            observations, quote_errors = fn(eligible)
            holder["observations"] = observations
            errors.extend(quote_errors)
            # Rows from an invalid KR batch cannot count as verified input.
            invalid = {code for e in quote_errors if e.get("stage") == "quotes" for code in e.get("symbols", [])}
            for k, item in eligible.items():
                if item["symbol"] in invalid:
                    observations.pop(k, None)
        except Exception as exc:
            errors.append({"stage": "collection", "error": type(exc).__name__, "message": str(exc)})
        observations = holder.get("observations", {})
        ended = self.clock()
        rows = []
        for k, item in inventory.items():
            row = {"key": k, **item, "policy_reason": data.policy(market, item), "signals": [],
                   "event_review": "not_checked", "observation": observations.get(k)}
            if row["policy_reason"]:
                row.update(status="excluded", reason=row["policy_reason"])
            elif not row["observation"]:
                row.update(status="data_missing", reason="no valid common observation")
            else:
                try:
                    self._validate_observation(row["observation"], ended, job["trade_date"], market)
                    self._features(row, previous_rows.get(k))
                    row.update(status="candidate" if row["signals"] else "waiting",
                               reason="price/activity evidence for detailed review" if row["signals"] else
                               "no price/activity trigger; new event evidence remains unchecked")
                except (ValueError, KeyError, TypeError) as exc:
                    row.update(status="data_missing", reason=str(exc))
            rows.append(row)
        excluded = sum(r["status"] == "excluded" for r in rows)
        missing = sum(r["status"] == "data_missing" for r in rows)
        received = len(rows) - excluded - missing
        candidates = sum(r["status"] == "candidate" for r in rows)
        complete = inventory_complete and missing == 0 and not any(e["stage"] == "collection" for e in errors)
        with self.lock:
            times = [timestamp(o[f]) for o in observations.values() for f in ("received_at", "volume_received_at")
                     if f in o]
            job.update(status="complete" if complete else "incomplete", finished_at=ended.isoformat(),
                       inventory_complete=inventory_complete, data_complete=complete,
                       sources=sources, errors=errors, rows=rows,
                       counts={"N": len(rows), "E": excluded, "U": len(rows) - excluded,
                               "V": received, "D": missing} if inventory_complete else
                              {k: None for k in ("N", "E", "U", "V", "D")},
                       observed_counts={"inventory": len(rows), "excluded": excluded, "received": received,
                                        "missing": missing},
                       stages={"policy": {"input": len(rows), "excluded": excluded, "passed": len(rows) - excluded},
                               "common_data": {"input": len(rows) - excluded, "passed": received, "missing": missing},
                               "discovery": {"input": received, "candidates": candidates,
                                             "waiting": received - candidates, "rule_rejected": 0}},
                       count_identities_verified=inventory_complete and len(rows) == excluded + received + missing,
                       data_valid_until=(min(times, default=ended) + timedelta(
                           seconds=self.config["max_quote_age_seconds"])).isoformat(),
                       provider_timestamp_note="Reception times are not fabricated broker timestamps; inspect each observation.")
            if complete:
                self.completed[market] = sid
            try:
                self._persist(job)
            except (OSError, ValueError) as exc:
                job.update(status="failed", data_complete=False)
                job["errors"].append({"stage": "persistence", "message": str(exc)})
            keep = self.config["retained_scans_per_market"]
            old = sorted((j for j in self.jobs.values() if j["market"] == market and j["status"] in TERMINAL),
                         key=lambda j: j["started_at"], reverse=True)
            for prior in old[keep:]:
                pid = prior["scan_id"]
                if pid not in {self.latest.get(market), self.completed.get(market)}:
                    self.jobs.pop(pid, None)
                    self.threads.pop(pid, None)
                    (self.directory / (pid + ".json")).unlink(missing_ok=True)

    def _validate_observation(self, obs, ended, trade_date, market):
        for field in ("received_at", "volume_received_at"):
            if field in obs and not timedelta(0) <= ended - timestamp(obs[field]) <= timedelta(
                    seconds=self.config["max_quote_age_seconds"]):
                raise ValueError("stale or future observation: " + field)
        source = obs.get("provider_timestamp")
        if source and number(obs["volume"]) > 0:
            provider = timestamp(source)
            if provider.astimezone(self._zone(market)).strftime("%Y%m%d") != trade_date:
                raise ValueError("source timestamp is from another trading date")
            if provider > ended + timedelta(seconds=60):
                raise ValueError("source timestamp is in the future")

    def _features(self, row, previous):
        obs = row["observation"]
        price, opened, volume = (number(obs[k]) for k in ("last", "open", "volume"))
        features = {"return_from_open_percent": str((price / opened - 1) * 100) if opened else None,
                    "share_volume_per_second": None, "activity_acceleration": None,
                    "rvol": None, "rvol_note": "same-time historical denominator is unavailable"}
        if opened > 0 and price > opened and volume > 0 and not obs.get("trading_suspended"):
            row["signals"].append("price_above_session_open")
        if previous and previous.get("observation"):
            old = previous["observation"]
            at = timestamp(obs.get("volume_received_at", obs["received_at"]))
            before = timestamp(old.get("volume_received_at", old["received_at"]))
            zone = self._zone("KR" if row["exchange"] == "KRX" else "US")
            if at.astimezone(zone).date() == before.astimezone(zone).date():
                seconds = Decimal(str((at - before).total_seconds()))
                delta = volume - number(old["volume"])
                if seconds > 0 and delta >= 0:
                    rate = delta / seconds
                    features["share_volume_per_second"] = str(rate)
                    prior_rate = previous.get("features", {}).get("share_volume_per_second")
                    if prior_rate is not None and number(prior_rate) > 0:
                        acceleration = rate / number(prior_rate)
                        features["activity_acceleration"] = str(acceleration)
                        if acceleration >= number(self.config["activity_acceleration_ratio"]):
                            row["signals"].append("share_volume_acceleration")
        row["features"] = features

    def _summary(self, job):
        out = copy.deepcopy({k: v for k, v in job.items() if k not in {"rows", "errors"}})
        return out | {"errors": copy.deepcopy(job["errors"][:20]),
            "total_errors": len(job["errors"]), "errors_truncated": len(job["errors"]) > 20,
            "total_rows": len(job["rows"]), "page_size_limit": 500,
            "usable_for_new_buys": bool(job["data_complete"] and job.get("data_valid_until") and
                self.clock() <= timestamp(job["data_valid_until"]) and
                self.clock().astimezone(self._zone(job["market"])).strftime("%Y%m%d") == job["trade_date"]),
            "next_action": "poll get_market_scan; then page through all candidates and waiting/event-review rows"}

    def report(self, market, scan_id=None, view="all", offset=0, limit=100):
        self._zone(market)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        if view not in {"all", "candidate", "waiting", "data_missing", "excluded"}:
            raise ValueError("invalid scan view")
        with self.lock:
            sid = scan_id or self.latest.get(market)
            if not sid:
                return {"market": market, "status": "not_started", "data_complete": False}
            self._valid_id(sid)
            job = self.jobs.get(sid)
            if not job or job["market"] != market:
                raise ValueError("unknown scan for market")
            rows = [r for r in job["rows"] if view == "all" or r["status"] == view]
            part = copy.deepcopy(rows[offset:offset + limit])
            return self._summary(job) | {"view": view, "offset": offset, "limit": limit,
                    "view_total": len(rows), "rows": part,
                    "next_offset": offset + len(part) if offset + len(part) < len(rows) else None}

    def require_complete(self, market, code, exchange):
        with self.lock:
            job = self.jobs.get(self.latest.get(market))
            if not job or not job["data_complete"] or job["status"] != "complete":
                raise ValueError("full-market scan is incomplete; no new buy")
            current = self.clock()
            if current.astimezone(self._zone(market)).strftime("%Y%m%d") != job["trade_date"]:
                raise ValueError("full-market scan is from another trading date")
            target = None
            for row in job["rows"]:
                if row["status"] != "excluded" and row["observation"]:
                    self._validate_observation(row["observation"], current, job["trade_date"], market)
                if row["symbol"] == code and row["exchange"] == exchange:
                    target = row
            if not target or target["status"] not in {"candidate", "waiting"}:
                raise ValueError("buy instrument has no verified eligible full-market input")
            if target["observation"].get("trading_suspended"):
                raise ValueError("buy instrument reports trading suspension")
            if number(target["observation"]["last"]) <= 0 or target["observation"].get("no_session_trades"):
                raise ValueError("buy instrument has no current session price/trade evidence")
            return job["scan_id"]

    def close(self):
        self.stop.set()
        # Network calls have their own finite timeout. Do not wait for a whole
        # catalog or block saving the order ledger during process shutdown.


class UnavailableScanner:
    """Fail closed for buys while allowing existing position/exit management."""
    def __init__(self, error):
        self.error = str(error)

    def report(self, market, *args, **kwargs):
        return {"market": market, "status": "unavailable", "data_complete": False,
                "usable_for_new_buys": False, "error": self.error}

    start = report

    def capabilities(self):
        return {"version": VERSION, "supported": False, "error": self.error}

    def require_complete(self, *args):
        raise ValueError("full-market scan unavailable; no new buy: " + self.error)

    def heartbeat(self):
        pass

    def close(self):
        pass
