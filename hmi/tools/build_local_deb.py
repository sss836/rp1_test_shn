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
    quick_resources = Path(_directory).resolve() == (SOURCE / "factory_hmi/resources/quick").resolve()
    approved_npz = set()
    if quick_resources:
        manifest = json.loads((Path(_directory) / "manifest.json").read_text())
        approved_npz = {item["file"] for item in manifest["trajectories"]}
    return {name for name in names if name in {"__pycache__", ".venv", "secrets", "factory_data", "build", "dist"}
            or name.startswith(".env") or name.endswith((".pyc", ".csv", ".npy", ".sqlite", ".db", ".key"))
            or (name.endswith(".npz") and name not in approved_npz)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Verified offline Python 3.12 release runtime")
    parser.add_argument("--output-dir", type=Path, default=SOURCE / "dist")
    parser.add_argument("--version", default="", help="Debian version; default is packaging/LOCAL_VERSION")
    parser.add_argument("--platform-url", default="", help="Dashboard URL written only into this package's launcher")
    parser.add_argument("--python-binary", type=Path, help="Interpreter copied into the package and used instead of system python3")
    parser.add_argument("--python-stdlib", type=Path, help="Matching complete lib/python3.12 for the bundled interpreter")
    parser.add_argument("--depends-profile", choices=("noble", "jammy"), default="noble")
    parser.add_argument("--native-provenance", type=Path, help="SDK provenance JSON for this runtime")
    parser.add_argument("--stage-only", action="store_true", help="Assemble and inspect the payload without compressing a deb")
    parser.add_argument("--compression-level", type=int, choices=range(1, 10), default=6)
    args = parser.parse_args()
    runtime = args.runtime_dir.expanduser().resolve(strict=True)
    for module in ("PySide6", "pymodbus", "pyroute2", "yaml", "numpy"):
        if not (runtime / module).is_dir():
            raise RuntimeError(f"Runtime must include {module}")
    if bool(args.python_binary) != bool(args.python_stdlib):
        raise RuntimeError("Bundled Python requires both --python-binary and --python-stdlib")
    if args.depends_profile == "jammy" and not args.python_binary:
        raise RuntimeError("Ubuntu 22.04 requires a complete bundled Python 3.12")
    if args.python_stdlib:
        stdlib = args.python_stdlib.expanduser().resolve(strict=True)
        for relative in ("encodings/__init__.py", "os.py", "lib-dynload"):
            if not (stdlib / relative).exists():
                raise RuntimeError(f"Bundled standard library is incomplete: {relative}")
        if not (runtime / "libxcb-cursor.so.0").is_file():
            raise RuntimeError("Bundled Ubuntu 22.04 runtime must include libxcb-cursor.so.0")
    quick_root = SOURCE / "factory_hmi/resources/quick"
    for item in json.loads((quick_root / "manifest.json").read_text())["trajectories"]:
        path = (quick_root / item["file"]).resolve(strict=True)
        if path.parent != quick_root.resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(f"Built-in trajectory does not match its manifest: {item['file']}")
    # Reject incompatible SDK/library bundles before assembling an installable package.
    verify_command = [sys.executable, str(SOURCE / "tools/verify_native_sdk.py"), "--runtime-dir", str(runtime)]
    if args.native_provenance:
        verify_command.extend(["--provenance", str(args.native_provenance.expanduser().resolve(strict=True))])
    probe = subprocess.run(verify_command, capture_output=True, text=True)
    if probe.returncode:
        raise RuntimeError("Native SDK verification failed:\n" + probe.stderr.strip())
    native_sdk = json.loads(probe.stdout)
    version = args.version.strip() or (SOURCE / "packaging/LOCAL_VERSION").read_text().strip()
    platform_url = args.platform_url.strip()
    if platform_url and not platform_url.startswith("https://"):
        raise RuntimeError("platform URL must start with https://")
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
    shutil.copy2(SOURCE / "pyproject.toml", target_source / "pyproject.toml")
    (target_source / "docs").mkdir()
    for name in ("NPZ_UPLOAD_LIMITS.md", "QUICK_VERIFICATION.md", "SAMPLE_SELECTION.md",
                 "WAISTHIP_SUPPORT.md", "UBUNTU22_DEPLOYMENT.md"):
        shutil.copy2(SOURCE / "docs" / name, target_source / "docs" / name)
    for name in ("can-disabled", "local-can-policy.yaml", "platform-ca.crt", "LOCAL_VERSION",
                 "can-helper", "physical-can-policy.yaml", "com.rp1.test-hmi.can.policy",
                 "rp1-test-hmi.desktop", "native-sdk-runtime.json"):
        target = target_source / "packaging" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / "packaging" / name, target)
    (target_source / "packaging/LOCAL_VERSION").write_text(version + "\n")
    if args.native_provenance:
        shutil.copy2(args.native_provenance, target_source / "packaging/native-sdk-runtime.json")
    shutil.copy2(SOURCE / "launch.py", app / "launch.py")
    if platform_url:
        launch_path = app / "launch.py"
        launch_text = launch_path.read_text(encoding="utf-8")
        old_url = '"RP1_FACTORY_PLATFORM_URL": "" if offline else "https://localhost:8443",'
        new_url = f'"RP1_FACTORY_PLATFORM_URL": "" if offline else "{platform_url}",'
        if old_url not in launch_text:
            raise RuntimeError("launcher platform URL line was not found")
        launch_path.write_text(launch_text.replace(old_url, new_url, 1), encoding="utf-8")
    shutil.copy2(SOURCE / "packaging/user_launcher.py", app / "user_launcher.py")
    shutil.copytree(runtime, app / "runtime", symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    binary = package / "usr/bin/rp1-test-hmi"
    binary.parent.mkdir(parents=True)
    if args.python_binary:
        bundled_python = app / "python3.12"
        shutil.copy2(args.python_binary.expanduser().resolve(strict=True), bundled_python)
        shutil.copytree(stdlib, app / "lib/python3.12", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages", "dist-packages",
                                                     "sitecustomize.py", "usercustomize.py"))
        binary.write_text(
            '#!/bin/sh\n'
            'export LD_LIBRARY_PATH="/opt/rp1-test-hmi/runtime${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
            'exec /opt/rp1-test-hmi/python3.12 /opt/rp1-test-hmi/user_launcher.py "$@"\n'
        )
    else:
        binary.write_text('#!/bin/sh\nexec /usr/bin/python3 /opt/rp1-test-hmi/user_launcher.py "$@"\n')
    desktop = package / "usr/share/applications/rp1-test-hmi.desktop"
    desktop.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/rp1-test-hmi.desktop", desktop)
    helper = package / "usr/lib/rp1-test-hmi/factory-hmi-can-helper"
    helper.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "packaging/can-helper", helper)
    if args.python_binary:
        for path in (helper, target_source / "packaging/can-helper"):
            body = path.read_text().split("\n", 1)[1]
            path.write_text("#!/opt/rp1-test-hmi/python3.12 -I\n" + body)
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
    shutil.copy2(SOURCE / "docs/UBUNTU22_DEPLOYMENT.md", doc)
    examples = doc / "examples"
    examples.mkdir()
    for name in ("10-rp1-test-hmi-local-admin.pkla", "49-rp1-test-hmi-local-admin.rules"):
        shutil.copy2(SOURCE / "packaging" / name, examples / name)
    for path in app.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
            raise RuntimeError(f"Package contains an external symlink: {path.relative_to(app)}")
    for name in ("HOST_MODBUS_TCP_PROTOCOL_V1.0.md", "HOST_MODBUS_TCP_PROTOCOL_V1.1.md",
                 "HOST_MODBUS_TCP_PROTOCOL_V1.2.md", "MOTOR_FAULT_RECOVERY.md"):
        shutil.copy2(SOURCE / "docs" / name, doc / name)
    shutil.copy2(SOURCE / "docs/PLC_CONTRACT_CHECKLIST.md", doc)
    shutil.copy2(SOURCE.parent / "docs/TEST-DATA-APPROVAL.md", doc)
    manifest = {
        str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(app.rglob("*")) if p.is_file() and not p.is_symlink()
        and app / "runtime" not in p.parents and app / "lib" not in p.parents
    }
    (doc / "source-manifest.json").write_text(json.dumps({"version": version, "files_sha256": manifest}, ensure_ascii=False, indent=2) + "\n")
    runtime_manifest = {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for directory in (app / "runtime", app / "lib") if directory.is_dir()
                        for p in sorted(directory.rglob("*")) if p.is_file() and not p.is_symlink()}
    (doc / "runtime-manifest.json").write_text(json.dumps(runtime_manifest, indent=2) + "\n")
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
Depends: {("iproute2, policykit-1, ca-certificates, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xinerama0, libxkbcommon-x11-0, libegl1, libgl1, libfontconfig1, libdbus-1-3, fonts-noto-cjk, zlib1g, libexpat1, libbz2-1.0, liblzma5, libssl3, libffi8, libsqlite3-0, libreadline8, libncursesw6, libgdbm6, libgdbm-compat4, libuuid1, tk8.6" if args.depends_profile == "jammy" else "python3 (>= 3.12), python3 (<< 3.13), python3-yaml, iproute2, pkexec, polkitd, ca-certificates, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xinerama0, libxkbcommon-x11-0, libegl1, libgl1, libfontconfig1, libdbus-1-3, fonts-noto-cjk")}
Description: RP1 Modbus TCP PLC and physical SocketCAN HMI
 Protocol V1.2 control with V1.0 and V1.1 monitoring only.
 Motor fault reset requires fresh feedback and keeps motors disabled.
 Explicitly verified records upload immutable CSV through reliability_v1 archives.
 Metadata corrections and result analysis retain append-only versions.
 Formal statistics include only classified and approved executions.
 Control and monitor profiles use relative YAML references.
 Local recording works without platform bench identifiers.
 Physical CAN requires administrator authentication and a root-owned allowlist.
 Installation does not start hardware or configure CAN.
""")
    (debian / "md5sums").write_text("".join(f"{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(package)}\n" for p in files))
    executables = {binary, app / "launch.py", app / "python3.12", target_source / "packaging/can-disabled", helper,
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
              "depends_profile": args.depends_profile, "bundled_python": bool(args.python_binary),
              "bundled_stdlib": bool(args.python_stdlib), "runtime_file_count": len(runtime_manifest),
              "compression_level": args.compression_level,
              "native_sdk_load_check": native_sdk, "platform_url": platform_url or "https://localhost:8443"}
    if not args.stage_only:
        output = out / f"rp1-test-hmi_{version}_amd64.deb"
        if output.exists():
            raise RuntimeError("Output deb already exists; do not overwrite")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", f"-z{args.compression_level}", "--threads-max=2", str(package), str(output)], check=True,
                       env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch)))
        sha = hashlib.sha256(output.read_bytes()).hexdigest()
        (out / "SHA256SUMS").write_text(f"{sha}  {output.name}\n")
        report.update(file=str(output), size_bytes=output.stat().st_size, sha256=sha)
    (out / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
