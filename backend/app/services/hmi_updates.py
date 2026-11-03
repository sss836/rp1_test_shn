"""Signed DEB distribution, separate from test-data storage and approvals.

Only a complete validated directory is atomically made visible. A process-wide
file lock serializes the release-id and target/version uniqueness checks across
API workers; incomplete uploads never become releases.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import BinaryIO

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas.hmi_updates import SignedRelease


def canonical_manifest(release: SignedRelease) -> bytes:
    return json.dumps(release.manifest.model_dump(), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_release(raw: bytes) -> SignedRelease:
    try:
        data = json.loads(raw, object_pairs_hook=_json_object)
        return SignedRelease.model_validate(data)
    except (ValueError, TypeError, UnicodeError, ValidationError) as exc:
        raise ApiError(422, "invalid_hmi_release", "软件发布清单不符合签名契约。") from exc


def _run(args: list[str], *, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, input=input, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(503, "hmi_update_tool_unavailable", "软件更新校验工具不可用或超时。") from exc


def validate_version(version: str) -> None:
    if (not re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]{0,199}", version)
            or _run(["dpkg", "--validate-version", version]).returncode != 0):
        raise ApiError(422, "invalid_hmi_version", "版本必须为有效 Debian 版本。")


def version_newer(candidate: str, current: str) -> bool:
    validate_version(candidate)
    validate_version(current)
    result = _run(["dpkg", "--compare-versions", candidate, "gt", current])
    if result.returncode not in (0, 1):
        raise ApiError(503, "hmi_version_compare_failed", "无法比较软件版本。")
    return result.returncode == 0


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class HmiUpdateStorage:
    parse_release = staticmethod(parse_release)

    def validate_release_version(self, release):
        validate_version(release.manifest.version)

    def target_version(self, manifest):
        return (manifest.os_id, manifest.os_version, manifest.architecture, manifest.version)

    def same_content(self, old, manifest):
        return old.sha256 == manifest.sha256 and old.size_bytes == manifest.size_bytes

    def __init__(self, settings: Settings):
        if (not settings.hmi_updates_enabled or not settings.hmi_updates_root.strip()
                or not settings.hmi_updates_public_key_file.strip()):
            raise ApiError(503, "hmi_updates_unavailable", "软件更新服务未启用或可信配置尚未完成。")
        self.settings = settings
        raw_root = Path(settings.hmi_updates_root).expanduser()
        if not raw_root.is_absolute():
            raise ApiError(503, "hmi_updates_unavailable", "软件更新持久目录必须为仓库外绝对路径。")
        self.root = raw_root.resolve()
        application_root = Path(__file__).resolve().parents[2]
        # Checkout: <repository>/backend/app/services; image: /app/app/services.
        # A fixed parents[3] is '/' in the image and would reject every store.
        repository = application_root.parent if application_root.name == "backend" else application_root
        archive = Path(settings.archive_root).expanduser().resolve()
        if any(self.root == boundary or self.root in boundary.parents or boundary in self.root.parents
               for boundary in (repository, archive)):
            raise ApiError(503, "hmi_updates_unavailable", "软件发布目录不能与仓库或测试归档目录重叠。")
        try:
            key_path = Path(settings.hmi_updates_public_key_file).expanduser().resolve(strict=True)
            key_info = key_path.stat()
            if not stat.S_ISREG(key_info.st_mode) or key_info.st_mode & 0o022 or key_info.st_size > 16384:
                raise ValueError("invalid public key file")
            self.public_key = key_path.read_bytes()
            # Ed25519 SubjectPublicKeyInfo has this fixed DER prefix and 32-byte key.
            key = _run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=self.public_key)
            if (key.returncode != 0 or len(key.stdout) != 44
                    or not key.stdout.startswith(bytes.fromhex("302a300506032b6570032100"))):
                raise ValueError("not an Ed25519 public key")
            self.releases = self.root / "releases"
            self.incoming = self.root / ".incoming"
            self.releases.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.incoming.mkdir(mode=0o700, exist_ok=True)
            if self.releases.is_symlink() or self.incoming.is_symlink():
                raise ValueError("symlink storage directory")
        except (OSError, ValueError) as exc:
            raise ApiError(503, "hmi_updates_unavailable", "软件更新持久目录或可信公钥不可用。") from exc

    def from_header(self, encoded: str) -> SignedRelease:
        if len(encoded) > ((self.settings.hmi_updates_max_manifest_bytes + 2) // 3) * 4:
            raise ApiError(413, "hmi_manifest_too_large", "软件发布清单超过大小限制。")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ApiError(422, "invalid_hmi_release", "X-HMI-Release 必须为 base64 发布清单。") from exc
        if len(raw) > self.settings.hmi_updates_max_manifest_bytes:
            raise ApiError(413, "hmi_manifest_too_large", "软件发布清单超过大小限制。")
        release = self.parse_release(raw)
        if release.manifest.size_bytes > self.settings.hmi_updates_max_package_bytes:
            raise ApiError(413, "hmi_package_too_large", "软件包超过大小限制。")
        self.verify_signature(release)
        return release

    def verify_signature(self, release: SignedRelease) -> None:
        self.validate_release_version(release)
        try:
            signature = base64.b64decode(release.signature, validate=True)
            if len(signature) != 64:
                raise ValueError("signature length")
        except (ValueError, binascii.Error) as exc:
            raise ApiError(422, "invalid_hmi_signature", "软件发布签名格式无效。") from exc
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=self.incoming) as directory:
            temporary = Path(directory)
            (temporary / "public.pem").write_bytes(self.public_key)
            (temporary / "manifest.json").write_bytes(canonical_manifest(release))
            (temporary / "signature.bin").write_bytes(signature)
            result = _run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey",
                           str(temporary / "public.pem"), "-rawin", "-in",
                           str(temporary / "manifest.json"), "-sigfile",
                           str(temporary / "signature.bin")])
        if result.returncode != 0:
            raise ApiError(422, "invalid_hmi_signature", "软件发布清单未通过可信 Ed25519 签名校验。")

    @contextmanager
    def lock(self):
        fd = os.open(self.root / ".publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def stage(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="upload-", dir=self.incoming))

    def _read_release(self, directory: Path) -> SignedRelease:
        try:
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("invalid release directory")
            fd = os.open(directory / "release.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invalid manifest file")
                raw = stream.read(self.settings.hmi_updates_max_manifest_bytes + 1)
            if len(raw) > self.settings.hmi_updates_max_manifest_bytes:
                raise ValueError("oversized stored manifest")
            release = self.parse_release(raw)
            if release.manifest.release_id != directory.name:
                raise ValueError("release id mismatch")
            self.verify_signature(release)
            return release
        except (OSError, ValueError, ApiError) as exc:
            raise ApiError(503, "hmi_release_corrupt", "软件发布目录校验失败，请管理员核对。") from exc

    def _catalog(self):
        for directory in sorted(self.releases.iterdir()):
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
                raise ApiError(503, "hmi_release_corrupt", "软件发布目录存在无效条目。")
            yield self._read_release(directory)

    def latest(self, os_id: str, os_version: str, architecture: str, current_version: str) -> SignedRelease | None:
        validate_version(current_version)
        latest = None
        for release in self._catalog():
            manifest = release.manifest
            if (manifest.os_id, manifest.os_version, manifest.architecture) != (os_id, os_version, architecture):
                continue
            if version_newer(manifest.version, current_version) and (
                    latest is None or version_newer(manifest.version, latest.manifest.version)):
                latest = release
        return latest

    def validate_package(self, path: Path, release: SignedRelease) -> None:
        manifest = release.manifest
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != manifest.size_bytes:
                raise ApiError(422, "hmi_package_size_mismatch", "软件包大小与签名清单不符。")
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest.sha256:
                raise ApiError(422, "hmi_package_hash_mismatch", "软件包 SHA256 与签名清单不符。")
        for field, expected in (("Package", manifest.package_name), ("Version", manifest.version),
                                ("Architecture", manifest.architecture)):
            result = _run(["dpkg-deb", "--field", str(path), field])
            if result.returncode != 0 or result.stdout.decode("utf-8", errors="replace").strip() != expected:
                raise ApiError(422, "hmi_package_metadata_mismatch", "DEB 包名、版本或架构与签名清单不符。")

    def publish(self, stage: Path, release: SignedRelease, *, actor_id: str, request_id: str) -> dict:
        manifest = release.manifest
        self.validate_package(stage / manifest.filename, release)
        # Everything needed to read a release is fsynced before its rename.
        envelope = json.dumps(release.model_dump(), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        receipt = {"publisher_id": actor_id, "request_id": request_id,
                   "received_at": datetime.now(timezone.utc).isoformat()}
        for name, raw in (("release.json", envelope), ("publication.json", json.dumps(receipt).encode())):
            with (stage / name).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        _fsync_directory(stage)
        with self.lock():
            for previous in self._catalog():
                old = previous.manifest
                same_target_version = self.target_version(old) == self.target_version(manifest)
                if old.release_id == manifest.release_id or same_target_version:
                    # A signed retry with the same package is idempotent even
                    # when its release id/notes/timestamp were regenerated. The
                    # first envelope and its publisher receipt stay immutable.
                    if same_target_version and self.same_content(old, manifest):
                        return {"release": previous.model_dump(), "created": False}
                    raise ApiError(409, "hmi_release_conflict", "发布编号或相同目标版本已存在，禁止覆盖；重试须使用原签名清单与相同软件包。")
            os.rename(stage, self.releases / manifest.release_id)
            _fsync_directory(self.releases)
        return {"release": release.model_dump(), "created": True}

    def package(self, release_id: str) -> tuple[SignedRelease, BinaryIO]:
        if not re.fullmatch(r"[0-9a-f]{32}", release_id):
            raise ApiError(404, "hmi_release_not_found", "软件发布版本不存在。")
        directory = self.releases / release_id
        if not directory.exists():
            raise ApiError(404, "hmi_release_not_found", "软件发布版本不存在。")
        release = self._read_release(directory)
        stream = None
        try:
            fd = os.open(directory / release.manifest.filename, os.O_RDONLY | os.O_NOFOLLOW)
            stream = os.fdopen(fd, "rb")
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size != release.manifest.size_bytes
                    or hashlib.file_digest(stream, "sha256").hexdigest() != release.manifest.sha256):
                raise ValueError("stored package size/hash mismatch")
            stream.seek(0)
            return release, stream
        except (OSError, ValueError) as exc:
            if stream:
                stream.close()
            raise ApiError(503, "hmi_release_corrupt", "软件包持久副本校验失败，已拒绝下载。") from exc
