"""Identify the loaded code once, separately from the installed DEB metadata."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path


def code_version() -> str:
    path = Path(__file__).resolve().parents[1] / "packaging" / "LOCAL_VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def process_identity() -> dict[str, object]:
    try:
        stat = Path("/proc/self/stat").read_text()
        ticks = stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        ticks = ""
    return {"running_version": code_version(), "pid": os.getpid(),
            "start_ticks": ticks, "gateway_created_at": datetime.now(timezone.utc).isoformat()}
