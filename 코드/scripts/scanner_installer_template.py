#!/usr/bin/env python3
"""Money V7: source-only repair, preserving configuration and the existing ledger.

No plan submission, order submission, ledger reset, credential change or task
creation. Existing confirmed holdings/target orders resume normal management.
"""
import ast
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
import zlib

FILES = {}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(project):
    if not FILES:
        raise RuntimeError("배포 파일이 아닙니다. 빌드된 money_fix_20261016_v7.py를 실행하세요.")
    changes = []
    for name, record in FILES.items():
        path = project / name
        if path.is_symlink():
            raise RuntimeError("심볼릭 링크 파일은 자동 변경하지 않습니다: " + name)
        before = path.read_bytes() if path.exists() else None
        after = zlib.decompress(base64.b64decode(record["data"]))
        if digest(after) != record["updated"]:
            raise RuntimeError("수정 파일 검증 실패: " + name)
        if before is None:
            if record["original"] is not None:
                raise RuntimeError("기존 V6 파일이 없습니다: " + name)
        elif digest(before) not in {record["original"], record["updated"]}:
            raise RuntimeError("알려진 V6/V7와 다른 파일입니다. 덮어쓰지 않았습니다: " + name)
        if name.endswith(".py"):
            ast.parse(after, filename=name)
        if before != after:
            changes.append((path, before, after))
    return changes


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(path.name + ".money-v7.tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with stage.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(stage, mode)
    os.replace(stage, path)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def has_session():
    return subprocess.run(["tmux", "has-session", "-t", "money"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def stop_server(port):
    result = subprocess.run(["tmux", "list-panes", "-s", "-t", "money", "-F",
                             "#{pane_pid} #{pane_current_command}"], check=True, capture_output=True, text=True)
    panes = result.stdout.strip().splitlines()
    if len(panes) != 1:
        raise RuntimeError("money 세션이 여러 창/패널입니다. 자동 종료하지 않았습니다.")
    pid, command = panes[0].split(maxsplit=1)
    if not command.startswith("python"):
        raise RuntimeError("money 세션의 Python 실행기를 확인하지 못했습니다. 종료하지 않았습니다.")
    os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + 45
    while (port_open(port) or has_session()) and time.monotonic() < deadline:
        time.sleep(0.25)
    if port_open(port) or has_session():
        raise RuntimeError("기존 실행기 종료를 확인하지 못했습니다. 소스는 변경하지 않았습니다.")


def start_server(project):
    subprocess.run(["tmux", "new-session", "-d", "-s", "money", "bash scripts/start-termux.sh"],
                   cwd=project, check=True)


def post(port, token, path, payload, timeout=45):
    req = Request("http://127.0.0.1:%d%s" % (port, path), json.dumps(payload).encode(),
                  {"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def read_status(port, token):
    response = post(port, token, "/mcp", {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "get_execution_status", "arguments": {}}})
    result = response["result"]
    if result.get("isError"):
        raise RuntimeError("실행 상태 조회 실패")
    return result["structuredContent"]


def verify_server(port, token):
    deadline = time.monotonic() + 15
    while not port_open(port) and time.monotonic() < deadline:
        time.sleep(0.25)
    if not port_open(port):
        raise RuntimeError("수정 서버가 시작되지 않았습니다.")
    status = read_status(port, token)
    caps = status.get("execution_capabilities", {})
    policy = caps.get("holding_policy", {})
    scanner = caps.get("market_scanner", {})
    if (status.get("mode") != "kiwoom-demo" or caps.get("adapter_version") != "us-demo-v6"
            or caps.get("us_buying_power_method") != "conservative_usd_cash"
            or scanner.get("version") != "whole-market-v1" or scanner.get("supported") is not True
            or policy.get("force_close_at_session_end") is not False
            or policy.get("force_close_on_session_rollover") is not False
            or policy.get("overnight_holdings") is not True):
        raise RuntimeError("V7 전 종목 수집기 및 기존 현금/보유 정책을 확인하지 못했습니다.")
    return status


def read_ledger(project):
    path = project / "runtime" / "state.sqlite"
    if not path.is_file():
        raise RuntimeError("기존 장부가 없습니다. 새 장부를 만들지 않았습니다.")
    db = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
    finally:
        db.close()
    if not row:
        raise RuntimeError("기존 장부 내용을 확인하지 못했습니다.")
    state = json.loads(row[0])
    if str(state.get("capital")) != "1000000":
        raise RuntimeError("장부의 공통 원금 100만원을 확인하지 못했습니다.")
    return row[0], state


def require_no_pending_buys(state):
    terminal = {"done", "expired", "rejected", "superseded"}
    current = datetime.now(timezone.utc)
    for command in state.get("commands", {}).values():
        if command.get("action") == "BUY" and command.get("state") not in terminal:
            expires = datetime.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
            if expires > current:
                raise RuntimeError("유효한 대기 매수 지시가 있습니다. 자동 재개하지 않도록 적용을 중단했습니다.")
    for order in state.get("orders", {}).values():
        if order.get("side") == "BUY" and order.get("status") in {"open", "sending", "unknown", "cancel_requested"}:
            raise RuntimeError("미확정 매수 주문이 있습니다. 확인 전까지 자동 적용하지 않았습니다.")


def install(project, config, changes, token):
    if not shutil.which("tmux"):
        raise RuntimeError("tmux가 없습니다. pkg install tmux 후 다시 실행하세요.")
    running, port = has_session(), int(config["port"])
    if not running and port_open(port):
        raise RuntimeError("money tmux 세션 외부에서 서버가 실행 중입니다. 변경하지 않았습니다.")
    _, prior = read_ledger(project)
    require_no_pending_buys(prior)
    protected = {name: (project / name).read_bytes() for name in (".env", "config.json")}
    if running:
        print("기존 실행기를 종료하고 장부 저장을 기다립니다.", flush=True)
        stop_server(port)
    lock = (project / "runtime" / "executor.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger, state = read_ledger(project)
        require_no_pending_buys(state)
        backup = project / "runtime" / "code-backups" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_v7")
        backup.mkdir(parents=True)
        for path, before, _ in changes:
            if before is not None:
                saved = backup / path.relative_to(project)
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(before)
        applied = []
        try:
            for path, before, after in changes:
                applied.append((path, before, after))
                atomic_write(path, after)
            checked = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                     cwd=project, capture_output=True, text=True)
            if checked.returncode:
                raise RuntimeError("V7 설치 후 자체 테스트 실패: " + checked.stderr[-1800:])
            if read_ledger(project)[0] != ledger or any((project / k).read_bytes() != v for k, v in protected.items()):
                raise RuntimeError("설정/장부가 설치 도중 변경됐습니다. 소스 적용을 복원합니다.")
        except Exception:
            for path, before, _ in reversed(applied):
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, before)
            raise
    except Exception:
        lock.close()
        if running and not has_session() and not port_open(port):
            start_server(project)
        raise
    lock.close()
    try:
        start_server(project)
        status = verify_server(port, token)
    except Exception as exc:
        if has_session():
            stop_server(port)
        for path, before, _ in reversed(changes):
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, before)
        if running:
            start_server(project)
        raise RuntimeError("V7 시작 확인 실패로 이전 소스를 복원했습니다: " + str(exc)) from exc
    print("V7 소스 적용·자체 테스트·서버 재시작 확인 완료.", flush=True)
    print("설정·키·장부 유지. 소스 백업:", backup, flush=True)
    print("마감/거래일 변경 강제청산: False / 익일 보유: True", flush=True)
    return status


def diagnose(config, token):
    port = int(config["port"])
    market = "KR"
    print("V7 수집 연결을 확인합니다. 매매 계획/주문을 제출하지 않습니다.", flush=True)
    try:
        response = post(port, token, "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "start_market_scan", "arguments": {"market": market}}})
        result = response["result"]
        if result.get("isError"):
            raise RuntimeError("전 종목 수집 시작 실패: " + result["content"][0]["text"])
        scan = result["structuredContent"]
        print("수집기: whole-market-v1 / 시장:", market, flush=True)
        print("수집 ID:", scan.get("scan_id"), "/ 상태:", scan.get("status"), flush=True)
        print("현재 수집 건수:", scan.get("counts"), flush=True)
        print("코드 설치 성공과 실시간 전수 수집 완료는 별개입니다.", flush=True)
        print("이 출력 내용을 대화에 전달하면 실제 수집 완료·누락을 이어 확인할 수 있습니다.", flush=True)
        return 0
    except Exception as exc:
        print("V7 소스 적용은 완료됐으나 수집 진단은 실패했습니다:", type(exc).__name__, str(exc), flush=True)
        print("설정·키·장부는 유지됐습니다. 새 매매 계획/주문을 제출하지 않았습니다.", flush=True)
        print("실시간 전수 수집을 확인하기 전에는 새 매수를 진행하지 않습니다.", flush=True)
        return 2


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 이상이 필요합니다.")
    project = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.home() / "money-work" / "money"
    config = json.loads((project / "config.json").read_text())
    if config.get("broker") != "kiwoom-demo" or str(config.get("capital_krw")) != "1000000":
        raise RuntimeError("기존 키움 모의투자/100만원 설정이 아닙니다. 변경하지 않았습니다.")
    if config.get("host") != "127.0.0.1":
        raise RuntimeError("기존 로컬 서버 설정을 확인하지 못했습니다.")
    changes = prepare(project)
    sys.path.insert(0, str(project))
    from money.cli import load_env
    load_env(project / ".env")
    token = os.environ.get("MONEY_BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("기존 브리지 인증키를 읽지 못했습니다. 변경하지 않았습니다.")
    install(project, config, changes, token)
    return diagnose(config, token)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("실행 실패:", type(exc).__name__, str(exc), flush=True)
        sys.exit(1)
