"""Typed Phase 2.2b station-to-SUMO cross-section analysis contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STATION_CROSS_SECTION_SCHEMA_VERSION = 1
STATION_CROSS_SECTION_PRODUCER_VERSION = "phase-2.2b-v1"
STATION_CROSS_SECTION_ALGORITHM_VERSION = "relation-member-projection-v1"
E1_FEASIBILITY_VERSION = "sumo-e1-meso-checkpoint-fragments-v1"

_SHA256 = r"^[0-9a-f]{64}$"


class StrictCrossSectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CrossSectionFileIdentity(StrictCrossSectionModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class StationCrossSectionManifestV1(StrictCrossSectionModel):
    schema_version: Literal[STATION_CROSS_SECTION_SCHEMA_VERSION]
    artifact_type: Literal["commute_help_station_sumo_cross_section_analysis"]
    artifact_status: Literal["complete_characterization_pending_acceptance_policy"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["portal_station_sumo_cross_section_projector"]
    producer_version: Literal[STATION_CROSS_SECTION_PRODUCER_VERSION]
    projection_algorithm_version: Literal[STATION_CROSS_SECTION_ALGORITHM_VERSION]
    e1_feasibility_version: Literal[E1_FEASIBILITY_VERSION]
    graph_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    station_mapping_sha256: str = Field(pattern=_SHA256)
    detector_metadata_sha256: str = Field(pattern=_SHA256)
    edge_map_sha256: str = Field(pattern=_SHA256)
    comparison_relation_content_digest: str = Field(pattern=_SHA256)
    comparison_relation_parquet_sha256: str = Field(pattern=_SHA256)
    historical_profile_content_digest: str = Field(pattern=_SHA256)
    quality_policy_content_digest: str = Field(pattern=_SHA256)
    acceptance_policy: Literal[
        "none_characterize_distance_and_boundary_distributions_before_threshold_selection"
    ]
    mapping_output: CrossSectionFileIdentity
    summary_output: CrossSectionFileIdentity
    report_output: CrossSectionFileIdentity
    e1_feasibility_output: CrossSectionFileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> StationCrossSectionManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.mapping_output.row_count is None:
            raise ValueError("cross-section mapping output requires a row count")
        return self
