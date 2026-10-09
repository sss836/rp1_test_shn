#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || {
  echo 'Build on Ubuntu 24.04 x86_64.' >&2; exit 1;
}
unset PYTHONPATH LD_PRELOAD
runtime_dir="$PWD/hmi/build/release-runtime"
[[ ! -e "$runtime_dir" ]] || { echo 'Use a fresh hmi/build/release-runtime directory.' >&2; exit 1; }
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r hmi/requirements.lock --target "$runtime_dir"
python3 hmi/tools/rebuild_native_sdk.py --runtime-dir "$runtime_dir" --build-dir hmi/build/native-sdk --verify-concurrency
export PYTHONPATH="$PWD/hmi:$runtime_dir"
export LD_LIBRARY_PATH="$runtime_dir"
export QT_QPA_PLATFORM=offscreen
(cd hmi && ../.venv-hmi/bin/python -m pytest tests -q)
python3 hmi/tools/verify_setpoint_input.py --runtime-dir "$runtime_dir"
python3 hmi/tools/verify_can_support.py --runtime-dir "$runtime_dir"
python3 hmi/tools/verify_local_control.py --runtime-dir "$runtime_dir"
python3 hmi/tools/build_local_deb.py --runtime-dir "$runtime_dir"
(cd hmi/dist && sha256sum "rp1-test-hmi_$(cat ../packaging/LOCAL_VERSION)_amd64.deb" > "rp1-test-hmi_$(cat ../packaging/LOCAL_VERSION)_amd64.deb.sha256")
echo 'Package and checksums are in hmi/dist/.'
