"""109 Iq × device-read Kt. No configured/target parameter fallback."""
import math
import time

SUPPORTED_MODELS = {0: "LRO_5550", 1: "LRO_6562", 2: "LRO_8462", 3: "LRO_10062"}


def decode_kt(payload):
    # Same unsigned big-endian /100 encoding as deploy_motorlro_params.py.
    if len(payload) < 4 or payload[:2] != bytes((0xA0, 0x16)):
        raise ValueError("Kt响应头或长度无效")
    value = int.from_bytes(payload[2:4], "big") / 100.0
    if value <= 0:
        raise ValueError("Kt必须为正值")
    return value


def estimate(entry, observation, iq, *, stale=180., now=None):
    missing = float("nan")
    now = time.monotonic() if now is None else now
    if not observation or observation.get("status") != "known":
        return missing, missing, "Kt待读取"
    identity = (entry.interface, entry.motor_id, SUPPORTED_MODELS.get(entry.motor_model))
    if identity[2] is None or identity != tuple(observation.get(k) for k in ("interface", "motor_id", "model")):
        return missing, missing, "Kt目标身份不符"
    kt = observation.get("torque_kt_nm_a")
    seen = observation.get("kt_monotonic")
    if not isinstance(kt, (int, float)) or not math.isfinite(kt) or kt <= 0:
        return missing, missing, observation.get("kt_error") or "Kt待读取"
    if not isinstance(seen, (int, float)) or not math.isfinite(seen) or not 0 <= now-seen <= stale:
        return missing, missing, "Kt读回已过期"
    if not math.isfinite(iq):
        return missing, kt, "Iq反馈不可用"
    return iq * kt, kt, "device_kt_times_iq"
