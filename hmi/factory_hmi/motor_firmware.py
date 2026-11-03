"""Signed motor downloads and recoverable private cache. Never installs/flashes."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .motor_firmware_contract import SignedMotorFirmware
from .updates import _private_directory, _atomic_json, _sync_directory, _public_key, _run


def compatible(m, target):
    import re
    current = target.get("current_version", "")
    if re.fullmatch(r"\d+\.\d+\.\d+", current):
        if tuple(map(int, m["software_version"].split("."))) <= tuple(map(int, current.split("."))):
            return False
    return (m["model"] == target.get("model") and target.get("hardware") in m["hardware_versions"]
            and target.get("protocol") in m["protocol_versions"]
            and target.get("current_version") in m["from_versions"]
            and target.get("current_version") != m["version"])


def verify_firmware(release, key, target):
    envelope = SignedMotorFirmware.model_validate(release).model_dump()
    m = envelope["manifest"]
    if not compatible(m, target):
        raise ValueError("固件与型号、硬件、协议或实际版本不兼容")
    signature = base64.b64decode(envelope["signature"], validate=True)
    if len(signature) != 64:
        raise ValueError("电机固件签名长度无效")
    key = _public_key(key)
    with tempfile.TemporaryDirectory(prefix="motor-firmware-verify-") as work:
        work = Path(work)
        (work / "manifest").write_bytes(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        (work / "signature").write_bytes(signature)
        result = _run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
                       "-in", str(work / "manifest"), "-sigfile", str(work / "signature")])
        if result.returncode:
            raise ValueError("电机固件未通过可信签名校验")
    return m


def _valid_package(path, m):
    if path.is_symlink():
        raise ValueError("电机固件缓存不能是符号链接")
    with path.open("rb") as stream:
        if stream.read(8) == b"!<arch>\n":
            raise ValueError("DEB包不能用作电机固件")
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size != m["size_bytes"] or hashlib.file_digest(stream, "sha256").hexdigest() != m["sha256"]:
            raise ValueError("电机固件缓存长度或SHA256不符")


def stage_firmware(release, *, key, target, cache_root, open_stream):
    m = verify_firmware(release, key, target)
    root = _private_directory(Path(cache_root))
    fd = os.open(root / "download.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("已有电机固件下载正在进行") from exc
        releases = _private_directory(root / "releases")
        destination = releases / m["release_id"]
        cached = False
        if destination.is_symlink():
            raise ValueError("缓存发布目录不能是符号链接")
        if destination.exists():
            metadata = destination / "release.json"
            if metadata.is_symlink() or json.loads(metadata.read_text()) != release:
                raise ValueError("同一发布编号的缓存清单冲突，禁止覆盖")
            try:
                _valid_package(destination / m["filename"], m)
                cached = True
            except (ValueError, OSError):
                # Quarantine the corrupt completed copy; keep its evidence and
                # retry the same signed release from the authenticated source.
                os.rename(destination, releases / (".corrupt-" + m["release_id"] + "-" + str(time.time_ns())))
        if not cached:
            # Remove only abandoned partials while holding the process lock.
            for old in releases.glob(".partial-*"):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old)
            temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=releases))
            try:
                path = temporary / m["filename"]
                started, length = time.monotonic(), 0
                with open_stream() as reader, path.open("xb") as writer:
                    while True:
                        if time.monotonic() - started > 300:
                            raise TimeoutError("电机固件下载超过五分钟，请重试")
                        chunk = reader.read(min(1024**2, m["size_bytes"] - length + 1))
                        if not chunk:
                            break
                        length += len(chunk)
                        if length > m["size_bytes"]:
                            raise ValueError("电机固件超过签名长度")
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                _valid_package(path, m)
                _atomic_json(temporary / "release.json", release)
                os.rename(temporary, destination)
                _sync_directory(releases)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        receipt = {"artifact_type": "motor_firmware", "status": "downloaded", "cache_hit": cached,
                   "manifest": m, "target_observation": dict(target),
                   "firmware_path": str(destination / m["filename"]),
                   "downloaded_at": datetime.now(timezone.utc).isoformat(),
                   "installed": False, "actual_version_verified": False}
        _atomic_json(destination / "download.json", receipt)
        return receipt
