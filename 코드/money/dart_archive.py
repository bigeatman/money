"""Immutable OpenDART response files and a queryable SQLite index.

Filenames are content addresses, never provider paths. File replacement is
atomic and precedes database references. A request ledger includes failed and
unchanged attempts; a separate head advances only after transport validation.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile

from .dart_client import DartError, MAX_BYTES, unpack, xml_root
from .dart_data import Text, dumps


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def media_type(raw):
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    stripped = raw.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith((b"{", b"[")):
        return "json"
    if stripped.startswith(b"<"):
        return "xml"
    return "bin"


def content_hash(raw):
    """Hash every value/member, ignoring JSON spacing and ZIP packaging only.

Member names, bytes and duplicate entries all count. XML attributes, binary
attachments and whitespace inside members are deliberately not discarded.
"""
    media = media_type(raw)
    if media == "json":
        try:
            return sha256(dumps(json.loads(raw)).encode())
        except (ValueError, UnicodeError):
            pass
    elif media == "zip":
        entries = sorted((name, len(body), sha256(body)) for name, body in unpack(raw))
        return sha256(b"zip-members-v1\0" + dumps(entries).encode())
    return sha256(raw)


def leaves(value, path=""):
    """Lossless scalar index using JSON Pointer paths, including empty values."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from leaves(child, path + "/" + escaped)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from leaves(child, path + "/" + str(index))
    else:
        kind = ("null" if value is None else "boolean" if isinstance(value, bool)
                else "object" if isinstance(value, dict) else "array" if isinstance(value, list)
                else "string" if isinstance(value, str) else "number")
        yield path, kind, dumps(value)


def xml_value(node):
    # Keep attributes, namespaces, repeated children, text and tails.
    return {"tag": node.tag, "attributes": node.attrib, "text": node.text,
            "tail": node.tail, "children": [xml_value(child) for child in node]}


class DartArchive:
    def __init__(self, root, connect, clock):
        self.root, self.connect, self.clock = Path(root), connect, clock
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS archive_files (
                    sha256 TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL, media TEXT NOT NULL, created TEXT NOT NULL,
                    provenance TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_content (
                    sha256 TEXT PRIMARY KEY, structured_json TEXT, text TEXT NOT NULL,
                    index_error TEXT);
                CREATE TABLE IF NOT EXISTS archive_fields (
                    sha256 TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
                    value TEXT NOT NULL, PRIMARY KEY(sha256,path));
                CREATE TABLE IF NOT EXISTS archive_members (
                    sha256 TEXT NOT NULL, ordinal INTEGER NOT NULL, name TEXT NOT NULL,
                    member_sha256 TEXT NOT NULL, PRIMARY KEY(sha256,ordinal));
                CREATE TABLE IF NOT EXISTS archive_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL,
                    corp TEXT NOT NULL, endpoint TEXT NOT NULL, params TEXT NOT NULL,
                    checked TEXT NOT NULL, http_status INTEGER, error TEXT,
                    sha256 TEXT, content_sha256 TEXT, previous_content_sha256 TEXT,
                    changed INTEGER, complete INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS archive_request_source ON archive_requests(source_key,id);
                CREATE INDEX IF NOT EXISTS archive_request_corp ON archive_requests(corp,id);
                CREATE TABLE IF NOT EXISTS archive_heads (
                    source_key TEXT PRIMARY KEY, request_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_sources (
                    key TEXT PRIMARY KEY, sha256 TEXT NOT NULL, request_id INTEGER);
                CREATE TABLE IF NOT EXISTS archive_versions (
                    version_hash TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_data_hashes (
                    key TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL);
            """)

    @staticmethod
    def source_key(endpoint, params):
        return "api:" + endpoint + ":" + sha256(dumps(params).encode())

    def _write(self, reader):
        """Stream the whole HTTP body to disk, even when too big to parse."""
        stage_dir = self.root / "tmp"
        stage_dir.mkdir(exist_ok=True, mode=0o700)
        fd, stage = tempfile.mkstemp(dir=stage_dir)
        digest, size, prefix, complete = hashlib.sha256(), 0, b"", True
        try:
            with os.fdopen(fd, "wb") as stream:
                while True:
                    try:
                        chunk = reader.read(1024 * 1024)
                    except Exception as exc:
                        partial = getattr(exc, "partial", b"")
                        if isinstance(partial, bytes) and partial:
                            stream.write(partial)
                            digest.update(partial)
                            size += len(partial)
                            if not prefix:
                                prefix = partial[:128]
                        complete = False
                        break
                    if not chunk:
                        break
                    if not prefix:
                        prefix = chunk[:128]
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            checksum, media = digest.hexdigest(), media_type(prefix)
            relative = Path("objects") / checksum[:2] / (checksum + "." + media)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A valid new download also repairs a missing/corrupt same-hash file.
            os.replace(stage, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return checksum, str(relative), size, media, complete
        finally:
            Path(stage).unlink(missing_ok=True)

    def _register(self, db, info, provenance):
        checksum, relative, size, media, _ = info
        db.execute("INSERT OR IGNORE INTO archive_files VALUES (?,?,?,?,?,?)",
                   (checksum, relative, size, media, self.clock().isoformat(), provenance))
        return checksum

    def _index(self, db, checksum, raw):
        if db.execute("SELECT 1 FROM archive_content WHERE sha256=?", (checksum,)).fetchone():
            return
        media, structured, text, error = media_type(raw), None, "", None
        if media == "zip":
            try:
                parts = []
                for ordinal, (name, body) in enumerate(unpack(raw)):
                    child = self.store(db, body, "archive_member")
                    db.execute("INSERT INTO archive_members VALUES (?,?,?,?)", (checksum, ordinal, name, child))
                    child_text = db.execute("SELECT text FROM archive_content WHERE sha256=?", (child,)).fetchone()[0]
                    if child_text:
                        parts.append(name + "\n" + child_text)
                text = "\n\n".join(parts)
            except DartError as exc:
                error = exc.code
        elif media == "json":
            try:
                structured = json.loads(raw)
                text = dumps(structured)
            except (ValueError, UnicodeError):
                error = "invalid_json"
        else:
            # Binary attachments must not put NULs into the enclosing document
            # text (SQLite character lengths stop at NUL). Their bytes remain
            # available through the file index.
            for encoding in (() if media == "bin" and b"\0" in raw else ("utf-8-sig", "cp949")):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            if "\0" in text:
                text = text.replace("\0", "\\u0000")
                error = "nul_in_text"
            if media == "xml":
                try:
                    structured = xml_value(xml_root(raw))
                except (DartError, RecursionError) as exc:
                    # HTML, DTD-bearing documents and unparseable XML retain
                    # their complete decoded markup for search/evidence.
                    error = exc.code if isinstance(exc, DartError) else "xml_depth"
                parser = Text()
                try:
                    parser.feed(text)
                    readable = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
                except Exception:
                    readable = ""
                # Keep markup (including attributes) in the DB as well.
                text = text if not readable else readable + "\n\n" + text
        db.execute("INSERT INTO archive_content VALUES (?,?,?,?)",
                   (checksum, dumps(structured) if structured is not None else None, text, error))
        if structured is not None:
            try:
                db.executemany("INSERT INTO archive_fields VALUES (?,?,?,?)",
                               ((checksum, path, kind, value) for path, kind, value in leaves(structured)))
            except RecursionError:
                db.execute("UPDATE archive_content SET index_error='json_depth' WHERE sha256=?", (checksum,))

    def store(self, db, raw, provenance):
        checksum = sha256(raw)
        existing = db.execute("SELECT * FROM archive_files WHERE sha256=?", (checksum,)).fetchone()
        if not existing or not (self.root / existing["path"]).is_file():
            self._register(db, self._write(io.BytesIO(raw)), provenance)
        self._index(db, checksum, raw)
        return checksum

    def receive(self, endpoint, params, response, http_status):
        """Transport hook: file and ledger commit before response validation."""
        try:
            info = self._write(response)
            checksum, relative, size, _, complete = info
            expected = response.getheader("Content-Length") if hasattr(response, "getheader") else None
            if expected is not None and expected.isdigit() and int(expected) != size:
                complete = False
                info = (*info[:-1], False)
            raw = (self.root / relative).read_bytes() if size <= MAX_BYTES else None
            try:
                logical = content_hash(raw) if raw is not None and complete else None
            except (DartError, ValueError, RecursionError):
                logical = None
            with self.connect() as db:
                self._register(db, info, "http_response")
                cursor = db.execute("""INSERT INTO archive_requests
                    (source_key,corp,endpoint,params,checked,http_status,error,sha256,content_sha256,complete)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (self.source_key(endpoint, params), params.get("corp_code", ""),
                    endpoint, dumps(params), self.clock().isoformat(), http_status,
                    "validation_pending" if complete else "incomplete_response", checksum, logical, int(complete)))
                request_id = cursor.lastrowid
            # Indexing is separate: a parser failure must never roll back the
            # raw response/request record. Errors remain inspectable in SQLite.
            if raw is not None and complete:
                try:
                    with self.connect() as db:
                        self._index(db, checksum, raw)
                except Exception:
                    with self.connect() as db:
                        db.execute("INSERT OR REPLACE INTO archive_content VALUES (?,NULL,'','index_failed')", (checksum,))
            else:
                with self.connect() as db:
                    db.execute("INSERT OR IGNORE INTO archive_content VALUES (?,NULL,'',?)",
                               (checksum, "response_size" if complete else "incomplete_response"))
            if not complete:
                raise DartError("incomplete_response")
            return raw, request_id
        except OSError:
            raise DartError("archive_write_error") from None

    def finish(self, request_id, error=None):
        if request_id is None:
            return
        with self.connect() as db:
            row = db.execute("SELECT * FROM archive_requests WHERE id=?", (request_id,)).fetchone()
            previous = db.execute("""SELECT r.content_sha256 FROM archive_heads h
                JOIN archive_requests r ON r.id=h.request_id WHERE h.source_key=?""", (row["source_key"],)).fetchone()
            before = previous[0] if previous else None
            changed = int(before != row["content_sha256"]) if not error and row["content_sha256"] else None
            db.execute("UPDATE archive_requests SET error=?,previous_content_sha256=?,changed=? WHERE id=?",
                       (error, before, changed, request_id))
            if not error:
                db.execute("INSERT OR REPLACE INTO archive_heads VALUES (?,?)", (row["source_key"], request_id))

    def failure(self, endpoint, params, error):
        with self.connect() as db:
            db.execute("""INSERT INTO archive_requests
                (source_key,corp,endpoint,params,checked,error,complete) VALUES (?,?,?,?,?,?,0)""",
                (self.source_key(endpoint, params), params.get("corp_code", ""), endpoint,
                 dumps(params), self.clock().isoformat(), error))

    def attach(self, db, key, payload, version_hash, request_key=None):
        raw = payload if isinstance(payload, bytes) else dumps(payload).encode()
        head = db.execute("""SELECT r.* FROM archive_heads h JOIN archive_requests r
            ON r.id=h.request_id WHERE h.source_key=?""", (request_key or key,)).fetchone()
        if head and head["content_sha256"] == content_hash(raw):
            checksum, request_id = head["sha256"], head["id"]
        else:
            checksum, request_id = self.store(db, raw, "serialized_or_legacy_payload"), None
        db.execute("INSERT OR REPLACE INTO archive_sources VALUES (?,?,?)", (key, checksum, request_id))
        db.execute("INSERT OR IGNORE INTO archive_versions VALUES (?,?)", (version_hash, checksum))

    def metadata(self, db, key):
        row = db.execute("""SELECT f.*,s.request_id,r.content_sha256,r.changed,r.checked,
            c.index_error FROM archive_sources s JOIN archive_files f ON f.sha256=s.sha256
            LEFT JOIN archive_requests r ON r.id=s.request_id
            LEFT JOIN archive_content c ON c.sha256=f.sha256 WHERE s.key=?""", (key,)).fetchone()
        return dict(row) if row else None

    def text_page(self, db, key, offset, limit):
        return db.execute("""SELECT substr(c.text,?,?) AS text,length(c.text) AS total
            FROM archive_content c WHERE c.sha256=COALESCE(
                (SELECT sha256 FROM archive_sources WHERE key=?),
                (SELECT r.sha256 FROM archive_heads h JOIN archive_requests r ON r.id=h.request_id WHERE h.source_key=?))""",
            (offset + 1, limit, key, key)).fetchone()

    def query(self, view="sources", source_key=None, corp_code=None, endpoint=None,
              field=None, text=None, offset=0, limit=20):
        if view not in {"sources", "fields", "requests", "files"}:
            raise ValueError("view must be sources, fields, requests or files")
        for value in (source_key, corp_code, endpoint, field, text):
            if value is not None and (not isinstance(value, str) or len(value) > 500):
                raise ValueError("invalid archive filter")
        if field is not None and view != "fields":
            raise ValueError("field is only supported by the fields view")
        if view == "fields" and (corp_code is not None or endpoint is not None):
            raise ValueError("fields uses source_key; filter corp_code/endpoint in sources first")
        if view == "files" and any(value is not None for value in (corp_code, endpoint, text)):
            raise ValueError("files supports source_key only")
        if view == "requests" and text is not None:
            raise ValueError("text search is supported by sources and fields")
        filters, args = [], []
        with self.connect() as db:
            if view == "fields":
                if not source_key:
                    raise ValueError("source_key is required for fields")
                source = db.execute("""SELECT sha256 FROM archive_sources WHERE key=? UNION ALL
                    SELECT r.sha256 FROM archive_heads h JOIN archive_requests r ON r.id=h.request_id
                    WHERE h.source_key=? LIMIT 1""", (source_key, source_key)).fetchone()
                if not source:
                    raise ValueError("source not archived; read it or run dart-migrate first")
                sql = """WITH RECURSIVE files(sha256,member,ordinal) AS (
                    SELECT ?,'','' UNION ALL
                    SELECT m.member_sha256,CASE WHEN f.member='' THEN m.name ELSE f.member||'!/'||m.name END,
                        f.ordinal||'/'||printf('%08d',m.ordinal)
                    FROM archive_members m JOIN files f ON f.sha256=m.sha256)
                    SELECT f.member,f.ordinal,v.path,v.kind,v.value FROM files f
                    JOIN archive_fields v ON v.sha256=f.sha256 WHERE 1=1"""
                args = [source[0]]
                if field is not None:
                    sql += " AND instr(v.path,?)>0"; args.append(field)
                if text is not None:
                    sql += " AND instr(v.value,?)>0"; args.append(text)
                sql += " ORDER BY f.ordinal,v.path"
            elif view == "files":
                sql = "SELECT f.*,c.index_error FROM archive_files f LEFT JOIN archive_content c USING(sha256) WHERE 1=1"
                if source_key:
                    sql += """ AND f.sha256 IN (WITH RECURSIVE root(sha256) AS (
                        SELECT sha256 FROM archive_sources WHERE key=? UNION
                        SELECT r.sha256 FROM archive_heads h JOIN archive_requests r ON r.id=h.request_id WHERE h.source_key=? UNION
                        SELECT m.member_sha256 FROM archive_members m JOIN root USING(sha256))
                        SELECT sha256 FROM root)"""
                    args += [source_key, source_key]
                sql += " ORDER BY f.sha256"
            else:
                for value, column in ((source_key, "r.source_key"), (corp_code, "r.corp"), (endpoint, "r.endpoint")):
                    if value is not None:
                        filters.append(column + "=?"); args.append(value)
                if view == "requests":
                    sql = "SELECT r.* FROM archive_requests r WHERE 1=1"
                    order = " ORDER BY r.id"
                else:
                    # Include raw API pages/master calls as well as cache aliases.
                    endpoint_sql = "COALESCE(r.endpoint,t.endpoint,CASE WHEN s.key LIKE 'api:%' THEN substr(s.key,5,instr(substr(s.key,5),':')-1) END)"
                    sql = """WITH sources(key,sha256,request_id) AS (
                        SELECT key,sha256,request_id FROM archive_sources UNION ALL
                        SELECT h.source_key,r.sha256,r.id FROM archive_heads h JOIN archive_requests r
                        ON r.id=h.request_id WHERE NOT EXISTS(SELECT 1 FROM archive_sources s WHERE s.key=h.source_key))
                        SELECT s.key AS source_key,s.sha256,f.path,f.media,f.size,f.provenance,
                        ENDPOINT AS endpoint,COALESCE(r.params,t.params) AS params,
                        r.checked,r.content_sha256,r.changed,COALESCE(c.corp,r.corp) AS corp,c.availability,
                        i.index_error FROM sources s JOIN archive_files f ON f.sha256=s.sha256
                        LEFT JOIN archive_requests r ON r.id=s.request_id
                        LEFT JOIN cache c ON c.key=s.key LEFT JOIN tasks t ON t.key=s.key
                        LEFT JOIN archive_content i ON i.sha256=s.sha256 WHERE 1=1""".replace("ENDPOINT", endpoint_sql)
                    filters, args = [], []
                    for value, column in ((source_key, "s.key"), (corp_code, "COALESCE(c.corp,r.corp)"), (endpoint, endpoint_sql)):
                        if value is not None:
                            filters.append(column + "=?"); args.append(value)
                    if text is not None:
                        filters.append("instr(i.text,?)>0"); args.append(text)
                    order = " ORDER BY s.key"
                if filters:
                    sql += " AND " + " AND ".join(filters)
                sql += order
            rows = db.execute(sql + " LIMIT ? OFFSET ?", (*args, limit + 1, offset)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            if "value" in item:
                item["value"] = json.loads(item["value"])
            if item.get("params") is not None:
                item["parameters"] = json.loads(item.pop("params"))
        return {"read_only": True, "view": view, "rows": items,
                "next_offset": offset + limit if len(rows) > limit else None,
                "untrusted_source_data": True}

    def verify(self, offset=0, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM archive_files ORDER BY sha256 LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
        results = []
        for row in rows[:limit]:
            path, digest, size = self.root / row["path"], hashlib.sha256(), 0
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk); size += len(chunk)
                status = "ok" if digest.hexdigest() == row["sha256"] and size == row["size"] else "checksum_mismatch"
            except OSError as exc:
                status = "missing" if isinstance(exc, FileNotFoundError) else "unreadable"
            results.append({"sha256": row["sha256"], "path": row["path"], "status": status})
        return {"read_only": True, "rows": results, "all_ok": all(r["status"] == "ok" for r in results),
                "next_offset": offset + limit if len(rows) > limit else None}

    def copy_snapshot(self, db, destination):
        destination = Path(destination)
        if destination.exists():
            raise ValueError("archive export destination already exists")
        destination.mkdir(parents=True, mode=0o700)
        try:
            for row in db.execute("SELECT * FROM archive_files ORDER BY sha256"):
                source, target = self.root / row["path"], destination / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                target.chmod(0o600)
                # A backup must not silently copy corrupt originals.
                with target.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != row["sha256"] or target.stat().st_size != row["size"]:
                    raise DartError("archive_checksum_mismatch")
        except Exception:
            shutil.rmtree(destination)
            raise
