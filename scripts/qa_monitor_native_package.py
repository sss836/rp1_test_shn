#!/usr/bin/env python3
"""Install a candidate only in a network-isolated disposable Noble QA container.

Invoke with the candidate mounted read-only at /candidate and a writable
/reports directory. Never run this script on an operator's host.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


VERSION = "0.4.1+platform.20261106.1"


def main():
    if not Path("/.dockerenv").exists() or os.environ.get("RP1_DISPOSABLE_PACKAGE_QA") != "yes":
        raise RuntimeError("A disposable QA container is required; do not run on the host")
    candidates = list(Path("/candidate").glob("rp1-monitor-app_*.deb"))
    if len(candidates) != 1:
        raise RuntimeError("Mount one candidate build directory")
    package = candidates[0]
    identity = subprocess.check_output(["dpkg-deb", "-f", str(package), "Package", "Version", "Architecture"], text=True)
    assert f"Version: {VERSION}" in identity and "Package: rp1-monitor-app" in identity and "Architecture: amd64" in identity
    with tempfile.TemporaryDirectory(prefix="rp1-package-qa-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        home = root / "operator"
        home.mkdir(mode=0o700)
        os.chown(home, 1000, 1000)
        preserved = home / "existing-results.csv"
        preserved.write_bytes(b"synthetic preserved operator file\n")
        os.chown(preserved, 1000, 1000)
        original = hashlib.sha256(preserved.read_bytes()).hexdigest()
        # Only the disposable container's /opt and dpkg database are modified.
        subprocess.run(["dpkg", "-i", str(package)], check=True)
        installed = subprocess.check_output(["dpkg-query", "-W", "-f=${Version} ${Status}", "rp1-monitor-app"], text=True)
        assert installed == VERSION + " install ok installed"
        python = "/opt/rp1-monitor-app/venv/bin/python"
        probe = subprocess.check_output([python, "-c", "import importlib.metadata,json; from rp1_monitor import __version__; from PySide6.QtWidgets import QApplication; from rp1_monitor.desktop_platform import PlatformMonitorWindow; print(json.dumps([importlib.metadata.version('rp1-monitor-pc'),__version__]))"], text=True)
        assert json.loads(probe) == [VERSION, VERSION]
        state = home / "station"
        factory = home / "factory"
        screenshot = home / "packaged-login.png"
        environment = os.environ.copy()
        environment.update(HOME=str(home), XDG_DATA_HOME=str(home / "data"),
                           QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "RP1_PLATFORM_URL", "RP1_PLATFORM_CA"):
            environment.pop(key, None)

        def operator():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)

        subprocess.run([python, "-m", "rp1_monitor.pc_cli", "desktop", "--quick-connect",
                        "--station-dir", str(state), "--factory-dir", str(factory),
                        "--screenshot", str(screenshot), "--quit-after", "3"],
                       env=environment, cwd=str(home), preexec_fn=operator, check=True, timeout=30)
        assert screenshot.is_file() and screenshot.stat().st_size > 10000
        assert not (state / "station-owner.json").exists(), "no station bootstrap before login"
        assert hashlib.sha256(preserved.read_bytes()).hexdigest() == original
        reports = Path("/reports")
        reports.mkdir(exist_ok=True)
        shutil.copyfile(screenshot, reports / "packaged-login.png")
        (reports / "packaged-login.png").chmod(0o644)
    print(json.dumps({"result": "PASS", "version": VERSION,
                      "installation": "disposable Ubuntu 24.04; bundled Python wheels; network=none",
                      "gui": "installed native Qt login, offscreen, unprivileged uid=1000",
                      "prelogin_station_started": False, "operator_file_preserved": True,
                      "real_devices": False}), flush=True)


if __name__ == "__main__":
    main()
