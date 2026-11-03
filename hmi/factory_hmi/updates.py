"""Independent, fail-closed HMI release verification and download staging.

This module never stops control processes, enables hardware, or installs packages.
OpenSSL supplies Ed25519; dpkg supplies Debian version and package semantics.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SCHEMA = "rp1-hmi-release-v1"
PACKAGE = "rp1-test-hmi"
MAX_PACKAGE_BYTES = 4 * 1024**3
MAX_JSON_BYTES = 128 * 1024
FIELDS = {"schema_version", "release_id", "version", "os_id", "os_version",
          "architecture", "package_name", "filename", "size_bytes", "sha256",
          "notes", "published_at"}


def _code_version() -> str:
    try:
        return (Path(__file__).resolve().parents[1] / "packaging/LOCAL_VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# Keep the version of this loaded updater stable if a separately authorized
# installation replaces files while the current process remains alive.
_CODE_VERSION_AT_IMPORT = _code_version()


@dataclass(frozen=True)
class HostInfo:
    os_id: str
    os_version: str
    architecture: str
    current_version: str


def _run(argv: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法运行更新校验工具 {argv[0]}。") from exc


def installed_version() -> str:
    result = _run(["dpkg-query", "-W", "-f=${db:Status-Status}\n${Version}", PACKAGE])
    lines = result.stdout.splitlines()
    return lines[1] if result.returncode == 0 and len(lines) == 2 and lines[0] == "installed" else ""


def detect_host() -> HostInfo:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                parsed = shlex.split(value)
                values[key] = parsed[0] if len(parsed) == 1 else ""
    except (OSError, ValueError) as exc:
        raise ValueError("无法可靠识别本机系统。") from exc
    architecture = _run(["dpkg", "--print-architecture"])
    if architecture.returncode:
        raise ValueError("无法识别 Debian 主机架构。")
    # Installed metadata may already have changed while the old process remains
    # loaded. Never present it as the version of the currently running code.
    # Read the same adjacent file as version.code_version without importing the
    # package initializer (which loads the motion/numpy runtime).
    return HostInfo(values.get("ID", ""), values.get("VERSION_ID", ""),
                    architecture.stdout.strip(), _CODE_VERSION_AT_IMPORT)


def canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _version(value: str) -> None:
    if (not isinstance(value, str) or not value or len(value) > 200
            or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", value)):
        raise ValueError("版本号必须是有效的 Debian 版本。")
    if _run(["dpkg", "--validate-version", value]).returncode:
        raise ValueError("版本号必须是有效的 Debian 版本。")


def _manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("更新清单字段不完整或不受支持。")
    data = dict(value)
    if data["schema_version"] != SCHEMA or data["package_name"] != PACKAGE:
        raise ValueError("更新清单协议或软件包名称不匹配。")
    if not isinstance(data["release_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["release_id"]):
        raise ValueError("发布编号无效。")
    if not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise ValueError("软件包 SHA256 无效。")
    filename = data["filename"]
    if (not isinstance(filename, str) or len(filename) > 200
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*\.deb", filename)
            or filename in {".", ".."}):
        raise ValueError("软件包文件名必须是安全的 DEB basename。")
    if type(data["size_bytes"]) is not int or not 0 < data["size_bytes"] <= MAX_PACKAGE_BYTES:
        raise ValueError("软件包声明长度超出限制。")
    _version(data["version"])
    for field in ("os_id", "os_version", "architecture"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._\-]{0,63}", data[field]):
            raise ValueError("系统或架构标识无效。")
    if not isinstance(data["notes"], str) or len(data["notes"].encode("utf-8")) > 32000:
        raise ValueError("更新说明无效或过长。")
    try:
        when = datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("发布时间必须包含时区。") from exc
    return data


def _public_key(path: Path | str) -> Path:
    path = Path(path).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("尚未部署可信更新公钥；请联系管理员完成首次配置。") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("可信更新公钥必须是不可被组或其他用户写入的普通文件。")
    check = _run(["openssl", "pkey", "-pubin", "-in", str(path), "-text", "-noout"])
    if check.returncode or "ED25519" not in check.stdout.upper():
        raise ValueError("更新公钥必须是 Ed25519 公钥。")
    return path.resolve(strict=True)


def _verify(release: dict, public_key_path: Path | str, host: HostInfo | dict | None,
            *, require_newer: bool) -> dict:
    if not isinstance(release, dict) or set(release) != {"manifest", "signature"}:
        raise ValueError("更新发布必须包含签名及清单。")
    manifest = _manifest(release["manifest"])
    try:
        signature = base64.b64decode(release["signature"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("更新签名编码无效。") from exc
    if len(signature) != 64:
        raise ValueError("Ed25519 签名长度无效。")
    key = _public_key(public_key_path)
    with tempfile.TemporaryDirectory(prefix="rp1-release-verify-") as work:
        message, signed = Path(work) / "manifest", Path(work) / "signature"
        message.write_bytes(canonical_manifest(manifest))
        signed.write_bytes(signature)
        result = _run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key),
                       "-rawin", "-in", str(message), "-sigfile", str(signed)])
    if result.returncode:
        raise ValueError("更新清单签名验证失败。")
    return _validate_release_target(manifest, host, require_newer=require_newer)


def _validate_release_target(manifest: dict, host: HostInfo | dict | None, *, require_newer: bool) -> dict:
    target = detect_host() if host is None else host
    target = asdict(target) if isinstance(target, HostInfo) else dict(target)
    # These are the supported local DEB platforms, never inferred from a URL.
    if (target.get("os_id") != "ubuntu" or target.get("os_version") not in {"22.04", "24.04"}
            or target.get("architecture") not in {"amd64", "arm64"}):
        raise ValueError("本机系统或架构尚不受更新器支持。")
    if any(manifest[field] != target.get(field) for field in ("os_id", "os_version", "architecture")):
        raise ValueError("更新包与本机系统版本或架构不兼容。")
    if require_newer:
        _version(target.get("current_version", ""))
        if _run(["dpkg", "--compare-versions", manifest["version"], "gt", target["current_version"]]).returncode:
            raise ValueError("更新版本必须高于当前运行代码版本，禁止隐式降级。")
    return manifest


def verify_release(release: dict, public_key_path: Path | str,
                   host: HostInfo | dict | None = None) -> dict:
    """Verify an externally supplied release; no network or installed changes."""
    return _verify(release, public_key_path, host, require_newer=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or any(ord(char) < 32 for char in value)):
        raise ValueError("备用更新源必须为无内嵌凭据的 HTTP(S) 地址。")
    return value


@contextmanager
def _download(url: str, headers: dict | None = None):
    try:
        with build_opener(_NoRedirect()).open(Request(_url(url), headers=headers or {}), timeout=15) as stream:
            yield stream
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        # Do not echo URLs, credentials or response bodies into cache/logs.
        raise RuntimeError("更新传输失败；已安装版本与已缓存版本保持不变。") from exc


def _read_json(stream) -> dict:
    data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("更新目录超过长度限制。")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("更新目录不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise ValueError("更新目录必须是对象。")
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def check_source(source_url: str, *, public_key_path: Path | str,
                 host: HostInfo | None = None, headers: dict | None = None) -> dict:
    """The LAN mirror uses the platform's dedicated read-only API contract."""
    host = host or detect_host()
    base = _url(source_url).rstrip("/")
    if urlsplit(base).query:
        raise ValueError("更新源地址不能带查询参数；认证应经进程内请求头传入。")
    endpoint = base + "/api/v1/hmi/updates/latest?" + urlencode(asdict(host))
    with _download(endpoint, headers) as stream:
        result = _read_json(stream).get("data")
    if not isinstance(result, dict) or type(result.get("available")) is not bool:
        raise ValueError("更新目录响应无效。")
    if result["available"]:
        verify_release(result.get("release"), public_key_path, host)
    return {**result, "host": asdict(host)}


def _private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    # Never allow a pre-existing symlink to redirect package/cache writes.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("更新缓存路径不能包含符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("更新缓存必须由当前用户持有且仅当前用户可访问（0700）。")
    return path.resolve(strict=True)


def _deb_metadata(path: Path, manifest: dict) -> None:
    for name, expected in (("Package", manifest["package_name"]),
                           ("Version", manifest["version"]),
                           ("Architecture", manifest["architecture"])):
        result = _run(["dpkg-deb", "-f", str(path), name])
        if result.returncode or result.stdout.strip() != expected:
            raise ValueError(f"DEB 元数据 {name} 与发布清单不一致。")


def _package(path: Path, manifest: dict) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != manifest["size_bytes"]:
        raise ValueError("软件包长度或文件类型不符合清单。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != manifest["sha256"]:
        raise ValueError("软件包 SHA256 不一致。")
    _deb_metadata(path, manifest)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def stage_release(release: dict, *, public_key_path: Path | str, cache_dir: Path | str,
                  host: HostInfo | dict | None = None, package_stream=None,
                  source_url: str | None = None, headers: dict | None = None) -> dict:
    """Stage a signed DEB atomically; credentials and source URLs are not saved."""
    try:
        return _stage_release(release, public_key_path=public_key_path, cache_dir=cache_dir,
                              host=host, package_stream=package_stream,
                              source_url=source_url, headers=headers)
    except (OSError, HTTPException) as exc:
        raise RuntimeError("软件缓存或传输失败；未执行安装，请核查已有缓存后重试。") from exc


def _stage_release(release: dict, *, public_key_path: Path | str, cache_dir: Path | str,
                   host: HostInfo | dict | None = None, package_stream=None,
                   source_url: str | None = None, headers: dict | None = None) -> dict:
    manifest = verify_release(release, public_key_path, host)
    if (package_stream is None) == (source_url is None):
        raise ValueError("必须且只能指定软件包下载流或下载地址。")
    return _cache_verified_release(release, manifest=manifest, cache_dir=cache_dir,
                                   host=host, package_stream=package_stream,
                                   source_url=source_url, headers=headers)


def _cache_verified_release(release: dict, *, manifest: dict, cache_dir: Path | str,
                            host: HostInfo | dict | None = None, package_stream=None,
                            source_url: str | None = None, headers: dict | None = None,
                            verification: dict | None = None, before_publish=None) -> dict:
    """Private byte/cache core. Callers must establish release authenticity first.

    Offline/URL callers pass the signature verifier; the independent online
    platform entry establishes an authenticated HTTPS publication response.
    There is no public unsigned/update-source override.
    """
    root = _private_directory(Path(cache_dir))
    lock_fd = os.open(root / "stage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("当前已有更新缓存操作。") from exc
        releases = _private_directory(root / "releases")
        destination = releases / manifest["release_id"]
        previous = None
        pointer = root / "current.json"
        if pointer.exists():
            with pointer.open("rb") as stream:
                old = _read_json(stream)
            old_id = old.get("release_id")
            if not isinstance(old_id, str) or not re.fullmatch(r"[0-9a-f]{32}", old_id):
                raise ValueError("更新缓存索引损坏，保留原缓存等待人工核查。")
            old_release = releases / old_id / "release.json"
            with old_release.open("rb") as stream:
                old_manifest = _manifest(_read_json(stream).get("manifest"))
            if old_id != manifest["release_id"]:
                previous = str(releases / old_id / old_manifest["filename"])
            else:
                previous = old.get("previous_package_path")
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink():
                raise ValueError("更新缓存发布目录不能是符号链接。")
            with (destination / "release.json").open("rb") as stream:
                cached = _read_json(stream)
            if cached != release:
                raise ValueError("同一发布编号已有不同清单，禁止覆盖。")
            _package(destination / manifest["filename"], manifest)
        else:
            temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=releases))
            try:
                output = temporary / manifest["filename"]
                def copy(stream):
                    length = 0
                    digest = hashlib.sha256()
                    started = time.monotonic()
                    with output.open("xb") as writer:
                        os.chmod(output, 0o600)
                        while True:
                            if time.monotonic() - started > 300:
                                raise RuntimeError("软件下载超过五分钟预算，请稍后重试。")
                            chunk = stream.read(min(1024 * 1024, manifest["size_bytes"] - length + 1))
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                raise ValueError("软件下载流必须为二进制数据。")
                            length += len(chunk)
                            if length > manifest["size_bytes"]:
                                raise ValueError("软件包超过发布清单声明的长度。")
                            digest.update(chunk)
                            writer.write(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if length != manifest["size_bytes"] or digest.hexdigest() != manifest["sha256"]:
                        raise ValueError("软件包长度或 SHA256 不一致。")
                if package_stream is not None:
                    copy(package_stream)
                else:
                    with _download(source_url, headers) as stream:
                        copy(stream)
                _deb_metadata(output, manifest)
                _atomic_json(temporary / "release.json", release)
                if before_publish is not None:
                    before_publish()
                os.rename(temporary, destination)
                _sync_directory(releases)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        receipt_path = destination / "download.json"
        downloaded_at = datetime.now(timezone.utc).isoformat()
        if receipt_path.exists():
            if receipt_path.is_symlink():
                raise ValueError("下载回执不能是符号链接。")
            with receipt_path.open("rb") as stream:
                downloaded_at = _read_json(stream).get("downloaded_at", downloaded_at)
        receipt = {"status": "staged", "manifest": manifest,
                   "artifact_type": "hmi_deb", "installed": False,
                   "downloaded_at": downloaded_at,
                   "running_version_at_download": host.current_version if isinstance(host, HostInfo) else (host or {}).get("current_version"),
                   "package_path": str(destination / manifest["filename"]),
                   "release_path": str(destination / "release.json"),
                   "previous_package_path": previous}
        if before_publish is not None:
            before_publish()
        if verification:
            receipt.update(verification)
        _atomic_json(pointer, {"release_id": manifest["release_id"], "previous_package_path": previous})
        _atomic_json(destination / "download.json", receipt)
        return receipt


def _launcher_command() -> list[str]:
    source = Path(__file__).resolve().parents[1]
    # Checkout: hmi/launch.py. Local DEB: /opt/rp1-test-hmi/launch.py
    # alongside the hmi/ source child. Never search PATH or arbitrary ancestors.
    candidates = [source / "launch.py"]
    if source.name == "hmi":
        candidates.append(source.parent / "launch.py")
    for launcher in candidates:
        try:
            if stat.S_ISREG(launcher.lstat().st_mode):
                return [sys.executable, str(launcher)]
        except FileNotFoundError:
            continue
    raise RuntimeError("找不到与当前更新器源码或 DEB 布局匹配的本机启动器。")


def install_preflight(release_path: Path | str, *, public_key_path: Path | str,
                      state_dir: Path | str, host: HostInfo | None = None,
                      proof: dict | None = None, policy=None, executor=None,
                      previous_release_path: Path | str | None = None) -> dict:
    """Read-only checks. Physical enable-state evidence is currently unavailable."""
    if proof is not None:
        if __package__:
            from .installer import preflight_install_transaction
        else:
            import runpy
            preflight_install_transaction = runpy.run_path(str(Path(__file__).with_name("installer.py")))["preflight_install_transaction"]
        return preflight_install_transaction(release_path, state_dir=state_dir, proof=proof,
                    policy=policy, executor=executor, host=host, previous_release_path=previous_release_path)
    release_path = Path(release_path).resolve(strict=True)
    with release_path.open("rb") as stream:
        release = _read_json(stream)
    manifest = verify_release(release, public_key_path, host)
    package = release_path.parent / manifest["filename"]
    _package(package, manifest)
    root = Path(state_dir).expanduser().resolve(strict=True)
    if not (root / "run").is_dir():
        raise ValueError("该状态目录没有运行身份信息，不能确认全部旧实例。")
    installed = installed_version()
    reasons = ["当前缺少可信的真实电机失能确认链，自动安装保持禁用；JSON 声明不能替代设备反馈。"]
    if installed and _run(["dpkg", "--compare-versions", manifest["version"], "gt", installed]).returncode:
        reasons.append("目标版本不高于已安装 DEB，禁止隐式降级或重复安装。")
    # Same launcher and same state root as the ordinary status command. Never
    # invoke stop, kill, unlink a lock, or change user configuration here.
    status_result = _run([*_launcher_command(), "status", "--state-dir", str(root)])
    try:
        status = json.loads(status_result.stdout)
    except ValueError:
        status = {}
    if status_result.returncode or status.get("state_root") != str(root) or status.get("running") is not False:
        reasons.append("同一状态目录尚未确认 running=false，请先按安全流程正常退出旧实例。")
    lock_fd = os.open(root / "run/launcher.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reasons.append("同一状态目录的启动锁仍由旧实例持有。")
    # apt simulation does not install, repair dependencies or contact mirrors.
    dependencies = _run(["apt-get", "--simulate", "--no-download", "install", str(package)], timeout=30)
    if dependencies.returncode:
        reasons.append("本机依赖模拟检查未通过；本工具不会自动联网修复或安装依赖。")
    return {"ready": False, "status": "blocked", "manifest": manifest,
            "state_root": str(root), "installed_version": installed,
            "launcher_status": status, "dependencies_ok": dependencies.returncode == 0,
            "reasons": reasons}


def protected_install(release_path: Path | str, *, state_dir: Path | str,
                      proof: dict | None = None, public_key_path: Path | str | None = None,
                      previous_release_path: Path | str | None = None, restart: bool = False,
                      policy=None, executor=None, host=None) -> dict:
    """Execute only with a signed safe-state proof and independently trusted OS policy.

    public_key_path is kept for check/stage API compatibility; actual privileged
    installation obtains both trusted keys solely from its fixed root policy.
    """
    if proof is None:
        raise RuntimeError("安装缺少可信签发的真实电机失能及 PLC 正常总停止证明。")
    if __package__:
        from .installer import execute_install_transaction
    else:
        import runpy
        execute_install_transaction = runpy.run_path(str(Path(__file__).with_name("installer.py")))["execute_install_transaction"]
    return execute_install_transaction(release_path, state_dir=state_dir, proof=proof,
                 previous_release_path=previous_release_path, restart=restart,
                 policy=policy, executor=executor, host=host)


def verify_installation(release_path: Path | str, *, public_key_path: Path | str,
                        state_dir: Path | str, previous_identity: dict,
                        host: HostInfo | None = None) -> dict:
    """Verify an externally authorized manual install and manually started HMI."""
    with Path(release_path).open("rb") as stream:
        release = _read_json(stream)
    manifest = _verify(release, public_key_path, host, require_newer=False)
    root = Path(state_dir).expanduser().resolve(strict=True)
    status_result = _run([*_launcher_command(), "status", "--state-dir", str(root)])
    try:
        status = json.loads(status_result.stdout)
    except ValueError as exc:
        raise ValueError("无法核实新实例运行状态。") from exc
    if status_result.returncode or status.get("state_root") != str(root) or status.get("running") is not True:
        raise ValueError("同一状态目录的新实例尚未运行。")
    pid = status.get("gateway_pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("新实例缺少实际网关 PID。")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        ticks = raw[raw.rfind(")") + 2:].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IndexError) as exc:
        raise ValueError("无法核实新进程实际启动时间。") from exc
    if (not isinstance(previous_identity, dict)
            or type(previous_identity.get("pid")) is not int
            or not str(previous_identity.get("start_ticks", "")).isdigit()
            or not previous_identity.get("boot_id")):
        raise ValueError("必须提供更新前记录的真实网关 PID、start_ticks 与 boot_id。")
    if (boot, pid, ticks) == (previous_identity["boot_id"], previous_identity["pid"], str(previous_identity["start_ticks"])):
        raise ValueError("实际运行的仍为旧进程，安装尚未生效。")
    address = status.get("gateway", "")
    parsed = urlsplit(address)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("网关身份地址不符合本机启动器约定。")
    with _download(address + "/api/v1/health") as stream:
        health = _read_json(stream)
    software = health.get("software") or {}
    installed = installed_version()
    if (installed != manifest["version"] or health.get("ok") is not True
            or software.get("running_version") != manifest["version"]
            or software.get("pid") != pid or str(software.get("start_ticks")) != ticks):
        raise ValueError("安装版本、运行代码版本、实际进程身份或健康状态尚未全部一致。")
    return {"verified": True, "installed_version": installed, "software": software,
            "boot_id": boot, "state_root": str(root), "hardware_validated": False}


def prepare_release(package: Path | str, *, private_key_path: Path | str,
                    os_id: str, os_version: str, output: Path | str,
                    notes: str = "", release_id: str | None = None) -> dict:
    """Offline release signing with an existing operator-managed Ed25519 key."""
    package = Path(package).resolve(strict=True)
    def field(name):
        result = _run(["dpkg-deb", "-f", str(package), name])
        if result.returncode:
            raise ValueError("无法读取待签名 DEB。")
        return result.stdout.strip()
    digest = hashlib.sha256()
    with package.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    manifest = _manifest({"schema_version": SCHEMA, "release_id": release_id or uuid.uuid4().hex,
                          "version": field("Version"), "os_id": os_id, "os_version": os_version,
                          "architecture": field("Architecture"), "package_name": field("Package"),
                          "filename": package.name, "size_bytes": package.stat().st_size,
                          "sha256": digest.hexdigest(), "notes": notes,
                          "published_at": datetime.now(timezone.utc).isoformat()})
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("签名输出文件已存在，禁止覆盖。")
    with tempfile.TemporaryDirectory(prefix="rp1-release-sign-") as work:
        message, signature, public = (Path(work) / name for name in ("manifest", "signature", "public.pem"))
        message.write_bytes(canonical_manifest(manifest))
        derive = _run(["openssl", "pkey", "-in", str(private_key_path), "-pubout", "-out", str(public)])
        if derive.returncode:
            raise ValueError("无法读取现有签名密钥。")
        public.chmod(0o600)
        _public_key(public)
        signed = _run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_key_path),
                       "-rawin", "-in", str(message), "-out", str(signature)])
        if signed.returncode:
            raise ValueError("离线签名失败。")
        release = {"manifest": manifest, "signature": base64.b64encode(signature.read_bytes()).decode("ascii")}
        target = HostInfo(os_id, os_version, manifest["architecture"], manifest["version"])
        _verify(release, public, target, require_newer=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive write prevents a concurrent signer from losing their manifest.
    with output.open("x", encoding="utf-8") as writer:
        json.dump(release, writer, ensure_ascii=False, indent=2)
        writer.write("\n")
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP1 独立更新工具：校验、缓存及安装安全门禁")
    parser.add_argument("command", choices=("check", "stage", "install-preflight", "install", "verify"))
    parser.add_argument("--source", default=os.environ.get("RP1_HMI_UPDATE_SOURCE", ""))
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--release", type=Path, help="已签名 release.json；stage 时可离线使用")
    parser.add_argument("--package", type=Path, help="stage 时使用的离线 DEB")
    parser.add_argument("--previous-identity", type=Path, help="更新前保存的网关 pid/start_ticks/boot_id JSON")
    parser.add_argument("--proof", type=Path, help="由独立受信签发端产生的短时安全证明")
    parser.add_argument("--previous-release", type=Path, help="与当前已安装版本对应的签名恢复包清单")
    parser.add_argument("--restart", action="store_true", help="安装后仅恢复监控模式，不使能或测试")
    args = parser.parse_args(argv)
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    root = (args.state_dir or state_home / PACKAGE).expanduser().resolve()
    key = args.public_key or Path(os.environ.get("RP1_HMI_UPDATE_PUBLIC_KEY", str(root / "config/update-public-key.pem")))
    try:
        if args.command in {"check", "stage"}:
            if args.release:
                with args.release.open("rb") as stream:
                    release = _read_json(stream)
                verify_release(release, key)
                result = {"available": True, "release": release, "source": "offline"}
            elif args.source:
                result = check_source(args.source, public_key_path=key)
                release = result.get("release")
            else:
                raise ValueError("请指定已配置的内网 --source 或离线 --release。")
            if args.command == "stage" and result.get("available"):
                cache = args.cache_dir or root / "data/software-updates"
                if args.package:
                    with args.package.open("rb") as stream:
                        result = stage_release(release, public_key_path=key, cache_dir=cache, package_stream=stream)
                elif args.source:
                    source = args.source.rstrip("/") + "/api/v1/hmi/updates/releases/" + release["manifest"]["release_id"] + "/package"
                    result = stage_release(release, public_key_path=key, cache_dir=cache, source_url=source)
                else:
                    raise ValueError("离线缓存还需要 --package。")
        else:
            if not args.release:
                raise ValueError("请通过 --release 指定已缓存的签名清单。")
            common = {"public_key_path": key, "state_dir": root}
            if args.command == "verify":
                if not args.previous_identity:
                    raise ValueError("verify 需要 --previous-identity 更新前进程身份。")
                with args.previous_identity.open("rb") as stream:
                    identity = _read_json(stream)
                result = verify_installation(args.release, previous_identity=identity, **common)
            elif args.command == "install":
                if os.geteuid() != 0:
                    raise ValueError("install 需要维护人员显式取得本机系统授权；平台登录不等于系统权限。")
                if not args.proof:
                    raise ValueError("install 需要 --proof 可信短时安全证明。")
                with args.proof.open("rb") as stream:
                    proof = _read_json(stream)
                result = protected_install(args.release, proof=proof, previous_release_path=args.previous_release,
                                           restart=args.restart, **common)
            else:
                proof = None
                if args.proof:
                    with args.proof.open("rb") as stream:
                        proof = _read_json(stream)
                result = install_preflight(args.release, proof=proof, previous_release_path=args.previous_release, **common)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get("status") == "failed" else 3 if result.get("ready") is False else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
