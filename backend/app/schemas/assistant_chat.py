from typing import Literal, Annotated
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class HmiContext(BaseModel):
    """Diagnostic provenance only; never an authorization claim or device command."""
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    window_id: str = Field(min_length=1, max_length=128)
    station: str = Field(max_length=64)
    running_version: str = Field(max_length=200)


class AssistantReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['assets', 'stations', 'executions']
    id: UUID
    label: str = Field(default='', max_length=200)


class PageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: str = Field(default="home", max_length=40, pattern=r"^[a-z-]+$")
    kind: Literal["MODULE", "WHOLE_MACHINE"] = "WHOLE_MACHINE"
    object_id: UUID | None = None
    archive_id: UUID | None = None
    selection_empty: bool = False
    metrics: list[str] = Field(default_factory=list, max_length=8)
    subjects: list[str] = Field(default_factory=list, max_length=8)
    start: float | None = Field(default=None,ge=0,le=1e12,allow_inf_nan=False)
    end: float | None = Field(default=None,ge=0,le=1e12,allow_inf_nan=False)
    stage: str | None = Field(default=None,max_length=100)
    cycle: str | None = Field(default=None,max_length=100)
    target_part_code: str | None = Field(default=None,max_length=30)
    hmi: HmiContext | None = None
    references: list[AssistantReference] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_selection(self):
        if self.start is not None and self.end is not None and self.end<self.start: raise ValueError("区间结束不能早于开始")
        if any(len(v)>255 for v in self.metrics+self.subjects): raise ValueError("曲线标识过长")
        return self


class ChatSubmit(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)
    request_key: UUID
    thread_id: UUID | None = None
    message: str = Field(min_length=1,max_length=8000)
    context: PageContext = Field(default_factory=PageContext)
    attachments: list[UUID] = Field(default_factory=list,max_length=3)
    after_job_id: UUID | None = None


class ThreadUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)


class SteeringInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_key: UUID
    expected_job_id: UUID
    message: str = Field(min_length=1, max_length=8000)


class PendingInputPoll(BaseModel):
    model_config = ConfigDict(extra='forbid')
    closing: bool = False


class ReadTool(BaseModel):
    model_config=ConfigDict(extra="forbid")
    tool: str = Field(pattern=r"^[a-z_]{1,50}$")
    arguments: dict = Field(default_factory=dict)


class MeterCall(BaseModel):
    model_config=ConfigDict(extra="forbid")
    phase: Literal["reserve","settle"]
    sequence: StrictInt = Field(ge=1,le=6)
    usage: dict | None = None
    model: str | None = Field(default=None, max_length=200)


class CurveArguments(BaseModel):
    model_config=ConfigDict(extra="forbid")
    reference: str | None = Field(default=None,max_length=300)
    metrics: list[str] = Field(default_factory=list,max_length=8)
    subjects: list[str] = Field(default_factory=list,max_length=8)
    start: float | None = Field(default=None,ge=0,le=1e12,allow_inf_nan=False)
    end: float | None = Field(default=None,ge=0,le=1e12,allow_inf_nan=False)
    stage: str | None = Field(default=None,max_length=100)
    cycle: str | None = Field(default=None,max_length=100)


class ProgressArguments(BaseModel):
    model_config=ConfigDict(extra="forbid")
    campaign_id: UUID | None = None


class DiagnosticAttachmentArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attachment_id: UUID
    operation: Literal['index', 'read', 'csv_statistics'] = 'read'
    member: str | None = Field(default=None, min_length=1, max_length=1024)
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=16384, ge=1, le=65536)
    rows_start: StrictInt = Field(default=0, ge=0)
    rows_limit: StrictInt | None = Field(default=None, ge=1, le=10_000_000)
    columns: list[str] | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode='after')
    def valid_columns(self):
        if self.columns and (len(set(self.columns)) != len(self.columns) or any(not x or len(x) > 255 for x in self.columns)):
            raise ValueError('CSV列须为不重复的明确列名')
        return self
