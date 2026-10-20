"""Bounded-memory reception of local NPZ trajectories."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

DEFAULT_TRAJECTORY_MAX_BYTES = 1024**3
TRAJECTORY_LIMIT_ENV = "RP1_FACTORY_TRAJECTORY_MAX_BYTES"


def configured_trajectory_limit(value: int | None = None) -> int:
    """Return the byte limit; zero explicitly disables the size cap."""
    if value is None:
        raw = os.environ.get(TRAJECTORY_LIMIT_ENV, "").strip()
        try:
            value = int(raw) if raw else DEFAULT_TRAJECTORY_MAX_BYTES
        except ValueError as exc:
            raise ValueError(f"{TRAJECTORY_LIMIT_ENV} must be a non-negative integer") from exc
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{TRAJECTORY_LIMIT_ENV} must be a non-negative integer")
    return value


def _size_error(limit: int) -> HTTPException:
    detail = (
        f"trajectory must be 1 byte to {limit} bytes ({limit / 1024**2:g} MiB)"
        if limit else "trajectory must contain at least 1 byte"
    )
    return HTTPException(status_code=413, detail=detail)


async def receive_trajectory(
    request: Request, directory: Path, limit: int,
) -> tuple[Path, int]:
    """Stream into a unique temporary file, removing incomplete uploads."""
    declared = request.headers.get("content-length")
    expected: int | None = None
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if expected < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if expected == 0 or (limit and expected > limit):
            raise _size_error(limit)

    descriptor, name = await run_in_threadpool(
        tempfile.mkstemp, prefix=".trajectory-", suffix=".npz.part", dir=directory,
    )
    temporary = Path(name)
    received = 0
    try:
        with os.fdopen(descriptor, "wb") as destination:
            async for chunk in request.stream():
                if limit and received + len(chunk) > limit:
                    raise _size_error(limit)
                if chunk:
                    await run_in_threadpool(destination.write, chunk)
                    received += len(chunk)
            await run_in_threadpool(destination.flush)
        if received == 0:
            raise _size_error(limit)
        if expected is not None and received != expected:
            raise HTTPException(status_code=400, detail="trajectory Content-Length mismatch")
        return temporary, received
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
