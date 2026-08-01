"""Bounded PORTAL highway acquisition contracts."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.traffic import TrafficImportResponse


class PortalHighway(BaseModel):
    id: int
    name: str
    direction: str
    station_count: int = Field(ge=1)


class PortalHighwaysResponse(BaseModel):
    highways: list[PortalHighway]


class PortalAcquireRequest(BaseModel):
    start_date: date
    end_date: date
    highway_ids: list[int] = Field(min_length=1, max_length=8)
    resolution: Literal["00:15:00", "01:00:00"] = "00:15:00"
    days_of_week: list[int] = Field(default_factory=lambda: [2, 3, 4, 5, 6])

    @model_validator(mode="after")
    def validate_window(self) -> "PortalAcquireRequest":
        if self.end_date < self.start_date:
            raise ValueError("PORTAL end date must be on or after the start date.")
        if (self.end_date - self.start_date).days > 61:
            raise ValueError("PORTAL acquisitions are limited to 62 days per run.")
        if self.end_date > date.today():
            raise ValueError("PORTAL acquisitions cannot request future observations.")
        if len(set(self.highway_ids)) != len(self.highway_ids):
            raise ValueError("Choose each PORTAL highway only once.")
        if not self.days_of_week or any(day < 1 or day > 7 for day in self.days_of_week):
            raise ValueError("PORTAL weekdays must use values 1 through 7.")
        return self


class PortalAcquireResponse(BaseModel):
    requested_highways: list[PortalHighway]
    downloaded_observation_count: int = Field(ge=0)
    normalized_station_count: int = Field(ge=0)
    import_result: TrafficImportResponse
