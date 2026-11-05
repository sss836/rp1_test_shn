"""Factory result summaries are separate from telemetry and formal executions."""
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class FactoryRobotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FactoryRobotPresence(FactoryRobotModel):
    robot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    serial_number: str = Field(min_length=1, max_length=200)
    model: Literal["24轴", "28轴"]
    station_name: str = Field(min_length=1, max_length=128)
    online: bool
    observed_at: AwareDatetime | None = None
    data_fresh: bool | None = None

    @model_validator(mode="after")
    def identity(self):
        for value in (self.serial_number, self.station_name):
            if value != value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError("样品和工位身份不得含首尾空格或控制字符")
        if self.observed_at and self.observed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("观测时间不能在当前时间五分钟后")
        return self


class FactoryRobotHeartbeat(FactoryRobotModel):
    protocol: Literal["reliability_v1"]
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    source_kind: Literal["real", "simulated"]
    robots: list[FactoryRobotPresence] = Field(max_length=100)

    @model_validator(mode="after")
    def distinct(self):
        if len({r.robot_id for r in self.robots}) != len(self.robots):
            raise ValueError("同一上位机的机器人ID不得重复")
        if len({r.serial_number for r in self.robots}) != len(self.robots):
            raise ValueError("同一上位机的SN不得重复绑定不同机器人")
        return self


class FactoryRobotUpload(FactoryRobotModel):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    robot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    source_kind: Literal["real", "simulated"]
    filename: str = Field(min_length=5, max_length=200)
    size_bytes: int = Field(ge=1, le=4 * 1024**2)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified: bool
    completed: bool
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def metadata(self):
        if not self.verified or not self.completed:
            raise ValueError("必须由人员确认测试已结束并核实后显式提交")
        if (PurePosixPath(self.filename).name != self.filename
                or PureWindowsPath(self.filename).name != self.filename
                or not self.filename.lower().endswith(".csv")
                or any(ord(c) < 32 for c in self.filename)):
            raise ValueError("文件名必须是无目录的CSV文件名")
        if not self.reason.strip():
            raise ValueError("请填写人工核实与提交说明")
        return self


class FactoryRobotReview(FactoryRobotModel):
    decision: Literal["APPROVE", "REJECT"]
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def reason_required(self):
        if not self.reason.strip():
            raise ValueError("审核必须说明理由")
        return self
