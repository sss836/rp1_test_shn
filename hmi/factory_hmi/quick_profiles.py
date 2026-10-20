"""Versioned factory verification presets; trajectories are immutable local assets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parent / 'resources' / 'quick'
PRESETS = {
    'left_arm': ('左臂 · 7 自由度', 7, ['can0']),
    'right_arm': ('右臂 · 7 自由度', 7, ['can1']),
    'left_short_arm': ('左臂 · 5 自由度', 5, ['can0']),
    'right_short_arm': ('右臂 · 5 自由度', 5, ['can1']),
    'left_short_leg': ('左腿 · 5 自由度', 5, ['can0']),
    'right_short_leg': ('右腿 · 5 自由度', 5, ['can1']),
    # NPZ motor order: right waist_roll, left waist_yaw, right hip_pitch, left hip_pitch.
    'waist_hip': ('腰胯 · 4 自由度', 4, ['can1', 'can0', 'can1', 'can0']),
}


def catalog() -> list[dict]:
    manifest = json.loads((RESOURCE_ROOT / 'manifest.json').read_text())
    assets = {item['limb']: item for item in manifest['trajectories']}
    return [dict(id=key, label=label, motor_count=count, interfaces=list(interfaces),
                 config_path=key + '_motors.yaml', trajectory=assets[key]['file'],
                 sha256=assets[key]['sha256'], speed=1.0)
            for key, (label, count, interfaces) in PRESETS.items()]


def preset(key: str) -> dict:
    for item in catalog():
        if item['id'] == key:
            path = (RESOURCE_ROOT / item['trajectory']).resolve(strict=True)
            if path.parent != RESOURCE_ROOT.resolve():
                raise ValueError('内置轨迹路径无效')
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('内置轨迹校验失败，请重新安装完整安装包')
            return dict(item, trajectory_path=path)
    raise ValueError('未知快速验证部位')
