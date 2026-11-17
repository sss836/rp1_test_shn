"""Load the offline limit replays supplied with the desktop application."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import math
from pathlib import Path
from zipfile import ZipFile

import numpy as np

from .limit_block_check import Coordinates, MODULES, nominal_station
from factory_hmi.limit_replay_assets import PRESET_KEYS, RESOURCE_ROOT, read_manifest, verified_asset

SUPPORTED = tuple((key, label) for key, label in MODULES if key in PRESET_KEYS)
PHASES = {
    "home": "默认位", "transfer": "联动转场", "reset": "就位", "baseline": "采集力矩基线",
    "search": "低速搜索", "unload": "停止推进", "retreat": "缓退",
    "fold_knee": "先屈膝", "folded_transfer": "保持屈膝 · 髋侧摆", "unfold_transfer": "髋回正后展开膝部",
    "fold_elbow": "先屈肘", "folded_shoulder": "保持屈肘 · 肩部摆动",
    "gain_transition": "切换控制增益", "homing": "自动回轨迹起点", "positioning": "摆到下一检查位",
    "clearance": "离开搜索区", "returning": "全部检查完毕 · 回起点", "finished": "检查完成 · 已回起点",
}


@dataclass(frozen=True)
class ReplayEvent:
    label: str
    axis: int
    reference_deg: float
    simulated_deg: float
    model_boundary: bool
    start: int
    contact: int
    end: int


@dataclass(frozen=True)
class LimitReplay:
    npz_name: str
    npz_bytes: bytes
    video: Path
    speed: float
    pictures: ZipFile
    picture_hz: int
    picture_count: int
    time: np.ndarray
    position: np.ndarray
    phases: np.ndarray
    events: tuple[ReplayEvent, ...]

    @property
    def duration(self) -> float:
        return float(self.time[-1])

    def frame_at(self, second: float) -> int:
        return max(0, min(len(self.time) - 1, int(np.searchsorted(self.time, second, side="right") - 1)))

    def picture_at(self, second: float) -> int:
        if second >= self.duration:
            return self.picture_count
        return min(self.picture_count, max(1, int(second / self.speed * self.picture_hz) + 1))


def load_limit_replay(limb: str, root: Path = RESOURCE_ROOT) -> LimitReplay:
    if limb not in PRESET_KEYS:
        raise ValueError("当前模块没有内置连续轨迹")
    bundle = read_manifest(root)[limb]
    source, data = verified_asset(root, bundle["npz"])
    video, _ = verified_asset(root, bundle["video"])
    _, pictures_data = verified_asset(root, bundle["frames"])
    pictures = ZipFile(BytesIO(pictures_data))
    picture_hz, picture_count = (bundle["frames"][key] for key in ("fps", "count"))
    if (type(picture_hz) is not int or type(picture_count) is not int or picture_hz <= 0 or picture_count <= 0
            or pictures.namelist() != [f"{i:06d}.jpg" for i in range(1, picture_count + 1)]):
        raise ValueError("动作画面的帧索引无效")
    speed = float(bundle["video_speed"])
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("视频速率无效")
    coords = Coordinates(nominal_station(limb))
    with np.load(BytesIO(data), allow_pickle=False) as stored:
        if str(stored["limb"].item()) != limb or tuple(stored["joint_names"].tolist()) != coords.names:
            raise ValueError("连续轨迹的机型或具名关节不匹配")
        if (limb == "waist_hip" or "route" in bundle) and "motor_pos" in stored:
            raise ValueError("模拟资源不得携带工位电机轨迹")
        time, position = stored["time"], stored["sim_joint_pos"]
        phases = stored["phase_labels"]
        event_rows = json.loads(str(stored["event_report_json"].item()))
    if (time.ndim != 1 or time.size < 2 or time[0] != 0 or not np.all(np.isfinite(time))
            or not np.all(np.diff(time) > 0) or position.shape != (len(time), len(coords.names))
            or not np.all(np.isfinite(position)) or phases.shape != time.shape
            or not set(np.unique(phases)).issubset(PHASES)):
        raise ValueError("连续轨迹的时间、位置或阶段数据无效")
    known = {(d.joint, d.sign): d for d in coords.directions}
    events = []
    for row in event_rows:
        direction = known.pop((row["joint"], row["direction"]), None)
        start, contact, end = (row[name] for name in ("start_frame", "contact_frame", "end_frame"))
        if (direction is None or not all(type(i) is int for i in (start, contact, end))
                or not 0 <= start <= contact <= end < len(time) or events and events[-1].end >= start
                or direction.reference_deg != row["reference_deg"]):
            raise ValueError("连续轨迹的检查窗口与目录不符")
        actual = float(row["actual_deg"])
        if (not math.isfinite(actual) or abs(math.degrees(position[contact, direction.index]) - actual) > .05):
            raise ValueError("模拟止挡角与轨迹样本不匹配")
        events.append(ReplayEvent(f"{direction.label} · {direction.label_direction}", direction.index,
                                  direction.reference_deg, actual, row["sim_stop_source"] != "mechanical_reference",
                                  start, contact, end))
    if known:
        raise ValueError("存在缺失的关节检查方向")
    return LimitReplay(source.name, data, video, speed, pictures, picture_hz, picture_count,
                       time, position, phases, tuple(events))
