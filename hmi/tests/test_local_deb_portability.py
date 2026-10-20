"""Regressions for the source-based package used by Ubuntu 22.04 deliveries."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("local_deb_builder", ROOT / "tools/build_local_deb.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_only_manifested_factory_trajectories_can_enter_package():
    root = ROOT / "factory_hmi/resources/quick"
    names = [item["file"] for item in json.loads((root / "manifest.json").read_text())["trajectories"]]
    ignored = builder.ignored(str(root), [*names, "recording.npz", "run.csv", ".env", "private.key"])
    assert not set(names) & ignored
    assert {"recording.npz", "run.csv", ".env", "private.key"} <= ignored
    assert "recording.npz" in builder.ignored(str(ROOT / "scripts"), ["recording.npz"])


def test_launcher_bootstraps_yaml_without_system_or_user_site(tmp_path):
    launcher = tmp_path / "launch.py"
    launcher.write_bytes((ROOT / "launch.py").read_bytes())
    (tmp_path / "runtime").mkdir()
    # status never parses YAML: this minimal module represents the bundled
    # dependency while -S removes the system site that previously hid the bug.
    (tmp_path / "runtime/yaml.py").write_text("BUNDLED_YAML = True\n")
    result = subprocess.run([sys.executable, "-I", "-S", str(launcher), "status",
                             "--state-dir", str(tmp_path / "state")],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["running"] is False
