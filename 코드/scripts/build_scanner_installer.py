"""Build a self-contained, hash-checked V6 -> V7 source-only installer."""
import argparse
import ast
import base64
import hashlib
from pathlib import Path
import subprocess
import zlib

TARGETS = ["money/common.py", "money/engine.py", "money/kiwoom.py", "money/server.py", "money/cli.py",
           "money/marketdata.py", "money/scanner.py", "tests/test_server.py", "tests/test_scanner.py",
           "tests/test_scanner_installer.py", "scripts/build_scanner_installer.py", "scripts/scanner_installer_template.py"]


def record(before, after):
    return {"original": hashlib.sha256(before).hexdigest() if before is not None else None,
            "updated": hashlib.sha256(after).hexdigest(),
            "data": base64.b64encode(zlib.compress(after, 9)).decode()}


def render(template, records):
    tree = ast.parse(template)
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "FILES" for t in n.targets))
    lines = template.splitlines(keepends=True)
    result = "".join(lines[:assignment.lineno - 1]) + "FILES = " + repr(records) + "\n" + "".join(lines[assignment.end_lineno:])
    ast.parse(result)
    return result


def build(root, output, baseline="HEAD"):
    root = Path(root).resolve()
    records = {}
    for target in TARGETS:
        result = subprocess.run(["git", "show", baseline + ":코드/" + target], cwd=root,
                                capture_output=True)
        before = result.stdout if result.returncode == 0 else None
        if result.returncode and "does not exist" not in result.stderr.decode(errors="replace") and "exists on disk" not in result.stderr.decode(errors="replace"):
            raise RuntimeError("cannot read baseline for " + target + ": " + result.stderr.decode(errors="replace"))
        after = (root / target).read_bytes()
        ast.parse(after, filename=target)
        records[target] = record(before, after)
    template = (root / "scripts/scanner_installer_template.py").read_text()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(template, records))
    return {"file": str(output), "payload_files": len(records), "bytes": output.stat().st_size}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--baseline", default="HEAD")
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.output, args.baseline)))
