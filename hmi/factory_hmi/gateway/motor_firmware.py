"""Motor distribution coordinator using current local version observations."""
from pathlib import Path
import os
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
