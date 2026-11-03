"""Online software from the authenticated HTTPS platform publication channel.

The platform verifies release signatures at publication. These clients do not need
a per-user update key; they retain TLS, login, fixed APIs and full payload checks.
Offline and arbitrary URL update paths remain independently signature-verified.
"""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from factory_hmi import updates
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient


def _platform_origin(client: Any, identity: str) -> str:
    if not isinstance(client, ReliabilityPlatformClient) or not identity:
        raise ValueError("软件更新需要已登录的测试平台账号。")
    value = client.base_url
    target = urlsplit(value)
    if (target.scheme != "https" or not target.hostname or target.username is not None
            or target.password is not None or target.query or target.fragment
            or target.path not in {"", "/"} or any(ord(c) < 32 for c in value)):
        raise ValueError("在线更新只能使用已配置且验证证书的 HTTPS 平台根地址。")
    # Cookie presence is not a replacement for authentication: the dedicated
    # server endpoints still resolve/authorize the actual human session.
    with client._operator_lock:
        if not any(not cookie.is_expired() for cookie in client.cookies):
            raise ValueError("软件更新需要已登录的测试平台账号。")
    return value.rstrip("/")


def _platform_manifest(release: dict, host: dict) -> dict:
    if not isinstance(release, dict) or set(release) != {"manifest", "signature"}:
        raise ValueError("平台软件发布清单格式无效。")
    manifest = updates._manifest(release["manifest"])
    # Retain the server's publication contract. Authenticity here comes from the
    # authenticated TLS endpoint, not a client-provided/adjacent public key.
    try:
        signature = base64.b64decode(release["signature"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("平台软件发布契约无效。") from exc
    if len(signature) != 64:
        raise ValueError("平台软件发布契约无效。")
    return updates._validate_release_target(manifest, host, require_newer=True)


def check_platform_update(client: ReliabilityPlatformClient, *, identity: str, host: dict) -> dict:
    _platform_origin(client, identity)
    result = client.software_latest(host)  # Fixed API; client refuses redirects.
    if (not isinstance(result, dict) or type(result.get("available")) is not bool
            or type(result.get("can_update")) is not bool):
        raise ValueError("平台软件更新响应无效。")
    if result["available"]:
        _platform_manifest(result.get("release"), host)
    return result


def stage_platform_update(client: ReliabilityPlatformClient, *, identity: str,
                          release_id: str, cache_dir: Path | str, host: dict,
                          before_publish=None) -> dict:
    # Refetch using this real session, never accept caller-supplied metadata or
    # package streams as if they came from the authenticated platform.
    origin = _platform_origin(client, identity)
    result = check_platform_update(client, identity=identity, host=host)
    if not result["can_update"]:
        raise ValueError("当前账号无权下载更新，请使用执行员或管理员账号。")
    if not result["available"] or not isinstance(result.get("release"), dict):
        raise ValueError("当前没有可下载的兼容新版本。")
    release = result["release"]
    manifest = _platform_manifest(release, host)
    if manifest["release_id"] != release_id:
        raise ValueError("平台发布版本已变化，请重新检查更新。")
    if before_publish is not None:
        before_publish()
    # The only stream source is the fixed authenticated package endpoint.
    with client.software_package(release_id) as stream:
        return updates._cache_verified_release(
            release, manifest=manifest, cache_dir=cache_dir, host=host,
            package_stream=stream,
            verification={"release_authentication": "authenticated_https_platform", "platform_origin": origin},
            before_publish=before_publish,
        )
