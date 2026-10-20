#!/bin/sh
# Build a Python 3.12 HMI runtime inside Ubuntu 22.04 so its libraries stay within glibc 2.35.
set -eu
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl software-properties-common gnupg \
  build-essential cmake pkg-config git binutils \
  libboost-dev libboost-system-dev libeigen3-dev \
  zlib1g-dev libexpat1-dev libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 \
  libxcb-shape0 libxcb-xinerama0 libxkbcommon-x11-0 libegl1 libgl1 \
  libfontconfig1 libdbus-1-3 fonts-noto-cjk tk8.6 xvfb xauth
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3.12 python3.12-dev python3.12-venv
python3.12 -m ensurepip --upgrade
python3.12 -m pip install --upgrade pip
python3.12 -m pip install 'pybind11==2.13.6'

prefix=/opt/jammy-deps
mkdir -p /tmp/src
curl -fsSL -o /tmp/fmt.tar.gz https://github.com/fmtlib/fmt/archive/refs/tags/9.1.0.tar.gz
curl -fsSL -o /tmp/spdlog.tar.gz https://github.com/gabime/spdlog/archive/refs/tags/v1.12.0.tar.gz
tar -xzf /tmp/fmt.tar.gz -C /tmp/src
tar -xzf /tmp/spdlog.tar.gz -C /tmp/src
cmake -S /tmp/src/fmt-9.1.0 -B /tmp/fmt -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" -DFMT_DOC=OFF -DFMT_TEST=OFF -DBUILD_SHARED_LIBS=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON
cmake --build /tmp/fmt --target install --parallel 2
cmake -S /tmp/src/spdlog-1.12.0 -B /tmp/spdlog -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DSPDLOG_FMT_EXTERNAL=ON -DSPDLOG_BUILD_EXAMPLE=OFF -DBUILD_SHARED_LIBS=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_PREFIX_PATH="$prefix"
cmake --build /tmp/spdlog --target install --parallel 2

export LD_LIBRARY_PATH="$prefix/lib"
pybind_dir=$(python3.12 -m pybind11 --cmakedir)
cmake -S /src/motors -B /tmp/motors -DCMAKE_BUILD_TYPE=Release -DRP1_NATIVE_OPTIMIZATION=OFF \
  -DRP1_BUILD_SDK_TESTS=OFF -DCMAKE_PREFIX_PATH="$prefix" -Dpybind11_DIR="$pybind_dir" -DCMAKE_CXX_FLAGS="-I$prefix/include" \
  -DBoost_NO_BOOST_CMAKE=ON \
  -DPython3_EXECUTABLE=/usr/bin/python3.12 \
  -DPython3_INCLUDE_DIR=/usr/include/python3.12 \
  -DPython3_LIBRARY=/usr/lib/x86_64-linux-gnu/libpython3.12.so \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH='$ORIGIN'
cmake --build /tmp/motors --target motors_py --parallel 2

runtime=/out/runtime
test ! -e "$runtime"
mkdir -p "$runtime"
python3.12 -m pip install --target "$runtime" --upgrade \
  'PySide6==6.8.3' 'pymodbus==3.8.6' 'PyYAML==6.0.2' 'numpy==2.2.6' \
  'fastapi==0.115.12' 'uvicorn==0.34.2' 'pydantic==2.11.4' 'python-dotenv==1.1.0' 'websockets==15.0.1' \
  'pyroute2==0.9.4'
cp -a /tmp/motors/motors_py.cpython-312-*.so "$runtime/"
cp -aL "$prefix/lib/libspdlog.so.1.12" "$prefix/lib/libfmt.so.9" "$runtime/"
cp -a /usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0 "$runtime/"
cp -a /usr/bin/python3.12 /out/python3.12
chmod 755 /out/python3.12
mkdir -p /out/lib
cp -a /usr/lib/python3.12 /out/lib/python3.12
rm -f /out/lib/python3.12/sitecustomize.py /out/lib/python3.12/usercustomize.py
# Keep the standard library with the executable, including encodings and C modules.
# Python -I discovers this sibling lib/ prefix without PYTHONHOME or user site.
find /out/lib/python3.12 -type d -name __pycache__ -prune -exec rm -rf '{}' +
for name in libxcb-cursor.so.0 libxcb-util.so.1 libxcb-image.so.0 libxcb-render-util.so.0; do
  cp -aL "/usr/lib/x86_64-linux-gnu/$name" "$runtime/$name"
done

python3.12 - <<'PY'
import hashlib, json
from pathlib import Path
source = Path('/src')
paths = [p for p in (source / 'motors').rglob('*') if p.is_file()
         and (p.suffix in {'.cpp', '.hpp', '.h', '.cmake', '.xml'} or p.name == 'CMakeLists.txt')
         and not any(part in {'build', '__pycache__', '.git'} for part in p.parts)]
runtime = Path('/out/runtime')
libraries = [next(runtime.glob('motors_py.cpython-312-*.so')).name, 'libspdlog.so.1.12', 'libfmt.so.9']
report = dict(sdk_source_files_sha256={str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},
              libraries_sha256={n: hashlib.sha256((runtime/n).read_bytes()).hexdigest() for n in libraries},
              sdk_built_from='hmi/motors', python='3.12', spdlog='1.12', fmt='9',
              native_optimization=False, runtime_search_path='$ORIGIN', hardware_actions=[])
Path('/out/native-sdk-runtime.json').write_text(json.dumps(report, indent=2) + '\n')
PY

python3.12 - <<'PY'
import os, subprocess
root = "/out/runtime"
bad = []
for dirpath, _, names in os.walk(root):
    for name in names:
        if ".so" not in name:
            continue
        path = os.path.join(dirpath, name)
        try:
            out = subprocess.check_output(["objdump", "-T", path], text=True, errors="replace")
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            if "GLIBC_" not in line:
                continue
            ver = line.split("GLIBC_")[-1].split()[0].rstrip(")")
            parts = []
            for piece in ver.split(".")[:3]:
                if piece.isdigit():
                    parts.append(int(piece))
                else:
                    parts = []
                    break
            if tuple(parts) >= (2, 36):
                bad.append((ver, os.path.relpath(path, root)))
if bad:
    raise SystemExit("library needs glibc newer than Ubuntu 22.04:\n" + "\n".join(map(str, sorted(set(bad))[:30])))
print("glibc check ok", len(list(os.walk(root))))
PY
echo "jammy runtime ready"
