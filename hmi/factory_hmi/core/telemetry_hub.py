"""Bounded observation cache. This module never owns or reads a device."""
from __future__ import annotations
from collections import deque
import json, math, threading, time, uuid

METRICS = {
 "position_rad": ("rad", "measured_motor_angle"),
 "command_position_rad": ("rad", "commanded_motor_angle"),
 "speed_rad_s": ("rad/s", "measured"), "torque_nm": ("N·m", "adapter_reported_torque"),
 "temperature_c": ("°C", "measured"), "bus_voltage_v": ("V", "optional_bus_measurement"),
 "bus_current_a": ("A", "optional_bus_measurement"), "error_id": ("code", "drive_event"),
 "iq_current_a": ("A", "drive_reported_iq_current"),
 "feedback_age_s": ("s", "host_derived"),
}
class TelemetryHub:
 def __init__(self, *, max_bytes=4*1024*1024, window_s=60., max_frames=1200):
  if min(max_bytes,window_s,max_frames)<=0: raise ValueError("invalid cache budget")
  self.max_bytes=max_bytes; self.window_s=window_s; self.max_frames=max_frames
  self.boot_id=uuid.uuid4().hex; self.stream_id=uuid.uuid4().hex; self.run_id=""; self.config_digest=""
  self._frames=deque(); self._bytes=0; self._sequence=0; self._lock=threading.Lock(); self._last_publish=0.
 def begin(self, run_id, config_digest=""):
  with self._lock:
   self.run_id=str(run_id or ""); self.config_digest=config_digest; self.stream_id=uuid.uuid4().hex
   self._frames.clear(); self._bytes=0; self._sequence=0; self._last_publish=0.
 def publish(self, samples, *, now=None, utc=None):
  now=time.monotonic() if now is None else now
  # Bound work in the control path: at most 20 host observations/s, never claim a device rate.
  with self._lock:
   if now-self._last_publish < .05: return
   self._last_publish=now; self._sequence+=1
   values={}; quality={}; source_times=[]
   for i,s in enumerate(samples):
    d=s.to_dict(); name=s.joint_name or str(i)
    source_times.append(d.get("timestamp_s"))
    for metric in METRICS:
     key=f"{name}/{metric}"; value=d.get(metric); age=d.get("feedback_age_s")
     values[key]=value
     quality[key]="missing" if value is None else "stale" if age is None or age>.5 else "valid"
   frame=dict(schema_version="telemetry-v1",boot_id=self.boot_id,stream_id=self.stream_id,
    run_id=self.run_id,config_digest=self.config_digest,sequence=self._sequence,
    received_monotonic_s=now,received_utc_s=time.time() if utc is None else utc,
    source_times_s=source_times,time_quality="host_observation",values=values,quality=quality)
   size=len(json.dumps(frame,allow_nan=False,separators=(",",":")).encode())
   if size>self.max_bytes: return
   self._frames.append((frame,size)); self._bytes+=size
   while self._frames and (self._bytes>self.max_bytes or len(self._frames)>self.max_frames or now-self._frames[0][0]["received_monotonic_s"]>self.window_s):
    self._bytes-=self._frames.popleft()[1]
 def read(self, *, after=0, limit=600, max_response_bytes=None, now=None):
  if not 1<=limit<=600: raise ValueError("limit must be 1..600")
  if max_response_bytes is not None and max_response_bytes < 1: raise ValueError("invalid display byte budget")
  now=time.monotonic() if now is None else now
  with self._lock:
   frames=[]
   used=0
   for frame,size in reversed(self._frames):
    if frame["sequence"]<=after or len(frames)>=limit:
     break
    if max_response_bytes is not None and used+size+1>max_response_bytes:
     break
    frames.append(frame)
    used+=size+1
   frames.reverse()
   age=max(0.,now-self._frames[-1][0]["received_monotonic_s"]) if self._frames else None
   return dict(schema_version="telemetry-v1",run_id=self.run_id,stream_id=self.stream_id,
    boot_id=self.boot_id,frames=frames,age_s=age,stale=age is None or age>.5,
    cache_bytes=self._bytes,window_s=self.window_s,first_sequence=self._frames[0][0]["sequence"] if self._frames else None,
    last_sequence=self._sequence,sequence_gap=(self._sequence>after and (
      not frames or frames[0]["sequence"]>after+1 or frames[-1]["sequence"]<self._sequence
      or frames[-1]["sequence"]-frames[0]["sequence"]+1!=len(frames))),
    display_rate_limit_hz=20,source_rate_hz=None,
    observed_rate_hz=(len(self._frames)-1)/(self._frames[-1][0]["received_monotonic_s"]-self._frames[0][0]["received_monotonic_s"]) if len(self._frames)>1 else None)
 def capabilities(self):
  data=self.read(); keys=data["frames"][-1]["values"] if data["frames"] else {}
  return dict(schema_version="telemetry-v1",metrics=[dict(id=k,unit=METRICS[k.rsplit("/",1)[1]][0],source=METRICS[k.rsplit("/",1)[1]][1],available=keys[k] is not None) for k in keys],
   max_metrics=12,max_points=600,max_bytes=self.max_bytes,window_s=self.window_s,
   control_rate_is_not_measurement_rate=True,time_quality="host_observation")
