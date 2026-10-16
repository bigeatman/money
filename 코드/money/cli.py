import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import threading
import time

from .engine import Engine
from .kiwoom import KiwoomBroker
from .paper import PaperBroker
from .server import make_server
from .scanner import MarketScanner, UnavailableScanner
from .store import Store


def load_env(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.startswith(("MONEY_", "KIWOOM_")):
                os.environ.setdefault(k, v.strip())


def main():
    p = argparse.ArgumentParser(description="Work analysis → Termux Kiwoom DEMO execution")
    p.add_argument("command", choices=["init", "credentials", "run", "import-plan", "status", "mcp-url"])
    p.add_argument("--root", default=".")
    p.add_argument("--market", choices=["KR", "US"])
    p.add_argument("--file")
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
    engine = Engine(store, broker, config, scanner=scanner)
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
        if scanner is not None:
            scanner.close()
        server.shutdown()
        with engine.lock:
            store.save()
        store.close()


if __name__ == "__main__":
    main()
