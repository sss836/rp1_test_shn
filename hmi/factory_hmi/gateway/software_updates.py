"""Software-only OTA orchestration, independent of test archive transport."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
import threading
from typing import Any


class SoftwareUpdates:
    def __init__(self, platform: Any, data_root: Path) -> None:
        from factory_hmi.version import code_version
        self.platform = platform
        self.running_version = code_version()
        self.cache_root = data_root / "software-updates"
        self._transfer = threading.Lock()

    def _session(self) -> tuple[Any, str]:
        # Only snapshot references under the coordinator lock. Network I/O,
        # hashing and package writes never hold its presence/submission lock.
        with self.platform._lock:
            uploader = self.platform._uploader
            identity = self.platform._identity
            if self.platform._mode != "reliability_v1" or not identity or uploader is None:
                raise ValueError("请先登录测试平台再检查软件更新。")
            return uploader.client, identity

    def _assert_same_session(self, client: Any, identity: str) -> None:
        current, current_identity = self._session()
        if current is not client or current_identity != identity:
            raise ValueError("检查或下载期间平台账号已变化，请重新登录后检查更新。")

    def check(self) -> dict[str, Any]:
        client, identity = self._session()
        from factory_hmi.updates import detect_host
        from .platform_software_updates import check_platform_update
        host = detect_host()
        if self.running_version:
            host = replace(host, current_version=self.running_version) if is_dataclass(host) else {
                **host, "current_version": self.running_version}
        host_data = asdict(host) if is_dataclass(host) else dict(host)
        result = check_platform_update(client, identity=identity, host=host_data)
        self._assert_same_session(client, identity)
        return {**result, "host": host_data, "source": "platform"}

    def download(self, release_id: str) -> dict[str, Any]:
        if not self._transfer.acquire(blocking=False):
            raise ValueError("本机已有软件下载正在进行，请稍后查看。")
        try:
            client, identity = self._session()
            result = self.check()
            self._assert_same_session(client, identity)
            if result.get("can_update") is not True:
                raise ValueError("当前账号无权发起更新；请使用执行员或管理员账号。")
            release = result.get("release")
            if not result.get("available") or not isinstance(release, dict):
                raise ValueError("当前没有可下载的兼容新版本。")
            if release.get("manifest", {}).get("release_id") != release_id:
                raise ValueError("平台发布版本已变化，请重新检查更新。")
            from .platform_software_updates import stage_platform_update
            receipt = stage_platform_update(client, identity=identity, release_id=release_id,
                                            cache_dir=self.cache_root, host=result["host"],
                                            before_publish=lambda: self._assert_same_session(client, identity))
            self._assert_same_session(client, identity)
            return receipt
        finally:
            self._transfer.release()


def installation_readiness(hub: Any) -> dict[str, Any]:
    """Never turn cached software booleans into physical torque-off evidence.

    The current motor telemetry has position/speed/error but no acknowledged
    enable-state field. Until that evidence is provided by the device adapter,
    this check intentionally cannot grant automatic installation permission.
    """
    reasons: list[str] = []
    with hub._lock:
        runtimes = list(hub._sessions.items())
    if not runtimes:
        reasons.append("尚未取得全部窗口的设备状态。")
    for owner, runtime in runtimes:
        value = runtime._invoke(("snapshot", "get_snapshot"))
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        station = value.get("station") or {}
        playback = value.get("playback") or {}
        completion = playback.get("completion") or {}
        if (str(station.get("state") or value.get("state")) in {"running", "paused", "armed"}
                or completion.get("active") is True):
            reasons.append(f"窗口 {owner} 尚未停止测试或收尾。")
        if station.get("motors_enabled") is not False:
            reasons.append(f"窗口 {owner} 未报告电机失能。")
    # MotorSample currently carries no physical enabled/disabled acknowledgement.
    reasons.append("当前电机反馈不含可核实的实际失能确认，不能自动签发安全安装证明。")
    plc = hub.plc.snapshot()
    status = plc.get("status") or {}
    feedback = (plc.get("feedback") or {}).get("AllStop") or {}
    if (plc.get("driver") != "modbus_tcp" or plc.get("communication_state") != "LIVE"
            or plc.get("stale") is not False or status.get("MainReady") is not False
            or status.get("ChannelPermit") != [False, False, False, False]
            or status.get("MainContactorFB") is not False
            or status.get("PS1FeedbackValid") is not True
            or status.get("PS1StopOffConfirmed") is not True
            or feedback.get("state") != "confirmed"):
        reasons.append("尚未核实 PLC 正常总停止的新鲜实际反馈。")
    return {"ready": False, "reasons": reasons,
            "next_step": "保留已校验安装包。维护人员核实设备安全状态并正常退出旧实例后，按更新说明安装。"}
