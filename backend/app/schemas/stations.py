from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RegisteredStation(BaseModel):
    id: UUID
    code: str
    name: str
    station_type: Literal["WHOLE_MACHINE", "MODULE", "SHARED"]
    enabled: bool
    lab_code: str
    lab_name: str
    lab_enabled: bool
    site_code: str
    site_name: str
    site_location: str
    site_enabled: bool
    available: bool


class RegisteredStationList(BaseModel):
    items: list[RegisteredStation]
    next_cursor: UUID | None = None
