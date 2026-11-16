"""Provision approved offline resources from the installed package, never flash.

The source-pinned digest is the trust anchor for this bundle. It is not supplied
by a catalog/download. All existing custom keys and policies remain untouched.
Only public material is shipped; each firmware manifest and policy is signed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

BUNDLE_SHA256 = "0201683b1bbb7caa9171337bbf22f34322b24772f7f6491c9ecc4f01248412c7"
PUBLIC_KEY_SHA256 = "b7e7b1771dc311635177fa711805c7f4096bad8fec1267afe0e669c5901ae23f"
MODELS = frozenset({"LRO_5550", "LRO_6562", "LRO_8462", "LRO_10062"})
FIRMWARE_SHA256 = "7277645da5e2d461e0339e87e21b5603abcb13065416fb416a1e279d48eb966a"


def _digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"电机资源不是普通文件：{path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_signed(envelope, field, key):
    with tempfile.TemporaryDirectory(prefix="rp1-bundle-verify-") as directory:
        root = Path(directory)
        (root / "message").write_text(json.dumps(envelope[field], sort_keys=True,
            separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        (root / "signature").write_bytes(base64.b64decode(envelope["signature"], validate=True))
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key),
            "-rawin", "-in", str(root / "message"), "-sigfile", str(root / "signature")],
            capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("安装包电机资源签名校验失败")


def verify_bundle(bundle):
    bundle = Path(bundle)
    if bundle.is_symlink() or _digest(bundle / "bundle.json") != BUNDLE_SHA256:
        raise ValueError("安装包电机资源清单缺失或 SHA256 不符")
    manifest = json.loads((bundle / "bundle.json").read_text())
    if manifest["schema"] != "rp1-bundled-motor-assets-v1":
        raise ValueError("电机资源格式不支持")
    actual_files = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()}
    if actual_files != set(manifest["files"]) | {"bundle.json"}:
        raise ValueError("电机资源包含未列入清单的文件或缺少文件")
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("电机资源路径无效")
        path = bundle / name
        if not path.resolve().is_relative_to(bundle.resolve()) or _digest(path) != expected:
            raise ValueError(f"安装包电机资源损坏：{name}")
    key = bundle / "motor-firmware-public-key.pem"
    if _digest(key) != PUBLIC_KEY_SHA256:
        raise ValueError("安装包电机资源公钥不符")
    policy = json.loads((bundle / "motor-ota-policy.json").read_text())
    _verify_signed(policy, "policy", key)
    if policy["policy"]["preferred_109_sha256"] != FIRMWARE_SHA256:
        raise ValueError("OTA 映射未指向批准的 10 月 28 日固件")
    rows = manifest["releases"]
    if len(rows) != 4 or {row["model"] for row in rows} != MODELS:
        raise ValueError("安装包必须包含四款电机的升级资源")
    for row in rows:
        release = json.loads((bundle / "releases" / row["release_id"] / "release.json").read_text())
        _verify_signed(release, "manifest", key)
        m = release["manifest"]
        if (m["model"] != row["model"] or m["release_id"] != row["release_id"]
                or m["sha256"] != FIRMWARE_SHA256 or m["filename"] != row["filename"]):
            raise ValueError("固件型号或文件身份不符")
    return manifest


def _directory(path):
    if path.is_symlink():
        raise ValueError(f"电机资源目录不能是符号链接：{path.name}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _seed_file(source, destination):
    """No replacement, including a concurrently created custom setting."""
    if destination.exists() or destination.is_symlink():
        return _digest(destination) == _digest(source)
    fd, name = tempfile.mkstemp(prefix=".motor-seed-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(source.read_bytes())
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return _digest(destination) == _digest(source)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def seed_motor_assets(state_root, bundle):
    """Idempotent per Linux user/state root; no device/network access."""
    root, bundle = Path(state_root), Path(bundle)
    manifest = verify_bundle(bundle)  # validate everything before any provisioning
    config = _directory(root / "config")
    data = _directory(root / "data")
    cache = _directory(data / "motor-firmware")
    fd = os.open(cache / "bundle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not _seed_file(bundle / "motor-firmware-public-key.pem", config / "motor-firmware-public-key.pem"):
            return {"status": "custom_key_preserved", "imported": 0}
        policy_matches = _seed_file(bundle / "motor-ota-policy.json", config / "motor-ota-policy.json")
        releases = _directory(cache / "releases")
        imported = 0
        for row in manifest["releases"]:
            source = bundle / "releases" / row["release_id"]
            destination = releases / row["release_id"]
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink():
                    raise ValueError("固件缓存不能是符号链接")
                for name in ("release.json", row["filename"]):
                    if _digest(destination / name) != _digest(source / name):
                        raise ValueError("已有固件缓存内容冲突，请检查维护详情")
                continue
            temporary = Path(tempfile.mkdtemp(prefix=".bundled-", dir=releases))
            try:
                for name in ("release.json", row["filename"]):
                    shutil.copyfile(source / name, temporary / name)
                    (temporary / name).chmod(0o600)
                os.rename(temporary, destination)
                imported += 1
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        receipt = {"status": "ready" if policy_matches else "custom_policy_preserved", "imported": imported,
            "models": sorted(MODELS), "bundle_sha256": BUNDLE_SHA256,
            "firmware_sha256": FIRMWARE_SHA256, "source": "installed_package",
            "device_writes": False, "verified_at": datetime.now(timezone.utc).isoformat()}
        target = cache / "bundle-import.json"
        fd, name = tempfile.mkstemp(prefix=".bundle-receipt-", dir=cache)
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        os.replace(name, target)
        return receipt
