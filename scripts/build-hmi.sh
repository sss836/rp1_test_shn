#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || {
  echo 'Build on Ubuntu 24.04 x86_64; do not ship an incompatible Python/CPU binary.' >&2; exit 1;
}
unset PYTHONPATH
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r hmi/requirements.lock
.venv-hmi/bin/python -m pip install --no-deps --no-build-isolation -e './hmi[desktop,test,build]'
# Run from the HMI package root so its scripts package cannot be shadowed by
# the delivery repository's top-level scripts directory in an editable install.
(cd hmi && QT_QPA_PLATFORM=offscreen ../.venv-hmi/bin/python -m pytest tests -q)
.venv-hmi/bin/python hmi/tools/build_factory_hmi.py all --one-dir
.venv-hmi/bin/python hmi/tools/build_factory_hmi_deb.py --version "$(cat VERSION)+ubuntu24.04"
(cd hmi/dist && sha256sum "rp1-test-hmi_$(cat ../../VERSION)+ubuntu24.04_amd64.deb" > "rp1-test-hmi_$(cat ../../VERSION)+ubuntu24.04_amd64.deb.sha256")
echo 'Package and SHA256 are in hmi/dist/.'
