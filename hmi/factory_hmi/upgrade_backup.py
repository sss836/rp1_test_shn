"""Offline upgrade snapshot; never restore over live state or auto-downgrade.

Caller must hold the launcher lock after confirming all writers have exited.
Only run/ (process identities and locks) is excluded. No network is used.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import time

SCHEMA = "rp1-hmi-upgrade-state-v1"


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_tree(source_fd, destination, records, prefix="", deadline=None):
    names = sorted(os.listdir(source_fd))
    identity = {}
    for name in names:
        if not prefix and name == "run":
            continue
        item = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        identity[name] = (item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    for name in names:
        if not prefix and name == "run":
            continue
        relative = prefix + name
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd)
            try:
                child = destination / name
                child.mkdir(mode=0o700)
                _copy_tree(fd, child, records, relative + "/", deadline)
                _sync(child)
            finally:
                os.close(fd)
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
            with os.fdopen(fd, "rb") as reader, (destination / name).open("xb") as writer:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("备份源文件类型发生变化。")
                os.chmod(destination / name, 0o600)
                digest, size = hashlib.sha256(), 0
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        raise TimeoutError("升级备份超时；未允许安装。")
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                after = os.fstat(reader.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("备份期间状态仍有写入，禁止升级。")
                writer.flush()
                os.fsync(writer.fileno())
            records[relative] = {"size": size, "sha256": digest.hexdigest()}
        else:
            raise ValueError("持久状态含符号链接或特殊文件，不能安全备份：" + relative)

    after = {}
    for name in sorted(os.listdir(source_fd)):
        if not prefix and name == "run":
            continue
        item = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        after[name] = (item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if identity != after:
        raise ValueError("备份期间目录或文件发生变化，禁止使用不一致快照。")


def verify_snapshot(snapshot):
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or (snapshot / "manifest.json").is_symlink():
        raise ValueError("备份路径不能是符号链接。")
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), dict):
        raise ValueError("备份清单无效。")
    payload = snapshot / "state"
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("备份内容目录无效。")
    actual = {}
    for parent, dirs, files in os.walk(payload, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("备份含符号链接。")
        for name in files:
            path = Path(parent) / name
            if not path.is_file():
                raise ValueError("备份含特殊文件。")
            with path.open("rb") as stream:
                actual[path.relative_to(payload).as_posix()] = {
                    "size": path.stat().st_size,
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    if actual != manifest["files"]:
        raise ValueError("备份完整性校验失败。")
    return manifest


def create_snapshot(state_root, destination, *, timeout=30):
    root = Path(state_root).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to(root):
        raise ValueError("备份必须位于状态目录之外。")
    destination.mkdir(mode=0o700, exist_ok=False)
    payload = destination / "state"
    payload.mkdir(mode=0o700)
    files = {}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _copy_tree(fd, payload, files, deadline=time.monotonic() + timeout)
    finally:
        os.close(fd)
    manifest = {"schema": SCHEMA, "source": str(root), "excluded": ["run"],
                "files": files, "restore_policy": "export_to_new_directory_only"}
    with (destination / "manifest.json").open("x") as stream:
        os.chmod(stream.name, 0o600)
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    _sync(payload)
    _sync(destination)
    _sync(destination.parent)
    verify_snapshot(destination)
    return {"path": str(destination), "verified": True, "file_count": len(files),
            "restore_policy": manifest["restore_policy"]}


def export_snapshot(snapshot, destination):
    """Export for manual reconciliation, never replace a running state root.

    Package downgrade and database schema compatibility require independent
    review. Fresh data/configuration must not be overwritten by an old snapshot.
    """
    snapshot = Path(snapshot)
    verify_snapshot(snapshot)
    destination = Path(destination)
    destination.mkdir(mode=0o700, exist_ok=False)
    fd = os.open(snapshot / "state", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    files = {}
    try:
        _copy_tree(fd, destination, files)
    finally:
        os.close(fd)
    if files != verify_snapshot(snapshot)["files"]:
        raise ValueError("恢复导出校验失败，禁止使用该目录。")
    _sync(destination)
    _sync(destination.parent)
    return destination
