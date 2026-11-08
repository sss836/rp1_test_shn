"""Exercise disposable-runner boundaries without invoking Docker or devices."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

import pytest


RUNNER = Path(__file__).resolve().parents[2] / "scripts/test-assistant-isolation.py"


def run_diagnosis(report, *, skipped=False):
    commands = []
    generated = {}

    def fake_run(command, *, env, **kwargs):
        # These values would override --env-file if the runner inherited them.
        assert env["QA_ARCHIVE_ROOT"] != "/unrelated/site/archives"
        assert env["QA_BRIDGE_SECRET_FILE"] != "/unrelated/site/bridge"
        assert env["QA_REPORT_DIR"] == str(report)
        assert Path(env["QA_ARCHIVE_ROOT"]).parent.name.startswith("rp1-assistant-qa-")
        assert Path(env["QA_BRIDGE_SECRET_FILE"]).parent == Path(env["QA_ARCHIVE_ROOT"]).parent
        assert env["QA_API_IMAGE"] == env["QA_MIGRATE_IMAGE"] == "synthetic-explicit-qa-image"
        assert not {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"}.intersection(env)
        generated.update({key: env[key] for key in ("QA_ARCHIVE_ROOT", "QA_BRIDGE_SECRET_FILE")})
        commands.append(command)
        output = ""
        if command[:2] == ["docker", "inspect"]:
            def container(name, network, *, pi=False):
                return {"Name": name, "Image": "synthetic-image-id",
                        "Config": {"User": "node" if pi else "postgres", "Env": []},
                        "NetworkSettings": {"Networks": {network: {}}},
                        "HostConfig": {"PortBindings": {}, "ReadonlyRootfs": pi,
                                       "CapDrop": ["ALL"] if pi else []},
                        "Mounts": [{"Destination": "/run/secrets/bridge"}] if pi else []}
            output = json.dumps([container("/synthetic-pi-1", "synthetic-tools", pi=True),
                                 container("/synthetic-postgres-1", "synthetic-database")])
        elif "ps" in command:
            output = "synthetic-pi\nsynthetic-postgres\n"
        elif command[-3:] == ["run", "--rm", "checks"]:
            skip = "<skipped/>" if skipped else ""
            cases = "".join(f'<testcase name="diagnosis-{index}">{skip if index == 0 else ""}</testcase>'
                            for index in range(4))
            (report / "backend-junit.xml").write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        return subprocess.CompletedProcess(command, 0, output)

    host = {"QA_ARCHIVE_ROOT": "/unrelated/site/archives",
            "QA_BRIDGE_SECRET_FILE": "/unrelated/site/bridge",
            "QA_REPORT_DIR": "/unrelated/site/reports",
            "QA_API_IMAGE": "synthetic-explicit-qa-image",
            "QA_MIGRATE_IMAGE": "synthetic-explicit-qa-image",
            "COMPOSE_FILE": "/unrelated/site/compose.yml",
            "COMPOSE_PROJECT_NAME": "unrelated-site", "COMPOSE_PROFILES": "live"}
    with patch.dict(os.environ, host), patch.object(sys, "argv", [str(RUNNER), "--diagnosis-checks", "--report-dir", str(report)]), \
            patch("subprocess.run", side_effect=fake_run), patch("signal.signal"):
        runpy.run_path(str(RUNNER), run_name="__main__")
    assert any("down" in command for command in commands)
    assert all(not Path(path).exists() for path in generated.values())
    return commands


def test_diagnosis_runner_owns_generated_paths_despite_exported_host_values(tmp_path):
    run_diagnosis(tmp_path)
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "checks_passed"


def test_diagnosis_runner_refuses_a_green_exit_with_skipped_regressions(tmp_path):
    with pytest.raises(AssertionError, match="cannot pass by skipping"):
        run_diagnosis(tmp_path, skipped=True)
    assert not (tmp_path / "result.json").exists()
    assert (tmp_path / "stack-cleanup.txt").exists()


def test_diagnosis_runner_rejects_live_mode_before_any_docker_operation(tmp_path):
    with patch.object(sys, "argv", [str(RUNNER), "--diagnosis-checks", "--report-dir", str(tmp_path),
                                    "--deepseek-key-file", "/unrelated/site/live-key"]), \
            patch("subprocess.run") as docker, patch("signal.signal"):
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(RUNNER), run_name="__main__")
    assert stopped.value.code == 2
    docker.assert_not_called()
