#!/usr/bin/env python3
"""Keep one explicit DART collection in one issuer folder. No network calls."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

CORP = "01639508"
ORIGINAL_SHA = "51f8115f11f3ec7dfea6077163a7f7dcc93bb663b4bbd0602b2d55846f96ae76"
UPDATED_SHA = "1fd86be0f264afd0aa1c6282fc8b5d01adc29e88f591fa35daa4713ec7b994d1"
ENTRIES = [{"title":"공시목록","endpoint":"list.json","parameters":{"bgn_de":"20231019","corp_code":"01639508","end_de":"20261019","last_reprt_at":"N","page_count":100,"page_no":1,"sort":"date","sort_mth":"desc"},"source_key":"api:list.json:60264301031bc844fa8dde8301813fc1be9e4b2bea1a4d1056115135d9449662","request_id":4838,"sha256":"e0b155dfe4cb009d62cb04e7c04ca27fa4f97dd0317835153b08f204b7dc960c","size":9735,"media":"json","object_path":"objects/e0/e0b155dfe4cb009d62cb04e7c04ca27fa4f97dd0317835153b08f204b7dc960c.json","checked":"2026-10-19T13:16:09.605950+00:00","provenance":"http_response"},{"title":"기업개황","endpoint":"company.json","parameters":{"corp_code":"01639508"},"source_key":"api:company.json:afd1e2e3e5e6cf39d268b8e2a2fec742ad2cbd27c20534f546ef98e99746d3f5","request_id":4839,"sha256":"437c11745f3af5dadc42a7786eed48527f30c5dea6fbfee6b835fbafa35a4606","size":495,"media":"json","object_path":"objects/43/437c11745f3af5dadc42a7786eed48527f30c5dea6fbfee6b835fbafa35a4606.json","checked":"2026-10-19T13:16:09.793679+00:00","provenance":"http_response"},{"title":"주식등의대량보유상황보고서(약식)","endpoint":"document.xml","parameters":{"rcept_no":"20261002000612"},"source_key":"api:document.xml:36890e605c3fb0494afc832737f4687c78584060273974e511cdf0943a6f6761","request_id":4840,"sha256":"6a27e79e0b57ca72fd26cf8c1193d60ad3cb964b362b26ce67088a602dbee9c0","size":11495,"media":"zip","object_path":"objects/6a/6a27e79e0b57ca72fd26cf8c1193d60ad3cb964b362b26ce67088a602dbee9c0.zip","checked":"2026-10-19T13:16:20.995565+00:00","provenance":"http_response"},{"title":"사업보고서 (2025.12)","endpoint":"document.xml","parameters":{"rcept_no":"20260318000699"},"source_key":"api:document.xml:840728247c7349cee693939f225033e7e27c724d255aebaa324b600a8cde093a","request_id":4841,"sha256":"904b4d2a03827efd273fcf3eda987b32835b3537f0374170743a7edabdd79964","size":195803,"media":"zip","object_path":"objects/90/904b4d2a03827efd273fcf3eda987b32835b3537f0374170743a7edabdd79964.zip","checked":"2026-10-19T13:16:21.353965+00:00","provenance":"http_response"},{"title":"반기보고서 (2026.06)","endpoint":"document.xml","parameters":{"rcept_no":"20260814000864"},"source_key":"api:document.xml:913ec8a1e83b1530302975790619376227f7380f2d3aea1f7c9554fc60f3d51a","request_id":4842,"sha256":"c8513edbb13b2385483a38ad37a23d5ffffdad314bc329101a8865765e869f10","size":132645,"media":"zip","object_path":"objects/c8/c8513edbb13b2385483a38ad37a23d5ffffdad314bc329101a8865765e869f10.zip","checked":"2026-10-19T13:16:21.629631+00:00","provenance":"http_response"},{"title":"주요사항보고서(유형자산양수결정)","endpoint":"document.xml","parameters":{"rcept_no":"20261015000168"},"source_key":"api:document.xml:b47c8d8edc7c9d3174cb6d4916ff8a4af48423fdce332965ac2174cc241bc454","request_id":4843,"sha256":"e891a1de6e73ed3179535da727be3bf0ac558c7eb38e20372e7da4e8baea7fc1","size":4509,"media":"zip","object_path":"objects/e8/e891a1de6e73ed3179535da727be3bf0ac558c7eb38e20372e7da4e8baea7fc1.zip","checked":"2026-10-19T13:16:21.893054+00:00","provenance":"http_response"}]
UPDATED_SOURCE = "\"\"\"Organize cached provider responses by issuer without downloading or unpacking.\"\"\"\nimport fcntl\nimport hashlib\nimport json\nimport os\nfrom pathlib import Path\nimport re\nimport tempfile\n\nfrom .dart_data import dumps\n\n\nclass DartFolders:\n    def __init__(self, fundamentals):\n        self.f = fundamentals\n        self.root = self.f.archive.root / \"companies\"\n\n    def _records(self, corp):\n        \"\"\"Associate binary/error attempts through the explicit saved job plan.\"\"\"\n        with self.f.db() as db:\n            keys = {r[0] for r in db.execute(\"SELECT key FROM cache WHERE corp=?\", (corp,))}\n            for r in db.execute(\"SELECT endpoint,params FROM dart_job_requests WHERE corp=?\", (corp,)):\n                keys.add(self.f.archive.source_key(r[\"endpoint\"], json.loads(r[\"params\"])))\n            found = {r[\"id\"]: dict(r) for r in db.execute(\n                \"SELECT * FROM archive_requests WHERE corp=? ORDER BY id\", (corp,))}\n            keys = sorted(keys)\n            for start in range(0, len(keys), 400):\n                batch = keys[start:start + 400]\n                sql = \"SELECT * FROM archive_requests WHERE source_key IN (\" + \",\".join(\"?\" for _ in batch) + \") ORDER BY id\"\n                found.update((r[\"id\"], dict(r)) for r in db.execute(sql, batch))\n            needed = sorted({x[\"sha256\"] for x in found.values() if x[\"sha256\"]})\n            files = {}\n            for start in range(0, len(needed), 400):\n                batch = needed[start:start + 400]\n                sql = \"SELECT * FROM archive_files WHERE sha256 IN (\" + \",\".join(\"?\" for _ in batch) + \")\"\n                files.update((r[\"sha256\"], dict(r)) for r in db.execute(sql, batch))\n            represented = {x[\"source_key\"] for x in found.values() if x[\"sha256\"]}\n            legacy = [dict(r) for r in db.execute(\"\"\"SELECT s.key AS source_key,f.* FROM archive_sources s\n                JOIN cache c ON c.key=s.key JOIN archive_files f ON f.sha256=s.sha256\n                WHERE c.corp=? ORDER BY s.key\"\"\", (corp,)) if r[\"source_key\"] not in represented]\n        return sorted(found.values(), key=lambda x: x[\"id\"]), files, legacy\n\n    def _source(self, entry):\n        relative = Path(entry[\"path\"])\n        source = self.f.archive.root / relative\n        # Paths in the index are not permissions to expose other local files.\n        if (relative.is_absolute() or \"..\" in relative.parts or not relative.parts\n                or relative.parts[0] != \"objects\" or source.is_symlink()\n                or not source.resolve().is_relative_to((self.f.archive.root / \"objects\").resolve())):\n            raise ValueError(\"invalid archived response path\")\n        return source\n\n    @staticmethod\n    def _atomic(path, raw):\n        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\n        if path.is_symlink():\n            raise ValueError(\"refusing a symlink in stock folder\")\n        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=\".dart-\")\n        try:\n            with os.fdopen(fd, \"wb\") as stream:\n                stream.write(raw)\n                stream.flush()\n                os.fsync(stream.fileno())\n            os.replace(temp, path)\n        finally:\n            if os.path.exists(temp):\n                os.unlink(temp)\n\n    def _copy(self, entry, destination, folder):\n        if not destination.parent.resolve().is_relative_to(folder.resolve()):\n            raise ValueError(\"invalid stock response destination\")\n        source = self._source(entry)\n        if not re.fullmatch(r\"[0-9a-f]{64}\", entry[\"sha256\"]):\n            raise ValueError(\"invalid archived response checksum\")\n        # Copy bytes, never reserialize JSON or rebuild a ZIP from its members.\n        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\n        if destination.is_symlink():\n            raise ValueError(\"refusing a symlink in stock folder\")\n        if destination.is_file() and destination.stat().st_size == entry[\"size\"]:\n            existing = hashlib.sha256()\n            with destination.open(\"rb\") as stream:\n                for chunk in iter(lambda: stream.read(1024 * 1024), b\"\"):\n                    existing.update(chunk)\n            if existing.hexdigest() == entry[\"sha256\"]:\n                return\n        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=\".dart-\")\n        digest, size = hashlib.sha256(), 0\n        try:\n            with os.fdopen(fd, \"wb\") as target, source.open(\"rb\") as stream:\n                while True:\n                    body = stream.read(1024 * 1024)\n                    if not body:\n                        break\n                    target.write(body)\n                    digest.update(body)\n                    size += len(body)\n                target.flush()\n                os.fsync(target.fileno())\n            if size != entry[\"size\"] or digest.hexdigest() != entry[\"sha256\"]:\n                raise ValueError(\"archived response checksum mismatch; stock folder not published\")\n            os.replace(temporary, destination)\n        finally:\n            if os.path.exists(temporary):\n                os.unlink(temporary)\n\n    def report(self, corp, endpoint=None, offset=0, limit=20):\n        if not isinstance(corp, str) or not re.fullmatch(r\"[0-9]{8}\", corp):\n            raise ValueError(\"corp_code must have eight digits\")\n        if endpoint is not None and (not isinstance(endpoint, str)\n                or not re.fullmatch(r\"[A-Za-z][A-Za-z0-9]*\\.(json|xml)\", endpoint)):\n            raise ValueError(\"invalid endpoint filter\")\n        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)\n        if self.root.is_symlink():\n            raise ValueError(\"refusing a symlink stock root\")\n        folder = self.root / corp\n        if folder.is_symlink():\n            raise ValueError(\"refusing a symlink stock folder\")\n        folder.mkdir(exist_ok=True, mode=0o700)\n        lock_path = self.root / (corp + \".lock\")\n        if lock_path.is_symlink():\n            raise ValueError(\"refusing a symlink stock folder lock\")\n        with lock_path.open(\"a+\") as lock:\n            try:\n                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n            except BlockingIOError:\n                raise ValueError(\"stock folder is being organized; retry after it finishes\") from None\n            requests, objects, legacy = self._records(corp)\n            selection = None\n            manifest_path = folder / \"MANIFEST.json\"\n            if manifest_path.is_file():\n                selection = json.loads(manifest_path.read_text(encoding=\"utf-8\")).get(\"selection\")\n            if selection is not None:\n                ids = selection.get(\"request_ids\")\n                if (selection.get(\"mode\") != \"exact_requests\"\n                        or selection.get(\"corp_code\") != corp\n                        or not isinstance(ids, list) or not ids\n                        or any(type(x) is not int or x < 1 for x in ids)\n                        or len(ids) != len(set(ids))):\n                    raise ValueError(\"invalid current-collection selection\")\n                requests = [r for r in requests if r[\"id\"] in set(ids)]\n                if {r[\"id\"] for r in requests} != set(ids):\n                    raise ValueError(\"current-collection request record missing\")\n                legacy = []\n            rows, bodyless = [], []\n            for request in requests:\n                row = {k: v for k, v in request.items() if k not in {\"params\"}}\n                row[\"parameters\"] = json.loads(request[\"params\"])\n                if request[\"sha256\"] is None:\n                    row[\"response_file\"] = None\n                    bodyless.append(row)\n                    continue\n                entry = objects.get(request[\"sha256\"])\n                if entry is None:\n                    raise ValueError(\"archived response file record missing\")\n                relative = Path(\"responses\") / request[\"endpoint\"] / (\"request_%08d_\" % request[\"id\"] + request[\"sha256\"][:12] + \".\" + entry[\"media\"])\n                self._copy(entry, folder / relative, folder)\n                row.update(response_file=str(relative), size=entry[\"size\"], provenance=entry[\"provenance\"])\n                rows.append(row)\n            for entry in legacy:\n                label = hashlib.sha256(entry[\"source_key\"].encode()).hexdigest()[:20]\n                relative = Path(\"legacy\") / (label + \".\" + entry[\"media\"])\n                self._copy(entry, folder / relative, folder)\n                rows.append({\"source_key\": entry[\"source_key\"], \"response_file\": str(relative),\n                    \"sha256\": entry[\"sha256\"], \"size\": entry[\"size\"], \"provenance\": entry[\"provenance\"],\n                    \"endpoint\": entry[\"source_key\"].split(\":\")[1] if entry[\"source_key\"].startswith(\"api:\") else None,\n                    \"legacy\": True})\n            manifest = {\"corp_code\": corp, \"raw_response_files\": len(rows),\n                \"attempts_without_body\": bodyless, \"files\": rows,\n                \"network_requests\": 0, \"source\": \"exact_requests\" if selection else \"existing_local_archive\",\n                \"selection\": selection,\n                \"legacy_note\": \"Legacy JSON, when present, is reconstructed and is not historical HTTP bytes.\"}\n            self._atomic(folder / \"MANIFEST.json\", json.dumps(manifest, ensure_ascii=False, indent=2).encode())\n            paths = sorted([r[\"response_file\"] for r in rows] + [\"MANIFEST.json\", \"TREE.json\"])\n            self._atomic(folder / \"TREE.json\", json.dumps({\"root\": str(folder), \"paths\": paths}, ensure_ascii=False, indent=2).encode())\n            # Existing evidence/fields tools can read this index even when the\n            # server still has older code loaded. This is a local index, not a\n            # provider response; corp='' prevents recursive folder inclusion.\n            tree_key = \"stock-files:\" + corp\n            inventory = {\"corp_code\": corp, \"folder_root\": str(folder), \"paths\": paths,\n                         \"files\": rows, \"attempts_without_body\": bodyless,\n                         \"provenance\": \"local_file_inventory\", \"provider_fact\": False}\n            with self.f.db() as db:\n                self.f.archive.store(db, dumps(inventory).encode(), \"local_file_inventory\")\n                self.f._save(db, tree_key, \"\", inventory)\n        selected = [r for r in rows if endpoint is None or r.get(\"endpoint\") == endpoint]\n        page = selected[offset:offset + limit]\n        return {\"read_only\": False, \"local_only\": True, \"network_requests\": 0,\n            \"view\": \"files\", \"corp_code\": corp, \"folder_root\": str(folder),\n            \"manifest_file\": str(folder / \"MANIFEST.json\"), \"full_tree_file\": str(folder / \"TREE.json\"),\n            \"tree_source_key\": tree_key,\n            \"total_response_files\": len(rows), \"filtered_response_files\": len(selected),\n            \"attempts_without_body\": len(bodyless), \"rows\": page,\n            \"tree_page\": [r[\"response_file\"] for r in page],\n            \"next_offset\": offset + limit if offset + limit < len(selected) else None,\n            \"untrusted_source_data\": True}\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".current-write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ensure_plain_tree(path):
    if path.is_symlink():
        raise ValueError("refusing symlink: " + str(path))
    if path.exists():
        for p in path.rglob("*"):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                raise ValueError("refusing link/special file: " + str(p))


def remove_staging(path, companies):
    path = Path(path)
    if (path.parent.resolve() != companies.resolve()
            or not path.name.startswith("." + CORP + "-")):
        raise ValueError("refusing deletion outside the exact issuer staging area")
    ensure_plain_tree(path)
    if path.exists():
        shutil.rmtree(path)


def prepare(project, entries):
    project = Path(project).resolve(strict=True)
    archive = project / "runtime" / "fundamentals.files"
    companies = archive / "companies"
    folder = companies / CORP
    for part in [project / "runtime", archive, archive / "objects", companies, folder]:
        if part.is_symlink() or not part.is_dir():
            raise ValueError("expected plain existing directory: " + str(part))
    ensure_plain_tree(folder)
    source_path = project / "money" / "dart_folders.py"
    if source_path.is_symlink():
        raise ValueError("refusing symlink module")
    previous_source = source_path.read_bytes()
    if sha(previous_source) not in {ORIGINAL_SHA, UPDATED_SHA}:
        raise ValueError("unknown dart_folders.py version; nothing deleted")
    updated = UPDATED_SOURCE.encode("utf-8")
    if sha(updated) != UPDATED_SHA:
        raise ValueError("updater source checksum mismatch")
    ast.parse(updated)
    ids = [e["request_id"] for e in entries]
    if len(ids) != 6 or len(set(ids)) != 6 or any(type(x) is not int for x in ids):
        raise ValueError("the exact six current requests are required")
    verified = []
    for e in entries:
        relative = Path(e["object_path"])
        source = archive / relative
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or relative.parts[0] != "objects" or source.is_symlink()
                or not source.resolve().is_relative_to((archive / "objects").resolve())):
            raise ValueError("invalid current response source")
        raw = source.read_bytes()
        if len(raw) != e["size"] or sha(raw) != e["sha256"]:
            raise ValueError("current response missing/corrupt; nothing deleted")
        if e["endpoint"] not in {"company.json", "list.json", "document.xml"}:
            raise ValueError("unexpected endpoint")
        if e["media"] not in {"json", "zip"}:
            raise ValueError("unexpected current response media")
        response = ("responses/" + e["endpoint"] + "/request_%08d_" % e["request_id"]
                    + e["sha256"][:12] + "." + e["media"])
        verified.append(({**e, "id": e["request_id"], "response_file": response}, raw))
    return project, companies, folder, source_path, previous_source, updated, verified


def apply_current(project, entries, publish, check=False):
    project, companies, folder, module, previous, updated, verified = prepare(project, entries)
    selection = {"mode": "exact_requests", "corp_code": CORP,
                 "request_ids": sorted(e["request_id"] for e in entries),
                 "collected_at": [min(e["checked"] for e in entries), max(e["checked"] for e in entries)]}
    rows = [e for e, raw in verified]
    paths = sorted(["MANIFEST.json", "TREE.json"] + [e["response_file"] for e in rows])
    manifest = {"corp_code": CORP, "selection": selection, "raw_response_files": 6,
                "files": rows, "attempts_without_body": [], "source": "exact_requests",
                "network_requests": 0}
    inventory = {"corp_code": CORP, "folder_root": str(folder), "paths": paths,
                 "files": rows, "attempts_without_body": [], "selection": selection,
                 "provenance": "local_file_inventory", "provider_fact": False}
    if check:
        return {"checked": True, "current_responses": 6, "paths": paths, "network_requests": 0}
    stage = Path(tempfile.mkdtemp(prefix="." + CORP + "-current-", dir=companies))
    old = companies / ("." + CORP + "-old-" + uuid.uuid4().hex)
    previous_count = sum(p.is_file() for p in folder.rglob("*"))
    swapped = False
    patched = False
    committed = False
    try:
        for entry, raw in verified:
            target = stage / entry["response_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if sha(target.read_bytes()) != entry["sha256"]:
                raise ValueError("staged response checksum mismatch")
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (stage / "TREE.json").write_text(json.dumps({"root": str(folder), "paths": paths}, ensure_ascii=False, indent=2), encoding="utf-8")
        if previous != updated:
            atomic(module, updated)
            patched = True
        folder.rename(old)
        try:
            stage.rename(folder)
        except BaseException:
            old.rename(folder)
            raise
        swapped = True
        publish(inventory)
        committed = True
        remove_staging(old, companies)
        actual = sorted(str(p.relative_to(folder)).replace(os.sep, "/")
                        for p in folder.rglob("*") if p.is_file())
        if actual != paths:
            raise ValueError("resulting file inventory mismatch")
        return {"folder": str(folder), "previous_files_removed": previous_count,
                "current_responses": 6, "management_files": 2, "total_files": len(paths),
                "paths": paths, "network_requests": 0, "tree_source_key": "stock-files:" + CORP}
    except BaseException:
        if not committed:
            if swapped:
                folder.rename(stage)
                old.rename(folder)
            if patched:
                atomic(module, previous)
        raise
    finally:
        if stage.exists():
            remove_staging(stage, companies)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(Path.home() / "money-work" / "money" / "코드"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    project = Path(args.project).expanduser().resolve(strict=True)
    if not (project / "runtime" / "fundamentals.sqlite").is_file():
        raise ValueError("existing DART database not found")
    prepare(project, ENTRIES)
    sys.path.insert(0, str(project))
    from money.fundamentals import Fundamentals
    f = Fundamentals(project / "runtime" / "fundamentals.sqlite")
    with f.db() as db:
        for entry in ENTRIES:
            row = db.execute("SELECT * FROM archive_requests WHERE id=?", (entry["request_id"],)).fetchone()
            if (row is None or row["sha256"] != entry["sha256"]
                    or row["endpoint"] != entry["endpoint"] or row["http_status"] != 200
                    or row["error"] is not None or json.loads(row["params"]) != entry["parameters"]
                    or row["checked"] != entry["checked"]):
                raise ValueError("current request ledger mismatch; nothing deleted")
    def publish(inventory):
        from money.dart_data import dumps
        with f.db() as db:
            f.archive.store(db, dumps(inventory).encode(), "local_file_inventory")
            f._save(db, "stock-files:" + CORP, "", inventory)
    import fcntl
    lock_path = project / "runtime" / "fundamentals.files" / "companies" / (CORP + ".lock")
    if lock_path.is_symlink():
        raise ValueError("refusing symlink lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = apply_current(project, ENTRIES, publish, args.check)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

