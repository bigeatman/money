"""Build a standalone OpenDART v2 -> v3 updater for the existing Termux app.

Reuse the source installer's backup/rollback checks, but verify the DART
runtime and local DB queries instead of starting a whole-market scan.
"""
import argparse
import ast
import importlib.util
import json
from pathlib import Path
import subprocess

BASELINE = "69d2ecdd80da1c2d9345a083098312ac36422caa"
_spec = importlib.util.spec_from_file_location(
    "_money_source_installer_builder", Path(__file__).with_name("build_scanner_installer.py"))
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)
TARGETS = scanner.TARGETS + ["scripts/build_dart_installer.py", "tests/test_dart_installer.py"]

DATABASE_BACKUP = '''def backup_dart_database(project, backup):
    source_path = project / "runtime" / "fundamentals.sqlite"
    if not source_path.is_file():
        raise RuntimeError("기존 DART DB가 없습니다. 새 DB를 만들지 않았습니다.")
    destination = backup / "fundamentals-before.sqlite"
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect("file:" + quote(str(source_path), safe="/") + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    return destination


def restore_dart_database(project, backup):
    with sqlite3.connect("file:" + quote(str(backup), safe="/") + "?mode=ro", uri=True) as source:
        with sqlite3.connect(project / "runtime" / "fundamentals.sqlite") as target:
            source.backup(target)
'''

VERIFY = '''def verify_server(port, token):
    status = verify_existing_server(port, token)
    dart = status.get("execution_capabilities", {}).get("fundamentals", {})
    if (dart.get("supported") is not True or dart.get("version") != "opendart-v3"
            or dart.get("schema_version") != 2 or dart.get("configured") is not True
            or dart.get("enabled") is not True
            or dart.get("archive", {}).get("capture_exact_http_bodies") is not True):
        raise RuntimeError("OpenDART v3 / schema 2 / API 키 / 원문 파일 저장을 확인하지 못했습니다.")
    response = post(port, token, "/mcp", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "query_dart_data", "arguments": {"view": "requests", "limit": 1}}})
    result = response.get("result", {})
    if result.get("isError") or not isinstance(result.get("structuredContent", {}).get("rows"), list):
        raise RuntimeError("query_dart_data 로컬 DB 조회를 확인하지 못했습니다.")
    return status
'''

DIAGNOSE = '''def diagnose(config, token):
    status = verify_server(int(config["port"]), token)
    dart = status["execution_capabilities"]["fundamentals"]
    print("적용 확인: opendart-v3 / schema_version=2 / configured=true", flush=True)
    print("신규 원본 파일: runtime/fundamentals.files/objects/", flush=True)
    print("기존 자료의 파일 이관 대기:", dart["archive"]["legacy_versions_pending"], flush=True)
    print("이 업데이트의 DART API 호출: 0회. 자료 수집은 별도 요청으로 시작합니다.", flush=True)
    print("기존 자료 전체 파일 이관: python -m money.cli dart-migrate --limit 100 --all", flush=True)
    print("ChatGPT money 연결의 도구 목록을 새로 고침하세요.", flush=True)
    return 0
'''

MAIN = '''def main():
    import argparse
    parser = argparse.ArgumentParser(description="기존 Termux OpenDART v2를 v3로 업데이트")
    parser.add_argument("project", nargs="?", default=str(Path.home() / "money-work" / "money" / "코드"))
    parser.add_argument("--check", action="store_true", help="파일 호환성만 확인; 변경/재시작 없음")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 이상이 필요합니다.")
    project = Path(args.project).expanduser().resolve()
    config = json.loads((project / "config.json").read_text())
    if config.get("broker") != "kiwoom-demo" or str(config.get("capital_krw")) != "1000000":
        raise RuntimeError("기존 키움 모의투자/100만원 설정이 아닙니다. 변경하지 않았습니다.")
    if config.get("host") != "127.0.0.1":
        raise RuntimeError("기존 로컬 서버 설정을 확인하지 못했습니다.")
    changes = prepare(project)
    if args.check:
        print("호환성 확인 완료. 변경 대상:", len(changes), "/ 전체 관리 파일:", len(FILES))
        return 0
    sys.path.insert(0, str(project))
    from money.cli import load_env
    load_env(project / ".env")
    token = os.environ.get("MONEY_BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("기존 브리지 인증키를 읽지 못했습니다. 변경하지 않았습니다.")
    install(project, config, changes, token)
    return diagnose(config, token)
'''


def template(root):
    text = (Path(root) / "scripts/scanner_installer_template.py").read_text()
    text = text.replace("money_fix_20261016_v7.py", "money_dart_v3_20261019.py")
    text = text.replace("V6", "OpenDART v2").replace("V7", "OpenDART v3")
    text = text.replace(".money-v7.tmp", ".money-dart-v3.tmp").replace(' + "_v7"', ' + "_dart_v3"')
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    replacements = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"install", "verify_server", "diagnose", "main"}:
            if node.name == "verify_server":
                original = "".join(lines[node.lineno - 1:node.end_lineno])
                updated = original.replace("def verify_server(", "def verify_existing_server(", 1) + "\n\n" + VERIFY
            elif node.name == "install":
                updated = "".join(lines[node.lineno - 1:node.end_lineno])
                insert_backup = "        backup.mkdir(parents=True)\n"
                insert_restore = "        if running:\n            start_server(project)\n        raise RuntimeError"
                if updated.count(insert_backup) != 1 or updated.count(insert_restore) != 1:
                    raise RuntimeError("source installer backup/rollback layout changed")
                updated = updated.replace(insert_backup, "        backup.mkdir(parents=True, mode=0o700)\n"
                                          "        dart_backup = backup_dart_database(project, backup)\n")
                updated = updated.replace(insert_restore, "        restore_dart_database(project, dart_backup)\n" + insert_restore)
                updated = updated.replace('    return status\n', '    print("DART DB 백업:", dart_backup, flush=True)\n    return status\n')
                updated = DATABASE_BACKUP + "\n\n" + updated
            else:
                updated = DIAGNOSE if node.name == "diagnose" else MAIN
            replacements.append((node.lineno - 1, node.end_lineno, updated + "\n"))
    if len(replacements) != 4:
        raise RuntimeError("source installer template layout changed; review the DART updater")
    for start, end, updated in reversed(replacements):
        lines[start:end] = [updated]
    result = "".join(lines)
    ast.parse(result)
    return result


def build(root, output, baseline=BASELINE):
    root = Path(root).resolve()
    records = {}
    for target in TARGETS:
        result = subprocess.run(["git", "show", baseline + ":코드/" + target], cwd=root,
                                capture_output=True)
        before = result.stdout if result.returncode == 0 else None
        if result.returncode and not any(message in result.stderr.decode(errors="replace")
                                        for message in ("does not exist", "exists on disk")):
            raise RuntimeError("cannot read baseline: " + target)
        after = (root / target).read_bytes()
        ast.parse(after, filename=target)
        records[target] = scanner.record(before, after)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(scanner.render(template(root), records))
    return {"file": str(output), "baseline": baseline, "payload_files": len(records),
            "bytes": output.stat().st_size}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--baseline", default=BASELINE)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.output, args.baseline)))
