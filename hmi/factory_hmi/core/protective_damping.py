"""Passive MIT damping for loss of the operator, never a claim of support/braking."""
from __future__ import annotations
import math
import threading
import time


class ProtectiveDamping:
    def __init__(self, backend, *, rate_hz=200., kd=1., on_samples=None,
                 overtemperature_c=100., expected_generation=None):
        if not math.isfinite(rate_hz) or not 0 < rate_hz <= 200:
            raise ValueError('protective damping rate must be in (0, 200] Hz')
        if not math.isfinite(kd) or not 0 < kd <= 1:
            raise ValueError('protective damping Kd must be in (0, 1]')
        self.backend=backend;self.rate_hz=rate_hz;self.kd=kd
        self.generation=backend.control_generation if expected_generation is None else expected_generation
        self.on_samples=on_samples;self.overtemperature_c=overtemperature_c
        self.stop_event=threading.Event();self.thread=None;self.owner=None
        self._lifecycle=threading.RLock()
        self._guard=threading.Lock();self._status={'mode':'protective_damping',
            'active':False,'phase':'idle','kp':0.,'kd':kd,'velocity_target':0.,
            'feedforward_torque':0.,'safe_confirmed':False,'command_count':0,
            'feedback':'unknown','error':None,'disable_request':'not_requested'}

    def snapshot(self):
        with self._guard:return dict(self._status)

    def _update(self, **fields):
        with self._guard:self._status.update(fields)

    def start(self):
        with self._lifecycle:self._start()

    def _start(self):
        if self.stop_event.is_set():return
        if self.thread and self.thread.is_alive():return
        try:samples=self.backend.read_samples()
        except Exception as exc:
            self._update(feedback="unknown",feedback_error=str(exc));samples=[]
        if any(s.error_id or (math.isfinite(s.temp_c) and s.temp_c>=self.overtemperature_c) for s in samples):
            raise RuntimeError('drive fault or overtemperature prohibits protective damping')
        self.owner=self.backend.begin_protective_damping(self.generation)
        try:
            self.backend.command_protective_damping(self.owner,self.kd)
            self._update(active=True,phase='commanded',command_count=1,
                         last_command_monotonic_s=time.monotonic())
            if self.stop_event.is_set():
                self.backend.end_protective_damping(self.owner)
                self._update(active=False,phase="cancelled")
                return
            self.thread=threading.Thread(target=self._run,name='factory-protective-damping',daemon=True)
            self.thread.start()
        except Exception:
            self.backend.end_protective_damping(self.owner)
            raise

    def _run(self):
        period=1/self.rate_hz;next_tick=time.monotonic();last_read=float('-inf')
        try:
            while not self.stop_event.is_set():
                now=time.monotonic()
                if now-last_read>=.05:
                    try:samples=self.backend.read_samples()
                    except Exception as exc:
                        self._update(feedback='unknown',feedback_error=str(exc));samples=[]
                    if samples:
                        if any(s.error_id or (math.isfinite(s.temp_c) and s.temp_c>=self.overtemperature_c) for s in samples):
                            raise RuntimeError('drive fault or overtemperature during protective damping')
                        fresh=all(math.isfinite(s.feedback_age_s) and s.feedback_age_s<.5 for s in samples)
                        self._update(feedback='fresh' if fresh else 'stale',feedback_error=None)
                        if self.on_samples:self.on_samples(samples)
                    last_read=now
                if self.stop_event.is_set():break
                self.backend.command_protective_damping(self.owner,self.kd)
                with self._guard:
                    self._status['command_count']+=1
                    self._status['last_command_monotonic_s']=time.monotonic()
                next_tick+=period;delay=next_tick-time.monotonic()
                if delay>0:self.stop_event.wait(delay)
                elif -delay>period*4:next_tick=time.monotonic()
        except Exception as exc:
            self._update(phase='failed',error=str(exc),disable_request='requested')
            # A confirmed drive fault or an unusable command transport cannot
            # be represented as successful damping. Preserve the hard-fault path.
            try:
                sent=self.backend.disable_protective_owner(self.owner)
                self._update(disable_request='sent_unconfirmed' if sent else 'superseded_no_command')
            except Exception as error:self._update(disable_request='failed',disable_error=str(error))
        finally:
            self.backend.end_protective_damping(self.owner)
            self._update(active=False)

    def stop(self, timeout=2.):
        self.stop_event.set()
        with self._lifecycle:
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout)
                if self.thread.is_alive():raise TimeoutError('protective damping worker did not stop')
        self.backend.end_protective_damping(self.owner)
        self._update(active=False,phase='stopped' if self.snapshot()['phase']!='failed' else 'failed')
