"""Versioned shared scenario API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from backend.app.schemas.routing import (
    ClosureRoadSelectionResponse,
    SnappedLocation,
)


class ScenarioRestriction(BaseModel):
    """Restriction settings saved independently from selected directions."""

    type: Literal["full", "lane", "speed"] = "full"
    remaining_lanes: float | None = Field(default=None, gt=0)
    speed_limit_kph: float | None = Field(default=None, ge=5, le=130)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "ScenarioRestriction":
        if self.type == "lane" and self.remaining_lanes is None:
            raise ValueError("Lane restrictions require remaining_lanes.")
        if self.type == "speed" and self.speed_limit_kph is None:
            raise ValueError("Speed restrictions require speed_limit_kph.")
        if self.type != "lane" and self.remaining_lanes is not None:
            raise ValueError("remaining_lanes is only valid for lane restrictions.")
        if self.type != "speed" and self.speed_limit_kph is not None:
            raise ValueError("speed_limit_kph is only valid for speed restrictions.")
        if (self.starts_at is None) != (self.ends_at is None):
            raise ValueError("Schedules require both starts_at and ends_at.")
        if self.starts_at is not None and self.ends_at is not None:
            if not _aware(self.starts_at) or not _aware(self.ends_at):
                raise ValueError("Schedule timestamps must include a timezone.")
            if self.ends_at <= self.starts_at:
                raise ValueError("Restriction end time must be after its start time.")
        return self


class SavedClosureSection(BaseModel):
    """Road selection, chosen directions, and its restriction."""

    selection: ClosureRoadSelectionResponse
    selected_edge_ids: list[str] = Field(min_length=1)
    restriction: ScenarioRestriction

    @model_validator(mode="after")
    def validate_selected_edges(self) -> "SavedClosureSection":
        available = {
            direction.edge.edge_id for direction in self.selection.directions
        }
        if not set(self.selected_edge_ids).issubset(available):
            raise ValueError("A selected closure direction is missing from its road snapshot.")
        return self


class ScenarioMapState(BaseModel):
    center_lng: float = Field(ge=-180, le=180)
    center_lat: float = Field(ge=-90, le=90)
    zoom: float = Field(ge=0, le=24)


class ScenarioReliabilitySettings(BaseModel):
    """Restorable arrival-risk inputs; calculated samples are not persisted here."""

    planning_mode: Literal["arrive_by", "depart_at"] = "arrive_by"
    arrival_deadline: datetime | None = None
    buffer_minutes: int = Field(default=8, ge=0, le=180)
    confidence_target: float = Field(default=0.9, ge=0.5, le=0.99)
    sample_count: int = Field(default=1000, ge=100, le=10000)
    profile_id: UUID | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> "ScenarioReliabilitySettings":
        if self.planning_mode == "arrive_by":
            if self.arrival_deadline is None or not _aware(self.arrival_deadline):
                raise ValueError("Arrive-by scenarios require a timezone-aware deadline.")
        return self


class ScenarioDiversionSettings(BaseModel):
    """Restorable inputs for a synthetic network-diversion run."""

    demand_vph: int = Field(default=600, ge=50, le=5000)
    demand_pair_count: int = Field(default=10, ge=5, le=100)
    iterations: int = Field(default=3, ge=2, le=10)
    dispersion_radius_m: int = Field(default=1000, ge=0, le=10000)


class ScenarioContent(BaseModel):
    """The complete restorable planning state."""

    schema_version: Literal[1] = 1
    graph_version: str
    origin: SnappedLocation
    destination: SnappedLocation
    departure_time: datetime
    closures: list[SavedClosureSection] = Field(default_factory=list)
    selected_route_id: str | None = None
    map_state: ScenarioMapState | None = None
    reliability_conditions: ScenarioReliabilitySettings | None = None
    diversion_conditions: ScenarioDiversionSettings | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "ScenarioContent":
        if not _aware(self.departure_time):
            raise ValueError("departure_time must include a timezone.")
        if any(
            section.selection.graph_version != self.graph_version
            for section in self.closures
        ):
            raise ValueError("All saved closures must use the scenario graph version.")
        if (
            self.reliability_conditions is not None
            and self.reliability_conditions.planning_mode == "arrive_by"
            and self.reliability_conditions.arrival_deadline is not None
            and self.reliability_conditions.arrival_deadline <= self.departure_time
        ):
            raise ValueError("Arrival deadline must be after scenario departure time.")
        return self
class ScenarioCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: ScenarioContent
    editor_name: str | None = Field(default=None, max_length=80)


class ScenarioUpdateRequest(ScenarioCreateRequest):
    expected_revision: int = Field(ge=1)


class ScenarioListItem(BaseModel):
    id: UUID
    name: str
    revision: int
    graph_version: str
    archived: bool
    created_at: datetime
    updated_at: datetime


class ScenarioRecord(ScenarioListItem):
    schema_version: Literal[1] = 1
    content: ScenarioContent
    graph_status: Literal["current", "rematched", "review_required"]
    warnings: list[str] = Field(default_factory=list)


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioListItem]


class ScenarioDuplicateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    editor_name: str | None = Field(default=None, max_length=80)


class ScenarioImportRequest(BaseModel):
    scenario: ScenarioRecord
    name: str | None = Field(default=None, min_length=1, max_length=120)
    editor_name: str | None = Field(default=None, max_length=80)


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
