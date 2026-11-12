"""Motor distribution coordinator using current local version observations."""
from pathlib import Path
import json
import os
import stat
from factory_hmi.motor_firmware import verify_firmware, stage_firmware


def target_from_observation(row):
    if row.get("status") != "known" or not all(row.get(k) for k in ("model", "hardware", "protocol")):
        raise ValueError("实际电机版本未知、过期或缺少硬件/协议版本，不能选择兼容固件")
    current = row.get("raw_firmware") or row.get("software")
    if not current:
        raise ValueError("未取得实际固件版本")
    return {"model": row["model"], "hardware": row["hardware"], "protocol": row["protocol"],
            "current_version": current, "interface": row["interface"], "motor_id": row["motor_id"],
            "observed_at": row["observed_at"]}


class MotorFirmwareDownloads:
    def __init__(self, software_updates, data_root):
        self.sessions = software_updates
        self.cache_root = Path(data_root) / "motor-firmware"

    @staticmethod
    def key():
        value = os.environ.get("RP1_MOTOR_FIRMWARE_PUBLIC_KEY", "").strip()
        if not value:
            raise ValueError("尚未配置可信电机固件公钥")
        return Path(value).expanduser()

    def check(self, observation):
        target = target_from_observation(observation)
        client, _ = self.sessions._session()
        result = client.motor_firmware_releases(target)
        for release in result.get("releases", []):
            verify_firmware(release, self.key(), target)
        return {**result, "target": target}

    def download(self, release_id, observation):
        target = target_from_observation(observation)
        client, identity = self.sessions._session()
        result = self.check(observation)
        release = next((r for r in result.get("releases", []) if r["manifest"]["release_id"] == release_id), None)
        if release is None:
            raise ValueError("适用固件目录已变化，请重新查询")
        receipt = stage_firmware(release, key=self.key(), target=target, cache_root=self.cache_root,
            open_stream=lambda: client.motor_firmware_package(release_id, target))
        current, current_identity = self.sessions._session()
        if current is not client or current_identity != identity:
            raise ValueError("下载期间账号已变化；文件仅为缓存，请重新登录查询")
        return receipt

    def import_signed_release(self, manifest_path, observation):
        target = target_from_observation(observation)
        source = Path(manifest_path).expanduser()
        if not source.is_absolute():
            raise ValueError("请选择本机签名固件清单的绝对路径")
        release = read_firmware_release(source)
        key = self.key()
        manifest = verify_firmware(release, key, target)
        policy_source = source.parent / "motor-ota-policy.json"
        policy = None
        policy_target = None
        if policy_source.exists() or policy_source.is_symlink():
            from .motor_maintenance import read_signed_ota_policy
            state_root = os.environ.get("RP1_HMI_STATE_ROOT", "")
            if not state_root:
                raise ValueError("缺少上位机本机状态目录，无法安装 OTA 兼容映射")
            policy_target = Path(state_root) / "config" / "motor-ota-policy.json"
            configured = os.environ.get("RP1_MOTOR_OTA_POLICY", "")
            if configured and Path(configured).expanduser() != policy_target:
                raise ValueError("工位已配置专用 OTA 映射，请通过原配置路径维护")
            policy = read_signed_ota_policy(policy_source, key)
        receipt = stage_firmware(release, key=key, target=target, cache_root=self.cache_root,
                                 open_stream=lambda: _open_local_file(source.parent / manifest["filename"]))
        if policy_target is not None:
            from factory_hmi.updates import _atomic_json
            _atomic_json(policy_target, policy)
            receipt["policy_imported"] = True
        return receipt


def read_firmware_release(path):
    limit = 128 * 1024
    with _open_local_file(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("签名固件清单过大")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("签名固件清单不是有效的 JSON") from exc


def _open_local_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        stream.close()
        raise ValueError("导入文件须为本机普通文件")
    return stream
