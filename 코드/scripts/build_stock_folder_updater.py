"""Build a v4 stock-folder patch that does not stop/restart the running server."""
import ast
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import zlib

BASELINE = "f7856f23aaae9f05eb544696d50949366c0b5878"
TARGETS = ["money/dart_folders.py", "money/fundamentals.py", "money/server.py",
           "scripts/build_scanner_installer.py", "scripts/build_stock_folder_updater.py",
           "tests/test_dart_folders.py", "docs/opendart-stock-folders.md"]

TEMPLATE = '''#!/usr/bin/env python3
"""Add issuer raw-response folders. No server/session restart and no DART calls."""
import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib

FILES = {}

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def atomic(path, raw, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".money-folder-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def prepare(root):
    changes = []
    for name, record in FILES.items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError("refusing symlink/outside project: " + name)
        before = path.read_bytes() if path.exists() else None
        raw = zlib.decompress(base64.b64decode(record["data"]))
        if digest(raw) != record["updated"]:
            raise RuntimeError("patch payload checksum mismatch: " + name)
        if (before is None and record["original"] is not None
                or before is not None and digest(before) not in {record["original"],record["updated"]}):
            raise RuntimeError("unknown source version; not overwritten: " + name)
        if name.endswith(".py"):
            ast.parse(raw, filename=name)
        if before != raw:
            changes.append((path, before, raw, path.stat().st_mode & 0o777 if path.exists() else 0o644))
    return changes

def main():
    p = argparse.ArgumentParser()
    p.add_argument("project", nargs="?", default=str(Path.home()/"money-work"/"money"/"코드"))
    p.add_argument("--corp-code")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    root = Path(args.project).expanduser().resolve()
    if not (root/"runtime"/"fundamentals.sqlite").is_file():
        raise RuntimeError("existing DART database not found")
    changes = prepare(root)
    if args.check:
        print("호환성 확인: 변경 대상", len(changes), "개. 파일/DB/서버 변경 없음.")
        return 0
    backup = root/"runtime"/"code-backups"/(datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")+"_stock_folders")
    if changes:
        backup.mkdir(parents=True)
        for path,before,raw,mode in changes:
            if before is not None:
                saved=backup/path.relative_to(root)
                saved.parent.mkdir(parents=True,exist_ok=True)
                saved.write_bytes(before)
    applied=[]
    try:
        for path,before,raw,mode in changes:
            atomic(path,raw,mode)
            applied.append((path,before,mode))
        subprocess.run([sys.executable,"-m","unittest","discover","-s","tests","-p","test_dart_folders.py","-q"],cwd=root,check=True)
    except BaseException:
        for path,before,mode in reversed(applied):
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path,before,mode)
        raise
    print("코드 적용·폴더 기능 검사 완료. 기존 서버/세션은 종료하지 않았습니다.",flush=True)
    if args.corp_code:
        result=subprocess.run([sys.executable,"-m","money.cli","dart-query","--view","files",
            "--corp-code",args.corp_code,"--limit","1"],cwd=root,check=True,capture_output=True,text=True)
        info=json.loads(result.stdout)
        print("종목 폴더:",info["folder_root"])
        print("원본/과거 자료 파일:",info["total_response_files"])
        print("전체 트리:",info["full_tree_file"])
        print("기존 도구 조회 source_key:",info["tree_source_key"])
        print("DART API 추가 호출:",info["network_requests"])
    print("실행 중 서버의 새 files+corp_code 처리 코드는 다음 정상 재시작 때 로드됩니다.")
    print("지금 만든 폴더 트리는 기존 query_dart_data(fields) 또는 get_fundamental_evidence로 바로 읽을 수 있습니다.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

def build(root, output):
    root, output = Path(root).resolve(), Path(output)
    records = {}
    for name in TARGETS:
        old = subprocess.run(["git", "show", BASELINE + ":코드/" + name], cwd=root, capture_output=True)
        if old.returncode and not any(s in old.stderr.decode() for s in ("does not exist", "exists on disk")):
            raise RuntimeError("cannot read baseline " + name)
        raw = (root/name).read_bytes()
        records[name] = {"original": hashlib.sha256(old.stdout).hexdigest() if old.returncode==0 else None,
                         "updated": hashlib.sha256(raw).hexdigest(),
                         "data": base64.b64encode(zlib.compress(raw,9)).decode()}
    text = TEMPLATE.replace("FILES = {}", "FILES = " + repr(records), 1)
    ast.parse(text)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(text)
    return {"file":str(output),"sha256":hashlib.sha256(output.read_bytes()).hexdigest(),"payload_files":len(records)}

if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument("output");p.add_argument("--root",default=str(Path(__file__).resolve().parent.parent));a=p.parse_args()
    print(json.dumps(build(a.root,a.output),ensure_ascii=False))
