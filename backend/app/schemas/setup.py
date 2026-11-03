from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, StrictBool, model_validator


Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Part = Literal["SYS", "ARM", "SARM", "LEG", "SLEG", "UPPER", "LOWER", "WAISTHIP", "CHEST", "HEAD", "BAT"]
Kind = Literal["WHOLE_MACHINE", "MODULE"]
CreationScenario = Literal["PRODUCTION", "RND_TEST"]
ASSET_PART_NAMES = {
    "SYS": "整机", "ARM": "7自由度手臂", "SARM": "5自由度手臂",
    "LEG": "6自由度腿部", "SLEG": "5自由度腿部", "UPPER": "上肢",
    "LOWER": "下肢", "WAISTHIP": "腰胯", "CHEST": "胸腔", "HEAD": "头部", "BAT": "动力电池",
}


def asset_identity(code: str):
    family, part, sequence = code.rsplit("-", 2)
    return family, part, "WHOLE_MACHINE" if part == "SYS" else "MODULE", f"{ASSET_PART_NAMES[part]}-{sequence}"


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=2048)


class AssetCreate(Change):
    reason: str = Field(default="sample registration", min_length=1, max_length=2048)
    asset_code: str = Field(pattern=r"^RP[0-9]+\.[0-9]+-(SYS|ARM|SARM|LEG|SLEG|UPPER|LOWER|WAISTHIP|CHEST|HEAD|BAT)-[0-9]{3}$")
    auto_number: StrictBool | None = None
    creation_scenario: CreationScenario | None = None
    asset_kind: Kind | None = None
    target_part_code: Part | None = None
    product_family: Name | None = None
    serial_number: Name | None = None
    display_name: Name | None = None
    model: Name | None = None
    configuration_fingerprint: Name | None = None
    hardware_manifest: dict[str, Any] = Field(default_factory=dict)
    software_manifest: dict[str, Any] = Field(default_factory=dict)
    parameter_manifest: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def consistent_part(self):
        legacy = all(getattr(self, name) is not None for name in (
            "asset_kind", "target_part_code", "product_family", "serial_number", "display_name", "model", "configuration_fingerprint"))
        if not legacy:
            missing = []
            if self.auto_number is None: missing.append("是否向下编号（是/否）")
            if self.creation_scenario is None: missing.append("创建场景（生产/研发测试）")
            if missing: raise ValueError("请选择" + "、".join(missing))
        family, part, kind, _ = asset_identity(self.asset_code)
        if self.target_part_code is not None and part != self.target_part_code:
            raise ValueError("样品编号部位必须与选择的部位一致")
        if self.asset_kind is not None and self.asset_kind != kind:
            raise ValueError("整机仅使用SYS，模块必须使用模块部位")
        if not legacy and self.product_family is not None and self.product_family != family:
            raise ValueError("产品族必须与产品编号的版本前缀一致")
        if self.configuration_fingerprint is None and any((self.hardware_manifest, self.software_manifest, self.parameter_manifest)):
            raise ValueError("包含配置清单的旧完整接口需要配置标识；普通编号建档无需填写配置")
        # Do not add derived/default fields to model_fields_set: legacy receipts
        # must retain their pre-upgrade single-request payload and hash.
        for field, value in {"asset_kind": kind, "target_part_code": part,
                             "product_family": self.product_family or family,
                             "auto_number": self.auto_number if self.auto_number is not None else False,
                             "creation_scenario": self.creation_scenario or "RND_TEST"}.items():
            object.__setattr__(self, field, value)
        return self

    @property
    def legacy_input(self):
        return (not {"auto_number", "creation_scenario"}.intersection(self.model_fields_set)
                and all(getattr(self, name) is not None for name in (
                    "serial_number", "display_name", "model", "configuration_fingerprint")))


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
