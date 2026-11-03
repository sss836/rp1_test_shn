"""Single-operator control lease used by the factory gateway."""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass


class LeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseSnapshot:
    owner: str | None
    age_s: float | None
    timeout_s: float
    expired: bool
    generation: int
    revoked: bool


DEFAULT_LEASE_TIMEOUT_S = 30.0


def validate_lease_timeout(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError("lease timeout must be finite and in (0, 300] seconds")
    return value


class ControlLease:
    def __init__(self, timeout_s: float = DEFAULT_LEASE_TIMEOUT_S) -> None:
        self.timeout_s = validate_lease_timeout(timeout_s)
        self._owner: str | None = None
        self._last_refresh: float | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._revoked = False
        self._requires_claim = False

    def claim(self, owner: str, *, now: float | None = None) -> None:
        owner = owner.strip()
        if not owner:
            raise ValueError("lease owner cannot be empty")
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._revoked or self._expired_locked(timestamp):
                raise LeaseConflict("lease expired; wait for safe cleanup before claiming again")
            if (
                self._owner is not None
                and self._owner != owner
                and not self._expired_locked(timestamp)
            ):
                raise LeaseConflict(f"station is controlled by {self._owner}")
            if self._owner is None:
                self._generation += 1
            self._owner = owner
            self._last_refresh = timestamp
            self._requires_claim = False

    def refresh(self, owner: str, *, now: float | None = None,
                generation: int | None = None, require_active: bool = False,
                command_completed: bool = False) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._revoked:
                raise LeaseConflict("lease revoked; explicit reconfiguration required")
            if generation is not None and generation != self._generation:
                raise LeaseConflict("stale lease generation")
            if self._owner is None:
                if require_active or self._requires_claim:
                    raise LeaseConflict("no active lease; explicit reconfiguration required")
                self._owner = owner
                self._generation += 1
            elif self._owner != owner:
                raise LeaseConflict(f"station is controlled by {self._owner}")
            elif self._expired_locked(timestamp) and not command_completed:
                raise LeaseConflict("lease expired; explicit reconfiguration required")
            self._last_refresh = timestamp

    def revoke_expired(self, *, now: float | None = None) -> LeaseSnapshot | None:
        """Atomic deadline decision; a late heartbeat cannot undo revocation."""
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._revoked or not self._expired_locked(timestamp):
                return None
            self._revoked = True
            self._requires_claim = True
            return LeaseSnapshot(self._owner, timestamp - self._last_refresh,
                                 self.timeout_s, True, self._generation, True)

    def release(self, owner: str | None = None) -> None:
        with self._lock:
            if owner is not None and self._owner not in {None, owner}:
                raise LeaseConflict(f"station is controlled by {self._owner}")
            self._owner = None
            self._last_refresh = None
            self._revoked = False
            self._generation += 1

    def expired(self, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._expired_locked(timestamp)

    def snapshot(self, *, now: float | None = None) -> LeaseSnapshot:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            age = None if self._last_refresh is None else max(0.0, timestamp - self._last_refresh)
            return LeaseSnapshot(
                owner=self._owner,
                age_s=age,
                timeout_s=self.timeout_s,
                expired=self._expired_locked(timestamp),
                generation=self._generation,
                revoked=self._revoked,
            )

    def _expired_locked(self, now: float) -> bool:
        return (
            self._owner is not None
            and self._last_refresh is not None
            and (self._revoked or now - self._last_refresh > self.timeout_s)
        )
