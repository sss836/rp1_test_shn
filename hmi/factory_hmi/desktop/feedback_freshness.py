"""Pure display-age helpers. Server clocks are never compared to client clocks."""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


DISPLAY_STALE_S = 0.2
STATUS_SILENCE_S = 1.0


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def feedback_age_s(motor: Mapping[str, Any], server_now: Any, elapsed_s: float) -> float:
    """Conservatively combine reported age, same-server timestamps and local gap."""
    if "timestamp_s" in motor:
        stamp = finite_number(motor["timestamp_s"])
        if stamp is None or stamp <= 0:
            return math.inf
    else:
        stamp = None
    reported = None
    for key, scale in (("feedback_age_s", 1.), ("feedback_age_ms", .001),
                       ("feedback_age", .001), ("age_ms", .001)):
        if key not in motor:
            continue
        value = finite_number(motor[key])
        if value is None or value < 0:
            return math.inf
        reported = value * scale
        break
    server_time = finite_number(server_now)
    if stamp is not None and server_time is not None:
        if server_time < stamp:
            return math.inf
        reported = max(reported or 0., server_time - stamp)
    if reported is None:
        return math.inf
    return reported + max(0., elapsed_s)


def snapshot_key(snapshot: Mapping[str, Any], motors: Any) -> tuple | None:
    """Re-rendering/replaying an identical cached snapshot must not reset age."""
    if not isinstance(motors, (list, Mapping)):
        return None
    rows = motors.values() if isinstance(motors, Mapping) else motors
    keys = tuple((str(row.get("bus", row.get("can", ""))), str(row.get("motor_id", row.get("id", ""))),
                  str(row.get("joint_name", row.get("joint", ""))),
                  finite_number(row.get("timestamp_s")),
                  tuple(finite_number(row.get(k)) for k in ("feedback_age_s", "feedback_age_ms", "feedback_age", "age_ms")))
                 for row in rows if isinstance(row, Mapping))
    return (finite_number(snapshot.get("timestamp_s")), keys, str(snapshot.get("feedback_read_error") or ""))
