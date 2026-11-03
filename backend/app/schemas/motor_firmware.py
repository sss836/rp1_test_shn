"""Motor firmware has a separate signed contract; a DEB is never valid here."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MotorFirmwareManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["rp1-motor-firmware-v1"]
    artifact_type: Literal["motor_firmware"]
    release_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z._+\-]*$")
    software_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    vendor: Literal["LRO"]
    model: Literal["LRO_5550", "LRO_6562", "LRO_8462", "LRO_10062"]
    interface_type: Literal["canfd"]
    hardware_versions: list[str] = Field(min_length=1, max_length=64)
    protocol_versions: list[str] = Field(min_length=1, max_length=64)
    from_versions: list[str] = Field(min_length=1, max_length=128)
    filename: str = Field(max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]*\.lsepbin$")
    size_bytes: int = Field(gt=0, le=512 * 1024**2)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://[^\s]+$")
    scope: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=8000)
    published_at: str = Field(max_length=64)

    @field_validator("hardware_versions", "protocol_versions", "from_versions")
    @classmethod
    def exact_conditions(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+\-]{0,63}", v) for v in values):
            raise ValueError("Compatibility requires unique explicit versions; wildcards are forbidden")
        return values

    @field_validator("published_at")
    @classmethod
    def timestamp(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone required")
        return value


class SignedMotorFirmware(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest: MotorFirmwareManifest
    signature: str = Field(min_length=88, max_length=88)
