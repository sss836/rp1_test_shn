"""Protected HMI installation transaction with an injectable OS executor.

The CLI has no simulation/bypass switch. Tests inject an executor explicitly;
the real executor uses an independently provisioned, root-owned policy and keys.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

# Loading as a standalone privileged helper must not import the motion runtime.
if __package__:
    from . import updates, upgrade_backup
else:
    from types import SimpleNamespace
    updates = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("updates.py"))))
    upgrade_backup = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("upgrade_backup.py"))))

POLICY_PATH = Path("/etc/rp1-test-hmi/installer-policy.json")
PROOF_SCHEMA = "rp1-hmi-safe-stop-v2"
PROOF_FIELDS = {"schema_version", "proof_id", "release_id", "package_sha256", "state_root",
                "observed_at", "expires_at", "boot_id", "launcher", "gateway", "requester",
                "all_windows_checked", "windows", "plc", "control_inhibited", "quiescence_token"}


@dataclass(frozen=True)
class InstallerPolicy:
    state_root: Path
    spool_root: Path
    release_public_key: Path
    safety_public_key: Path
    launcher_path: Path
    python_path: Path
    run_uid: int
    run_gid: int
    session_env: dict | None = None


def _trusted_path(path: Path, *, directory: bool = False) -> Path:
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)):
        raise ValueError("受信安装配置路径必须为无符号链接的绝对规范路径。")
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("安装政策、工具与公钥必须位于 root 持有且非组写/其他用户写的路径。")
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("受信安装路径类型无效。")
    return path


def load_policy() -> InstallerPolicy:
    _trusted_path(POLICY_PATH)
    with POLICY_PATH.open("rb") as stream:
        value = updates._read_json(stream)
    required = {"schema_version", "state_root", "spool_root", "release_public_key", "safety_public_key",
                "launcher_path", "python_path", "run_uid", "run_gid"}
    if not required <= set(value) or set(value) - required - {"session_env"} or value["schema_version"] != "rp1-hmi-installer-policy-v1":
        raise ValueError("安装器受信政策字段不完整。")
    for name in ("run_uid", "run_gid"):
        if type(value[name]) is not int or value[name] <= 0:
            raise ValueError("安装后必须恢复为明确的非 root 运行账号。")
    session = value.get("session_env", {})
    if (not isinstance(session, dict) or set(session) - {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"}
            or any(not isinstance(item, str) or len(item) > 4096 for item in session.values())):
        raise ValueError("桌面会话环境必须由首次部署政策明确指定。")
    policy = InstallerPolicy(**{key: (Path(item) if key.endswith(("root", "key", "path")) else item)
                                for key, item in value.items() if key != "schema_version"})
    for path in (policy.release_public_key, policy.safety_public_key, policy.launcher_path, policy.python_path):
        _trusted_path(path)
    _trusted_path(policy.spool_root, directory=True)
    root = policy.state_root
    if not root.is_absolute() or str(root) != str(root.resolve(strict=True)):
        raise ValueError("状态目录必须与首次部署政策中的绝对规范路径一致。")
    for path in (root, root / "run"):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != policy.run_uid or info.st_mode & 0o022:
            raise ValueError("状态目录必须由指定运行账号持有且不可被其他账号写入。")
    return policy


def _instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("安全证明时间必须包含时区。") from exc


def _identity(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"pid", "start_ticks"}
            or type(value["pid"]) is not int or value["pid"] <= 0
            or not isinstance(value["start_ticks"], str)
            or not re.fullmatch(r"[1-9][0-9]*", value["start_ticks"])):
        raise ValueError("安全证明缺少实际 PID/start_ticks。")
    return value


def _claims(value: object, *, manifest: dict, state_root: Path, boot_id: str,
            now: datetime) -> dict:
    if not isinstance(value, dict) or set(value) != PROOF_FIELDS:
        raise ValueError("安全证明字段不完整或不受支持。")
    data = dict(value)
    if (data["schema_version"] != PROOF_SCHEMA or not isinstance(data["proof_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", data["proof_id"])):
        raise ValueError("安全证明协议或编号无效。")
    if (data["release_id"] != manifest["release_id"] or data["package_sha256"] != manifest["sha256"]
            or data["state_root"] != str(state_root) or data["boot_id"] != boot_id):
        raise ValueError("安全证明未绑定本次软件包、状态目录与本次启动。")
    observed, expires = _instant(data["observed_at"]), _instant(data["expires_at"])
    if not 0 < (expires - observed).total_seconds() <= 60 or not observed <= now < expires:
        raise ValueError("安全证明已过期、来自未来或超过60秒有效期。")
    _identity(data["launcher"])
    _identity(data["gateway"])
    requester = data["requester"]
    if (not isinstance(requester, dict) or set(requester) != {"subject", "role"}
            or not isinstance(requester["subject"], str) or not requester["subject"].strip()
            or requester["role"] not in {"SYSTEM_ADMIN", "TEST_EXECUTOR"}):
        raise ValueError("安全证明未绑定有权发起更新的真实平台账号。")
    if (data["control_inhibited"] is not True or data["all_windows_checked"] is not True
            or data["quiescence_token"] != data["proof_id"]):
        raise ValueError("安全证明没有覆盖全部窗口并锁定后续控制写入。")
    windows = data["windows"]
    if not isinstance(windows, list) or not windows or len(windows) > 256:
        raise ValueError("安全证明必须覆盖全部实际窗口。")
    owners = set()
    for window in windows:
        if (not isinstance(window, dict)
                or set(window) != {"owner", "motion_active", "motors_disabled_confirmed", "feedback_fresh"}
                or not isinstance(window["owner"], str) or not window["owner"].strip()
                or window["owner"] in owners or window["motion_active"] is not False
                or window["motors_disabled_confirmed"] is not True or window["feedback_fresh"] is not True):
            raise ValueError("窗口尚未取得新鲜的实际电机失能确认，或窗口清单有重复。")
        owners.add(window["owner"])
    plc = data["plc"]
    expected = {"driver": "modbus_tcp", "communication_state": "LIVE", "stale": False,
                "normal_all_stop_confirmed": True, "feedback_fresh": True,
                "main_ready": False, "channel_permit": [False, False, False, False]}
    if not isinstance(plc, dict) or set(plc) != set(expected):
        raise ValueError("安全证明缺少 PLC 正常总停止实际反馈。")
    if any(type(plc[key]) is not type(item) or plc[key] != item for key, item in expected.items()):
        raise ValueError("PLC 非真实在线正常总停止状态。")
    if any(item is not False for item in plc["channel_permit"]):
        raise ValueError("PLC 通道许可必须均为精确 false。")
    return data


def verify_safety_proof(proof: dict, *, public_key_path: Path | str, manifest: dict,
                        state_root: Path | str, boot_id: str, now: datetime | None = None) -> dict:
    if not isinstance(proof, dict) or set(proof) != {"claims", "signature"}:
        raise ValueError("必须提供可信签发端签名的安全证明，不能使用裸 JSON 或布尔值。")
    claims = _claims(proof["claims"], manifest=manifest, state_root=Path(state_root),
                     boot_id=boot_id, now=now or datetime.now(timezone.utc))
    try:
        signature = base64.b64decode(proof["signature"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("安全证明签名编码无效。") from exc
    if len(signature) != 64:
        raise ValueError("安全证明签名长度无效。")
    key = updates._public_key(public_key_path)
    with tempfile.TemporaryDirectory(prefix="rp1-safe-proof-") as work:
        message, sig = Path(work) / "claims", Path(work) / "signature"
        message.write_bytes(updates.canonical_manifest(claims))
        sig.write_bytes(signature)
        checked = updates._run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key),
                                "-rawin", "-in", str(message), "-sigfile", str(sig)])
    if checked.returncode:
        raise ValueError("安全证明签名验证失败。")
    return claims


def sign_safety_proof(claims: dict, *, private_key_path: Path | str) -> dict:
    """Trusted issuer utility; caller must already have verified device evidence.

    Possessing the signing key is an authority boundary. Never deploy that key
    readable by a desktop caller or accept caller-authored claims at an issuer.
    """
    with tempfile.TemporaryDirectory(prefix="rp1-sign-safe-proof-") as work:
        message, signature = Path(work) / "claims", Path(work) / "signature"
        message.write_bytes(updates.canonical_manifest(claims))
        signed = updates._run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_key_path),
                               "-rawin", "-in", str(message), "-out", str(signature)])
        if signed.returncode:
            raise ValueError("安全证明签发失败。")
        return {"claims": claims, "signature": base64.b64encode(signature.read_bytes()).decode("ascii")}


def _copy_release(source: Path, destination: Path, key: Path, host, *, require_newer: bool) -> tuple[dict, Path]:
    with source.open("rb") as stream:
        release = updates._read_json(stream)
    manifest = updates._verify(release, key, host, require_newer=require_newer)
    package = source.parent / manifest["filename"]
    destination.mkdir(mode=0o700)
    output = destination / manifest["filename"]
    descriptor = os.open(package, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as reader, output.open("xb") as writer:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ValueError("安装输入包必须是普通文件。")
        os.chmod(output, 0o600)
        length = 0
        while True:
            chunk = reader.read(min(1024 * 1024, manifest["size_bytes"] - length + 1))
            if not chunk:
                break
            length += len(chunk)
            if length > manifest["size_bytes"]:
                raise ValueError("安装输入包超过签名长度。")
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    updates._package(output, manifest)
    updates._atomic_json(destination / "release.json", release)
    return manifest, output


def preflight_install_transaction(release_path: Path | str, *, state_dir: Path | str,
                                   proof: dict, policy: InstallerPolicy | None = None,
                                   executor=None, host=None, previous_release_path: Path | str | None = None) -> dict:
    """Inspect a signed installation request without consuming proof or stopping."""
    if executor is None:
        if policy is not None:
            raise ValueError("真实安装预检只能使用固定路径的受信政策。")
        policy = load_policy()
        executor = SystemExecutor()
    if policy is None or Path(state_dir).expanduser().resolve(strict=True) != policy.state_root:
        raise ValueError("预检状态目录不在受信政策范围内。")
    with Path(release_path).open("rb") as stream:
        release = updates._read_json(stream)
    manifest = updates.verify_release(release, policy.release_public_key, host)
    package = Path(release_path).parent / manifest["filename"]
    updates._package(package, manifest)
    current = executor.installed_version()
    if current:
        if updates._run(["dpkg", "--compare-versions", manifest["version"], "gt", current]).returncode:
            raise ValueError("目标版本必须高于已安装版本。")
        if previous_release_path is None:
            raise ValueError("缺少与当前已安装版本一致的受信上一版包。")
        with Path(previous_release_path).open("rb") as stream:
            previous = updates._read_json(stream)
        old = updates._verify(previous, policy.release_public_key, host, require_newer=False)
        updates._package(Path(previous_release_path).parent / old["filename"], old)
        if old["version"] != current:
            raise ValueError("上一版包与已安装版本不一致。")
    with tempfile.TemporaryDirectory(prefix="rp1-installer-preflight-") as work:
        executor.check_dependencies(package, Path(work) / "dependencies.log")
    claims = verify_safety_proof(proof, public_key_path=policy.safety_public_key, manifest=manifest,
                                state_root=policy.state_root, boot_id=executor.boot_id(), now=executor.now())
    executor.check_bound_processes(policy, executor.status(policy), claims)
    return {"ready": True, "status": "ready_to_exit", "manifest": manifest,
            "state_root": str(policy.state_root), "requires_os_authorization": True,
            "proof_expires_at": claims["expires_at"], "hardware_validated": False}


def execute_install_transaction(release_path: Path | str, *, state_dir: Path | str,
                                proof: dict, policy: InstallerPolicy | None = None,
                                executor=None, host=None, previous_release_path: Path | str | None = None,
                                restart: bool = False) -> dict:
    """Run one guarded transaction; fake executors are Python-test injection only."""
    if type(restart) is not bool:
        raise ValueError("restart 必须由明确的安装请求指定。")
    if executor is None:
        if policy is not None:
            raise ValueError("真实安装只能使用固定路径的受信政策。")
        policy = load_policy()
        executor = SystemExecutor()
    if policy is None:
        raise ValueError("安装事务缺少政策。")
    root = Path(state_dir).expanduser().resolve(strict=True)
    if root != policy.state_root:
        raise ValueError("请求状态目录不在首次部署政策授权范围内。")
    executor.authorize(policy)
    spool = updates._private_directory(policy.spool_root)
    transaction_id = uuid.uuid4().hex
    directory = spool / "transactions" / transaction_id
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    receipt = {"transaction_id": transaction_id, "status": "preparing", "verified": False,
               "state_root": str(root), "log_path": str(directory / "events.jsonl"),
               "receipt_path": str(directory / "receipt.json"), "previous_package_path": None,
               "hardware_validated": False}
    phase = "prepare"
    def event(name, **values):
        nonlocal phase
        phase = name
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(json.dumps({"at": executor.now().isoformat(), "phase": name, **values}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    def save():
        updates._atomic_json(directory / "receipt.json", receipt)
    try:
        # One root-owned global lock serializes helpers even across state roots.
        descriptor = os.open(spool / "installer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as global_lock:
            try:
                fcntl.flock(global_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("本机已有更新安装事务。") from exc
            manifest, package = _copy_release(Path(release_path), directory / "target", policy.release_public_key,
                                               host, require_newer=True)
            receipt["manifest"] = manifest
            before = executor.installed_version()
            receipt["installed_version_before"] = before
            if before:
                if updates._run(["dpkg", "--compare-versions", manifest["version"], "gt", before]).returncode:
                    raise ValueError("目标版本不高于已安装版本，禁止隐式降级。")
                if previous_release_path is None:
                    raise ValueError("安装前必须保留与当前已安装版本一致的受信上一版完整包。")
                previous, previous_package = _copy_release(Path(previous_release_path), directory / "previous",
                                                           policy.release_public_key, host, require_newer=False)
                if previous["version"] != before:
                    raise ValueError("提供的上一版恢复包不对应当前已安装版本。")
                receipt["previous_package_path"] = str(previous_package)
            event("packages_verified", target_version=manifest["version"], previous_version=before)
            executor.check_dependencies(package, directory / "dependencies.log")
            event("dependencies_verified")
            claims = verify_safety_proof(proof, public_key_path=policy.safety_public_key,
                                        manifest=manifest, state_root=root,
                                        boot_id=executor.boot_id(), now=executor.now())
            status = executor.status(policy)
            executor.check_bound_processes(policy, status, claims)
            # Consume before any process stop. A failed transaction needs a new
            # physical check/proof; it cannot replay a prior safe-stop decision.
            used = spool / "consumed-proofs"
            used.mkdir(exist_ok=True, mode=0o700)
            consumed = os.open(used / claims["proof_id"], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(consumed, "w") as writer:
                writer.write(transaction_id)
                writer.flush()
                os.fsync(writer.fileno())
            updates._sync_directory(used)
            receipt["previous_identity"] = {**claims["gateway"], "boot_id": claims["boot_id"]}
            event("safety_verified", proof_id=claims["proof_id"])
            verify_safety_proof(proof, public_key_path=policy.safety_public_key, manifest=manifest,
                                state_root=root, boot_id=executor.boot_id(), now=executor.now())
            executor.normal_exit(policy, claims)
            event("old_instance_exited")
            with executor.launcher_lock(policy):
                stopped = executor.status(policy)
                if stopped.get("running") is not False or stopped.get("state_root") != str(root):
                    raise ValueError("同一状态目录尚未确认 running=false。")
                executor.confirm_bound_exit(claims)
                # Recheck the root-owned copy immediately before dpkg; caller
                # changes to their cache can no longer alter installation bytes.
                updates._package(package, manifest)
                executor.check_dependencies(package, directory / "dependencies-final.log")
                event("backing_up_state")
                receipt["state_backup"] = upgrade_backup.create_snapshot(root, directory / "state-backup")
                event("state_backup_verified")
                save()
                event("installing")
                save()
                verify_safety_proof(proof, public_key_path=policy.safety_public_key, manifest=manifest,
                                    state_root=root, boot_id=executor.boot_id(), now=executor.now())
                executor.install(package, directory / "install.log")
                actual = executor.installed_version()
                receipt["installed_version_after"] = actual
                if actual != manifest["version"]:
                    raise RuntimeError("安装器返回后 dpkg 版本仍与目标不一致。")
                event("installed", installed_version=actual)
                receipt["status"] = "installed_pending_restart"
                save()
            if restart:
                # Release launcher's own lock so its ordinary start can acquire
                # the same inode. Never delete/replace it to force a new start.
                executor.start(policy, status, directory / "restart.log")
                event("verifying_new_instance")
                software = executor.verify_new_instance(policy, manifest, claims["gateway"])
                receipt.update(status="verified", verified=True, software=software)
                event("verified")
            save()
    except Exception as exc:
        # Do not roll back/downgrade, restart again, or touch user configuration.
        receipt.update(status="failed", failed_phase=phase, error=str(exc))
        try:
            receipt["installed_version_after"] = executor.installed_version()
        except Exception:
            receipt["installed_version_after"] = None
        event("failed", failed_phase=phase, error_type=type(exc).__name__)
        save()
    return receipt


class SystemExecutor:
    """Actual privileged operations; construction itself performs no operation."""
    def authorize(self, policy):
        if os.geteuid() != 0:
            raise ValueError("安装必须显式取得本机 OS 授权；平台账号权限不等于系统安装权限。")
        self.policy = policy

    def now(self):
        return datetime.now(timezone.utc)

    def boot_id(self):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def installed_version(self):
        return updates.installed_version()

    def _process(self, identity):
        try:
            raw = Path(f"/proc/{identity['pid']}/stat").read_text()
            ticks = raw[raw.rfind(")") + 2:].split()[19]
            return ticks == identity["start_ticks"]
        except (OSError, IndexError):
            return False

    def _environment(self, policy):
        account = pwd.getpwuid(policy.run_uid)
        return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": account.pw_dir,
                "USER": account.pw_name, "LOGNAME": account.pw_name, "LANG": "C.UTF-8",
                "XDG_RUNTIME_DIR": f"/run/user/{policy.run_uid}", **(policy.session_env or {})}

    def _launcher(self, policy, action, *arguments):
        return [str(policy.python_path), str(policy.launcher_path), action,
                "--state-dir", str(policy.state_root), *arguments]

    def status(self, policy):
        try:
            result = subprocess.run(self._launcher(policy, "status"), capture_output=True, text=True,
                                    timeout=8, check=False, user=policy.run_uid, group=policy.run_gid,
                                    extra_groups=[], env=self._environment(policy))
            value = json.loads(result.stdout)
            if result.returncode or value.get("state_root") != str(policy.state_root):
                raise ValueError
            return value
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError("无法核实同一状态目录的真实启动器状态。") from exc

    def check_bound_processes(self, policy, status, claims):
        if (status.get("running") is not True or status.get("pid") != claims["launcher"]["pid"]
                or str(status.get("start")) != claims["launcher"]["start_ticks"]
                or status.get("gateway_pid") != claims["gateway"]["pid"]
                or status.get("update_safe_exit") is not True):
            raise ValueError("安全证明绑定的旧实例身份不一致，或旧启动器不支持无强杀的升级退出。")
        for name in ("launcher", "gateway"):
            identity = claims[name]
            if not self._process(identity):
                raise ValueError("安全证明绑定的进程启动时间已变化。")
            process = Path(f"/proc/{identity['pid']}")
            if process.stat().st_uid != policy.run_uid:
                raise ValueError("旧进程不属于受信政策中的运行账号。")
            argv = (process / "cmdline").read_bytes().split(b"\0")
            expected = str(policy.launcher_path).encode() if name == "launcher" else b"factory_hmi.gateway"
            if expected not in argv:
                raise ValueError("旧进程实际命令行不匹配启动器或网关身份。")

    def normal_exit(self, policy, claims):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise ValueError("本机缺少 pidfd 安全进程信号支持，不能进行自动升级退出。")
        descriptor = os.pidfd_open(claims["launcher"]["pid"])
        try:
            # Open the process handle first, then recheck PID/start_ticks. If
            # that process exits, the handle cannot target a reused PID.
            self.check_bound_processes(policy, self.status(policy), claims)
            signal.pidfd_send_signal(descriptor, signal.SIGUSR1)
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if all(not self._process(claims[name]) for name in ("launcher", "gateway")):
                return
            time.sleep(.2)
        raise RuntimeError("旧实例正常退出超时；未强杀、未安装，请人工核查。")

    def confirm_bound_exit(self, claims):
        if any(self._process(claims[name]) for name in ("launcher", "gateway")):
            raise ValueError("安全证明绑定的旧实例仍在运行。")

    @contextmanager
    def launcher_lock(self, policy):
        descriptor = os.open(policy.state_root / "run/launcher.lock", os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "a") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != policy.run_uid or info.st_nlink != 1:
                raise ValueError("启动锁身份无效，不能替换或删除锁来强行安装。")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("旧实例启动锁仍被占用。") from exc
            yield

    def _command_log(self, argv, log, timeout):
        with Path(log).open("ab") as stream:
            os.chmod(log, 0o600)
            try:
                process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
                                                "DEBIAN_FRONTEND": "noninteractive"})
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"系统子进程 {process.pid} 超时且可能仍运行；未强杀，请人工核查，不会自动降级。") from exc
            if returncode:
                raise RuntimeError(f"系统工具 {Path(argv[0]).name} 返回 {returncode}；详见保留日志。")

    def check_dependencies(self, package, log):
        self._command_log(["/usr/bin/apt-get", "--simulate", "--no-download", "--no-remove", "install", str(package)], log, 30)
        # apt's simulation may succeed while proposing dependency downloads.
        # dpkg below installs only our signed package, so all dependencies must
        # already be satisfied and no unrelated package action may be proposed.
        for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("Inst ", "Conf ", "Remv ")):
                action, name, *_ = line.split()
                if action == "Remv" or name.split(":", 1)[0] != updates.PACKAGE:
                    raise ValueError("依赖检查仍要求安装/配置/移除其他包，请先经授权人工处理依赖。")

    def install(self, package, log):
        self._command_log(["/usr/bin/dpkg", "--force-confold", "--install", str(package)], log, 600)

    def start(self, policy, old_status, log):
        parsed = urlsplit(old_status.get("gateway", ""))
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise ValueError("旧实例网关地址不符合本机启动器约定。")
        with Path(log).open("ab") as stream:
            os.chmod(log, 0o600)
            subprocess.Popen(self._launcher(policy, "start", "--port", str(parsed.port)),
                             stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                             start_new_session=True, user=policy.run_uid, group=policy.run_gid,
                             extra_groups=[], env=self._environment(policy))

    def verify_new_instance(self, policy, manifest, previous):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                # A launcher can be alive before its gateway identity/health is
                # published. Retry within the same bounded verification budget.
                status = self.status(policy)
                pid = status.get("gateway_pid")
                if status.get("running") is True and type(status.get("read_only")) is bool and type(pid) is int and pid > 0:
                    raw = Path(f"/proc/{pid}/stat").read_text()
                    ticks = raw[raw.rfind(")") + 2:].split()[19]
                    address = status.get("gateway", "")
                    parsed = urlsplit(address)
                    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                            or not parsed.port or parsed.path or parsed.query
                            or parsed.fragment or parsed.username or parsed.password):
                        raise ValueError("新网关地址无效。")
                    with updates._download(address + "/api/v1/health") as stream:
                        health = updates._read_json(stream)
                    software = health.get("software") or {}
                    if (health.get("ok") is True and software.get("running_version") == manifest["version"]
                            and software.get("pid") == pid and str(software.get("start_ticks")) == ticks
                            and (pid, ticks) != (previous["pid"], previous["start_ticks"])
                            and self.installed_version() == manifest["version"]
                            and (health.get("snapshot") or {}).get("station", {}).get("motors_enabled") is False):
                        return software
            except (OSError, IndexError, RuntimeError, ValueError, TypeError, AttributeError):
                # Invalid/stale responses never grant success. Do not restart
                # again, downgrade, or enable hardware when verification fails.
                pass
            time.sleep(.25)
        raise RuntimeError("安装后的实际新进程版本或健康身份核对超时；不自动重试启动或使能。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="RP1 受信本机安装事务（实际调用须单独取得本机授权）")
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--proof", required=True, type=Path)
    parser.add_argument("--previous-release", type=Path)
    parser.add_argument("--restart", action="store_true", help="安装后仅正常启动界面，不使能或测试")
    args = parser.parse_args(argv)
    try:
        with args.proof.open("rb") as stream:
            proof = updates._read_json(stream)
        receipt = execute_install_transaction(args.release, state_dir=args.state_dir, proof=proof,
                    previous_release_path=args.previous_release, restart=args.restart)
        print(json.dumps(receipt, ensure_ascii=False))
        return 0 if receipt["status"] in {"verified", "installed_pending_restart"} else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
