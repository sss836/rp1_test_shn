"""Signed download resources cannot stand in for a flashable motor manifest."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

MotorModel = Literal['LRO_5550', 'LRO_6562', 'LRO_8462', 'LRO_10062']


class MotorFirmwareResourceManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['rp1-motor-firmware-resource-v1']
    artifact_type: Literal['motor_firmware_resource']
    usage: Literal['download_only']
    flash_eligible: Literal[False]
    release_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    version_label: str = Field(min_length=1, max_length=64, pattern=r'^[0-9A-Za-z][0-9A-Za-z._+\-]*$')
    version_label_source: Literal['filename_label']
    vendor: Literal['LRO']
    models: list[MotorModel] = Field(min_length=1, max_length=4)
    # Explicit unknowns: no truncation of a four-part filename into a claimed
    # three-byte software version and no wildcard hardware/OTA compatibility.
    software_version: None
    hardware_versions: None
    protocol_versions: None
    from_versions: None
    compatibility_source: Literal['user_direct_confirmation']
    compatibility_statement: str = Field(min_length=1, max_length=4000)
    confirmation_evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_kind: Literal['user_supplied_file']
    source: str = Field(min_length=1, max_length=2048)
    filename: str = Field(max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9._+\-]*\.lsepbin$')
    size_bytes: int = Field(gt=0, le=512*1024**2)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scope: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=8000)
    published_at: str = Field(max_length=64)

    @field_validator('flash_eligible', mode='before')
    @classmethod
    def never_flashable(cls, value):
        if type(value) is not bool or value is not False:
            raise ValueError('Download resources are never flash eligible')
        return value

    @field_validator('models')
    @classmethod
    def unique_models(cls, values):
        if len(set(values)) != len(values):
            raise ValueError('Resource models must be unique explicit supported models')
        return values

    @field_validator('published_at')
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Timezone required')
        return value


class SignedMotorFirmwareResource(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    manifest: MotorFirmwareResourceManifest
    signature: str = Field(min_length=88, max_length=88)
