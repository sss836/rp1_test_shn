"""Feedback-driven full-range arm search, independent of legacy pilot rules.

The reference is used for speed planning/comparison, never as a measured stop.
MIT load deflection does not end the search. The travel envelope bounds the
target and torque aborts are evaluated before all state transitions.
"""
from collections import deque
import math
import statistics

import numpy as np

from .limit_block_check import FeedbackError, SETTINGS


WRIST_RECHECK_DIRECTIONS = {("left_wrist_yaw_joint", -1), ("left_wrist_pitch_joint", 1)}
CONTACT_CONFIRM_SPEED_DEG_S = 1.


def trim(rows, now, seconds):
    # Retain the sample straddling the window boundary (freshness is checked by
    # the executor), so a 1-second window works at non-integral sample periods.
    while len(rows) > 1 and now - rows[1][0] >= seconds:
        rows.popleft()


class LoadContact:
    """Moving-load baseline freezes on loss of tracking, then verifies contact."""
    def __init__(self, point, baseline):
        self.point = point
        self.baseline = baseline
        self.threshold_nm = max(point.contact_floor_nm, baseline + point.contact_rise_nm)
        self.motion = deque()
        self.loads = deque()
        self.window = deque()
        self.frozen = False
        self.load_offset_deg = 0.
        self.offsets = deque()

    def observe(self, now, angle, torque, target):
        s = self.point.direction
        load = abs(torque)
        self.motion.append((now, angle, target))
        trim(self.motion, now, .2)
        first = self.motion[0]
        actual_progress = s * (angle - first[1])
        target_progress = s * (target - first[2])
        # Tracking is relative motion, not absolute load-induced angle error.
        moving = (now-first[0] >= .2-1e-9 and actual_progress > SETTINGS.stall_deg/2
                  and actual_progress >= .5*max(0., target_progress))
        self.frozen = not moving
        if moving:
            self.loads.append((now, load))
            self.offsets.append((now, s*(target-angle)))
            trim(self.loads, now, .5)
            trim(self.offsets, now, .5)
            self.baseline = statistics.median(row[1] for row in self.loads)
            self.load_offset_deg = float(statistics.median(row[1] for row in self.offsets))
            self.threshold_nm = max(self.point.contact_floor_nm, self.baseline+self.point.contact_rise_nm)
            self.window.clear()
            return None
        self.window.append((now, angle, load, target))
        trim(self.window, now, SETTINGS.stall_s)
        if len(self.window) < 3 or now-self.window[0][0] < SETTINGS.stall_s-1e-9:
            return None
        angles = [row[1] for row in self.window]
        loads = [row[2] for row in self.window]
        span = max(angles)-min(angles)
        # A single noisy high/low torque sample cannot create/erase contact.
        supported = sum(value >= self.threshold_nm for value in loads)/len(loads)
        pressing = s*(target-angle)-self.load_offset_deg > .02
        if span > SETTINGS.stall_deg or supported < .8 or not pressing:
            return None
        return {"actual_deg": statistics.median(angles), "angle_span_deg": span,
                "torque_floor_nm": min(loads), "torque_median_nm": statistics.median(loads),
                "torque_peak_nm": max(loads), "baseline_nm": self.baseline,
                "threshold_nm": self.threshold_nm, "torque_support_fraction": supported,
                "fresh_samples": len(loads), "contact_window_s": now-self.window[0][0],
                "load_offset_deg": self.load_offset_deg,
                "contact_lead_deg": s*(target-angle)-self.load_offset_deg}


class FeedbackLimitSearch:
    def __init__(self, cycle):
        self.c = cycle
        self.attempt = 1
        self.extension = cycle.point.search_extension
        self.command_limit = None
        self.extending = False
        self.attempt_limit = (2 if self.extension is None and cycle.coords.station.limb == "left_arm" and
                              (cycle.point.joint, cycle.point.direction) in WRIST_RECHECK_DIRECTIONS else 1)
        if self.extension is not None:
            origin = math.degrees(cycle.start[cycle.axis])
            sign = cycle.point.direction
            self.actual_limit = sign * (self.extension.actual_limit_deg - origin)
            if self.extension.command_limit_deg is not None:
                self.command_limit = sign * (self.extension.command_limit_deg - origin)
        self.far_rate = 0.
        self.attempts = []
        self.initial_loads = deque()
        self.hold_since = None
        self.cap_reason = None
        self.release_since = None
        self.retreat_distance = 0.

    def step(self, now, q, joint_tau, motor_tau):
        from .live_limit_probe import check_torque
        c, p = self.c, self.c.point
        a, s = c.axis, p.direction
        dt = now-c.previous
        if not 0 < dt <= SETTINGS.max_feedback_age_s:
            raise FeedbackError(f"检查中逐电机反馈不连续，阶段 {c.phase}，间隔 {dt*1000:.1f} ms")
        attempt = self.attempt
        c.samples.append({"time_s": now-c.samples_started, "phase": c.phase,
            "actual_deg": np.rad2deg(q).tolist(), "target_deg": np.rad2deg(c.target).tolist(),
            "joint_nm": joint_tau.tolist(), "motor_nm": motor_tau.tolist(), "kp_kd_scale": c.gains})
        c.sample_count += 1
        check_torque(c.coords, joint_tau, motor_tau, p.joint_abort_nm, p.motor_abort_nm,
                     "实测力矩达到工位中止门限")
        travel = s*math.degrees(q[a]-c.start[a])
        if self.extension is not None and c.phase in {"search", "retreat"} and travel > self.actual_limit + 1e-9:
            raise FeedbackError(f"实际搜索姿态超过核定行程，{p.joint} 实测 {travel:g}°，上限 {self.actual_limit:g}°")
        # MIT target error generates load/contact torque. Fixed target-error
        # gates must not reappear in reset, baseline, held axes or unloading.
        # Torque, fresh valid feedback and actual search travel stay enforced.
        if c.phase == "reset":
            c.target, _, settled = c.reset.step(c.coords, p, now, dt, q, "复位实测未停稳")
            if settled:
                c._phase("ready", now)
        elif c.phase == "baseline":
            changed = (p.reset_kp_scale, p.reset_kd_scale) != (p.search_kp_scale, p.search_kd_scale)
            c.gain_blend = min((now-c.since)/.5, 1.) if changed and self.attempt == 1 else 1.
            if c.gain_blend < 1.:
                self.initial_loads.clear()
            else:
                self.initial_loads.append((now, abs(joint_tau[a])))
                trim(self.initial_loads, now, .2)
                if (c.settling.settled(now, q, c.target) and len(self.initial_loads) >= 3
                        and now-self.initial_loads[0][0] >= .2-1e-9):
                    c.window = LoadContact(p, statistics.median(row[1] for row in self.initial_loads))
                    c.window.load_offset_deg = s*math.degrees(c.target[a]-q[a])
                    if self.attempt == 1:
                        self.search_origin = q.copy()
                    c._phase("search", now)
        elif c.phase == "search":
            angle, target = math.degrees(q[a]), math.degrees(c.target[a])
            if self.extension is None and travel > p.permitted_travel_deg+p.motor_step_deg:
                raise FeedbackError(f"实际搜索姿态超过核定行程，{p.joint} 实测 {travel:g}°，"
                                   f"上限 {p.permitted_travel_deg+p.motor_step_deg:g}°")
            c.candidate = c.window.observe(now, angle, joint_tau[a], target)
            if c.candidate is not None:
                self._begin_retreat(now, q, joint_tau[a])
            else:
                cap = p.permitted_travel_deg
                # Compensate a measured moving-load offset, not an assumed one.
                # Independently stop advancing at the actual travel envelope.
                command_cap = cap + c.window.load_offset_deg
                progress = s*math.degrees(c.target[a]-c.start[a])
                hold_reason = "command_travel"
                if self.extension is not None:
                    if travel >= cap or progress >= command_cap-1e-9:
                        self.extending = True
                    if self.extending:
                        if self.command_limit is None:
                            cap = self.actual_limit
                            # Follow the measured motion. If it stalls, finish
                            # one contact window with bounded additional lead.
                            contact_lead = p.contact_speed_deg_s * (SETTINGS.stall_s + .2)
                            command_cap = min(cap, travel + contact_lead) + max(0., c.window.load_offset_deg)
                            hold_reason = "feedback_stall"
                        else:
                            cap = self.actual_limit-p.motor_step_deg
                            command_cap = self.command_limit
                    elif self.command_limit is not None:
                        command_cap = min(command_cap, self.command_limit)
                limit = c.direction.reference_deg if c.fixture_cap is None else c.fixture_cap
                if c.fixture_cap is not None:
                    cap = s * (limit - math.degrees(c.start[a]))
                    command_cap = cap  # A fixture boundary never gets load-offset compensation.
                    if travel >= cap - 1e-9 or progress >= cap - 1e-9:
                        self.cap_reason = "fixture_limit"
                        self._begin_retreat(now, q, joint_tau[a])
                        c.previous, c.actual = now, q.copy()
                        return
                remaining = s*(limit-angle)
                speed = p.contact_speed_deg_s if remaining <= 6. or (
                    c.window.frozen and abs(joint_tau[a]) >= c.window.threshold_nm) else p.search_speed_deg_s
                if c.far_speed != p.search_speed_deg_s:
                    # Use the nearer of measured and commanded position for braking.
                    distance = min(remaining, s*(limit-target)) - 6.
                    rate = min(c.far_speed, math.sqrt(p.contact_speed_deg_s**2 + 120.*max(distance, 0.)))
                    if p.joint == "left_hip_yaw_joint" and s < 0 and max(angle,target) >= -125.:
                        # The post is close between -69 and -111 deg in the fixture preview.
                        # Allow braking before the corridor; accelerate only after both positions clear it.
                        buffer = max(0., min(angle,target) + 60.)
                        rate = min(rate, math.sqrt(p.search_speed_deg_s**2 + 120.*buffer))
                    self.far_rate = min(rate, self.far_rate + 60.*dt)
                    speed = min(self.far_rate, p.contact_speed_deg_s) if (
                        c.window.frozen and abs(joint_tau[a]) >= c.window.threshold_nm) else self.far_rate
                    # Do not accumulate rapid commands in front of a stalled joint.
                    lead = max(0., s*(target-angle)-c.window.load_offset_deg)
                    if speed > p.contact_speed_deg_s:
                        speed = min(speed, max(0., 2.-lead)/dt)
                if self.attempt > 1 or self.extending:
                    speed = min(speed, CONTACT_CONFIRM_SPEED_DEG_S)
                proposed = max(progress, min(command_cap, progress+dt*speed))
                # Re-evaluate on every fresh measurement. A changing measured
                # load offset must not latch an irreversible end-of-search state.
                at_boundary = travel >= cap-1e-9
                bounded = at_boundary or proposed <= progress+1e-9
                if bounded:
                    if self.hold_since is None:
                        self.cap_reason = "actual_travel" if at_boundary else hold_reason
                        self.hold_since = now
                    # A final stationary command still supplies pressure. Finish
                    # the same contact window before declaring no stop found.
                    if now-self.hold_since >= SETTINGS.stall_s+.2:
                        self._begin_retreat(now, q, joint_tau[a])
                else:
                    self.hold_since = None
                    self.cap_reason = None
                    self._advance_axis(dt, math.radians(s*proposed)+c.start[a])
        elif c.phase == "retreat":
            distance = s*math.degrees(self.retreat_origin-q[a])
            self.retreat_distance = distance
            # A stop at the starting pose has zero available retreat. Even
            # then, first remove the extra command pressure; zero distance
            # alone must never certify unloading while still pushing it.
            released = s*math.degrees(c.target[a]-q[a]) <= c.window.load_offset_deg+SETTINGS.stall_deg
            if distance >= self.retreat_required-1e-7 and released:
                if c.settling.settled(now, q, c.target):
                    self._finish_retreat(now, distance)
            else:
                if self.attempt_limit > 1 or self.extension is not None:
                    c.settling.window.clear()
                progress = s*(c.target[a]-self.retreat_command_floor)
                if progress > 1e-9:
                    self._advance_axis(dt, c.target[a]-s*min(progress, math.radians(dt*p.positioning_speed_deg_s)))
                    self.release_since = None
                else:
                    # Only stop after the complete reverse command and a full
                    # fresh window show that the mechanism did not retreat.
                    if self.release_since is None:
                        self.release_since = now
                    if now-self.release_since >= SETTINGS.stall_s:
                        raise FeedbackError(f"退让命令已回到搜索起点，但实退 {distance:.2f}°，"
                                            f"尚需 {self.retreat_required:.2f}°；停止后续搜索")
        c.samples[-1].update(self.diagnostics())
        if self.attempt_limit > 1 or self.extension is not None:
            c.samples[-1]["search_attempt"] = attempt
        c.previous, c.actual = now, q.copy()

    def _advance_axis(self, dt, goal):
        from .live_limit_probe import advance_motion
        c = self.c
        origin = c.target.copy()
        def pose(x):
            q = origin.copy()
            q[c.axis] += x*(goal-origin[c.axis])
            return q
        c.target = pose(advance_motion(c.coords, c.point, dt, pose, 0., 1.))

    def _begin_retreat(self, now, q, torque):
        c, p = self.c, self.c.point
        if self.attempt_limit > 1 or self.extension is not None or c.fixture_cap is not None:
            self.attempts.append({"attempt": self.attempt,
                                  "end_reason": "contact" if c.candidate else self.cap_reason,
                                  "search_end_actual_deg": math.degrees(q[c.axis]),
                                  "search_end_target_deg": math.degrees(c.target[c.axis]),
                                  "search_end_torque_nm": float(torque), "baseline_nm": c.window.baseline,
                                  "threshold_nm": c.window.threshold_nm})
        self.retreat_origin = q[c.axis]
        available = max(0., p.direction*math.degrees(q[c.axis]-self.search_origin[c.axis]))
        self.retreat_required = min(p.retreat_deg, available)
        self.retreat_command_floor = c.start[c.axis]
        c._phase("retreat", now)

    def _finish_retreat(self, now, distance):
        c = self.c
        if self.attempts:
            self.attempts[-1]["retreat_deg"] = distance
        if c.candidate is None and self.attempt < self.attempt_limit:
            # Recheck from the unloaded, stationary pose. The absolute travel
            # remains relative to the original search pose for both attempts.
            self.attempt += 1
            self.initial_loads.clear()
            self.hold_since = self.cap_reason = self.release_since = None
            self.retreat_distance = 0.
            del c.window
            c._phase("baseline", now)
            return
        self._finish(now, distance)

    def _finish(self, now, distance):
        c, p = self.c, self.c.point
        evidence = c.candidate or {}
        delta = evidence["actual_deg"]-c.direction.reference_deg if evidence else None
        verdict = ("not_detected" if delta is None else "early" if p.direction*delta < -p.comparison_tolerance_deg
                   else "late" if p.direction*delta > p.comparison_tolerance_deg else "within_reference")
        c.result = {"outcome": verdict, "deviation_deg": delta, "retreat_deg": distance,
                    "requested_retreat_deg": p.retreat_deg, "available_retreat_deg": self.retreat_required,
                    "search_end_reason": "contact" if evidence else self.cap_reason,
                    "baseline_nm": c.window.baseline, "threshold_nm": c.window.threshold_nm, **evidence}
        if c.fixture_cap is not None:
            c.result["fixture_stop_deg"] = c.fixture_cap
            c.result["search_end_actual_deg"] = math.degrees(self.retreat_origin)
            if not evidence and self.cap_reason == "fixture_limit":
                c.result["outcome"] = "fixture_limited"
        if self.attempts:
            c.result["search_attempts"] = self.attempts
        if self.extension is not None:
            c.result["search_extension"] = self.extension.model_dump()
        c._phase("finished", now)

    def diagnostics(self):
        c = self.c
        data = {}
        if c.phase in {"baseline", "retreat"}:
            data["settle_blocking"] = c.settling.blocking_joint(c.coords.names)
        if self.attempt_limit > 1:
            data.update(search_attempt=self.attempt, search_attempt_limit=self.attempt_limit)
        if self.extension is not None:
            data.update(search_extension_active=self.extending,
                        search_extension=self.extension.model_dump())
        if hasattr(c, "window"):
            data.update(baseline_nm=c.window.baseline, contact_threshold_nm=c.window.threshold_nm,
                        baseline_frozen=c.window.frozen, load_offset_deg=c.window.load_offset_deg,
                        search_hold=self.cap_reason, measured_retreat_deg=self.retreat_distance)
        return data
