#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$(dirname "$0")/.."
termux-wake-lock
exec python -m money.cli run
