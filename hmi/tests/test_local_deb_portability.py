"""Regressions for the source-based package used by Ubuntu 22.04 deliveries."""
import importlib.util
import json
import subprocess
import sys
import os
import runpy
from unittest.mock import patch
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


def test_installer_helper_is_fixed_root_target_without_automatic_authorization(tmp_path):
    helper = builder.stage_installer_helper(tmp_path)
    assert helper == tmp_path / "usr/lib/rp1-test-hmi/installer"
    assert helper.stat().st_mode & 0o777 == 0o755
    body = helper.read_text()
    assert 'exec /usr/bin/python3 -I -S /opt/rp1-test-hmi/hmi/factory_hmi/installer.py "$@"' in body
    assert "RP1_" not in body and "sudo" not in body
    assert not (tmp_path / "etc/rp1-test-hmi/installer-policy.json").exists()
    assert not (tmp_path / "usr/share/polkit-1/actions/com.rp1.test-hmi.installer.policy").exists()


def test_platform_url_survives_package_updates_without_rebuilding(tmp_path):
    launcher = runpy.run_path(str(ROOT / "launch.py"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/platform-url.txt").write_text("https://platform.example.invalid:8443/\n")
    with patch.dict(os.environ, {"RP1_FACTORY_PLATFORM_URL": ""}):
        env = launcher["gateway_environment"](tmp_path, None, offline=False, driver="mock", mode="control")
    assert env["RP1_FACTORY_PLATFORM_URL"] == "https://platform.example.invalid:8443"


def test_package_platform_url_default_and_explicit_override(tmp_path):
    source = (ROOT / "launch.py").read_text(encoding="utf-8")
    original, default = builder.configure_launcher_platform_url(source, "")
    assert original == source
    assert default == "https://10.43.17.214:8443"
    configured, url = builder.configure_launcher_platform_url(source, "https://platform.example.invalid:8443/")
    assert url == "https://platform.example.invalid:8443"
    launch = tmp_path / "launch.py"
    launch.write_text(configured, encoding="utf-8")
    launcher = runpy.run_path(str(launch))
    with patch.dict(os.environ, {"RP1_FACTORY_PLATFORM_URL": ""}):
        env = launcher["gateway_environment"](tmp_path, None, offline=False, driver="mock", mode="control")
    assert env["RP1_FACTORY_PLATFORM_URL"] == url


def test_offline_checks_cannot_reuse_the_saved_platform_connection(tmp_path):
    launcher = runpy.run_path(str(ROOT / "launch.py"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/platform-url.txt").write_text("https://platform.example.invalid:8443\n")
    env = launcher["gateway_environment"](tmp_path, None, offline=True, driver="mock", mode="control")
    assert env["RP1_FACTORY_PLATFORM_URL"] == ""


def test_saved_platform_url_cannot_send_login_credentials_over_plain_http(tmp_path):
    launcher = runpy.run_path(str(ROOT / "launch.py"))
    with patch.dict(os.environ, {"RP1_FACTORY_PLATFORM_URL": "http://platform.example.invalid"}):
        try:
            launcher["gateway_environment"](tmp_path, None, offline=False, driver="mock", mode="control")
        except ValueError as exc:
            assert "HTTPS" in str(exc)
        else:
            raise AssertionError("insecure platform login URL accepted")
