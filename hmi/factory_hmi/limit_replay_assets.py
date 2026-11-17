"""Integrity checks for the installed offline limit replay resources."""
import hashlib
import json
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parent / "resources" / "limit-replay"
PRESET_KEYS = frozenset({"left_short_arm", "right_short_arm", "left_arm", "right_arm",
                         "left_short_leg", "right_short_leg", "waist_hip"})


def read_manifest(root: Path) -> dict:
    items = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if set(items) != PRESET_KEYS:
        raise ValueError("连续限位检查资源的模块清单不完整")
    return items


def verified_asset(root: Path, asset: dict) -> tuple[Path, bytes]:
    name = asset["file"]
    path = (root / name).resolve(strict=True)
    if path.parent != root.resolve() or path.name != name:
        raise ValueError("轨迹资源文件名无效")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError(f"轨迹资源校验失败：{name}")
    return path, data


def verify_bundle(root: Path) -> list[str]:
    manifest = read_manifest(root)
    for files in manifest.values():
        verified_asset(root, files["npz"])
        verified_asset(root, files["video"])
        verified_asset(root, files["frames"])
        if "route" in files:
            verified_asset(root, files["route"])
    return sorted(manifest)
