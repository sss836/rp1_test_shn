#!/usr/bin/env python3
"""Build a local PLC-control deb directly from this HMI checkout, without hardware actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]


def ignored(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {"__pycache__", ".venv", "secrets", "factory_data", "build", "dist"}
            or name.startswith(".env") or name.endswith((".pyc", ".csv", ".npz", ".npy", ".sqlite", ".db", ".key"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Verified offline Python 3.12 release runtime")
    parser.add_argument("--output-dir", type=Path, default=SOURCE / "dist")
    parser.add_argument("--stage-only", action="store_true", help="Assemble and inspect the payload without compressing a deb")
    args = parser.parse_args()
    runtime = args.runtime_dir.expanduser().resolve(strict=True)
    if not (runtime / "PySide6").is_dir() or not (runtime / "pymodbus").is_dir():
        raise RuntimeError("Runtime must include the verified Qt and pymodbus libraries")
    # Reject incompatible SDK/library bundles before assembling an installable package.
    probe = subprocess.run([sys.executable, str(SOURCE / "tools/verify_native_sdk.py"),
                            "--runtime-dir", str(runtime)], capture_output=True, text=True)
    if probe.returncode:
        raise RuntimeError("Native SDK verification failed:\n" + probe.stderr.strip())
    native_sdk = json.loads(probe.stdout)
    version = (SOURCE / "packaging/LOCAL_VERSION").read_text().strip()
    out = args.output_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"package-root-{version}"
    if package.exists():
        raise RuntimeError(f"Staging already exists; choose a fresh --output-dir: {package}")
    app = package / "opt/rp1-test-hmi"
    app.mkdir(parents=True)
    target_source = app / "hmi"
    for directory in ("factory_hmi", "scripts"):
        shutil.copytree(SOURCE / directory, target_source / directory, ignore=ignored)
    for name in ("can-disabled", "local-can-policy.yaml", "platform-ca.crt", "LOCAL_VERSION",
                 "can-helper", "physical-can-policy.yaml", "com.rp1.test-hmi.can.policy",
                 "rp1-test-hmi.desktop", "native-sdk-runtime.json"):
        target = target_source / "packaging" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / "packaging" / name, target)
    shutil.copy2(SOURCE / "launch.py", app / "launch.py")
    shutil.copy2(SOURCE / "packaging/user_launcher.py", app / "user_launcher.py")
    shutil.copytree(runtime, app / "runtime", symlinks=True)
    binary = package / "usr/bin/rp1-test-hmi"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexec /usr/bin/python3 /opt/rp1-test-hmi/user_launcher.py "$@"\n')
    desktop = package / "usr/share/applications/rp1-test-hmi.desktop"
    desktop.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/rp1-test-hmi.desktop", desktop)
    helper = package / "usr/lib/rp1-test-hmi/factory-hmi-can-helper"
    helper.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/can-helper", helper)
    policy = package / "etc/rp1-test-hmi/can-policy.yaml"
    policy.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/physical-can-policy.yaml", policy)
    polkit = package / "usr/share/polkit-1/actions/com.rp1.test-hmi.can.policy"
    polkit.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/com.rp1.test-hmi.can.policy", polkit)
    icon = package / "usr/share/icons/hicolor/512x512/apps/rp1-test-hmi.png"
    icon.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/debian/rp1-factory-hmi.png", icon)
    doc = package / "usr/share/doc/rp1-test-hmi"
    doc.mkdir(parents=True)
    shutil.copytree(SOURCE / "third-party-licenses", doc / "third-party-licenses")
    shutil.copy2(SOURCE / "docs/LOCAL_CONTROL_BUILD.md", doc / "README.zh_CN.md")
    for name in ("HOST_MODBUS_TCP_PROTOCOL_V1.0.md", "HOST_MODBUS_TCP_PROTOCOL_V1.1.md"):
        shutil.copy2(SOURCE / "docs" / name, doc / name)
    manifest = {
        str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(app.rglob("*")) if p.is_file() and not p.is_symlink() and app / "runtime" not in p.parents
    }
    (doc / "source-manifest.json").write_text(json.dumps({"version": version, "files_sha256": manifest}, ensure_ascii=False, indent=2) + "\n")
    debian = package / "DEBIAN"
    debian.mkdir()
    (debian / "conffiles").write_text("/etc/rp1-test-hmi/can-policy.yaml\n")
    files = [p for p in sorted(package.rglob("*")) if p.is_file() and not p.is_symlink() and debian not in p.parents]
    installed_size = (sum(p.stat().st_size for p in files) + 1023) // 1024
    (debian / "control").write_text(f"""Package: rp1-test-hmi
Version: {version}
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: {installed_size}
Maintainer: RP1 Test SHN
Depends: python3 (>= 3.12), python3 (<< 3.13), python3-yaml, iproute2, pkexec, polkitd, ca-certificates, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xinerama0, libxkbcommon-x11-0, libegl1, libgl1, libfontconfig1, libdbus-1-3, fonts-noto-cjk
Description: RP1 Modbus TCP PLC and physical SocketCAN HMI
 Protocol V1.1 reset receipts and fault sources, with V1.0 compatibility.
 Control and monitor profiles use relative YAML references.
 Local recording works without platform bench identifiers.
 Physical CAN requires administrator authentication and a root-owned allowlist.
 Installation does not start hardware or configure CAN.
""")
    (debian / "md5sums").write_text("".join(f"{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(package)}\n" for p in files))
    executables = {binary, app / "launch.py", target_source / "packaging/can-disabled", helper,
                   target_source / "packaging/can-helper"}
    for p in [package, *package.rglob("*")]:
        if p.is_symlink():
            continue
        p.chmod(0o755 if p.is_dir() or p in executables else 0o644)
    # Stable timestamp input; no current clock, hostname, credentials or runtime
    # data are written into the package. The source manifest records exact bytes.
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "1791417600"))
    for p in [package, *package.rglob("*")]:
        if not p.is_symlink():
            os.utime(p, (epoch, epoch))
    report = {"source_root": str(SOURCE), "version": version, "package_root": str(package),
              "source_file_count": len(manifest), "source_manifest": str(doc / "source-manifest.json"),
              "source_date_epoch": epoch, "stage_only": args.stage_only, "hardware_actions": [],
              "native_sdk_load_check": native_sdk}
    if not args.stage_only:
        output = out / f"rp1-test-hmi_{version}_amd64.deb"
        if output.exists():
            raise RuntimeError("Output deb already exists; do not overwrite")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", "-z6", "--threads-max=2", str(package), str(output)], check=True,
                       env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch)))
        sha = hashlib.sha256(output.read_bytes()).hexdigest()
        (out / "SHA256SUMS").write_text(f"{sha}  {output.name}\n")
        report.update(file=str(output), size_bytes=output.stat().st_size, sha256=sha)
    (out / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
