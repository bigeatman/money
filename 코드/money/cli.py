import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import threading
import time

from .engine import Engine
from .kiwoom import KiwoomBroker
from .paper import PaperBroker
from .server import make_server
from .scanner import MarketScanner, UnavailableScanner
from .store import Store
from .fundamentals import Fundamentals, UnavailableFundamentals


def load_env(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.startswith(("MONEY_", "KIWOOM_", "OPENDART_")):
                os.environ.setdefault(k, v.strip())


def main():
    p = argparse.ArgumentParser(description="Work analysis → Termux Kiwoom DEMO execution")
    p.add_argument("command", choices=["init", "credentials", "run", "import-plan", "status", "mcp-url",
        "dart-credentials", "dart-status", "dart-show", "dart-export", "dart-api",
        "dart-query", "dart-verify", "dart-migrate", "dart-reindex", "dart-job", "dart-reviews", "dart-capacity"])
    p.add_argument("--root", default=".")
    p.add_argument("--market", choices=["KR", "US"])
    p.add_argument("--file")
    p.add_argument("--symbol")
    p.add_argument("--after-seq", type=int)
    p.add_argument("--through-seq", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--endpoint")
    p.add_argument("--params", default="{}", help="JSON OpenDART parameters; key is read locally")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--benchmark", action="store_true", help="dart-capacity: local fixture benchmark, no DART calls")
    p.add_argument("--include-data", action="store_true")
    p.add_argument("--source-key")
    p.add_argument("--corp-code")
    p.add_argument("--view", choices=["sources", "fields", "requests", "files", "text"], default="sources")
    p.add_argument("--job-id")
    p.add_argument("--after-sha", default="")
    p.add_argument("--field")
    p.add_argument("--text")
    p.add_argument("--all", action="store_true", help="dart-migrate: process all local legacy versions in batches")
    p.add_argument("--public-origin", help="HTTPS tunnel origin, e.g. https://your-private-host.example")
    args = p.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    envfile, configfile = root / ".env", root / "config.json"
    if args.command == "init":
        if not configfile.exists():
            example = Path(__file__).resolve().parent.parent / "config.example.json"
            if not example.exists():
                raise SystemExit("run init from the source checkout")
            shutil.copyfile(example, configfile)
        if not envfile.exists():
            envfile.write_text("MONEY_BRIDGE_TOKEN=" + secrets.token_urlsafe(48) + "\n")
            envfile.chmod(0o600)
        print("Created local configuration. Default broker=paper (offline).")
        return
    if args.command == "dart-credentials":
        if not envfile.exists():
            raise SystemExit("run init first")
        key = getpass.getpass("OpenDART API key: ").strip()
        if len(key) != 40 or not key.isascii() or not key.isalnum():
            raise SystemExit("a 40-character alphanumeric OpenDART key is required")
        lines = [line for line in envfile.read_text().splitlines() if line.partition("=")[0].strip() != "OPENDART_API_KEY"]
        stage = envfile.with_suffix(".tmp")
        fd = os.open(stage, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines + ["OPENDART_API_KEY=" + key]) + "\n")
        os.replace(stage, envfile)
        print("OpenDART key saved locally; restart the daemon to use it.")
        return
    if args.command == "credentials":
        if not args.market:
            raise SystemExit("--market KR or US is required")
        if not envfile.exists():
            raise SystemExit("run init first")
        lines = envfile.read_text().splitlines()
        prefix = f"KIWOOM_{args.market}_"
        lines = [x for x in lines if not x.startswith(prefix)]
        app = getpass.getpass(f"{args.market} DEMO App Key: ").strip()
        secret = getpass.getpass(f"{args.market} DEMO App Secret: ").strip()
        if not app or not secret:
            raise SystemExit("empty credential")
        lines.extend([prefix + "APPKEY=" + app, prefix + "SECRET=" + secret])
        envfile.write_text("\n".join(lines) + "\n")
        envfile.chmod(0o600)
        print("Demo credentials saved locally; values were not displayed.")
        return
    load_env(envfile)
    if args.command == "mcp-url":
        if not args.public_origin or not args.public_origin.startswith("https://"):
            raise SystemExit("--public-origin HTTPS_ORIGIN is required")
        token = os.environ["MONEY_BRIDGE_TOKEN"]
        print(args.public_origin.rstrip("/") + "/mcp/" + token)
        print("Private capability URL: register directly in your private connector; do not post it publicly.")
        return
    if not configfile.exists():
        raise SystemExit("run init first")
    config = json.loads(configfile.read_text())
    if args.command.startswith("dart-"):
        dart = Fundamentals(root / "runtime" / "fundamentals.sqlite", config.get("opendart"))
        if args.command == "dart-api":
            if not args.endpoint:
                raise SystemExit("--endpoint ENDPOINT is required")
            result = dart.call_api(args.endpoint, json.loads(args.params), args.refresh, args.include_data)
        elif args.command == "dart-status":
            result = dart.status()
        elif args.command == "dart-capacity":
            result = dart.collection_job("status", plan={"mode": "capacity", "benchmark": True} if args.benchmark else None)
        elif args.command == "dart-query":
            result = dart.query_data(args.view, args.source_key, args.corp_code, args.endpoint,
                                     args.field, args.text, args.offset, args.limit)
        elif args.command == "dart-verify":
            result = dart.verify_archive(args.offset, args.limit)
        elif args.command == "dart-reindex":
            cursor = args.after_sha
            processed, unresolved = 0, []
            while True:
                result = dart.reindex_archive(args.limit, cursor)
                processed += result["processed"]
                unresolved.extend(x for x in result["rows"] if x["status"] != "ok")
                if not args.all or result["next_after_sha"] is None:
                    result.update(total_processed=processed, unresolved=unresolved, all_ok=not unresolved)
                    break
                cursor = result["next_after_sha"]
                print(json.dumps(result, ensure_ascii=False), flush=True)
        elif args.command == "dart-job":
            if args.file:
                result = dart.collection_job("start", args.job_id, json.loads(Path(args.file).read_text()))
                worker = dart.structured if result["job_id"].startswith("structured_") else dart.jobs
                worker.thread.join()
                result = dart.collection_job("status", result["job_id"], limit=args.limit)
            else:
                result = dart.collection_job("status", args.job_id, offset=args.offset, limit=args.limit)
        elif args.command == "dart-reviews":
            if not args.file or not args.symbol or not args.job_id:
                raise SystemExit("--job-id, --symbol and --file are required")
            result = dart.record_review(args.job_id, args.symbol, json.loads(Path(args.file).read_text()))
        elif args.command == "dart-migrate":
            while True:
                result = dart.migrate_archive(args.limit)
                if not args.all or result["complete"]:
                    break
                print(json.dumps(result, ensure_ascii=False), flush=True)
        elif args.command == "dart-export":
            if not args.file:
                raise SystemExit("--file DESTINATION.sqlite is required")
            result = dart.export(args.file)
        else:
            result = dart.report(args.symbol, args.after_seq, args.offset, args.limit, args.through_seq)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if config["broker"] not in {"paper", "kiwoom-demo"}:
        raise SystemExit("Only offline paper or Kiwoom DEMO is supported")
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    lockfile = open(runtime / "executor.lock", "a+")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another executor owns this ledger; use HTTP /status or /plan")
    store = Store(runtime / "state.sqlite", config["capital_krw"])
    broker = KiwoomBroker() if config["broker"] == "kiwoom-demo" else PaperBroker()
    scanner = None
    if config["broker"] == "kiwoom-demo":
        try:
            scanner = MarketScanner(broker, runtime / "market-scans", config.get("market_scan"))
        except (OSError, ValueError) as exc:
            scanner = UnavailableScanner(exc)
    try:
        dart = Fundamentals(runtime / "fundamentals.sqlite", config.get("opendart"))
    except (OSError, ValueError, sqlite3.Error):
        dart = UnavailableFundamentals()
    engine = Engine(store, broker, config, scanner=scanner, fundamentals=dart)
    if args.command == "status":
        print(json.dumps(engine.status(), ensure_ascii=False, indent=2))
        store.close()
        return
    if args.command == "import-plan":
        if not args.file:
            raise SystemExit("--file PLAN_JSON is required")
        print(json.dumps(engine.submit(json.loads(Path(args.file).read_text())), ensure_ascii=False))
        store.close()
        return
    token = os.environ.get("MONEY_BRIDGE_TOKEN", "")
    server = make_server(engine, token, config["host"], int(config["port"]), config["allowed_origins"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Executor running on loopback port {config['port']}; broker={broker.mode}; capital={config['capital_krw']} KRW", flush=True)
    try:
        while not stop.is_set():
            if scanner is not None:
                scanner.heartbeat()
            engine.tick()
            stop.wait(max(1, float(config["poll_seconds"])))
    finally:
        dart.close()
        if scanner is not None:
            scanner.close()
        server.shutdown()
        server.server_close()
        with engine.lock:
            store.save()
        store.close()
        lockfile.close()


if __name__ == "__main__":
    main()
