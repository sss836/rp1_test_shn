"""Exercise the production worker with command-driven feedback and a virtual clock."""

from dataclasses import replace
import gc
import json
import random
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core import live_limit_probe
from factory_hmi.core.arm_limit_defaults import builtin_arm_profile
from factory_hmi.core.limit_block_check import Coordinates, nominal_station
from factory_hmi.core.live_limit_probe import ProbeWorker
from factory_hmi.models import MotorSample
from test_limit_feedback_groups import Clock, Event


def run_loss(monkeypatch, drop, duration, *, interval=0.025, empty=False):
    clock = Clock()
    coords = Coordinates(nominal_station("left_arm"))
    point = builtin_arm_profile(coords.station).windows[0].point
    commands, steps = [], []
    stamps = [clock.now] * 7
    end = clock.now + duration
    disabled = []

    def command(target, kp_scale, kd_scale, **guards):
        if guards:
            assert not guards["cancelled"].is_set() and clock.now < guards["dispatch_before"]
        commands.append((clock.now, target.copy(), kp_scale, kd_scale, bool(guards)))
        missing = drop(len(commands))
        for i in range(7):
            if i not in missing:
                stamps[i] = clock.now

    def read():
        if clock.now >= end:
            worker.stop_event.set()
        if empty and len(commands) == 1:
            return []
        return [
            MotorSample(
                pos_rad=0.0,
                timestamp_s=s,
                feedback_age_s=clock.now - s,
                bus=e.interface,
                motor_id=e.motor_id,
                joint_name=e.joint_name,
            )
            for e, s in zip(coords.entries, stamps)
        ]

    def step(now, *_):
        steps.append(now)
        cycle.target[0] += 1e-7

    cycle = SimpleNamespace(
        phase="ready",
        point=point,
        target=np.zeros(7),
        gains=(0.8, 0.9),
        step=step,
        samples=[],
        result=None,
        actual=np.zeros(7),
    )
    backend = SimpleNamespace(
        command_mit=command,
        disable_motors=lambda: disabled.append(True),
        disable_feedback=lambda: "confirmed_mock",
    )
    event = Event(clock, 10**7)
    monkeypatch.setattr(live_limit_probe.time, "monotonic", lambda: clock.now)
    worker = ProbeWorker(backend, coords, point, {}, read, lambda *_: None, lambda *_: None)
    worker.command_interval_s = interval
    worker._new_cycle = lambda *_: cycle
    worker.stop_event = event

    # The first command follows the initial snapshot; model the native RX callback after that command.
    def command_with_receive(*args, **kwargs):
        clock.now += 0.0001
        command(*args, **kwargs)

    backend.command_mit = command_with_receive
    callbacks = list(gc.callbacks)
    worker._run()
    assert gc.callbacks == callbacks and disabled
    report = worker.snapshot(include_samples=True)
    json.dumps(report, allow_nan=False)
    return worker, commands, steps, report, clock.now


@pytest.mark.parametrize("empty", [False, True])
def test_one_lost_reply_recovers_at_original_target(monkeypatch, empty):
    w, commands, steps, r, _ = run_loss(monkeypatch, lambda n: {6} if n == 1 else (), 0.3, empty=empty)
    retries = [i for i, c in enumerate(commands) if c[4]]
    assert retries == [1] and r["feedback_diagnostics"]["recovery_count"] == 1
    assert len(steps) == len(commands) - 2
    np.testing.assert_array_equal(commands[0][1], commands[1][1])
    assert commands[0][2:4] == commands[1][2:4]
    if not empty:
        assert r["feedback_diagnostics"]["missed_by_motor"] == {"can0#7": 1}
        assert r["reply_misses"][0]["whole_buses"] == []
    assert commands[2][1][0] > commands[1][1][0]


def test_bus_silence_has_a_deadline_and_counts_once(monkeypatch):
    _, sent, steps, report, end = run_loss(monkeypatch, lambda _: range(7), 2.0)
    assert 1 < len(sent) <= 4 and not steps
    assert end - 1 <= 0.150001
    assert report["feedback_diagnostics"]["whole_bus_missed"] == 1
    assert len(report["feedback_diagnostics"]["missed_by_motor"]) == 7
    assert "新鲜" in report["fault"] or "超时" in report["fault"]


def test_intermittent_fault_recovers_over_600_virtual_seconds(monkeypatch):
    rng = random.Random(42)
    _, commands, steps, r, end = run_loss(
        monkeypatch, lambda _: {rng.randrange(7)} if rng.random() < 0.001 else (), 600.0
    )
    assert end >= 601 and steps
    assert r["feedback_diagnostics"]["repoll_count"] > 0
    assert r["feedback_diagnostics"]["accepted_groups"] == len(steps) + 1


def test_degraded_link_stops_at_sliding_limit(monkeypatch):
    worker, _, _, r, _ = run_loss(monkeypatch, lambda n: {6} if n % 10 == 1 else (), 8.0, interval=0.005)
    assert worker.repoll_after_s == 0.015 and r["feedback_diagnostics"]["repoll_count"] == 20
    assert "链路劣化" in r["fault"]


def test_no_repoll_during_owner_handoff(monkeypatch):
    w, sent, _, _, end = run_loss(monkeypatch, lambda _: (), 0.1)
    w.stop_event.stopped = False
    w._handoff_lock = object()
    w._sent_at = end - 0.1
    previous = len(sent)
    w._wait_for_reply(end, end - 0.1, (end - 0.1,) * 7, [])
    assert len(sent) == previous
