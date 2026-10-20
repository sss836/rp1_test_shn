#!/usr/bin/env python3
"""Run only inside an isolated clean Ubuntu 22.04 container after DEB installation.

Uses mock PLC, fake CAN requests and a private state directory. No device actions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert Path("/.dockerenv").exists(), "This verifier is container-only"
    assert 'VERSION_ID="22.04"' in Path("/etc/os-release").read_text()
    assert not Path("/usr/lib/python3.12").exists(), "Clean target must not have system Python 3.12"
    app_root = Path("/opt/rp1-test-hmi")
    assert Path(sys.executable) == app_root / "python3.12"
    assert Path(sys.base_prefix) == app_root
    assert not [p for p in app_root.rglob("*") if p.is_symlink() and not p.resolve().is_relative_to(app_root)]
    sys.path[:0] = [str(app_root / "hmi"), str(app_root / "runtime")]
    import encodings, ssl, bz2, lzma, sqlite3, ctypes, _elementtree, _uuid, _ctypes, _ssl
    import yaml, pyroute2, numpy, pymodbus, fastapi
    from factory_hmi.quick_profiles import catalog, preset
    from PySide6.QtWidgets import QApplication, QWidget
    from factory_hmi.desktop.activation import restore_window

    assert str(Path(encodings.__file__).resolve()).startswith(str(app_root / "lib"))
    assert str(Path(pyroute2.__file__).resolve()).startswith(str(app_root / "runtime"))
    print("PASS bundled stdlib and dependency imports", flush=True)
    qt = QApplication([])
    window = QWidget()
    # Xvfb alone has no window manager. Minimize/restore is covered by the Qt
    # regression; here exercise the installed XCB plugin's visible window.
    window.show()
    qt.processEvents()
    restore_window(window)
    qt.processEvents()
    assert window.isVisible()
    assert qt.platformName() == "xcb", "Exercise real XCB plugin under Xvfb"
    assert str(app_root / "runtime/libxcb-cursor.so.0") in Path("/proc/self/maps").read_text()
    print("PASS XCB window and bundled cursor library", flush=True)
    window.close()
    assets = [preset(item["id"]) for item in catalog()]
    assert len(assets) == 7
    doc = Path("/usr/share/doc/rp1-test-hmi")
    verified = {}
    for filename, nested in (("source-manifest.json", True), ("runtime-manifest.json", False)):
        data = json.loads((doc / filename).read_text())
        files = data["files_sha256"] if nested else data
        for name, digest in files.items():
            assert hashlib.sha256((app_root / name).read_bytes()).hexdigest() == digest, name
        verified[filename] = len(files)
    helper = Path("/usr/lib/rp1-test-hmi/factory-hmi-can-helper")
    assert helper.read_text().startswith("#!/opt/rp1-test-hmi/python3.12 -I\n")
    # Deliberately unauthorized interface: imports pyroute2 but never calls netlink.
    poison = Path(tempfile.mkdtemp(prefix="rp1-poison-"))
    (poison / "pyroute2.py").write_text("raise RuntimeError('USER_SITE_WAS_USED')")
    result = subprocess.run([str(helper)], input='{"action":"disconnect","interfaces":["can99"]}',
                            env=dict(os.environ, PYTHONPATH=str(poison), PYTHONHOME=str(poison)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 77 and "not allowed" in result.stderr, result.stderr
    assert "USER_SITE_WAS_USED" not in result.stderr
    print("PASS isolated privileged helper with denied fake request", flush=True)
    status_request = subprocess.run([str(helper)], input='{"action":"status","interfaces":["can5"]}',
                                    text=True, capture_output=True, timeout=10)
    assert status_request.returncode == 0, status_request.stderr
    assert json.loads(status_request.stdout)["status"][0]["exists"] is False
    print("PASS helper netlink status in private namespace with no CAN devices", flush=True)
    from factory_hmi.can_helper import load_policy
    assert load_policy("/etc/rp1-test-hmi/can-policy.yaml").allowed_interfaces == frozenset(f"can{i}" for i in range(6))
    assert not Path("/etc/polkit-1/rules.d/49-rp1-test-hmi-local-admin.rules").exists()
    assert not Path("/etc/polkit-1/localauthority/50-local.d/10-rp1-test-hmi-local-admin.pkla").exists()

    with tempfile.TemporaryDirectory(prefix="rp1-jammy-") as state_dir:
        print("START private mock gateway and desktop", flush=True)
        launch = ["/usr/bin/rp1-test-hmi", "--offline-check", "--state-dir", state_dir, "--port", "18789"]
        with (args.output.parent / "clean-launch.log").open("w") as log:
            process = subprocess.Popen(launch, stdout=log, stderr=subprocess.STDOUT)
            root = Path(state_dir) / "offline-check"
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Isolated HMI exited; see clean-launch.log")
                    try:
                        with urlopen("http://127.0.0.1:18789/api/v1/health", timeout=.5) as response:
                            health = json.load(response)
                        with urlopen("http://127.0.0.1:18789/api/v1/plc/snapshot", timeout=.5) as response:
                            plc = json.load(response)
                        before = json.loads((root / "run/launcher.json").read_text())
                        if health["ok"] and Path(before["activation_socket"]).is_socket():
                            break
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError("Isolated HMI did not become ready")
                assert plc["driver"] == "mock" and plc["access_mode"] == "control"
                raised = subprocess.run(launch, text=True, capture_output=True, timeout=5)
                assert raised.returncode == 0 and "窗口恢复请求已发送" in raised.stdout, raised
                after = json.loads((root / "run/launcher.json").read_text())
                assert before["pid"] == after["pid"] and before["gateway_pid"] == after["gateway_pid"]
                assert process.poll() is None
            finally:
                stopped = subprocess.run([*launch, "stop"], capture_output=True, text=True, timeout=20)
                process.wait(timeout=15)
                for name in ("desktop.log", "gateway.log"):
                    path = root / "logs" / name
                    if path.exists():
                        (args.output.parent / f"clean-{name}").write_bytes(path.read_bytes())
            assert stopped.returncode == 0 and process.returncode == 0, stopped.stderr
            status = json.loads(subprocess.check_output([*launch, "status"], text=True))
            assert status["running"] is False
    report = dict(ok=True, os="Ubuntu 22.04", system_python312_installed=False,
                  bundled_python=sys.version, prefix=sys.base_prefix,
                  xcb_window=True, helper_isolation=True, six_channel_policy=True,
                  netlink_readonly_private_namespace=True,
                  passwordless_rule_auto_enabled=False, quick_trajectories=len(assets),
                  manifests_verified=verified, mock_gateway=True, relaunch_preserves_instance=True,
                  normal_shutdown=True, hardware_actions=[], installed_on_host=False)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
