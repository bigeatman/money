#!/data/data/com.termux/files/usr/bin/bash
set -eu
case "$HOME" in
  /data/data/com.termux/files/home|/data/user/0/com.termux/files/home) ;;
  *) printf 'Termux에서 실행하세요.\n'; exit 1 ;;
esac
cd "$(dirname "$0")/.."
command -v tmux >/dev/null
command -v pgrep >/dev/null
command -v pkill >/dev/null
python -c 'import money.dart_structured'
money_pattern='[p]ython.*-m money[.]cli run'
pkill -TERM -f "$money_pattern" 2>/dev/null || true
for money_wait in $(seq 1 30); do
  pgrep -f "$money_pattern" >/dev/null || break
  sleep 1
done
if pgrep -f "$money_pattern" >/dev/null; then
  printf '기존 서버가 종료되지 않아 중단했습니다.\n'
  exit 1
fi
# Restore server availability even if fetching or checking fails.
trap 'bash scripts/restart-termux.sh' EXIT
python scripts/check-dart-fresh.py --erase-research
