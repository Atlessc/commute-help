"""Curated, graph-versioned closure plan schemas."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from backend.app.schemas.scenarios import SavedClosureSection


class ClosurePresetSource(BaseModel):
    """Public source used to define and review a preset."""

    title: str
    url: HttpUrl
    checked_on: date


class ClosurePresetTiming(BaseModel):
    """Published timing without inventing an unconfirmed reopening time."""

    mainline_starts_at: datetime
    ramp_closures_may_start_at: datetime | None = None
    duration_note: str
    exact_end_confirmed: bool = False

    @model_validator(mode="after")
    def validate_timing(self) -> "ClosurePresetTiming":
        if self.mainline_starts_at.tzinfo is None:
            raise ValueError("Preset start time must include a timezone.")
        if (
            self.ramp_closures_may_start_at is not None
            and self.ramp_closures_may_start_at.tzinfo is None
        ):
            raise ValueError("Preset ramp start time must include a timezone.")
        return self


class ClosurePresetSection(SavedClosureSection):
    """One human-labeled, directed restriction within a preset."""

    label: str


class ClosurePresetDefinition(BaseModel):
    """Static source-of-truth definition loaded from the local catalog."""

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    summary: str
    graph_version: str
    timing: ClosurePresetTiming
    notices: list[str] = Field(default_factory=list)
    sources: list[ClosurePresetSource] = Field(min_length=1)
    sections: list[ClosurePresetSection] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph_versions(self) -> "ClosurePresetDefinition":
        if any(
            section.selection.graph_version != self.graph_version
            for section in self.sections
        ):
            raise ValueError("Every preset section must use the preset graph version.")
        return self


class ClosurePresetCatalog(BaseModel):
    schema_version: Literal[1] = 1
    presets: list[ClosurePresetDefinition]


class ClosurePresetRecord(ClosurePresetDefinition):
    graph_status: Literal["current", "review_required"]
    warnings: list[str] = Field(default_factory=list)


class ClosurePresetListResponse(BaseModel):
    presets: list[ClosurePresetRecord]
