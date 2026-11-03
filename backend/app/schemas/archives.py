"""Typed, bounded archive metadata; raw CSV is accepted only by the byte endpoint."""
from datetime import datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


class ArchiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class AcquisitionFacts(ArchiveModel):
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    active_seconds: float = Field(ge=0, le=315360000)
    completed_cycles: int = Field(ge=0, le=2147483647)
    machine_status: Literal["completed", "failed", "stopped", "interrupted", "unknown"]
    automatic_verdict: Literal["passed", "failed", "blocked", "unconfigured"]
    data_quality: Literal["VALID", "PARTIAL", "INVALID", "MISSING"]
    clock_quality: Literal["VALID", "PARTIAL", "INVALID", "MISSING"]
    exception_count: int = Field(ge=0, le=2147483647)
    summary: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def timing(self):
        if (self.started_at is None) != (self.ended_at is None):
            raise ValueError("开始与结束时间必须同时填写或同时缺失")
        if self.started_at is None:
            if self.clock_quality not in {"MISSING", "INVALID"}:
                raise ValueError("缺失采集时间不得标记为可靠时钟")
            return self
        if self.ended_at < self.started_at or self.active_seconds > (self.ended_at - self.started_at).total_seconds() + .001:
            raise ValueError("有效时长不得超过采集开始至结束的区间")
        if self.ended_at > datetime.now(self.ended_at.tzinfo) + timedelta(minutes=5):
            raise ValueError("结束时间不能在当前时间五分钟后")
        return self


class AcceptanceRules(ArchiveModel):
    criteria_version: str = Field(default="UNCONFIGURED", min_length=1, max_length=100)
    record_rate_hz: float | None = Field(default=None, gt=0, le=100000)
    required_active_seconds: float | None = Field(default=None, ge=0, le=315360000)
    required_cycles: int | None = Field(default=None, ge=0, le=2147483647)
    min_sample_completeness: float | None = Field(default=None, ge=0, le=1)
    max_gap_seconds: float | None = Field(default=None, ge=0, le=315360000)
    max_temperature_rise_c: float | None = Field(default=None, ge=0, le=10000)
    max_tracking_rms_rad: float | None = Field(default=None, ge=0, le=1000000)
    max_torque_abs_nm: float | None = Field(default=None, ge=0, le=1e12)


class ArchiveContext(ArchiveModel):
    campaign_id: UUID
    cycle_id: UUID
    segment_id: UUID
    asset_id: UUID
    configuration_id: UUID
    test_case_version_id: UUID
    station_id: UUID


class ArchiveCreate(ArchiveModel):
    data_domain: Literal["RESEARCH", "FACTORY"] | None = None
    serial_number: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=200)] | None = None
    bench_id: str | None = Field(default=None, min_length=1, max_length=100)
    execution_uuid: UUID
    test_name: str = Field(min_length=1, max_length=200)
    actual_object: str = Field(min_length=1, max_length=200)
    station_label: str = Field(min_length=1, max_length=100)
    origin: Literal["HMI", "HISTORY", "OTHER"] = "HMI"
    filename: str = Field(min_length=5, max_length=200)
    size_bytes: int = Field(ge=1, le=50 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts: AcquisitionFacts
    acceptance: AcceptanceRules = Field(default_factory=AcceptanceRules)
    classification: ArchiveContext | None = None

    @model_validator(mode="after")
    def file_metadata(self):
        if self.data_domain == "FACTORY" and (self.serial_number is None or not self.serial_number.strip()):
            raise ValueError("工厂归档必须提供本次原始SN")
        if (PurePosixPath(self.filename).name != self.filename
                or PureWindowsPath(self.filename).name != self.filename
                or not self.filename.lower().endswith(".csv")
                or "\x00" in self.filename):
            raise ValueError("文件名必须是无目录的 CSV 文件名")
        if self.origin != "HISTORY" and self.facts.started_at is None:
            raise ValueError("新测试必须包含真实采集开始和结束时间")
        return self


class ArchiveVerification(ArchiveModel):
    expected_revision: int = Field(ge=0)
    tester: str = Field(min_length=1, max_length=100)
    station_id: UUID
    verdict: Literal["passed", "failed", "blocked"]
    notes: str = Field(min_length=1, max_length=2000)
    discrepancy_reason: str = Field(default="", max_length=1000)
    declared_started_at: AwareDatetime | None = None
    declared_ended_at: AwareDatetime | None = None
    declared_active_seconds: float | None = Field(default=None, ge=0, le=315360000)
    declared_completed_cycles: int | None = Field(default=None, ge=0, le=2147483647)
    declared_exception_count: int | None = Field(default=None, ge=0, le=2147483647)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def timing(self):
        if (self.declared_started_at is None) != (self.declared_ended_at is None):
            raise ValueError("人工补录时间必须同时填写")
        if self.declared_started_at is not None:
            if self.declared_ended_at < self.declared_started_at:
                raise ValueError("人工补录结束时间必须不早于开始时间")
            if self.declared_ended_at > datetime.now(self.declared_ended_at.tzinfo) + timedelta(minutes=5):
                raise ValueError("人工补录结束时间不能在未来")
            if self.declared_active_seconds is not None and self.declared_active_seconds > (self.declared_ended_at - self.declared_started_at).total_seconds() + .001:
                raise ValueError("人工申报运行时长不得超过开始至结束的区间")
        return self


class ArchiveClassification(ArchiveModel):
    expected_revision: int = Field(ge=0)
    context: ArchiveContext
    reason: str = Field(min_length=1, max_length=300)


class ArchiveSubmit(ArchiveModel):
    expected_revision: int = Field(ge=0)
    expected_submission_revision: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=300)


class ArchiveIntakeVerification(ArchiveVerification):
    notes: str = Field(default="", max_length=1000)
    reason: str = Field(default="历史CSV补录送审", min_length=1, max_length=300)


class ArchiveIntakeSubmit(ArchiveModel):
    verification: ArchiveIntakeVerification
    context: ArchiveContext
    expected_submission_revision: int = Field(default=0, ge=0)


class ArchiveAnalysisCreate(ArchiveModel):
    test_case_version_id: UUID
    reason: str = Field(min_length=1,max_length=300)


class ArchiveImportCreate(ArchiveModel):
    data_domain: Literal["RESEARCH", "FACTORY"] | None = None
    serial_number: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=200)] | None = None
    bench_id: str | None = Field(default=None, min_length=1, max_length=100)
    """Historical CSV import metadata. Acquisition facts are server-owned unknowns."""
    filename: str = Field(min_length=5, max_length=200)
    size_bytes: int = Field(ge=1, le=50 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_name: str = Field(min_length=1, max_length=200)
    actual_object: str = Field(min_length=1, max_length=200)
    station_id: UUID
    classification: ArchiveContext | None = None

    @model_validator(mode="after")
    def metadata(self):
        if (PurePosixPath(self.filename).name != self.filename
                or PureWindowsPath(self.filename).name != self.filename
                or not self.filename.lower().endswith(".csv")
                or "\x00" in self.filename):
            raise ValueError("文件名必须是无目录的 CSV 文件名")
        if self.classification and self.classification.station_id != self.station_id:
            raise ValueError("正式关联工位必须与补录工位一致")
        return self


class ArchiveFactoryReview(ArchiveModel):
    expected_submission_revision: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=1, max_length=300)


class ArchiveDomainClassification(ArchiveModel):
    expected_revision: int = Field(ge=0)
    data_domain: Literal["RESEARCH", "FACTORY", "UNCLASSIFIED"]
    serial_number: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=200)] | None = None
    bench_id: str | None = Field(default=None, min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=300)

class RunSegment(ArchiveModel):
    sequence: int = Field(ge=0,lt=4096)
    size_bytes: int = Field(ge=1,le=128*1024**2+16384)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    previous_sha256: str = Field(max_length=64)
    sample_count: int = Field(ge=0)
    first_time_s: float | None = None
    last_time_s: float | None = None
    first_sequence: int | None = Field(default=None,ge=0)
    last_sequence: int | None = Field(default=None,ge=0)

class RunManifest(ArchiveModel):
    schema_version: Literal['run-segments-v1']
    execution_uuid: UUID
    logical_size_bytes: int = Field(ge=1,le=50*1024**3)
    logical_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    policy_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    segments: list[RunSegment] = Field(min_length=1,max_length=4096)

    @model_validator(mode='after')
    def chain(self):
        previous='';last_sequence=None
        for n,part in enumerate(self.segments):
            if part.sequence!=n or part.previous_sha256!=previous:
                raise ValueError('分段顺序或哈希链不连续')
            if last_sequence is not None and part.first_sequence!=last_sequence+1:
                raise ValueError('分段采样序号不连续')
            if part.first_sequence is not None and part.last_sequence is not None and part.last_sequence-part.first_sequence+1!=part.sample_count:
                raise ValueError('分段采样数量不一致')
            previous=part.sha256;last_sequence=part.last_sequence
        return self

class ArchiveLedgerFilters(ArchiveModel):
    q: str | None = Field(default=None,max_length=200)
    sn: str | None = Field(default=None,max_length=200)
    bench: str | None = Field(default=None,max_length=100)
    data_domain: Literal['RESEARCH','FACTORY','UNCLASSIFIED'] | None = None
    submission_status: Literal['PENDING','APPROVED','REJECTED'] | None = None
    origin: Literal['HMI','HISTORY','OTHER'] | None = None
    classification_state: Literal['UNCLASSIFIED','CLASSIFIED'] | None = None
    processing_state: Literal['WAITING','QUEUED','PROCESSING','READY','FAILED'] | None = None
    started_from: AwareDatetime | None = None
    started_to: AwareDatetime | None = None
