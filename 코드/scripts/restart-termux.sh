#!/data/data/com.termux/files/usr/bin/bash
# Restart the Python server while keeping the current tunnel and tmux sessions.
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
if tmux has-session -t money 2>/dev/null; then
  tmux new-window -d -t money -c "$PWD" -n server 'bash scripts/start-termux.sh'
else
  tmux new-session -d -s money -c "$PWD" 'bash scripts/start-termux.sh'
fi
python - <<'PY'
import json
import time
from urllib.request import urlopen
port = int(json.load(open('config.json'))['port'])
for attempt in range(20):
    try:
        with urlopen('http://127.0.0.1:' + str(port) + '/health', timeout=1) as response:
            if response.status == 200:
                print('Python 서버 재시작·응답 확인 완료')
                break
    except OSError:
        pass
    time.sleep(0.5)
else:
    raise SystemExit('서버 응답이 없습니다. tmux attach -t money 로 로그를 확인하세요.')
PY
