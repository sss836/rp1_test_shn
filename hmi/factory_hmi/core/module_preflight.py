"""Pure geometry checks in a spawned process. This worker owns no station devices."""
from __future__ import annotations

from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
import logging
import multiprocessing
import time
import threading

import numpy as np

from .limit_block_check import FeedbackError

logger = logging.getLogger(__name__)
_cached_geometry = None
_cached_completion = None


def check_bounds(safety, q, motor, names=None):
    for kind, value, lower, upper in (("关节", q, safety.joint_min_rad, safety.joint_max_rad),
                                     ("电机", motor, safety.motor_min_rad, safety.motor_max_rad)):
        invalid = ~np.isfinite(value)
        if lower is not None:
            invalid |= value < lower
        if upper is not None:
            invalid |= value > upper
        if np.any(invalid):
            i = int(np.flatnonzero(invalid)[0])
            low = None if lower is None else np.broadcast_to(lower, invalid.shape)[i]
            high = None if upper is None else np.broadcast_to(upper, invalid.shape)[i]
            raise FeedbackError(f"目标超出工位配置的位置边界，{kind} {names[i] if names is not None else i} "
                               f"目标 {value[i]:g} rad，范围 [{low}, {high}] rad")


def check_path(coords, bounds, segments):
    for begin, end in segments:
        for degrees in np.linspace(begin, end, 121):
            q = np.deg2rad(degrees)
            motor = coords.motor_target(q)
            bounds(q, motor)
            coords.jacobian(q)
            if np.max(abs(coords.joint_position(motor) - q)) > 1e-5:
                raise FeedbackError("核定轨迹的踝闭链支路不可逆")


def validate_entry(coords, profile, q, bounds):
    from .module_limit_check import entry_poses, path_segments
    poses = entry_poses(coords, profile, q)
    segments = list(zip(poses, poses[1:]))
    if coords.station.limb == "waist_hip":
        hips = poses[0][2:]
        segments.extend(((*a[:2], *hips), (*b[:2], *hips))
                        for a, b in path_segments(profile, coords.names))
    check_path(coords, bounds, segments)
    return poses


def prepare(config, revision, serial, entry):
    from .module_limit_check import load_module_plan, path_segments
    global _cached_geometry, _cached_completion
    start = time.monotonic()
    coords, profile, plan = load_module_plan(config, serial, revision)
    # The config revision changes when a station is applied. The effective
    # profile additionally covers edited site files and the resolved route.
    key = (revision, plan["station_sha256"], profile)

    def bounds(q, motor):
        check_bounds(config.safety_limits, q, motor, coords.names)

    reused = key == _cached_geometry
    if not reused:
        check_path(coords, bounds, path_segments(profile, coords.names))
        from .mujoco_completion import ARM_LIMBS
        completion = None
        if config.limb in ARM_LIMBS:
            from .limit_arm_completion import prepare_arm_completion
            completion = prepare_arm_completion(coords, np.deg2rad(profile.home_deg))
        _cached_geometry, _cached_completion = key, completion
    if entry is not None:
        validate_entry(coords, profile, entry, bounds)
    plan["preflight_ms"] = round(1000 * (time.monotonic() - start), 1)
    plan["static_path_cached"] = reused
    return plan, _cached_completion


@dataclass
class Preparation:
    id: str
    serial: str
    revision: str
    generation: int
    lease_generation: int | None
    entry: np.ndarray
    future: Future

    def arm_completion(self, config):
        _, completion = self.future.result()
        if completion is None:
            return None
        # The worker received a copy of the validated config. Restore object
        # identity for the runtime binding check only after the digest matches.
        plan = replace(completion.plan, config=config) if completion.plan is not None else None
        return replace(completion, config=config, plan=plan)


class ModulePreflight:
    def __init__(self):
        self.pool = None
        self.warmup = None
        self.guard = threading.RLock()
        self.epoch = 0
        self.job: Preparation | None = None

    def submit(self, config, revision, serial, entry):
        if self.pool is None:
            self.pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        try:
            return self.pool.submit(prepare, config, revision, serial, entry)
        except BrokenProcessPool:
            self.close()
            raise RuntimeError("方案计算进程已退出，请重新核对方案")

    def warm(self, config, revision):
        if self.warmup is not None:
            self.warmup.cancel()
        try:
            self.warmup = self.submit(config, revision, "预检", None)
        except (OSError, RuntimeError):
            logger.exception("Module preflight warm-up unavailable")

    def register(self, job, epoch):
        with self.guard:
            if epoch != self.epoch:
                job.future.cancel()
                raise ValueError("预检已被停止请求取消")
            self.job = job

    def consume(self, job):
        with self.guard:
            if self.job is not job:
                raise ValueError("预检已取消或已被替代")
            self.clear()

    def clear(self):
        with self.guard:
            self.epoch += 1
            job, self.job = self.job, None
        if job:
            job.future.cancel()

    def close(self):
        self.clear()
        self.warmup = None
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None
