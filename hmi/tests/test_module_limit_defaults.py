"""Named built-in defaults only: no PLC/CAN or physical motor operations."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core import module_limit_defaults as defaults
from factory_hmi.core.limit_block_check import Coordinates, nominal_station
from factory_hmi.core.live_limit_probe import station_sha
from factory_hmi.core.module_limit_check import ModuleCycle, path_segments, validate_module_plan
from factory_hmi.core.service import FactoryService
from factory_hmi.core.production_parameters import load_script
from factory_hmi.limit_replay_assets import PRESET_KEYS


@pytest.mark.parametrize("limb", sorted(PRESET_KEYS))
def test_every_module_has_complete_named_feedback_defaults(limb):
    config = nominal_station(limb)
    coords = Coordinates(config, include_waist_holds=True)
    profile = defaults.builtin_module_profile(config)
    _, profile, context = validate_module_plan(config, profile, 'OFFLINE-DEFAULTS', 'unit-test')
    assert profile.limb == limb and profile.source == "builtin_reference"
    assert profile.station_sha256 == station_sha(config)
    assert profile.transfer_speed == 20.
    assert context['source_kind'] == 'physical' and context['execution_mode'] == 'feedback_limit_search'
    assert len(profile.windows) == (4 if limb == "waist_hip" else 2*len(coords.names))
    assert {(w.point.joint, w.point.direction) for w in profile.windows} == {
        (d.joint, d.sign) for d in coords.directions}
    directions = {(d.joint, d.sign): d for d in coords.directions}
    script = load_script()
    for row in profile.windows:
        p = row.point
        a = coords.names.index(p.joint)
        d = directions[(p.joint, p.direction)]
        assert p.full_range_search
        assert p.search_speed_deg_s == 10. and p.contact_speed_deg_s == 2.
        assert p.positioning_speed_deg_s == 20.
        assert p.staging_pose_deg == p.search_pose_deg and p.search_pose_deg[a] == 0.
        assert p.permitted_travel_deg == pytest.approx(p.direction*d.reference_deg+2.9)
        assert p.search_extension.actual_limit_deg == pytest.approx(d.reference_deg + 10.*d.sign)
        assert p.search_extension.command_limit_deg is None
        assert p.contact_floor_nm == p.contact_rise_nm == .5
        assert (p.reset_kp_scale, p.reset_kd_scale, p.search_kp_scale, p.search_kd_scale) == (1.,)*4
        assert tuple(p.motor_abort_nm) == tuple(min(35., script.MODEL_PARAMS[e.motor_model]['tau_max'])
                                               for e in coords.entries)
        assert tuple(p.joint_abort_nm) == tuple(p.motor_abort_nm)
        assert "station_cap_and_production_range" in profile.torque_limit_source
    FactoryService._check_probe_path(coords, lambda *_: None, path_segments(profile, coords.names))
    if limb not in {"left_arm", "left_short_arm"}:
        assert profile.revision == "all-module-feedback-v1"
        np.testing.assert_allclose(profile.home_deg, np.rad2deg(coords.home))


@pytest.mark.parametrize("limb,sign", [("left_short_leg", -1), ("right_short_leg", 1)])
def test_leg_hip_roll_retains_folded_knee_clearance(limb, sign):
    profile = defaults.builtin_module_profile(nominal_station(limb))
    coords = Coordinates(nominal_station(limb))
    knee = coords.names.index(f'{limb.split("_")[0]}_knee_joint')
    roll = f'{limb.split("_")[0]}_hip_roll_joint'
    for w in [w for w in profile.windows if w.point.joint == roll]:
        assert w.point.search_pose_deg[knee] == sign*145.
        assert w.via_deg and w.via_deg[-1][knee] == sign*145.


@pytest.mark.parametrize("limb", ["right_arm", "right_short_arm"])
def test_right_shoulder_yaw_uses_named_downward_elbow_mirror(limb):
    config = nominal_station(limb)
    coords = Coordinates(config)
    elbow = coords.names.index("right_elbow_joint")
    yaw = [w for w in defaults.builtin_module_profile(config).windows
           if w.point.joint == "right_shoulder_yaw_joint"]
    assert {w.point.direction for w in yaw} == {-1, 1}
    for w in yaw:
        assert w.point.search_pose_deg[elbow] == pytest.approx(80.803, abs=.01)
        assert all(p[elbow] == pytest.approx(80.803, abs=.01) for p in w.via_deg)


def test_waist_has_only_two_checked_axes_and_preserves_configured_hips():
    config = nominal_station("waist_hip")
    profile = defaults.builtin_module_profile(config)
    assert {w.point.joint for w in profile.windows} == {"waist_roll_joint", "waist_yaw_joint"}
    assert len(profile.windows) == 4
    assert len(profile.home_deg) == 4
    for w in profile.windows:
        assert w.point.search_pose_deg[2:] == profile.home_deg[2:]
        assert all(p[2:] == profile.home_deg[2:] for p in w.via_deg)


def test_caps_and_identity_come_from_current_station_and_named_peak():
    config = nominal_station("right_short_arm")
    peaks = {e.joint_name: 9.+i for i, e in enumerate(config.entries)}
    raw = {**config.raw, "factory_hmi": {"faults": {"high_torque_nm": 12.},
            "limit_check": {"joint_peak_nm": peaks}}}
    config = replace(config, raw=raw, entries=tuple(replace(e, interface="can2", sign=-1.) for e in config.entries))
    profile = defaults.builtin_module_profile(config)
    p = profile.windows[0].point
    assert p.motor_abort_nm == (12.,)*5
    assert p.joint_abort_nm == (9.,10.,11.,12.,12.)
    assert profile.joint_peak_nm == tuple(peaks.values())
    assert profile.torque_limit_source.endswith("_and_joint_peak")
    assert profile.station_sha256 == station_sha(config)
    assert profile.station_sha256 != defaults.builtin_module_profile(nominal_station("right_short_arm")).station_sha256


@pytest.mark.parametrize("change", ["model", "kp", "peak_missing", "peak_nan", "cap_nan"])
def test_invalid_current_station_parameters_are_rejected(change):
    config = nominal_station("right_arm")
    if change in {"model", "kp"}:
        replacement = {"motor_model": 99} if change == "model" else {"kp": 0.}
        config = replace(config, entries=(replace(config.entries[0], **replacement), *config.entries[1:]))
    else:
        hmi = {"faults": {"high_torque_nm": float("nan")}} if change == "cap_nan" else {
            "limit_check": {"joint_peak_nm": {e.joint_name: float("nan") if change == "peak_nan" else 10.
                                             for e in config.entries}}}
        if change == "peak_missing":
            hmi["limit_check"]["joint_peak_nm"].pop(config.entries[0].joint_name)
        config = replace(config, raw={**config.raw, "factory_hmi": hmi})
    with pytest.raises(ValueError):
        defaults.builtin_module_profile(config)


def _alter_route(monkeypatch, tmp_path, edit, *, update_digest=True):
    limb = "right_short_leg"
    manifest = defaults.read_manifest(defaults.RESOURCE_ROOT)
    asset = manifest[limb]["route"]
    path = defaults.RESOURCE_ROOT/asset['file']
    route = json.loads(path.read_text())
    edit(route)
    raw = json.dumps(route).encode()
    (tmp_path/asset['file']).write_bytes(raw)
    if update_digest:
        asset['sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(defaults, 'RESOURCE_ROOT', tmp_path)


@pytest.mark.parametrize("failure", ["hash", "names", "coverage", "held", "nonfinite"])
def test_bad_route_resources_never_generate_executable_defaults(monkeypatch, tmp_path, failure):
    def edit(route):
        if failure in {"hash", "names"}: route['joint_names'][0] = 'wrong_joint'
        elif failure == 'coverage': route['windows'][-1] = route['windows'][0]
        elif failure == 'held': route['windows'][0]['held_pose_deg'][0] = 0.
        else: route['windows'][0]['via_deg'][0][1] = float('nan')
    _alter_route(monkeypatch, tmp_path, edit, update_digest=failure!='hash')
    with pytest.raises(ValueError):
        defaults.builtin_module_profile(nominal_station('right_short_leg'))


def test_bad_hanging_joint_mapping_is_rejected(monkeypatch):
    config = nominal_station('right_arm')
    data = json.loads(defaults.resources.files('factory_hmi').joinpath(
        'resources/completion/mujoco_hanging.json').read_text())
    data['presets']['right_arm']['joint_names'][0] = 'wrong_joint'
    fake = SimpleNamespace(joinpath=lambda *a: SimpleNamespace(read_text=lambda: json.dumps(data)))
    monkeypatch.setattr(defaults.resources, 'files', lambda *a: fake)
    with pytest.raises(ValueError, match='具名关节映射'):
        defaults.builtin_module_profile(config)


@pytest.mark.parametrize('limb', sorted(PRESET_KEYS))
def test_builtin_defaults_complete_all_measured_directions_without_playback(limb):
    config = nominal_station(limb)
    coords, profile, _ = validate_module_plan(config, defaults.builtin_module_profile(config),
                                             'OFFLINE-NO-DEVICE', 'unit-test')
    cycle = ModuleCycle(coords, profile, np.deg2rad(profile.home_deg), 1.)
    directions = {(d.joint, d.sign): d for d in coords.directions}
    checked_mapping = set()
    for i in range(1, 60000):
        q = cycle.target.copy()
        tau = np.full(len(q), .7)
        probe = cycle.probe
        if probe and probe.phase in {'search', 'retreat'}:
            key = (probe.point.joint, probe.point.direction)
            stop = math.radians(directions[key].reference_deg-.8*probe.point.direction)
            if probe.point.direction*(q[probe.axis]-stop) >= 0:
                q[probe.axis] = stop
                tau[probe.axis] = 2.
            if key not in checked_mapping:
                motor = coords.motor_target(q)
                np.testing.assert_allclose(coords.joint_position(motor), q, atol=1.e-5)
                assert np.all(np.isfinite(coords.jacobian(q)))
                checked_mapping.add(key)
        cycle.step(1+i*.025, q, tau, tau)
        if cycle.fixed_hips is not None:
            np.testing.assert_array_equal(cycle.target[2:], cycle.fixed_hips)
        if cycle.phase == 'finished':
            break
    assert cycle.phase == 'finished'
    assert len(cycle.results) == len(directions) and checked_mapping == set(directions)
    for result in cycle.results:
        assert result['outcome'] == 'within_reference'
        assert result['deviation_deg'] == pytest.approx(-.8*result['sign'])
        assert result['retreat_completed'] and result['retreat_deg'] >= .99
