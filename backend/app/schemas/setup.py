from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, StrictBool, model_validator


Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Part = Literal["SYS", "ARM", "SARM", "LEG", "SLEG", "UPPER", "LOWER", "WAISTHIP", "CHEST", "HEAD", "BAT"]
Kind = Literal["WHOLE_MACHINE", "MODULE"]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=2048)


class AssetCreate(Change):
    asset_code: str = Field(pattern=r"^RP[0-9]+\.[0-9]+-(SYS|ARM|SARM|LEG|SLEG|UPPER|LOWER|WAISTHIP|CHEST|HEAD|BAT)-[0-9]{3}$")
    asset_kind: Kind
    target_part_code: Part
    product_family: Name
    serial_number: Name
    display_name: Name
    model: Name
    configuration_fingerprint: Name
    hardware_manifest: dict[str, Any] = Field(default_factory=dict)
    software_manifest: dict[str, Any] = Field(default_factory=dict)
    parameter_manifest: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def consistent_part(self):
        if self.asset_code.split("-")[1] != self.target_part_code:
            raise ValueError("样品编号部位必须与选择的部位一致")
        if (self.asset_kind == "WHOLE_MACHINE") != (self.target_part_code == "SYS"):
            raise ValueError("整机仅使用SYS，模块必须使用模块部位")
        return self


class ProgramCreate(Change):
    program_code: Code
    name: Name
    objective: str = Field(default="", max_length=4096)


class CampaignCreate(Change):
    program_id: UUID
    campaign_code: Code
    name: Name
    asset_kind: Kind
    planned_start: AwareDatetime | None = None
    planned_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.planned_start and self.planned_end and self.planned_end < self.planned_start:
            raise ValueError("计划结束时间不能早于开始时间")
        return self


class StationCreate(Change):
    site_code: Code
    site_name: Name
    lab_code: Code
    lab_name: Name
    station_code: Code
    name: Name
    station_type: Literal["WHOLE_MACHINE", "MODULE", "SHARED"]


class ContextCreate(Change):
    asset_id: UUID
    test_case_version_id: UUID
    station_id: UUID
    configuration_id: UUID
    cycle_code: Code
    planned_runs: int = Field(default=1, ge=1, le=100000)


class CaseDefinition(Change):
    procedure_spec: dict[str, Any] = Field(default_factory=dict)
    stage_definitions: list[dict[str, Any]] = Field(default_factory=list)
    equipment_requirements: dict[str, Any] = Field(default_factory=dict)
    sampling_requirements: dict[str, Any] = Field(default_factory=dict)
    metric_requirements: list[Any] = Field(default_factory=list)
    fault_rules: dict[str, Any] = Field(default_factory=dict)
    termination_rules: dict[str, Any] = Field(default_factory=dict)
    decision_rules: dict[str, Any] = Field(default_factory=dict)
    source_references: list[Any] = Field(default_factory=list)
    deviation_notes: str = Field(default="",max_length=4096)
    publish: StrictBool = False


class CaseRevision(CaseDefinition):
    base_version_id: UUID | None = None


class CaseCreate(CaseDefinition):
    case_code: Code
    name: Name
    asset_kind: Kind
    target_part_code: Part
    domain: Name = "USER_DEFINED"
    evidence_type: Name = "UNSPECIFIED"

    @model_validator(mode="after")
    def correct_kind(self):
        if (self.asset_kind == "WHOLE_MACHINE") != (self.target_part_code == "SYS"):
            raise ValueError("用例对象类型与部位不一致")
        return self


class ConfigurationCreate(Change):
    fingerprint: Name
    hardware_manifest: dict[str, Any] = Field(default_factory=dict)
    software_manifest: dict[str, Any] = Field(default_factory=dict)
    parameter_manifest: dict[str, Any] = Field(default_factory=dict)


class MetadataChange(Change):
    name: Name
    objective: str | None = Field(default=None, max_length=4096)


class SetupOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["asset", "program", "campaign", "station", "case", "context", "case_revision", "case_publish", "configuration", "asset_update", "program_update", "campaign_update", "station_update", "campaign_activate"]
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    data: dict[str, Any]


class SetupBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    operations: list[SetupOperation] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def distinct_aliases(self):
        if len({op.alias for op in self.operations}) != len(self.operations):
            raise ValueError("组内对象引用名必须唯一")
        return self
