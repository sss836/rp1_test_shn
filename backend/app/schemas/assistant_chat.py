from typing import Literal, Annotated
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


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


class ReadTool(BaseModel):
    model_config=ConfigDict(extra="forbid")
    tool: Literal["get_curve_statistics","get_test_progress","list_hmi_stations"]
    arguments: dict = Field(default_factory=dict)


class MeterCall(BaseModel):
    model_config=ConfigDict(extra="forbid")
    phase: Literal["reserve","settle"]
    sequence: StrictInt = Field(ge=1,le=6)
    usage: dict | None = None


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
