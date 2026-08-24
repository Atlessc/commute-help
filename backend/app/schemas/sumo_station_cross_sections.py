"""Typed Phase 2.2b station-to-SUMO cross-section analysis contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STATION_CROSS_SECTION_SCHEMA_VERSION = 1
STATION_CROSS_SECTION_PRODUCER_VERSION = "phase-2.2b-v1"
STATION_CROSS_SECTION_ALGORITHM_VERSION = "relation-member-projection-v1"
STATION_CROSS_SECTION_SCHEMA_VERSION_V2 = 2
STATION_CROSS_SECTION_PRODUCER_VERSION_V2 = "phase-2.2b-v2"
STATION_CROSS_SECTION_GEOMETRY_SOURCE_V2 = (
    "frozen_sumo_network_edge_shape_xy_to_wgs84_to_epsg32610"
)
STATION_CROSS_SECTION_REPAIR_REASON_V2 = (
    "comparison_relations_v2_synthetic_boundary_member_dependency"
)
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


class StationCrossSectionScientificGuardsV2(StrictCrossSectionModel):
    traffic_values_loaded: Literal[False]
    sumo_behavior_executed: Literal[False]
    development_loaded: Literal[False]
    blind_loaded: Literal[False]
    projection_methodology_modified: Literal[False]
    station_eligibility_modified: Literal[False]


class StationCrossSectionManifestV2(StrictCrossSectionModel):
    schema_version: Literal[STATION_CROSS_SECTION_SCHEMA_VERSION_V2]
    artifact_type: Literal["commute_help_station_sumo_cross_section_analysis"]
    artifact_status: Literal["complete_characterization_pending_acceptance_policy"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["portal_station_sumo_cross_section_projector_v2"]
    producer_version: Literal[STATION_CROSS_SECTION_PRODUCER_VERSION_V2]
    projection_algorithm_version: Literal[STATION_CROSS_SECTION_ALGORITHM_VERSION]
    geometry_source: Literal[STATION_CROSS_SECTION_GEOMETRY_SOURCE_V2]
    projected_crs: Literal["EPSG:32610"]
    source_crs: Literal["EPSG:4326"]
    coordinate_representation_contract: Literal[
        "inherit_frozen_station_cross_sections_v1_binary64_coordinates"
    ]
    coordinate_provenance_classification: Literal[
        "C_RUNTIME_LIBRARY_VERSION_DEPENDENT_REPRESENTATION"
    ]
    source_coordinate_identity_guard: Literal[
        "frozen_station_mapping_sha256_plus_parent_v1_station_domain_binding_direction_and_finite_decimal_tokens"
    ]
    repair_reason: Literal[STATION_CROSS_SECTION_REPAIR_REASON_V2]
    e1_feasibility_version: Literal[E1_FEASIBILITY_VERSION]
    graph_version: str = Field(min_length=1, max_length=160)
    sumo_network_version: str = Field(min_length=1, max_length=160)
    parent_manifest_sha256: str = Field(pattern=_SHA256)
    parent_content_digest: str = Field(pattern=_SHA256)
    parent_mapping_sha256: str = Field(pattern=_SHA256)
    parent_row_content_sha256: str = Field(pattern=_SHA256)
    station_mapping_sha256: str = Field(pattern=_SHA256)
    detector_metadata_sha256: str = Field(pattern=_SHA256)
    edge_map_sha256: str = Field(pattern=_SHA256)
    graph_edges_sha256: str = Field(pattern=_SHA256)
    graph_manifest_sha256: str = Field(pattern=_SHA256)
    sumo_network_sha256: str = Field(pattern=_SHA256)
    sumo_network_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_relation_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_relation_content_digest: str = Field(pattern=_SHA256)
    comparison_relation_parquet_sha256: str = Field(pattern=_SHA256)
    comparison_validation_summary_sha256: str = Field(pattern=_SHA256)
    comparison_validation_provenance_sha256: str = Field(pattern=_SHA256)
    historical_profile_content_digest: str = Field(pattern=_SHA256)
    quality_policy_content_digest: str = Field(pattern=_SHA256)
    parent_e1_feasibility_sha256: str = Field(pattern=_SHA256)
    changed_app_edge_ids: list[str] = Field(min_length=1)
    dependency_station_ids: list[str] = Field(min_length=1)
    scientific_guards: StationCrossSectionScientificGuardsV2
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
    def validate_manifest(self) -> StationCrossSectionManifestV2:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.mapping_output.row_count is None:
            raise ValueError("cross-section mapping output requires a row count")
        if len(self.changed_app_edge_ids) != len(set(self.changed_app_edge_ids)):
            raise ValueError("changed APP edge IDs must be unique")
        if len(self.dependency_station_ids) != len(set(self.dependency_station_ids)):
            raise ValueError("dependency station IDs must be unique")
        return self
