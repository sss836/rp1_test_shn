#!/usr/bin/env bash
# Loopback-only synthetic UI preview; no database, model key or real writes.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_site="$task_root/outputs/assistant-preview/site"
case "${1:-status}" in
  build)
    cd "$task_root/frontend"
    node node_modules/vite/bin/vite.js build assistant-preview --config assistant-preview/vite.config.ts --outDir "$task_site"
    ;;
  start)
    test -f "$task_site/index.html"
    systemd-run --user --unit=rp1-assistant-preview --description='RP1 synthetic assistant UI preview, loopback only' --property=Restart=on-failure /usr/bin/python3 -m http.server 5179 --bind 127.0.0.1 --directory "$task_site"
    for task_attempt in {1..50}; do
      if curl --silent --fail --output /dev/null http://127.0.0.1:5179/; then
        echo 'ready: http://127.0.0.1:5179/ (synthetic UI only)'
        exit 0
      fi
      sleep .1
    done
    echo 'preview did not become ready' >&2
    exit 1
    ;;
  stop) systemctl --user stop rp1-assistant-preview.service ;;
  status) systemctl --user show rp1-assistant-preview.service --property=ActiveState,SubState,MainPID,ExecMainStartTimestamp ;;
  *) echo 'usage: assistant-preview.sh build|start|stop|status' >&2;exit 2 ;;
esac
