"""Trusted cache/policy and human role checks for local module maintenance."""
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from factory_hmi.motor_firmware import verify_firmware
from factory_hmi.motor_ota_policy import BASELINE_109_SHA256, V2, numeric_version, validate_policy
from factory_hmi.core.motor_versions import firmware_generation
from .motor_firmware import target_from_observation


def authorize(downloads):
    client, identity = downloads.sessions._session()
    grant = client._call("GET", "/api/v1/auth/me", service_auth=False, timeout=3)
    user = grant.get("user") or {}
    if (grant.get("session_type") != "BROWSER" or user.get("kind") != "HUMAN" or
            user.get("role") not in {"TEST_EXECUTOR", "SYSTEM_ADMIN"} or user.get("enabled") is not True
            or user.get("must_change_password") is not False or user.get("username") != identity):
        raise ValueError("电机维护需要当前登录的执行员或管理员真人会话")
    current, current_identity = downloads.sessions._session()
    if current is not client or current_identity != identity:
        raise ValueError("维护权限核对期间账号已变化")
    return {"username": identity, "client_session": str(id(client)), "role": user["role"]}


def compatibility_policy(key, target, manifest):
    name = os.environ.get("RP1_MOTOR_OTA_POLICY", "")
    if not name:
        raise ValueError("缺少管理员签名的108前提及完整版本兼容映射；禁止刷写")
    path = Path(name).expanduser()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("OTA兼容映射过大")
    envelope = json.loads(path.read_text())
    if not isinstance(envelope, dict) or set(envelope) != {"policy", "signature"} or not isinstance(envelope["signature"], str):
        raise ValueError("OTA兼容映射签名封装无效")
    policy = envelope["policy"]
    raw = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    from factory_hmi.updates import _public_key
    trusted = _public_key(key)
    with tempfile.TemporaryDirectory(prefix="rp1-ota-policy-") as temp:
        data, signature = Path(temp)/"policy", Path(temp)/"signature"
        data.write_bytes(raw)
        signature.write_bytes(base64.b64decode(envelope["signature"], validate=True))
        verified = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(trusted),
                                   "-rawin", "-in", str(data), "-sigfile", str(signature)],
                                  capture_output=True, timeout=10)
    if verified.returncode:
        raise ValueError("OTA兼容映射签名或类型不可信")
    validate_policy(policy)
    if policy["schema"] == V2 and manifest["sha256"] != policy.get("preferred_109_sha256", BASELINE_109_SHA256):
        raise ValueError("不是当前批准的 109 固件构建；不会按版本简称回退到其他固件")
    for row in policy["entries"]:
        if (row.get("model"), row.get("hardware"), row.get("protocol"), row.get("raw_version")) != (
                target["model"], target["hardware"], target["protocol"], target["current_version"]):
            continue
        version = row["software_version"]
        if policy["schema"] == V2:
            dest = row["target"]
            if (dest["raw_version"], dest["software_version"], dest["firmware_sha256"]) != (
                    manifest["version"], manifest["software_version"], manifest["sha256"]):
                continue
        elif numeric_version(manifest["software_version"]) <= numeric_version(version):
            raise ValueError("OTA目标软件版本必须高于实际版本")
        if version != target.get("software"):
            raise ValueError("实际数值软件版本与签名映射不一致")
        return {"entry": row, "schema": policy["schema"], "policy_sha256": hashlib.sha256(raw).hexdigest()}
    raise ValueError("没有匹配型号、硬件、协议、完整版本和固件的OTA适用记录")


def artifact(downloads, release_id, observation):
    target = target_from_observation(observation)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(target["observed_at"].replace("Z", "+00:00"))).total_seconds()
    if not 0 <= age <= 180:
        raise ValueError("实际版本证据过期或时钟异常")
    if not re.fullmatch(r"[0-9a-f]{32}", release_id):
        raise ValueError("无效的电机固件发布编号")
    root = downloads.cache_root / "releases" / release_id
    if root.is_symlink() or (root / "release.json").is_symlink():
        raise ValueError("不接受符号链接固件缓存")
    release = json.loads((root / "release.json").read_text())
    verify_firmware(release, downloads.key(), target)
    manifest = release["manifest"]
    if manifest["release_id"] != release_id:
        raise ValueError("缓存发布编号不匹配")
    path = root / manifest["filename"]
    if path.is_symlink():
        raise ValueError("不接受符号链接固件")
    if path.stat().st_size != manifest["size_bytes"]:
        raise ValueError("电机固件大小或SHA256校验失败")
    data = path.read_bytes()
    if data.startswith(b"!<arch>\n") or len(data) != manifest["size_bytes"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("电机固件类型、大小或SHA256校验失败")
    policy = compatibility_policy(downloads.key(), {**target, "software": observation.get("software")}, manifest)
    if firmware_generation(manifest["version"]) == 109 and policy["schema"] != V2:
        raise ValueError("109 固件缺少包含目标协议及固件哈希的签名映射")
    return {"manifest": manifest, "data": data, "policy": policy}


def cached_releases(downloads, observation):
    """Only signed motor manifests are offered; start rechecks bytes and OTA policy."""
    target = target_from_observation(observation)
    result = []
    for root in sorted((downloads.cache_root / 'releases').glob('*')):
        if not re.fullmatch(r'[0-9a-f]{32}', root.name) or root.is_symlink():
            continue
        path = root / 'release.json'
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 128*1024:
                continue
            release = json.loads(path.read_text())
            manifest = verify_firmware(release, downloads.key(), target)
            if manifest['release_id'] == root.name:
                result.append(manifest)
        except (ValueError, OSError):
            continue
    return result


def cached_109_releases(downloads, observation, manifests):
    """Offer only signed transitions with complete source and target evidence."""
    target = {**target_from_observation(observation), "software": observation.get("software")}
    choices, reasons = [], []
    for manifest in manifests:
        if firmware_generation(manifest["version"]) != 109:
            continue
        try:
            policy = compatibility_policy(downloads.key(), target, manifest)
            if policy["schema"] != V2:
                raise ValueError("109 固件缺少包含目标协议及固件哈希的签名映射")
            path = downloads.cache_root / "releases" / manifest["release_id"] / manifest["filename"]
            if path.is_symlink() or path.stat().st_size != manifest["size_bytes"]:
                raise ValueError("109 固件本地缓存不完整，请重新下载")
            choices.append(manifest)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            reasons.append(str(exc))
    if not choices and reasons:
        raise ValueError(reasons[0])
    return choices


def visible_job(task, actor):
    """A reauthenticated owner may read an interrupted receipt, never replay it.

    Write/ack/readback still require the exact original session in the task.
    """
    job = task.snapshot()
    if job.get("actor") and job["actor"].get("username") != actor.get("username"):
        return {"active": task.active, "status": "other_account", "targets": []}
    return job
