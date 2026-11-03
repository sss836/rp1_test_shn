"""Full installer transactions with inert DEBs and an explicit fake OS executor.

No test invokes SystemExecutor.install/normal_exit/start or a real device.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
from unittest.mock import Mock

import pytest

from factory_hmi import installer, updates
from test_software_updater import releases, HOST, command


def test_real_restart_command_does_not_force_monitor_or_offline_profile(tmp_path, monkeypatch):
    from types import SimpleNamespace
    executor = installer.SystemExecutor()
    launch = Mock()
    monkeypatch.setattr(installer.subprocess, 'Popen', launch)
    monkeypatch.setattr(executor, '_launcher', lambda policy, *args: ['launcher', *args])
    monkeypatch.setattr(executor, '_environment', lambda policy: {})
    executor.start(SimpleNamespace(run_uid=1000, run_gid=1000),
                   {'gateway': 'http://127.0.0.1:8766'}, tmp_path / 'restart.log')
    command = launch.call_args.args[0]
    assert command == ['launcher', 'start', '--port', '8766']
    assert launch.call_args.kwargs['user'] == 1000


def test_fresh_production_defaults_are_real_control_and_do_not_seed_completion_fixtures(tmp_path):
    import yaml
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'launch.py'))
    namespace['seed_configs'](tmp_path, offline=False, offline_access='control')
    config = yaml.safe_load((tmp_path / 'config/plc_cabinet.yaml').read_text())
    assert config['driver'] == 'modbus_tcp' and config['access_mode'] == 'control'
    for station in (tmp_path / 'config/stations').glob('*.yaml'):
        raw = yaml.safe_load(station.read_text())
        assert 'motion_completion' not in raw.get('factory_hmi', {})


class FakeExecutor:
    def __init__(self, policy, *, fail=None):
        self.policy = policy
        self.fail = fail
        self.calls = []
        self.version = "2.0.12"
        self.clock = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
        self.running = True
        self.locked = False
        self.started = False
        self.dependencies = 0

    def authorize(self, policy):
        self.calls.append("authorize")
        if self.fail == "authorize":
            raise ValueError("synthetic OS authorization denied")

    def now(self):
        return self.clock

    def boot_id(self):
        return "isolated-test-boot"

    def installed_version(self):
        return self.version

    def status(self, policy):
        return {"running": self.running, "state_root": str(policy.state_root),
                "pid": 101, "start": "1234", "gateway_pid": 102,
                "gateway": "http://127.0.0.1:8766", "update_safe_exit": True}

    def check_bound_processes(self, policy, status, claims):
        self.calls.append("identities")
        if claims["launcher"] != {"pid": 101, "start_ticks": "1234"} or claims["gateway"] != {"pid": 102, "start_ticks": "1235"}:
            raise ValueError("synthetic process identity mismatch")

    def check_dependencies(self, package, log):
        self.dependencies += 1
        self.calls.append("dependencies")
        Path(log).write_text("SIMULATED DEPENDENCY CHECK ONLY\n")
        if self.fail == "dependencies":
            raise RuntimeError("synthetic dependency failure")
        if self.fail == "proof-expires-final" and self.dependencies == 2:
            self.clock += timedelta(seconds=61)

    def normal_exit(self, policy, claims):
        self.calls.append("exit")
        if self.fail == "exit":
            raise RuntimeError("synthetic normal exit timeout; no kill")
        self.running = False

    @contextmanager
    def launcher_lock(self, policy):
        self.calls.append("lock")
        if self.fail == "lock":
            raise ValueError("synthetic old lock still held")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False
            self.calls.append("unlock")

    def confirm_bound_exit(self, claims):
        assert self.locked
        if self.running:
            raise ValueError("synthetic old process still running")

    def install(self, package, log):
        assert self.locked and not self.running
        assert self.policy.spool_root in package.parents
        self.calls.append("install")
        Path(log).write_text("SIMULATED INSTALL ONLY\n")
        if self.fail == "install":
            raise RuntimeError("synthetic installer failed")
        self.version = "2.0.13"
        if self.fail == "partial-install":
            raise RuntimeError("synthetic failure after package metadata changed")

    def start(self, policy, old_status, log):
        assert not self.locked
        self.calls.append("start-normal")
        self.started = True
        Path(log).write_text("SIMULATED NORMAL RESTART ONLY\n")

    def verify_new_instance(self, policy, manifest, previous):
        self.calls.append("verify")
        assert self.started and previous == {"pid": 102, "start_ticks": "1235"}
        if self.fail == "verify":
            raise RuntimeError("synthetic stale health version")
        return {"running_version": self.version, "pid": 202, "start_ticks": "2235"}


@pytest.fixture
def transaction(releases, tmp_path, monkeypatch):
    make, _, release_key = releases
    previous, previous_package = make("2.0.12")
    release, package = make("2.0.13")
    release_path = tmp_path / "target-release.json"
    previous_path = tmp_path / "previous-release.json"
    release_path.write_text(json.dumps(release))
    previous_path.write_text(json.dumps(previous))
    safety_private = tmp_path / "synthetic-safety-private.pem"
    safety_key = tmp_path / "synthetic-safety-public.pem"
    command(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(safety_private)])
    command(["openssl", "pkey", "-in", str(safety_private), "-pubout", "-out", str(safety_key)])
    safety_key.chmod(0o644)
    state = tmp_path / "state"
    (state / "run").mkdir(parents=True, mode=0o700)
    (state / "config").mkdir()
    (state / "data").mkdir()
    (state / "config/saved.yaml").write_text("driver: mock\n")
    (state / "data/record.csv").write_text("original measurement evidence\n")
    policy = installer.InstallerPolicy(state, tmp_path / "spool", release_key, safety_key,
                                       tmp_path / "inert-launcher", Path("/inert-python"), os.getuid(), os.getgid())
    fake = FakeExecutor(policy)
    proof_id = "a" * 32
    claims = {"schema_version": installer.PROOF_SCHEMA, "proof_id": proof_id,
              "release_id": release["manifest"]["release_id"], "package_sha256": release["manifest"]["sha256"],
              "state_root": str(state), "observed_at": fake.clock.isoformat(),
              "expires_at": (fake.clock + timedelta(seconds=60)).isoformat(), "boot_id": fake.boot_id(),
              "launcher": {"pid": 101, "start_ticks": "1234"}, "gateway": {"pid": 102, "start_ticks": "1235"},
              "requester": {"subject": "isolated-operator", "role": "TEST_EXECUTOR"},
              "control_inhibited": True, "quiescence_token": proof_id, "all_windows_checked": True,
              "windows": [{"owner": "window-1", "motion_active": False, "motors_disabled_confirmed": True, "feedback_fresh": True}],
              "plc": {"driver": "modbus_tcp", "communication_state": "LIVE", "stale": False,
                      "normal_all_stop_confirmed": True, "feedback_fresh": True, "main_ready": False,
                      "channel_permit": [False] * 4}}
    def sign(value):
        return installer.sign_safety_proof(value, private_key_path=safety_private)
    proof = sign(claims)
    # Any accidental fallback to actual system operations makes the test fail.
    for name in ("install", "normal_exit", "start"):
        monkeypatch.setattr(installer.SystemExecutor, name, Mock(side_effect=AssertionError("REAL OS EXECUTOR MUST NEVER RUN")))
    return {"release_path": release_path, "state_dir": state, "proof": proof,
            "policy": policy, "executor": fake, "host": HOST, "previous_release_path": previous_path}, sign


def test_complete_signed_transaction_verifies_new_instance_and_preserves_data(transaction):
    arguments, _ = transaction
    state = arguments["state_dir"]
    before = {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()}
    result = updates.protected_install(arguments.pop("release_path"), restart=True, **arguments)
    fake = arguments["executor"]
    assert result["status"] == "verified" and result["verified"] is True
    assert fake.calls == ["authorize", "dependencies", "identities", "exit", "lock", "dependencies", "install", "unlock", "start-normal", "verify"]
    assert Path(result["previous_package_path"]).exists()
    assert Path(result["log_path"]).exists()
    assert json.loads(Path(result["receipt_path"]).read_text())["status"] == "verified"
    assert result["hardware_validated"] is False
    assert {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()} == before


def test_default_install_does_not_restart_or_claim_new_code_running(transaction):
    arguments, _ = transaction
    result = installer.execute_install_transaction(**arguments)
    assert result["status"] == "installed_pending_restart"
    assert result["verified"] is False
    assert "install" in arguments["executor"].calls
    assert "start-normal" not in arguments["executor"].calls


def test_signed_preflight_is_ready_without_consuming_proof_or_stopping(transaction):
    arguments, _ = transaction
    result = updates.install_preflight(arguments.pop("release_path"), public_key_path=arguments["policy"].release_public_key,
                                       **arguments)
    assert result["ready"] is True and result["status"] == "ready_to_exit"
    assert arguments["executor"].calls == ["dependencies", "identities"]
    assert not arguments["policy"].spool_root.exists()


def test_dependency_simulation_cannot_hide_unavailable_packages(tmp_path, monkeypatch):
    executor = installer.SystemExecutor()
    def simulation(argv, log, timeout):
        Path(log).write_text("Inst unrelated-library (1.0 Ubuntu)\nInst rp1-test-hmi (2.0.13 local)\n")
    monkeypatch.setattr(executor, "_command_log", simulation)
    with pytest.raises(ValueError, match="依赖"):
        executor.check_dependencies(tmp_path / "inert.deb", tmp_path / "simulation.log")


@pytest.mark.parametrize("failure", ["dependencies", "exit", "lock", "install", "partial-install", "verify", "proof-expires-final"])
def test_failures_keep_logs_recovery_and_never_rollback_or_force_stop(transaction, failure):
    arguments, _ = transaction
    fake = arguments["executor"]
    fake.fail = failure
    result = installer.execute_install_transaction(**arguments, restart=True)
    assert result["status"] == "failed"
    assert Path(result["receipt_path"]).exists() and Path(result["log_path"]).exists()
    assert Path(result["previous_package_path"]).exists()
    assert "kill" not in fake.calls and "rollback" not in fake.calls
    if failure in {"dependencies", "exit", "lock", "proof-expires-final"}:
        assert "install" not in fake.calls
    if failure == "proof-expires-final":
        assert "过期" in result["error"] and fake.dependencies == 2
    if failure == "partial-install":
        assert result["installed_version_after"] == "2.0.13" and "start-normal" not in fake.calls
    assert (arguments["state_dir"] / "config/saved.yaml").read_text() == "driver: mock\n"


@pytest.mark.parametrize("mutation", ["bare", "signature", "hash", "root", "boot", "expired", "future", "ttl", "pid", "boolean", "mock", "duplicate", "role", "latch", "nonce"])
def test_signed_safety_contract_denies_wrong_or_untrusted_evidence(transaction, mutation):
    arguments, sign = transaction
    claims = deepcopy(arguments["proof"]["claims"])
    if mutation == "hash": claims["package_sha256"] = "f" * 64
    elif mutation == "root": claims["state_root"] += "-other"
    elif mutation == "boot": claims["boot_id"] = "old-boot"
    elif mutation == "expired": claims["observed_at"] = "2026-10-28T08:58:00+00:00"; claims["expires_at"] = "2026-10-28T08:59:00+00:00"
    elif mutation == "future": claims["observed_at"] = "2026-10-28T09:01:00+00:00"; claims["expires_at"] = "2026-10-28T09:02:00+00:00"
    elif mutation == "ttl": claims["expires_at"] = "2026-10-28T09:01:01+00:00"
    elif mutation == "pid": claims["launcher"]["start_ticks"] = "9999"
    elif mutation == "boolean": claims["windows"][0]["motors_disabled_confirmed"] = 1
    elif mutation == "mock": claims["plc"]["driver"] = "mock"
    elif mutation == "duplicate": claims["windows"] *= 2
    elif mutation == "role": claims["requester"]["role"] = "VIEWER"
    elif mutation == "latch": claims["control_inhibited"] = False
    elif mutation == "nonce": claims["quiescence_token"] = "b" * 32
    arguments["proof"] = sign(claims)
    if mutation == "bare": arguments["proof"] = claims
    if mutation == "signature": arguments["proof"]["signature"] = "AA=="
    result = installer.execute_install_transaction(**arguments)
    assert result["status"] == "failed"
    assert "exit" not in arguments["executor"].calls and "install" not in arguments["executor"].calls


def test_proof_is_single_use_and_requires_actual_installed_previous_package(transaction):
    arguments, _ = transaction
    first = installer.execute_install_transaction(**arguments)
    assert first["status"] == "installed_pending_restart"
    arguments["executor"] = FakeExecutor(arguments["policy"])
    again = installer.execute_install_transaction(**arguments)
    assert again["status"] == "failed" and "exit" not in arguments["executor"].calls
    arguments["executor"] = FakeExecutor(arguments["policy"])
    arguments["previous_release_path"] = None
    missing = installer.execute_install_transaction(**arguments)
    assert missing["status"] == "failed" and "上一版" in missing["error"]


def test_os_authorization_and_fixed_policy_cannot_be_replaced_by_cli_claims(transaction):
    arguments, _ = transaction
    arguments["executor"].fail = "authorize"
    with pytest.raises(ValueError, match="authorization"):
        installer.execute_install_transaction(**arguments)
    arguments["executor"] = None
    with pytest.raises(ValueError, match="固定路径"):
        installer.execute_install_transaction(**arguments)


def test_pidfd_signal_is_bound_before_rechecking_process(monkeypatch):
    executor = installer.SystemExecutor()
    calls = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid: calls.append(("open", pid)) or 55)
    monkeypatch.setattr(os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, sig: calls.append(("signal", fd, sig)))
    monkeypatch.setattr(executor, "status", lambda policy: {})
    monkeypatch.setattr(executor, "check_bound_processes", lambda *args: calls.append(("check",)))
    monkeypatch.setattr(executor, "_process", lambda identity: False)
    executor.normal_exit(None, {"launcher": {"pid": 123}, "gateway": {"pid": 456}})
    assert calls == [("open", 123), ("check",), ("signal", 55, signal.SIGUSR1), ("close", 55)]


def test_launcher_upgrade_shutdown_never_kills_on_timeout_and_sets_state_root(tmp_path):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "launch.py"))
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("inert-process", 8)
    assert namespace["finish"](process, allow_kill=False) is False
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    namespace["request_update_stop"]()
    assert namespace["request_update_stop"].__globals__["update_stopping"] is True
    environment = namespace["gateway_environment"](tmp_path, None, offline=True, driver="mock", mode="monitor")
    assert environment["RP1_HMI_STATE_ROOT"] == str(tmp_path)
