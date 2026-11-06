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


VERSION = os.environ.get("RP1_QA_PACKAGE_VERSION", "0.4.1+platform.20261106.2")


def main():
    if not Path("/.dockerenv").exists() or os.environ.get("RP1_DISPOSABLE_PACKAGE_QA") != "yes":
        raise RuntimeError("A disposable QA container is required; do not run on the host")
    assert {path.name for path in Path("/sys/class/net").iterdir()} == {"lo"}, "use docker --network none"
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
        runtime = home / "qt-runtime"
        runtime.mkdir(mode=0o700)
        os.chown(runtime, 1000, 1000)
        environment = os.environ.copy()
        environment.update(HOME=str(home), XDG_DATA_HOME=str(home / "data"),
                           XDG_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "RP1_PLATFORM_URL", "RP1_PLATFORM_CA", "LD_LIBRARY_PATH", "LD_PRELOAD",
                    "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            environment.pop(key, None)
        probe = json.loads(subprocess.check_output([python, "-c", "import importlib.metadata,json,rp1_monitor,hashlib; from pathlib import Path; import rp1_monitor.desktop_platform as ui; from PySide6.QtWidgets import QApplication; root=Path('/opt/rp1-monitor-app/venv').resolve(); paths=[Path(rp1_monitor.__file__).resolve(),Path(ui.__file__).resolve()]; assert all(p.is_relative_to(root) and 'site-packages' in p.parts for p in paths); gui_hash=hashlib.sha256(paths[1].read_bytes()).hexdigest(); assert gui_hash==hashlib.sha256(Path('/opt/rp1-monitor-app/source/src/rp1_monitor/desktop_platform.py').read_bytes()).hexdigest(); print(json.dumps({'versions':[importlib.metadata.version('rp1-monitor-pc'),rp1_monitor.__version__],'module_paths':[str(p) for p in paths],'gui_source_sha256':gui_hash}))"], env=environment, cwd=str(home), text=True))
        assert probe["versions"] == [VERSION, VERSION]
        state = home / "station"
        factory = home / "factory"
        screenshot = home / "packaged-local-unlogged.png"

        def operator():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)

        subprocess.run([python, "-m", "rp1_monitor.pc_cli", "desktop", "--factory",
                        "--station-dir", str(state), "--factory-dir", str(factory),
                        "--screenshot", str(screenshot), "--quit-after", "3"],
                       env=environment, cwd=str(home), preexec_fn=operator, check=True, timeout=30)
        assert screenshot.is_file() and screenshot.stat().st_size > 10000
        assert not (state / "station-owner.json").exists(), "factory shortcut does not bootstrap station"
        assert not state.with_name(state.name + "-platform").exists(), "local use must not initialize platform storage"
        local_probe = r'''
import json,sys,time
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication,QLineEdit
from rp1_monitor.desktop_platform import PlatformMonitorWindow
home=Path(sys.argv[1])
blocked=home/'unavailable-platform-storage'
blocked.write_text('synthetic non-directory platform path')
app=QApplication([])
window=PlatformMonitorWindow('http://127.0.0.1:1',factory_directory=home/'probe-factory',platform_state_directory=blocked)
window.show()
window.start_initial_connection(SimpleNamespace(factory=True))
app.processEvents()
assert not window._valid_session()
assert window.platform_stack.currentWidget() is window.local_workbench
assert window.local_workbench.isEnabled() and window.factory_panel.isVisible()
assert window.username_edit.isVisible() and window.password_edit.isVisible() and window.login_button.isVisible()
assert window.password_edit.echoMode()==QLineEdit.EchoMode.Password
assert blocked.read_text()=='synthetic non-directory platform path'
window.close()
end=time.monotonic()+5
while window.isVisible() and time.monotonic()<end:
 app.processEvents(); time.sleep(.01)
assert not window.isVisible()
window.finish_platform_workers()
print(json.dumps({'local_without_login':True,'top_login_bar_visible':True,'unavailable_platform_storage_did_not_block_local':True}))
'''
        local_result = json.loads(subprocess.check_output([python, "-c", local_probe, str(home)],
                                 env=environment, cwd=str(home), preexec_fn=operator, text=True, timeout=30))
        assert hashlib.sha256(preserved.read_bytes()).hexdigest() == original
        reports = Path("/reports")
        reports.mkdir(exist_ok=True)
        shutil.copyfile(screenshot, reports / "packaged-local-unlogged.png")
        (reports / "packaged-local-unlogged.png").chmod(0o644)
    print(json.dumps({"result": "PASS", "version": VERSION,
                      "installation": "disposable Ubuntu 24.04; bundled Python wheels; network=none",
                      "gui": "installed native Qt local factory workbench without login; top platform login bar; uid=1000",
                      "startup_mode": "factory", "platform_state_created": False,
                      "installed_module_paths": probe["module_paths"],
                      "installed_gui_source_sha256": probe["gui_source_sha256"],
                      "optional_login_checks": local_result,
                      "prelogin_station_started": False, "operator_file_preserved": True,
                      "real_devices": False}), flush=True)


if __name__ == "__main__":
    main()
