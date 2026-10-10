#!/usr/bin/env bash
set -euo pipefail
test "$(dpkg-query -W -f='${Version}' rp1-test-hmi)" = "${HMI_EXPECTED_VERSION:?Set HMI_VERSION when building the acceptance image}"
useradd --create-home --shell /bin/bash hmi-qa
runuser -u hmi-qa -- rp1-test-hmi --offline-check --gateway-only --duration 2 --port 18766
runuser -u hmi-qa -- rp1-test-hmi --offline-check --gateway-only --offline-access monitor --duration 2 --port 18767 --state-dir /home/hmi-qa/monitor
runuser -u hmi-qa -- env QT_QPA_PLATFORM=xcb xvfb-run -a rp1-test-hmi --offline-check --duration 3 --port 18768
test "$(stat -c %U /usr/lib/rp1-test-hmi/factory-hmi-can-helper)" = root
test "$(stat -c %U /etc/rp1-test-hmi/can-policy.yaml)" = root
echo 'PASS clean Ubuntu 24.04: installation, non-root mock gateway, monitor/control profiles and Qt xcb GUI.'
