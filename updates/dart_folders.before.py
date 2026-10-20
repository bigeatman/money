"""Organize cached provider responses by issuer without downloading or unpacking."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .dart_data import dumps


class DartFolders:
    def __init__(self, fundamentals):
        self.f = fundamentals
        self.root = self.f.archive.root / "companies"

    def _records(self, corp):
        """Associate binary/error attempts through the explicit saved job plan."""
        with self.f.db() as db:
            keys = {r[0] for r in db.execute("SELECT key FROM cache WHERE corp=?", (corp,))}
            for r in db.execute("SELECT endpoint,params FROM dart_job_requests WHERE corp=?", (corp,)):
                keys.add(self.f.archive.source_key(r["endpoint"], json.loads(r["params"])))
            found = {r["id"]: dict(r) for r in db.execute(
                "SELECT * FROM archive_requests WHERE corp=? ORDER BY id", (corp,))}
            keys = sorted(keys)
            for start in range(0, len(keys), 400):
                batch = keys[start:start + 400]
                sql = "SELECT * FROM archive_requests WHERE source_key IN (" + ",".join("?" for _ in batch) + ") ORDER BY id"
                found.update((r["id"], dict(r)) for r in db.execute(sql, batch))
            needed = sorted({x["sha256"] for x in found.values() if x["sha256"]})
            files = {}
            for start in range(0, len(needed), 400):
                batch = needed[start:start + 400]
                sql = "SELECT * FROM archive_files WHERE sha256 IN (" + ",".join("?" for _ in batch) + ")"
                files.update((r["sha256"], dict(r)) for r in db.execute(sql, batch))
            represented = {x["source_key"] for x in found.values() if x["sha256"]}
            legacy = [dict(r) for r in db.execute("""SELECT s.key AS source_key,f.* FROM archive_sources s
                JOIN cache c ON c.key=s.key JOIN archive_files f ON f.sha256=s.sha256
                WHERE c.corp=? ORDER BY s.key""", (corp,)) if r["source_key"] not in represented]
        return sorted(found.values(), key=lambda x: x["id"]), files, legacy

    def _source(self, entry):
        relative = Path(entry["path"])
        source = self.f.archive.root / relative
        # Paths in the index are not permissions to expose other local files.
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or relative.parts[0] != "objects" or source.is_symlink()
                or not source.resolve().is_relative_to((self.f.archive.root / "objects").resolve())):
            raise ValueError("invalid archived response path")
        return source

    @staticmethod
    def _atomic(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("refusing a symlink in stock folder")
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".dart-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _copy(self, entry, destination, folder):
        if not destination.parent.resolve().is_relative_to(folder.resolve()):
            raise ValueError("invalid stock response destination")
        source = self._source(entry)
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValueError("invalid archived response checksum")
        # Copy bytes, never reserialize JSON or rebuild a ZIP from its members.
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.is_symlink():
            raise ValueError("refusing a symlink in stock folder")
        if destination.is_file() and destination.stat().st_size == entry["size"]:
            existing = hashlib.sha256()
            with destination.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    existing.update(chunk)
            if existing.hexdigest() == entry["sha256"]:
                return
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".dart-")
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as stream:
                while True:
                    body = stream.read(1024 * 1024)
                    if not body:
                        break
                    target.write(body)
                    digest.update(body)
                    size += len(body)
                target.flush()
                os.fsync(target.fileno())
            if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise ValueError("archived response checksum mismatch; stock folder not published")
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def report(self, corp, endpoint=None, offset=0, limit=20):
        if not isinstance(corp, str) or not re.fullmatch(r"[0-9]{8}", corp):
            raise ValueError("corp_code must have eight digits")
        if endpoint is not None and (not isinstance(endpoint, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\.(json|xml)", endpoint)):
            raise ValueError("invalid endpoint filter")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("refusing a symlink stock root")
        folder = self.root / corp
        if folder.is_symlink():
            raise ValueError("refusing a symlink stock folder")
        folder.mkdir(exist_ok=True, mode=0o700)
        lock_path = self.root / (corp + ".lock")
        if lock_path.is_symlink():
            raise ValueError("refusing a symlink stock folder lock")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("stock folder is being organized; retry after it finishes") from None
            requests, objects, legacy = self._records(corp)
            rows, bodyless = [], []
            for request in requests:
                row = {k: v for k, v in request.items() if k not in {"params"}}
                row["parameters"] = json.loads(request["params"])
                if request["sha256"] is None:
                    row["response_file"] = None
                    bodyless.append(row)
                    continue
                entry = objects.get(request["sha256"])
                if entry is None:
                    raise ValueError("archived response file record missing")
                relative = Path("responses") / request["endpoint"] / ("request_%08d_" % request["id"] + request["sha256"][:12] + "." + entry["media"])
                self._copy(entry, folder / relative, folder)
                row.update(response_file=str(relative), size=entry["size"], provenance=entry["provenance"])
                rows.append(row)
            for entry in legacy:
                label = hashlib.sha256(entry["source_key"].encode()).hexdigest()[:20]
                relative = Path("legacy") / (label + "." + entry["media"])
                self._copy(entry, folder / relative, folder)
                rows.append({"source_key": entry["source_key"], "response_file": str(relative),
                    "sha256": entry["sha256"], "size": entry["size"], "provenance": entry["provenance"],
                    "endpoint": entry["source_key"].split(":")[1] if entry["source_key"].startswith("api:") else None,
                    "legacy": True})
            manifest = {"corp_code": corp, "raw_response_files": len(rows),
                "attempts_without_body": bodyless, "files": rows,
                "network_requests": 0, "source": "existing_local_archive",
                "legacy_note": "Legacy JSON, when present, is reconstructed and is not historical HTTP bytes."}
            self._atomic(folder / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            paths = sorted([r["response_file"] for r in rows] + ["MANIFEST.json", "TREE.json"])
            self._atomic(folder / "TREE.json", json.dumps({"root": str(folder), "paths": paths}, ensure_ascii=False, indent=2).encode())
            # Existing evidence/fields tools can read this index even when the
            # server still has older code loaded. This is a local index, not a
            # provider response; corp='' prevents recursive folder inclusion.
            tree_key = "stock-files:" + corp
            inventory = {"corp_code": corp, "folder_root": str(folder), "paths": paths,
                         "files": rows, "attempts_without_body": bodyless,
                         "provenance": "local_file_inventory", "provider_fact": False}
            with self.f.db() as db:
                self.f.archive.store(db, dumps(inventory).encode(), "local_file_inventory")
                self.f._save(db, tree_key, "", inventory)
        selected = [r for r in rows if endpoint is None or r.get("endpoint") == endpoint]
        page = selected[offset:offset + limit]
        return {"read_only": False, "local_only": True, "network_requests": 0,
            "view": "files", "corp_code": corp, "folder_root": str(folder),
            "manifest_file": str(folder / "MANIFEST.json"), "full_tree_file": str(folder / "TREE.json"),
            "tree_source_key": tree_key,
            "total_response_files": len(rows), "filtered_response_files": len(selected),
            "attempts_without_body": len(bodyless), "rows": page,
            "tree_page": [r["response_file"] for r in page],
            "next_offset": offset + limit if offset + limit < len(selected) else None,
            "untrusted_source_data": True}
