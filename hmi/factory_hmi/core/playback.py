"""Dedicated-thread trajectory playback at the configured control rate."""

from __future__ import annotations

import math
import json
import uuid
from dataclasses import replace
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from factory_hmi.models import (
    FaultStatus,
    MotorSample,
    PlaybackOptions,
    PlaybackProgress,
    StationConfig,
    StationState,
    TrajectoryData,
    json_number,
)

from ._legacy import (
    ControllerHeartbeat,
    MotorFeedbackWatchdog,
    MotorTelemetryRecorder,
    compute_hip_roll_grav_torque,
    hip_roll_grav_comp_enabled,
    trajectory_target_at,
)
from .backend import MotorBackend
from .fault_response import new_fault_response, request_fault_disable
from .fault_diagnosis import DiagnosisThresholds, FaultDiagnosisEngine
from .state_machine import InvalidStateTransition, StationStateMachine
from .motion_completion import MotionParameters, automatic_completion_parameters
from .first_frame_approach import plan_first_frame_approach
from .trajectory import preflight_trajectory
from .manual_control import motor_positions_to_joint


class PlaybackError(RuntimeError):
    pass


# Lightweight CSV close/finalize is serialized. Heavy report process isolation
# and a durable task queue are a separate follow-up; never run them here.
_RECORD_FINALIZER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="factory-record-close")
RECORD_CLOSE_TIMEOUT_S = 5.0


class _CommandPosition:
    def __init__(self, count: int) -> None:
        self._lock = threading.Lock()
        self._values = np.full(count, np.nan, dtype=np.float64)

    def set(self, values: np.ndarray) -> None:
        with self._lock:
            self._values[:] = values

    def as_list(self) -> list[float]:
        with self._lock:
            return [float(item) for item in self._values]


class _RunStatistics:
    def __init__(self, config: StationConfig) -> None:
        self._lock = threading.Lock()
        self._entries = tuple(config.entries)
        count = len(self._entries)
        self._samples = np.zeros(count, dtype=np.int64)
        self._tracking_sq = np.zeros(count, dtype=np.float64)
        self._tracking_max = np.zeros(count, dtype=np.float64)
        self._torque_abs_sum = np.zeros(count, dtype=np.float64)
        self._torque_peak = np.zeros(count, dtype=np.float64)
        self._temperature_start = np.full(count, np.nan, dtype=np.float64)
        self._temperature_current = np.full(count, np.nan, dtype=np.float64)
        self._temperature_peak = np.full(count, np.nan, dtype=np.float64)
        self._error_samples = np.zeros(count, dtype=np.int64)

    def update(
        self,
        samples: list[MotorSample],
        target: np.ndarray,
    ) -> None:
        with self._lock:
            for index, sample in enumerate(samples):
                if index >= len(self._entries):
                    break
                position = float(sample.pos_rad)
                command = float(target[index])
                torque = float(sample.torque_nm)
                temperature = float(sample.temp_c)
                if math.isfinite(position) and math.isfinite(command):
                    error = abs(command - position)
                    self._tracking_sq[index] += error * error
                    self._tracking_max[index] = max(
                        self._tracking_max[index],
                        error,
                    )
                if math.isfinite(torque):
                    absolute_torque = abs(torque)
                    self._torque_abs_sum[index] += absolute_torque
                    self._torque_peak[index] = max(
                        self._torque_peak[index],
                        absolute_torque,
                    )
                if math.isfinite(temperature):
                    if not math.isfinite(self._temperature_start[index]):
                        self._temperature_start[index] = temperature
                    self._temperature_current[index] = temperature
                    if not math.isfinite(self._temperature_peak[index]):
                        self._temperature_peak[index] = temperature
                    else:
                        self._temperature_peak[index] = max(
                            self._temperature_peak[index],
                            temperature,
                        )
                if int(sample.error_id) != 0:
                    self._error_samples[index] += 1
                self._samples[index] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            joints = []
            for index, entry in enumerate(self._entries):
                count = int(self._samples[index])
                start_temperature = self._temperature_start[index]
                current_temperature = self._temperature_current[index]
                joints.append(
                    {
                        "index": index,
                        "motor_id": int(entry.motor_id),
                        "joint_name": entry.joint_name,
                        "sample_count": count,
                        "tracking_rms_rad": json_number(
                            math.sqrt(self._tracking_sq[index] / count)
                            if count
                            else None
                        ),
                        "tracking_max_rad": json_number(
                            self._tracking_max[index] if count else None
                        ),
                        "torque_peak_nm": json_number(
                            self._torque_peak[index] if count else None
                        ),
                        "torque_mean_abs_nm": json_number(
                            self._torque_abs_sum[index] / count
                            if count
                            else None
                        ),
                        "temperature_start_c": json_number(start_temperature),
                        "temperature_current_c": json_number(current_temperature),
                        "temperature_rise_c": json_number(
                            current_temperature - start_temperature
                            if math.isfinite(start_temperature)
                            and math.isfinite(current_temperature)
                            else None
                        ),
                        "temperature_peak_c": json_number(
                            self._temperature_peak[index]
                        ),
                        "error_samples": int(self._error_samples[index]),
                    }
                )
            return {
                "sample_count": int(np.max(self._samples))
                if self._samples.size
                else 0,
                "joints": joints,
            }


class PlaybackController:
    """Own the 200 Hz control thread; callers only set thread-safe events."""

    def __init__(
        self,
        backend: MotorBackend,
        config: StationConfig,
        trajectory: TrajectoryData,
        *,
        state_machine: StationStateMachine | None = None,
        enable_watchdog: bool = True,
        enable_recording: bool = False,
        record_output: Path | str | None = None,
        record_rate_hz: float | None = None,
        test_id: str | None = None,
        robot_id: str | None = None,
        on_fault: Callable[[FaultStatus], None] | None = None,
        on_protective_fault: Callable[[FaultStatus, list[MotorSample]], bool] | None = None,
        on_complete: Callable[[], None] | None = None,
        completion_output: Path | str | None = None,
        telemetry_hub: Any = None,
    ) -> None:
        if len(backend.entries) != len(config.entries):
            raise ValueError("backend motor count does not match station config")
        if trajectory.motor_count != len(config.entries):
            raise ValueError("trajectory motor count does not match station config")
        if enable_recording and record_output is None:
            raise ValueError("record_output is required when recording is enabled")
        actual_record_rate_hz = float(
            record_rate_hz or min(config.control_rate_hz, 20.0)
        )
        if not 1.0 <= actual_record_rate_hz <= 20.0:
            raise ValueError("record_rate_hz must be between 1 and 20 Hz")
        self.backend = backend
        self.config = config
        self.trajectory = trajectory
        self.state_machine = state_machine or StationStateMachine(
            StationState.ARMED
        )
        self.enable_watchdog = bool(enable_watchdog)
        self.enable_recording = bool(enable_recording)
        self.record_output = Path(record_output) if record_output is not None else None
        # Hardware feedback is sampled by the control thread.  Recording should
        # consume that cache instead of reading raw motor handles concurrently
        # at the 200 Hz command rate.
        self.record_rate_hz = actual_record_rate_hz
        self.telemetry_hub = telemetry_hub
        self.test_id = test_id
        self.robot_id = robot_id
        self.on_fault = on_fault
        self.on_protective_fault = on_protective_fault
        self.on_complete = on_complete
        self.completion_output = Path(completion_output) if completion_output else (
            self.record_output.with_suffix(".completion.json") if self.record_output else
            Path.home() / ".local/state/rp1-test-hmi/motion-completion" / f"{uuid.uuid4()}.json")

        self.heartbeat = ControllerHeartbeat()
        self._command_position = _CommandPosition(len(config.entries))
        self._diagnosis = FaultDiagnosisEngine(
            DiagnosisThresholds.from_config(config.raw)
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._finish_event = threading.Event()
        self._test_finished = threading.Event()
        self._test_started = threading.Event()
        self._test_started_at = None
        self._approach_plan = None
        self._record_samples: list[MotorSample] = []
        self._parameters: MotionParameters | None = None
        self._mujoco_selected = False
        self._completion = dict(phase="running", active=False, safe_disabled=False,
            parameter_revision=None, return_seconds=0.0, settling_seconds=0.0, alarm=None,
            evidence_path=str(self.completion_output))
        self._completion_evidence: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._progress = PlaybackProgress()
        self._last_samples: list[MotorSample] = []
        self._fault: FaultStatus | None = None
        self._fault_response: dict | None = None
        self._options = PlaybackOptions()
        self._statistics = _RunStatistics(config)
        self._recorder: MotorTelemetryRecorder | None = None
        self._record_finished = False
        self._record_done = threading.Event()
        self._record_error: str | None = None
        self._record_finalize_lock = threading.Lock()
        self._ended_at: str | None = None
        self._stop_reason: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def fault(self) -> FaultStatus | None:
        with self._lock:
            return self._fault

    def start(self, options: PlaybackOptions | None = None) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise PlaybackError("playback is already running")
            if self.recording_close_pending():
                raise PlaybackError("previous recording still owns its CSV; wait for closure")
            if not self.backend.connected:
                raise PlaybackError("backend must be connected before playback")
            self.state_machine.require(StationState.ARMED)
            self._options = options or PlaybackOptions()
            self._mujoco_selected = self._options.completion_mode == "mujoco_hanging"
            self._parameters = automatic_completion_parameters(self.config)
            samples = self.backend.read_samples()
            self._last_samples = samples
            if len(samples) != len(self.config.entries) or any(sample.error_id for sample in samples):
                raise PlaybackError('电机反馈不完整或存在故障，禁止启动')
            current = np.asarray([sample.pos_rad for sample in samples], dtype=float)
            if self._parameters is not None:
                self._parameters.check_trajectory(self.config, self.trajectory,
                    speed=self._options.speed, loop=self._options.loop)
            scaled = replace(self.trajectory, timestamps=self.trajectory.timestamps / self._options.speed,
                             fps=self.trajectory.fps * self._options.speed)
            preflight_trajectory(scaled, entries=self.config.entries, config=self.config.raw,
                                 limits=self.config.safety_limits, current_motor_pos=current,
                                 check_initial_gap=False)
            self._approach_plan = plan_first_frame_approach(self.config, current,
                self.trajectory.motor_pos[0], parameters=self._parameters)
            if self._mujoco_selected:
                from .mujoco_completion import plan_return
                end_velocity = np.zeros(len(self.config.entries))
                if len(scaled.timestamps) > 1:
                    end_velocity = (scaled.motor_pos[-1]-scaled.motor_pos[-2]) / (scaled.timestamps[-1]-scaled.timestamps[-2])
                # Validate the selected completion connection before testing.
                plan_return(self.config, scaled.motor_pos[-1], end_velocity)
            self._completion.update(phase="approaching" if self._approach_plan.required else "running", active=True, safe_disabled=False,
                approach=self._approach_plan.to_dict(),
                parameter_revision=self._parameters.parameter_revision if self._parameters else None,
                automatic_return_enabled=self._parameters is not None or self._mujoco_selected, alarm=None,
                return_seconds=0.0, settling_seconds=0.0)
            self._finish_event.clear()
            self._test_finished.clear()
            self._test_started.clear()
            self._test_started_at = None
            self._record_samples = []
            self._progress = PlaybackProgress(cycle=1)
            self._statistics = _RunStatistics(self.config)
            self._recorder = None
            self._record_finished = False
            self._record_done.clear()
            self._record_error = None
            self._ended_at = None
            self._stop_reason = None
            if not self.enable_recording:
                self._record_done.set()
            self._fault = None
            self._last_samples = []
            self._stop_event.clear()
            self._pause_event.clear()
            self.state_machine.transition(
                StationState.RUNNING, expected=StationState.ARMED
            )
            self._thread = threading.Thread(
                target=self._run,
                name=f"factory-playback-{self.config.limb}",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self.state_machine.transition(
                    StationState.FAULT, expected=StationState.RUNNING
                )
                self._thread = None
                raise

    def pause(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.RUNNING)
            if self._completion["phase"] not in {"running", "finishing_cycle"}:
                raise PlaybackError("受控收尾期间不能暂停；可执行软件失能")
            self._pause_event.set()
            self.state_machine.transition(
                StationState.PAUSED, expected=StationState.RUNNING
            )

    def resume(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.PAUSED)
            self._pause_event.clear()
            self.state_machine.transition(
                StationState.RUNNING, expected=StationState.PAUSED
            )

    def finish(self, reason: str | None = None) -> None:
        """Request a normal decelerated finish; never block a gateway worker."""
        with self._lock:
            self.state_machine.require({StationState.RUNNING, StationState.PAUSED})
            if self.state_machine.state is StationState.PAUSED:
                self.state_machine.transition(StationState.RUNNING, expected=StationState.PAUSED)
            self._stop_reason = reason or "operator normal finish"
            self._finish_event.set()
            # A finish requested while paused starts from the held position.
            self._pause_event.clear()

    def stop(self, *, wait: bool = True, timeout: float = 2.0, reason: str | None = None) -> None:
        with self._lock:
            state = self.state_machine.state
            if state not in {StationState.RUNNING, StationState.PAUSED}:
                raise InvalidStateTransition(
                    f"stop requires running/paused, current={state.value}"
                )
            self.state_machine.transition(StationState.STOPPING, expected=state)
            self._pause_event.clear()
            self._stop_event.set()
            self._stop_reason = reason
            thread = self._thread
        if wait and thread is not None:
            thread.join(float(timeout))
            if thread.is_alive():
                raise TimeoutError("playback thread did not stop in time")

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def wait_recording(self, timeout: float | None = None) -> bool:
        return self._record_done.wait(timeout) and not self.recording_owner_alive()

    def recording_owner_alive(self) -> bool:
        recorder = self._recorder
        return bool(recorder is not None and recorder.owner_alive())

    def recording_close_pending(self) -> bool:
        return bool(self._recorder is not None and
                    (not self._record_done.is_set() or self.recording_owner_alive()))

    def finalize_recording(self, timeout: float | None = None) -> None:
        """Let an existing report waiter close this run without a global queue wait."""
        timeout = RECORD_CLOSE_TIMEOUT_S if timeout is None else timeout
        if not np.isfinite(timeout) or timeout < 0:
            raise ValueError("record close timeout must be finite and non-negative")
        if self.running:
            raise RuntimeError("playback must exit before closing its recording")
        if self._recorder is not None:
            self._finish_recording(self._recorder, timeout=timeout)

    def signal_fault(self, fault: FaultStatus) -> None:
        # Supervisor publication never waits for a command/snapshot lock.
        self._stop_event.set()
        self._fault = fault
        self._progress.outcome = "fault"
        self._completion.update(phase="fault", active=False, alarm=fault.message)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            recorder = self._recorder
            record_path = self.record_output if self.enable_recording else None
            return {
                "thread_alive": thread_alive,
                "paused": self._pause_event.is_set(),
                "test_id": self.test_id,
                "robot_id": self.robot_id,
                "control_rate_hz": float(self.config.control_rate_hz),
                "record_rate_hz": (
                    self.record_rate_hz if self.enable_recording else None
                ),
                "options": {
                    "loop": self._options.loop,
                    "cycles": self._options.cycles,
                    "duration_s": self._options.duration_s,
                    "speed": self._options.speed,
                },
                "progress": self._progress.to_dict(),
                "completion": dict(self._completion),
                "fault_response": ({**self._fault_response, "playback_stop": "stopped" if not thread_alive else "requested"}
                                   if self._fault_response else None),
                "test_started_at": self._test_started_at,
                "statistics": self._statistics.snapshot(),
                "recording": {
                    "enabled": bool(self.enable_recording),
                    "active": bool(
                        self.enable_recording
                        and thread_alive
                        and self._test_started.is_set()
                        and not self._test_finished.is_set()
                        and not self._record_finished
                    ),
                    "finished": bool(self._record_finished),
                    "closing": bool(self.enable_recording and not thread_alive and self.recording_close_pending()),
                    "owner_alive": self.recording_owner_alive(),
                    "error": self._record_error,
                    "filename": record_path.name if record_path is not None else None,
                    "row_count": int(recorder.row_count) if recorder is not None else 0,
                    "durability": recorder.durability_status() if recorder is not None else None,
                    "rate_hz": (
                        self.record_rate_hz if self.enable_recording else None
                    ),
                },
                "heartbeat_age_s": self.heartbeat.age(),
                "fault": self._fault.to_dict() if self._fault else None,
                "ended_at": self._ended_at,
                "record_error": self._record_error,
                "stop_reason": self._stop_reason,
            }

    def last_samples(self) -> list[MotorSample]:
        with self._lock:
            return list(self._last_samples)

    def recording_samples(self) -> list[MotorSample]:
        # Frozen at the test/return boundary, including a recorder read already
        # in flight; return feedback can never enter the test CSV.
        with self._lock:
            return list(self._record_samples)

    def _run(self) -> None:
        recorder: MotorTelemetryRecorder | None = None
        watchdog: MotorFeedbackWatchdog | None = None
        terminal_outcome = "completed"
        try:
            if self.enable_watchdog:
                watchdog = MotorFeedbackWatchdog(
                    self.backend.motors,
                    list(self.config.entries),
                    sample_hz=20.0,
                    stale_sec=2.0,
                    track_err_rad=0.5,
                    grace_sec=10.0,
                    abort_on_fault=True,
                    abort_on_tracking=False,
                    query_feedback=False,
                )
            if self.enable_recording:
                assert self.record_output is not None
                recorder = MotorTelemetryRecorder(
                    self.backend.motors,
                    list(self.config.entries),
                    self.record_output,
                    rate_hz=self.record_rate_hz,
                    query=False,
                    get_cmd_pos=self._command_position.as_list,
                    sample_provider=self.recording_samples,
                    sampling_gate=self._test_started,
                    print_status=False,
                    quiet=True,
                    limb=self.config.limb,
                    test_id=self.test_id,
                    robot_id=self.robot_id,
                    # Dispatch liveness is supervised independently by the
                    # backend, including when CSV I/O is stuck.
                    controller_heartbeat=None,
                    platform_required=False,
                    local_only=True,
                )
                recorder.start()
                with self._lock:
                    self._recorder = recorder

            if self._approach_first_frame():
                self._test_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                if recorder is not None:
                    recorder.begin_sampling()
                else:
                    self._test_started.set()
                with self._lock:
                    self._completion.update(phase="running", approach_completed=True)
                terminal_outcome = self._control_loop(watchdog, recorder)
            else:
                terminal_outcome = "stopped"
                with self._lock:
                    self._completion.update(phase="interrupted", active=False, approach_completed=False)
            with self._lock:
                self._progress.outcome = "fault" if self._fault else terminal_outcome
        except Exception as exc:
            self._handle_fault(exc)
        finally:
            if recorder is not None:
                recorder.request_stop_sampling()
            if self._ended_at is None:
                self._ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if self._stop_event.is_set() and self._completion["phase"] != "fault":
                self._completion.update(phase="interrupted", active=False)
            self._completion["evidence_path"] = str(self.completion_output)
            self._write_completion_evidence()
        current = self.state_machine.state
        if current in {StationState.RUNNING, StationState.STOPPING}:
            self.state_machine.transition(StationState.COMPLETED, expected=current)
        if (
            self.state_machine.state is StationState.COMPLETED
            and self.on_complete is not None
        ):
            try:
                self.on_complete()
            except Exception as exc:
                self._handle_fault(
                    PlaybackError(f"failed to resume position hold: {exc}")
                )
        if recorder is not None:
            _RECORD_FINALIZER.submit(self._finish_recording, recorder)
        else:
            self._record_done.set()

    def _write_completion_evidence(self, output: Path | None = None) -> None:
        output = output or self.completion_output
        payload = dict(self._completion_evidence)
        payload.update(completion=dict(self._completion), test_id=self.test_id,
            limb=self.config.limb, trajectory_sha256=self.trajectory.sha256,
            test_ended_at=self._ended_at, progress=self._progress.to_dict(),
            fault=self._fault.to_dict() if self._fault else None)
        def write():
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                # Unique execution file, no archive rewrite and no remote sink.
                with output.open("x", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            except Exception as exc:
                with self._lock:
                    self._completion["alarm"] = (self._completion.get("alarm") or "") + f"; 回位证据写入失败: {exc}"
        _RECORD_FINALIZER.submit(write)

    def _finish_recording(self, recorder: MotorTelemetryRecorder, *, timeout: float | None = None) -> None:
        if not self._record_finalize_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                if recorder is not self._recorder or self._record_done.is_set():
                    return
            timeout = RECORD_CLOSE_TIMEOUT_S if timeout is None else timeout
            recorder.finalize(final_status="blocked" if self._fault else None,
                              final_summary=self._fault.message if self._fault else None,
                              timeout=timeout)
            with self._lock:
                if recorder is self._recorder:
                    self._record_finished = True
        except Exception as exc:
            with self._lock:
                if recorder is self._recorder:
                    self._record_error = str(exc)
                    # This is post-motion file ownership, not a new motor command.
                    # Keep the existing verified stop/hold outcome and raw evidence.
                    self._completion["alarm"] = "记录封存失败: " + str(exc)
        finally:
            with self._lock:
                if recorder is self._recorder:
                    self._record_done.set()
            self._record_finalize_lock.release()

    def _control_loop(
        self, watchdog: MotorFeedbackWatchdog | None,
        recorder: MotorTelemetryRecorder | None,
    ) -> str:
        period = 1.0 / float(self.config.control_rate_hz)
        options, duration = self._options, self.trajectory.duration_s
        cycle_time = active_time = 0.0
        completed_cycles = 0
        next_tick = last_tick = time.monotonic()
        target = np.asarray(self.trajectory.motor_pos[0], dtype=float).copy()
        velocity = np.zeros_like(target)
        finish_cycle = False
        outcome = "completed"
        while not self._stop_event.is_set():
            now = time.monotonic()
            delta, last_tick = max(0.0, now - last_tick), now
            if self._finish_event.is_set():
                # Paused playback already commands a stationary target.
                outcome = "stopped"
                break
            if self._pause_event.is_set():
                velocity[:] = 0
                self._dispatch(target, active_time, watchdog, recorder)
                self._sample_feedback(target, active_time, update_statistics=True)
            else:
                active_time += delta
                cycle_time = min(duration, cycle_time + delta * options.speed)
                old_target = target.copy()
                target = trajectory_target_at(self.trajectory.motor_pos, self.trajectory.timestamps, cycle_time)
                if delta > 0:
                    velocity = (target - old_target) / delta
                self._dispatch(target, active_time, watchdog, recorder)
                self._sample_feedback(target, active_time, update_statistics=True)
                if options.duration_s is not None and active_time >= options.duration_s:
                    finish_cycle = True
                    self._completion.update(phase="finishing_cycle")
                if duration <= 0 or cycle_time >= duration:
                    completed_cycles += 1
                    if finish_cycle or not options.loop or (options.cycles > 0 and completed_cycles >= options.cycles):
                        outcome = "duration_complete" if finish_cycle else ("cycles_complete" if options.loop else "completed")
                        break
                    cycle_time = 0.0
            self._update_progress(cycle_time, active_time, completed_cycles, target)
            next_tick += period
            next_tick = self._tick(next_tick, period)
        self._update_progress(cycle_time, active_time, completed_cycles, target)
        if self._stop_event.is_set():
            return "stopped"
        if self._mujoco_selected:
            self._end_test_sampling(recorder)
            self._return_mujoco(target, velocity, active_time, watchdog)
            return "stopped" if self._stop_event.is_set() else outcome
        if self._parameters is None:
            self._end_test_sampling(recorder)
            self._completion.update(phase="holding", active=False, safe_disabled=False,
                alarm="自动回位/失能未配置：测试已结束并保持位置，未自动失能")
            self._completion_evidence.update(automatic_return_enabled=False,
                automatic_disable_requested=False, final_test_target_rad=target.tolist())
            return outcome
        manual_finish = self._finish_event.is_set()
        if manual_finish:
            target, deceleration_seconds = self._decelerate(target, velocity, active_time,
                watchdog, recorder, cycle_time, completed_cycles, test_time=True)
            active_time += deceleration_seconds
        self._end_test_sampling(recorder)
        if not self._stop_event.is_set():
            normal_deceleration_seconds = 0.0
            if not manual_finish:
                target, normal_deceleration_seconds = self._decelerate(target, velocity,
                    active_time, watchdog, None, cycle_time, completed_cycles, test_time=False)
            self._completion_evidence['normal_deceleration_seconds'] = normal_deceleration_seconds
            self._return_and_settle(target, active_time, watchdog, normal_deceleration_seconds)
        return "stopped" if self._stop_event.is_set() else outcome

    def _approach_first_frame(self) -> bool:
        """Run before the test gate opens. Fresh arrival, never elapsed time alone."""
        plan = self._approach_plan
        if plan is None or not plan.required:
            return not (self._stop_event.is_set() or self._finish_event.is_set())
        started = time.monotonic()
        period = 1. / self.config.control_rate_hz
        stable_since = None
        confirmed = 0
        stamps = np.full(len(self.config.entries), -math.inf)
        while not (self._stop_event.is_set() or self._finish_event.is_set()):
            tick = time.monotonic()
            elapsed = tick - started
            command = plan.at(elapsed)
            self._dispatch(command, 0., None, self._recorder)
            samples = self._sample_feedback(command, 0., update_statistics=False)
            with self._lock:
                self._completion.update(phase="approaching" if elapsed < plan.duration_s else "approach_settling",
                    approach_elapsed_s=elapsed)
            if elapsed >= plan.duration_s:
                positions = np.array([sample.pos_rad for sample in samples])
                speeds = np.array([sample.spd_rad_s for sample in samples])
                fresh_stamps = np.array([sample.timestamp_s for sample in samples])
                arrived = (np.all(np.abs(positions - plan.target) <= plan.position_tolerance_rad)
                    and np.all(np.abs(speeds) <= plan.speed_tolerance_rad_s))
                if not arrived:
                    stable_since, confirmed = None, 0
                    stamps = np.maximum(stamps, fresh_stamps)
                elif np.all(fresh_stamps > stamps):
                    # Feedback arrives independently per joint. Consume the
                    # baseline only after every joint has advanced; moving it
                    # on every control tick discards partial fresh cohorts.
                    stamps = fresh_stamps.copy()
                    stable_since = tick if stable_since is None else stable_since
                    confirmed += 1
                    if confirmed >= 2 and tick - stable_since >= plan.stable_seconds:
                        with self._lock:
                            self._completion.update(approach_elapsed_s=elapsed, approach_completed=True)
                            self._completion_evidence["first_frame_approach"] = {
                                **plan.to_dict(), "elapsed_s": elapsed,
                                "confirmed_feedback_sets": confirmed,
                                "actual_start_error_rad": (positions - plan.target).tolist(),
                                "position_confirmation": ("within_running_tracking_envelope"
                                    if plan.position_tolerance_source == "runtime_tracking_limit"
                                    else "within_explicit_position_tolerance"),
                                "arrival_feedback": [sample.to_dict() for sample in samples]}
                        return True
            if elapsed > plan.duration_s + plan.settling_timeout_s:
                position_ok = np.abs(positions - plan.target) <= plan.position_tolerance_rad
                speed_ok = np.abs(speeds) <= plan.speed_tolerance_rad_s
                with self._lock:
                    self._completion_evidence["first_frame_approach"] = {
                        **plan.to_dict(), "elapsed_s": elapsed, "outcome": "timeout",
                        "confirmed_feedback_sets": confirmed,
                        "stable_elapsed_s": 0. if stable_since is None else tick - stable_since,
                        "within_position": position_ok.tolist(), "within_speed": speed_ok.tolist(),
                        "position_error_rad": (positions - plan.target).tolist(),
                        "freshness_baseline_timestamps_s": [json_number(float(stamp)) for stamp in stamps],
                        "awaiting_fresh_feedback_indices": np.flatnonzero(fresh_stamps <= stamps).tolist(),
                        "last_feedback": [sample.to_dict() for sample in samples]}
                reasons = []
                for i, sample in enumerate(samples):
                    name = sample.joint_name or f"ID {sample.motor_id}"
                    if not position_ok[i]:
                        reasons.append(f"{name}位置误差 {positions[i] - plan.target[i]:.4f}rad")
                    elif not speed_ok[i]:
                        reasons.append(f"{name}速度 {speeds[i]:.4f}rad/s")
                if reasons:
                    prefix = "首帧缓慢接近后未到位，未开始测试："
                    if plan.position_tolerance_source == "runtime_tracking_limit":
                        prefix = ("首帧过渡后跟踪偏差超出工位运行范围，未开始测试："
                            if not np.all(position_ok)
                            else "首帧过渡后速度未稳定，未开始测试：")
                    raise PlaybackError(prefix + "；".join(reasons[:4]))
                raise PlaybackError("首帧位置和速度已在容差内，但稳定反馈确认未完成，未开始测试")
            self._tick(tick + period, period)
        return False

    def _return_mujoco(self, start, velocity, active_time, watchdog) -> None:
        from .mujoco_completion import (plan_return, POSITION_TOLERANCE,
            SPEED_TOLERANCE, STABLE_SECONDS, TIMEOUT_SECONDS)
        try:
            plan = plan_return(self.config, start, velocity)
        except ValueError as exc:
            # A manual finish can occur at a different position/velocity from
            # the validated normal endpoint. Preserve torque if no bounded
            # connection can be made, rather than disabling because of a plan.
            self._completion.update(phase="holding", active=False, safe_disabled=False,
                alarm=f"MuJoCo 收尾无法安全接续：{exc}；保持位置，未自动失能")
            self._completion_evidence.update(source='mujoco_hanging', connection_error=str(exc),
                automatic_disable_requested=False)
            return
        self._completion_evidence.update(plan.evidence(),
            parking_pose_confirmed=self._options.parking_pose_confirmed,
            return_started_at=datetime.now(timezone.utc).isoformat())
        self._completion.update(phase="returning", source='mujoco_hanging',
            parameter_revision=plan.revision, preset_sha256=plan.resource_sha256)
        period = 1./self.config.control_rate_hz
        started = time.monotonic()
        stable_since = None
        stable_timestamps = None
        timed_out = False
        while not self._stop_event.is_set():
            now = time.monotonic()
            elapsed = now-started
            target = plan.at(elapsed)
            plan.check_target(target)
            self._dispatch(target, active_time, watchdog, None)
            samples = self._sample_feedback(target, active_time, update_statistics=False)
            positions = np.array([sample.pos_rad for sample in samples])
            plan.check_target(positions, actual=True)
            self._completion.update(return_seconds=min(elapsed,plan.duration),
                settling_seconds=max(0.,elapsed-plan.duration))
            if elapsed >= plan.duration:
                resting = (np.all(abs(positions-plan.target_motor) <= POSITION_TOLERANCE)
                           and np.all(abs(np.array([sample.spd_rad_s for sample in samples])) <= SPEED_TOLERANCE))
                if resting:
                    stamps = np.array([sample.timestamp_s for sample in samples])
                    if stable_since is None:
                        stable_since, stable_timestamps = now, stamps.copy()
                    if not timed_out:
                        self._completion.update(phase="settling")
                    if (not timed_out and now-stable_since >= STABLE_SECONDS
                            and np.all(stamps-stable_timestamps >= STABLE_SECONDS)):
                        if self._stop_event.is_set():
                            return
                        self.backend.disable_motors()
                        self._completion.update(phase="safe_disabled",active=False,safe_disabled=True)
                        self._completion_evidence.update(automatic_disable_requested=True,
                            final_feedback=[sample.to_dict() for sample in samples],
                            stable_seconds_observed=now-stable_since,
                            completed_at=datetime.now(timezone.utc).isoformat())
                        return
                else:
                    stable_since, stable_timestamps = None, None
            if elapsed >= TIMEOUT_SECONDS and not timed_out:
                timed_out = True
                self._completion.update(phase="needs_attention",
                    alarm="MuJoCo 收尾到位/稳定确认超时：保持目标并报警，未自动失能")
                self._completion_evidence['timeout_feedback'] = [sample.to_dict() for sample in samples]
                self._write_completion_evidence(self.completion_output.with_name(self.completion_output.stem+'.needs_attention.json'))
            self._tick(time.monotonic()+period,period)

    def _decelerate(self, origin: np.ndarray, velocity: np.ndarray, active_time: float,
                    watchdog: MotorFeedbackWatchdog | None,
                    recorder: MotorTelemetryRecorder | None, cycle_time: float,
                    completed_cycles: int, *, test_time: bool) -> tuple[np.ndarray, float]:
        assert self._parameters is not None
        acceleration = self._parameters.values["return_acceleration_rad_s2"]
        axis_duration = np.abs(velocity) / acceleration
        duration = float(np.max(axis_duration))
        target = origin.copy()
        if duration <= 1e-9:
            return target, 0.0
        started, period = time.monotonic(), 1.0 / self.config.control_rate_hz
        self._completion.update(phase="decelerating")
        while not self._stop_event.is_set():
            elapsed = min(duration, max(0.0, time.monotonic() - started))
            clipped = np.minimum(elapsed, axis_duration)
            target = origin + velocity * (clipped - clipped ** 2 / (2 * np.maximum(axis_duration, 1e-12)))
            self._parameters.check_target(self.config, target)
            self._dispatch(target, active_time + elapsed if test_time else active_time, watchdog, recorder)
            self._sample_feedback(target, active_time, update_statistics=test_time)
            if test_time:
                self._update_progress(cycle_time, active_time + elapsed, completed_cycles, target)
            else:
                self._completion.update(return_seconds=elapsed)
            if elapsed >= duration:
                return target, elapsed
            self._tick(time.monotonic() + period, period)
        return target, max(0.0, time.monotonic() - started)

    def _tick(self, next_tick: float, period: float) -> float:
        now = time.monotonic()
        if next_tick > now:
            self._stop_event.wait(next_tick - now)
        elif now - next_tick > period * 4:
            return now
        return next_tick

    def _sample_feedback(self, target: np.ndarray, active_time: float,
                         *, update_statistics: bool) -> list[MotorSample]:
        samples = self.backend.read_samples()
        stale_seconds = (self._parameters.feedback_stale_seconds if self._parameters is not None
                         else self._diagnosis.thresholds.stale_seconds)
        if len(samples) != len(self.config.entries):
            raise PlaybackError("电机反馈数量不完整")
        for sample in samples:
            if (sample.error_id or not math.isfinite(sample.pos_rad)
                    or not math.isfinite(sample.spd_rad_s)
                    or not math.isfinite(sample.feedback_age_s)
                    or sample.feedback_age_s >= stale_seconds
                    or not math.isfinite(sample.timestamp_s) or sample.timestamp_s <= 0):
                raise PlaybackError(f"电机故障或反馈失效: id={sample.motor_id}")
        positions = np.array([s.pos_rad for s in samples])
        if self._parameters is not None:
            self._parameters.check_target(self.config, positions)
            speed_limit = self._parameters.values['max_velocity_rad_s']
        else:
            limits = self.config.safety_limits
            bounds = [(positions, limits.motor_min_rad, limits.motor_max_rad)]
            if limits.joint_min_rad is not None or limits.joint_max_rad is not None:
                bounds.append((motor_positions_to_joint(self.config, positions), limits.joint_min_rad, limits.joint_max_rad))
            for values, lower, upper in bounds:
                if ((lower is not None and np.any(values < np.asarray(lower)))
                        or (upper is not None and np.any(values > np.asarray(upper)))):
                    raise PlaybackError('实际位置超过当前配置限位')
            speed_limit = limits.max_velocity_rad_s
        if speed_limit is not None and np.any(np.abs([s.spd_rad_s for s in samples]) > speed_limit):
            raise PlaybackError("实际电机速度超过当前配置上限")
        with self._lock:
            self._last_samples = samples
            if update_statistics and not self._test_finished.is_set():
                self._record_samples = samples
        if self.telemetry_hub is not None:
            self.telemetry_hub.publish(samples)
        critical = [d for d in self._diagnosis.diagnose(samples,
            heartbeat_age_s=self.heartbeat.age(), now_s=time.monotonic()) if d.severity == "critical"]
        if critical:
            raise PlaybackError(f"safety rule {critical[0].rule}: {'; '.join(critical[0].evidence)}")
        if update_statistics:
            self._statistics.update(samples, target)
        return samples

    def _end_test_sampling(self, recorder: MotorTelemetryRecorder | None) -> None:
        with self._lock:
            self._test_finished.set()
            self._ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if recorder is not None:
            recorder.request_stop_sampling()

    def _return_and_settle(self, start: np.ndarray, active_time: float,
                           watchdog: MotorFeedbackWatchdog | None, deceleration_seconds: float = 0.0) -> None:
        assert self._parameters is not None
        params = self._parameters
        duration = params.return_duration(start)
        period = 1.0 / self.config.control_rate_hz
        started = time.monotonic()
        stable_since = None
        stable_timestamps = None
        timed_out = False
        self._completion.update(phase="returning")
        self._completion_evidence.update(configuration_sha256=params.configuration_sha256,
            parameter_revision=params.parameter_revision, parking_joint_rad=params.values['parking_joint_rad'].tolist(),
            parking_motor_rad=params.parking_motor_rad.tolist(), return_started_at=datetime.now(timezone.utc).isoformat(),
            planned_return_seconds=duration)
        while not self._stop_event.is_set():
            now = time.monotonic()
            elapsed = now - started
            fraction = min(1.0, elapsed / duration)
            blend = fraction ** 3 * (10 + fraction * (-15 + 6 * fraction))
            target = start + (params.parking_motor_rad - start) * blend
            params.check_target(self.config, target)
            self._dispatch(target, active_time, watchdog, None)
            samples = self._sample_feedback(target, active_time, update_statistics=False)
            return_seconds = deceleration_seconds + min(elapsed, duration)
            settling_seconds = max(0.0, elapsed - duration)
            self._completion.update(return_seconds=return_seconds, settling_seconds=settling_seconds)
            if fraction >= 1:
                positions = np.array([s.pos_rad for s in samples])
                speeds = np.array([s.spd_rad_s for s in samples])
                at_rest = bool(np.all(np.abs(positions - params.parking_motor_rad) <= params.values['position_tolerance_rad'])
                    and np.all(np.abs(speeds) <= params.values['speed_tolerance_rad_s']))
                if at_rest:
                    feedback_timestamps = np.array([s.timestamp_s for s in samples])
                    if stable_since is None:
                        stable_since = now
                        stable_timestamps = feedback_timestamps.copy()
                    feedback_stable = bool(np.all(feedback_timestamps - stable_timestamps >= params.stable_seconds))
                    if not timed_out:
                        self._completion.update(phase="settling")
                    if now - stable_since >= params.stable_seconds and feedback_stable and not timed_out:
                        # Final feedback validation immediately precedes removal
                        # of torque. Fault/disable preemption always wins.
                        if self._stop_event.is_set():
                            return
                        self.backend.disable_motors()
                        self._completion.update(phase="safe_disabled", active=False, safe_disabled=True)
                        self._completion_evidence.update(final_feedback=[s.to_dict() for s in samples],
                            stable_seconds_observed=now-stable_since,
                            completed_at=datetime.now(timezone.utc).isoformat())
                        return
                else:
                    stable_since = None
                    stable_timestamps = None
            if elapsed + deceleration_seconds >= params.timeout_seconds and not timed_out:
                timed_out = True
                self._completion.update(phase="needs_attention", alarm="回位/稳定确认超时：保持目标并报警，未自动失能")
                self._completion_evidence['timeout_feedback'] = [s.to_dict() for s in samples]
                timeout_path = self.completion_output.with_name(self.completion_output.stem + '.needs_attention.json')
                self._completion['evidence_path'] = str(timeout_path)
                self._write_completion_evidence(timeout_path)
            self._tick(time.monotonic() + period, period)

    def _dispatch(
        self,
        target: np.ndarray,
        active_time: float,
        watchdog: MotorFeedbackWatchdog | None,
        recorder: MotorTelemetryRecorder | None,
    ) -> None:
        if self._stop_event.is_set():
            return
        torque = None
        if hip_roll_grav_comp_enabled(self.config.raw):
            torque = compute_hip_roll_grav_torque(
                target,
                self.config.raw,
                self.config.limb,
                list(self.config.entries),
            )
        self.backend.command_mit(target, torque=torque)
        self.heartbeat.touch()
        self._command_position.set(target)
        with self._lock:
            self._progress.command_count += 1
        if recorder is not None and (record_error := recorder.health_error()) is not None:
            raise PlaybackError(record_error)
        if watchdog is not None and not watchdog.maybe_check(
            target, elapsed_s=active_time
        ):
            raise PlaybackError(
                watchdog.fault_message or "motor feedback watchdog fault"
            )

    def _update_progress(
        self,
        cycle_time: float,
        active_time: float,
        completed_cycles: int,
        target: np.ndarray,
    ) -> None:
        del target
        duration = self.trajectory.duration_s
        frame = int(
            np.searchsorted(
                self.trajectory.timestamps,
                min(cycle_time, duration),
                side="right",
            )
            - 1
        )
        frame = max(0, min(frame, self.trajectory.frame_count - 1))
        with self._lock:
            self._progress.cycle = completed_cycles + 1
            self._progress.completed_cycles = completed_cycles
            self._progress.frame = frame
            self._progress.active_seconds = active_time
            self._progress.trajectory_seconds = cycle_time
            self._progress.fraction = (
                min(1.0, max(0.0, cycle_time / duration))
                if duration > 0.0
                else 1.0
            )

    def _handle_fault(self, exc: Exception) -> None:
        with self._lock:
            if self._fault is not None:
                return
        self._stop_event.set()
        samples: list[MotorSample]
        # Fault selection uses the existing cache; do not poll a failed transport here.
        samples = self.last_samples()
        diagnoses = self._diagnosis.diagnose(
            samples,
            heartbeat_age_s=self.heartbeat.age(),
        )
        fault = FaultStatus(
            message=str(exc),
            kind=exc.__class__.__name__,
            timestamp_s=time.monotonic(),
            diagnoses=diagnoses,
        )
        with self._lock:
            self._fault = fault
            self._completion.update(phase="fault", active=False, alarm=str(exc))
            self._last_samples = samples
            self._progress.outcome = "fault"
        self._fault_response = new_fault_response()
        # Latch FAULT before a transport disable can block. A concurrent loop
        # exit must never transition to COMPLETED and restart position hold.
        current = self.state_machine.state
        if current is not StationState.FAULT:
            try:
                self.state_machine.transition(StationState.FAULT, expected=current)
            except InvalidStateTransition:
                pass
        try:
            protected = self.on_protective_fault(fault,samples) if self.on_protective_fault is not None else False
        except Exception as selector_error:
            self._fault_response['protective_selection_error'] = str(selector_error)
            protected = False
        if protected is True:
            self._fault_response.update(disable_request="not_requested",protective_damping="transition_requested")
        else:
            request_fault_disable(self.backend,self._fault_response)
        if self.on_fault is not None:
            self.on_fault(fault)


__all__ = ["PlaybackController", "PlaybackError"]
