"""Bounded cold-start discovery policy, separate from motion safety gates."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


MAX_DISCOVERY_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class MotorDiscoveryPolicy:
    timeout_s: float = 5.0
    poll_interval_s: float = 0.05

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MotorDiscoveryPolicy":
        hmi = config.get("factory_hmi") or {}
        if not isinstance(hmi, dict):
            raise ValueError("factory_hmi must be a mapping")
        raw = hmi.get("discovery")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("factory_hmi.discovery must be a mapping")
        value = raw.get("timeout_s", cls.timeout_s)
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("discovery.timeout_s must be finite and in (0, 10] seconds") from exc
        if isinstance(value, bool) or not math.isfinite(timeout) or not 0 < timeout <= MAX_DISCOVERY_TIMEOUT_S:
            raise ValueError("discovery.timeout_s must be finite and in (0, 10] seconds")
        return cls(timeout_s=timeout)
