#!/usr/bin/env python3
"""Erase active research storage offline, retaining issuer and market inventories.

Stop the money executor first. This script does not modify credentials, config,
the trading ledger, or tunnel settings. No provider requests are made.
"""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo


TABLES = {
    "meta", "issuers", "filings", "tasks", "versions", "cache", "changes", "usage",
    "archive_files", "archive_content", "archive_fields", "archive_members",
    "archive_requests", "archive_heads", "archive_sources", "archive_versions",
    "archive_data_hashes", "archive_parse_notes", "dart_jobs", "dart_job_requests",
    "dart_reviews", "dart_review_history",
    "structured_jobs", "structured_requests", "structured_rows", "structured_calculations",
}
IDENTITY = {"key", "symbol", "exchange", "name", "asset_type", "segment"}


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def inventory_only(directory, at):
    latest = {}
    if not directory.exists():
        return latest
    seen = set()
    for path in directory.iterdir():
        if not path.is_file() or path.is_symlink() or not re.fullmatch(
                r"(?:KR|US)-[A-Za-z0-9_-]+\.json", path.name):
            raise ValueError("Unexpected scan file; not deleting: " + str(path))
        if path.name in {"KR-index.json", "US-index.json"}:
            continue
        job = json.loads(path.read_text(encoding="utf-8"))
        market = job.get("market")
        if market not in {"KR", "US"}:
            raise ValueError("Unrecognized market inventory: " + str(path))
        seen.add(market)
        if job.get("inventory_complete") and job.get("rows"):
            if market not in latest or job["started_at"] > latest[market]["started_at"]:
                latest[market] = job
    if seen - latest.keys():
        raise ValueError("A complete saved market inventory is required before erasing scans")
    for market, job in latest.items():
        rows = []
        for original in job["rows"]:
            if not {"key", "symbol", "exchange"} <= original.keys():
                raise ValueError("Incomplete stock identity; not deleting scans")
            row = {key: original[key] for key in IDENTITY if key in original}
            row.update(observation=None, signals=[], event_review="not_checked",
                       policy_reason=None, status="data_missing", reason="research reset")
            rows.append(row)
        local = at.astimezone(ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York"))
        slot = local if market == "KR" else local - timedelta(minutes=30)
        latest[market] = {
            "scan_id": market + "-reset-" + at.strftime("%Y%m%dT%H%M%S"),
            "market": market, "status": "incomplete", "slot": slot.strftime("%Y%m%d-%H"),
            "trade_date": local.strftime("%Y%m%d"), "started_at": at.isoformat(),
            "finished_at": at.isoformat(), "inventory_as_of": job.get("finished_at"),
            "inventory_complete": True, "data_complete": False, "rows": rows,
            "counts": {"N": len(rows), "E": 0, "U": len(rows), "V": 0, "D": len(rows)},
            "errors": [], "sources": [], "progress": {}, "data_valid_until": None,
            "candidate_review_complete": False, "selection_status": "inventory only; research reset",
            "research_reset_at": at.isoformat(),
        }
    return latest


def reset(root):
    root = Path(root).resolve()
    runtime = root / "runtime"
    database = runtime / "fundamentals.sqlite"
    archive = runtime / "fundamentals.files"
    scans = runtime / "market-scans"
    for path in (runtime, database, archive, scans):
        if path.is_symlink():
            raise ValueError("Refusing a symlink: " + str(path))
    if not database.is_file():
        raise ValueError("Existing fundamentals.sqlite is required; nothing was erased")
    at = datetime.now(timezone.utc)
    with ExitStack() as locks:
        for name in ("executor.lock", "fundamentals.sqlite.lock", "fundamentals.jobs.lock"):
            lock = locks.enter_context((runtime / name).open("a+"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Stop the money server and DART jobs first; nothing was erased") from None
        inventories = inventory_only(scans, at)
        with closing(sqlite3.connect(database)) as old:
            old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            schema = old.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name").fetchall()
            names = {name for kind, name, _ in schema if kind == "table"}
            if not {"issuers", "cache", "archive_files", "meta", "usage"} <= names or names - TABLES:
                raise ValueError("Unrecognized research schema; nothing was erased")
            kept = {name: old.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall()
                    for name in ("issuers", "usage")}
            master = old.execute("SELECT * FROM meta WHERE key='master_at'").fetchall()
            version = old.execute("PRAGMA user_version").fetchone()[0]
        with tempfile.TemporaryDirectory(prefix=".research-reset-", dir=runtime) as staging:
            stage = Path(staging)
            fresh = stage / "fundamentals.sqlite"
            with closing(sqlite3.connect(fresh)) as new, new:
                for _, _, sql in schema:
                    new.execute(sql)
                for name, rows in kept.items():
                    if rows:
                        new.executemany('INSERT INTO "' + name + '" VALUES (' +
                                        ','.join('?' for _ in rows[0]) + ')', rows)
                new.executemany("INSERT INTO meta VALUES (?,?)", master)
                new.execute("INSERT INTO meta VALUES ('run',?)", (json.dumps(
                    {"status": "not_started", "reason": "research reset", "at": at.isoformat()}),))
                new.execute("PRAGMA user_version=" + str(int(version)))
                for name, rows in kept.items():
                    actual = new.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall()
                    if fingerprint(actual) != fingerprint(rows):
                        raise ValueError("Inventory preservation failed; nothing was erased")
                for name in names - {"issuers", "usage", "meta"}:
                    if new.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]:
                        raise ValueError("Fresh research table is not empty")
            fresh.chmod(0o600)
            new_scans = stage / "market-scans"
            new_scans.mkdir(mode=0o700)
            for market, job in inventories.items():
                write_json(new_scans / (job["scan_id"] + ".json"), job)
                write_json(new_scans / (market + "-index.json"),
                           {"latest": job["scan_id"], "completed": None})
            # All retained records are validated before the first destructive step.
            for suffix in ("-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            os.replace(fresh, database)
            if scans.exists():
                shutil.rmtree(scans)
            os.replace(new_scans, scans)
            if archive.exists():
                shutil.rmtree(archive)
            archive.mkdir(mode=0o700)
        with closing(sqlite3.connect(database)) as check:
            for name, rows in kept.items():
                if fingerprint(check.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall()) != fingerprint(rows):
                    raise ValueError("Post-reset inventory verification failed")
            for name in names - {"issuers", "usage", "meta"}:
                if check.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]:
                    raise ValueError("Research rows remain after reset")
        if any(archive.iterdir()):
            raise ValueError("Research archive is not empty after reset")
        return {"research_reset": True, "issuer_rows_preserved": len(kept["issuers"]),
                "market_inventory_rows_preserved": {m: len(j["rows"]) for m, j in inventories.items()},
                "cached_sources": 0, "pending_sources": 0, "archive_files": 0,
                "collection_jobs": 0, "review_records": 0, "network_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    try:
        print(json.dumps(reset(args.root), ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from None
