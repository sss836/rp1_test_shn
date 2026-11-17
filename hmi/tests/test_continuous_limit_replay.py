import io
import json

import numpy as np
import pytest

from factory_hmi.core.continuous_limit_replay import RESOURCE_ROOT, load_limit_replay
from factory_hmi.limit_replay_assets import verify_bundle


@pytest.mark.parametrize("limb,count", [("left_short_arm", 10), ("right_short_arm", 10),
    ("left_arm", 14), ("right_arm", 14), ("left_short_leg", 10), ("right_short_leg", 10), ("waist_hip", 4)])
def test_bundled_limit_replay(limb, count):
    replay = load_limit_replay(limb)
    assert len(replay.events) == count
    assert replay.frame_at(replay.duration) == len(replay.time) - 1
    assert replay.frame_at(-1) == 0
    assert replay.picture_at(replay.duration) >= replay.picture_count - 1
    assert replay.pictures.read("000001.jpg").startswith(b"\xff\xd8")
    with np.load(io.BytesIO(replay.npz_bytes), allow_pickle=False) as data:
        if limb == "waist_hip":
            assert tuple(data["joint_names"]) == ("waist_roll_joint", "waist_yaw_joint")
            assert "motor_pos" not in data
        assert "motor_pos" not in data
        assert "joint_pos" not in data
    if "_arm" in limb:
        mask = replay.phases == "folded_shoulder"
        assert mask.any()
        assert np.allclose(np.rad2deg(replay.position[mask, 3]), 58 if limb.startswith("left") else -58)
    if "_leg" in limb:
        mask = replay.phases == "folded_transfer"
        assert mask.any()
        assert np.allclose(np.rad2deg(replay.position[mask, 2]), -145 if limb.startswith("left") else 145)


@pytest.mark.parametrize("module, signs", [
    ("short_leg", [-1, -1, -1, 1, -1]),
    ("short_arm", [-1] * 5),
    ("arm", [-1] * 7),
])
def test_left_and_right_replays_are_mirrored(module, signs):
    left = load_limit_replay(f"left_{module}")
    right = load_limit_replay(f"right_{module}")
    np.testing.assert_array_equal(left.time, right.time)
    np.testing.assert_allclose(left.position * signs, right.position, atol=1.e-8)


def test_package_manifest_rejects_missing_or_unverified_assets(tmp_path):
    manifest = json.loads((RESOURCE_ROOT / "manifest.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    entry = next(iter(manifest.values()))["npz"]
    (tmp_path / entry["file"]).write_bytes(b"invalid npz")
    with pytest.raises(ValueError, match="校验失败"):
        verify_bundle(tmp_path)
