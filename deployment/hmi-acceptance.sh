#!/usr/bin/env bash
set -euo pipefail
test "$(dpkg-query -W -f='${Version}' rp1-test-hmi)" = "${HMI_EXPECTED_VERSION:-1.0.1+ubuntu24.04}"
qa_port=${HMI_QA_PORT:-8766}
install -d -o rp1-test-gateway -g rp1-factory /var/lib/rp1-test-hmi/qa
runuser -u rp1-test-gateway -- /opt/rp1-test-hmi/gateway/rp1-factory-gateway \
  --host 127.0.0.1 --port "$qa_port" --data-root /var/lib/rp1-test-hmi/qa > /tmp/gateway.log 2>&1 &
gateway_pid=$!
trap 'kill "$gateway_pid" 2>/dev/null || true' EXIT
for attempt in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$qa_port/api/v1/health" >/dev/null; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$qa_port/api/v1/health"
/opt/rp1-test-hmi/desktop/rp1-factory-desktop --help >/dev/null
set +e
QT_QPA_PLATFORM=xcb timeout "${HMI_QA_DESKTOP_SECONDS:-12}s" xvfb-run -a /opt/rp1-test-hmi/desktop/rp1-factory-desktop \
  --gateway-url "http://127.0.0.1:$qa_port" >/tmp/desktop.log 2>&1
result=$?
set -e
cat /tmp/desktop.log
[[ $result == 124 ]] || { cat /tmp/gateway.log; exit 1; }
echo 'PASS clean Ubuntu 24.04: package installation, non-root gateway, Qt xcb desktop and mock runtime.'
