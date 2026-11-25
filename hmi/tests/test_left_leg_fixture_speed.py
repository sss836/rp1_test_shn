"""Hardware-executor regressions with synthetic feedback; no CAN backend."""
import math
import numpy as np
import pytest

from factory_hmi.core.config import load_station_config
from factory_hmi.core.limit_block_check import FeedbackError
from factory_hmi.core.module_limit_check import (
    ModuleCycle, ModuleProfile, entry_poses, load_module_plan, path_segments, validate_module_plan,
)
from factory_hmi.core.module_limit_defaults import builtin_module_profile


def site_profile():
    station = load_station_config("left_short_leg")
    doc = builtin_module_profile(station).model_dump()
    doc.update(source="site", approved_by="test fixture", left_leg_fixture=dict(transfer_deg_s=60, far_search_deg_s=40))
    return station, doc


def test_whole_real_executor_with_fast_fixture(monkeypatch):
    import factory_hmi.core.module_limit_check as module
    station, doc = site_profile()
    monkeypatch.setattr(module, "read_site_profile", lambda path: ModuleProfile.model_validate(doc).model_dump_json().encode())
    coords, profile, context = load_module_plan(station, "isolated-test", "1")
    assert context["windows"][0]["fixture_stop_deg"] == -20.
    assert all(min(begin[0], end[0]) >= -20 for begin, end in path_segments(profile, coords.names))
    cycle = ModuleCycle(coords, profile, coords.home.copy(), 0.)
    dt = .01
    max_search = max_return = 0.
    for step in range(1, 12001):
        measured = cycle.target.copy()
        tau = np.full(len(measured), .2)
        probe, phase = cycle.probe, cycle.phase
        original = cycle.target.copy()
        if probe:
            d = probe.direction
            edge = math.radians(d.reference_deg)
            if d.sign * (measured[d.index] - edge) >= 0:
                measured[d.index] = edge
                tau[d.index] = 5.
        cycle.step(step*dt, measured, tau, tau)
        assert cycle.target[0] >= math.radians(-20.) - 1e-10
        rates = np.rad2deg(cycle.target-original)/dt
        if probe and phase=="search":
            axis = probe.axis
            v = abs(rates[axis])
            max_search = max(max_search, v)
            angle = math.degrees(measured[axis])
            if probe.point.joint=="left_hip_yaw_joint" and probe.point.direction==-1 and -120<=angle<=-62:
                assert v <= 10.00001
            ref = probe.direction.reference_deg if probe.fixture_cap is None else probe.fixture_cap
            if probe.point.direction*(ref-angle)<=5.9:
                assert v <= 2.00001
        if phase=="clearance":
            max_return = max(max_return, np.max(abs(rates)))
            if -120<=math.degrees(measured[1])<=-62:
                assert abs(rates[1]) <= 20.00001
        if cycle.phase=="finished":
            break
    assert step*dt < 120 and cycle.phase == "finished"
    assert max_search>30 and max_return>40
    first, *measured = cycle.results
    assert first["outcome"]=="fixture_limited" and first["deviation_deg"] is None
    assert first["search_end_actual_deg"]==pytest.approx(-20.) and first["retreat_completed"]
    assert len(measured)==9 and all(r["outcome"]=="within_reference" for r in measured)


def test_guard_feedback_faults_and_scope():
    station, doc = site_profile()
    coords, profile, _ = validate_module_plan(station, ModuleProfile(**doc), "TEST", "1")
    current=coords.home.copy()
    current[0] = math.radians(-20.1)
    with pytest.raises(FeedbackError, match="工装停止角"):
        entry_poses(coords, profile, current)
    cycle=ModuleCycle(coords,profile,coords.home.copy(),0.)
    tau=np.zeros(len(current))
    with pytest.raises(FeedbackError,match="整轮中止"):
        cycle.step(.01,current,tau,tau)
    with pytest.raises(FeedbackError,match="不连续"):
        cycle.step(.30,coords.home,tau,tau)
    with pytest.raises(FeedbackError,match="力矩"):
        cycle.step(.01,coords.home,tau+500,tau+500)
    with pytest.raises(ValueError,match="五轴左腿工位"):
        ModuleProfile(**dict(doc,limb="right_short_leg"))
    with pytest.raises(ValueError,match="核定"):
        ModuleProfile(**dict(doc,approved_by=None))
    with pytest.raises(ValueError):
        ModuleProfile(**dict(doc,left_leg_fixture={"transfer_deg_s":1000,"far_search_deg_s":300}))
    doc["home_deg"] = [-2.,*doc["home_deg"][1:]]
    with pytest.raises(ValueError,match="航点"):
        validate_module_plan(station,ModuleProfile(**doc),"TEST","1")


def test_obstruction_before_user_cap_is_recorded_as_early_contact():
    station, doc=site_profile()
    coords, profile, _ = validate_module_plan(station,ModuleProfile(**doc),"TEST","1")
    cycle=ModuleCycle(coords,profile,coords.home.copy(),0.)
    for i in range(1,4000):
        q=cycle.target.copy()
        tau=np.full(len(q),.2)
        if q[0] <= math.radians(-12.):
            q[0]=math.radians(-12.);tau[0]=5.
        cycle.step(.01*i,q,tau,tau)
        if cycle.results:
            break
    assert cycle.results[0]["outcome"]=="early"
    assert cycle.results[0]["actual_deg"]==pytest.approx(-12.)


def test_only_site_worker_accelerates_and_checks_partial_feedback(tmp_path):
    from factory_hmi.core.module_limit_check import ModuleWorker

    station, doc = site_profile()
    for source, interval in ((builtin_module_profile(station), .025), (ModuleProfile(**doc), .005)):
        coords, profile, context = validate_module_plan(station, source, "TEST", "1")
        worker = ModuleWorker(None, coords, profile, context, None, None, None, tmp_path/"unused", coords.home)
        assert not worker.started and worker.command_interval_s == interval
        if profile.left_leg_fixture:
            # _run calls this guard after decoding, before waiting for peers with repeated timestamps.
            q=coords.home.copy()
            q[0]=math.radians(-20.05)
            tau=np.zeros(len(q))
            with pytest.raises(FeedbackError, match="越过工装"):
                worker._check_received_loads(tau,tau,q)
