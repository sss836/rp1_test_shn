"""Exact signed release contract shared with the HMI updater."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["rp1-hmi-release-v1"]
    release_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: str = Field(min_length=1, max_length=200, pattern=r"^[0-9][0-9A-Za-z.+:~\-]*$")
    os_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    os_version: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    architecture: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    package_name: Literal["rp1-test-hmi"]
    filename: str = Field(min_length=5, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._+\-]*\.deb$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str = Field(max_length=8000)
    published_at: str = Field(min_length=1, max_length=64)

    @field_validator("published_at")
    @classmethod
    def aware_iso_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("published_at requires an ISO timestamp with timezone")
        return value


class SignedRelease(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest: ReleaseManifest
    signature: str = Field(min_length=88, max_length=88)


class LatestRelease(BaseModel):
    available: bool
    can_update: bool
    current_version: str
    release: SignedRelease | None
