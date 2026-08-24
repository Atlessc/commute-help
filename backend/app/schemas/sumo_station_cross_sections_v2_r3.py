"""Contracts for dependency-local station-cross-sections-v2-r3 repair artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileIdentity(StrictModel):
    relative_path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=_SHA256)
    byte_count: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)


class SyntheticBoundaryGeometryManifestV1(StrictModel):
    schema_version: Literal[1]
    artifact_type: Literal["commute_help_synthetic_boundary_member_projected_geometry"]
    artifact_status: Literal["complete_static_geometry"]
    generated_at: datetime
    producer_name: Literal["synthetic_boundary_member_geometry_materializer"]
    producer_version: Literal["station-cross-sections-v2-r3-prerequisite-v1"]
    source_crs: Literal["EPSG:4326"]
    projected_crs: Literal["EPSG:32610"]
    geometry_construction: Literal[
        "frozen_sumo_xy_to_sumolib_wgs84_to_pyproj_epsg32610_to_shapely_wkb"
    ]
    sumo_network_version: str = Field(min_length=1)
    sumo_network_sha256: str = Field(pattern=_SHA256)
    sumo_network_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_parquet_sha256: str = Field(pattern=_SHA256)
    comparison_validation_summary_sha256: str = Field(pattern=_SHA256)
    comparison_validation_provenance_sha256: str = Field(pattern=_SHA256)
    synthetic_member_ids: list[str] = Field(min_length=3, max_length=3)
    geometry_output: FileIdentity
    summary_output: FileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> SyntheticBoundaryGeometryManifestV1:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if len(set(self.synthetic_member_ids)) != 3:
            raise ValueError("synthetic member IDs must be unique")
        if self.geometry_output.row_count != 3:
            raise ValueError("synthetic geometry output must contain three rows")
        return self


class StationCrossSectionScientificGuardsV2R3(StrictModel):
    traffic_values_loaded: Literal[False]
    sumo_behavior_executed: Literal[False]
    development_loaded: Literal[False]
    blind_loaded: Literal[False]
    tolerance_or_rounding_introduced: Literal[False]
    unchanged_rows_reprojected: Literal[False]
    unchanged_member_geometry_reconstructed: Literal[False]
    projection_methodology_modified: Literal[False]
    station_eligibility_modified: Literal[False]
    comparison_relations_modified: Literal[False]
    sumo_network_modified: Literal[False]


class StationCrossSectionManifestV2R3(StrictModel):
    schema_version: Literal[2]
    artifact_revision: Literal["r3"]
    artifact_type: Literal["commute_help_station_sumo_cross_section_analysis"]
    artifact_status: Literal["complete_characterization_pending_acceptance_policy"]
    calibration_status: Literal["not_calibrated"]
    generated_at: datetime
    producer_name: Literal["portal_station_sumo_cross_section_projector_v2_r3"]
    producer_version: Literal["phase-2.2b-v2-r3"]
    projection_algorithm_version: Literal["relation-member-projection-v1"]
    repair_classification: Literal["D_MIXED_RUNTIME_AND_IMPLEMENTATION_EFFECT"]
    unchanged_row_contract: Literal["inherit_frozen_station_cross_sections_v1_row_exact"]
    changed_row_contract: Literal["recompute_only_comparison_v2_changed_app_relations"]
    geometry_contract: Literal[
        "frozen_edge_map_wkb_for_existing_members_plus_frozen_synthetic_boundary_geometry_v1"
    ]
    coordinate_representation_contract: Literal[
        "inherit_frozen_station_cross_sections_v1_binary64_coordinates"
    ]
    row_schema_policy: Literal[
        "inherited_rows_retain_v1_row_schema_version_changed_rows_use_v2_row_schema_version"
    ]
    parent_manifest_sha256: str = Field(pattern=_SHA256)
    parent_mapping_sha256: str = Field(pattern=_SHA256)
    parent_content_digest: str = Field(pattern=_SHA256)
    rejected_candidate_manifest_sha256: str = Field(pattern=_SHA256)
    rejected_candidate_mapping_sha256: str = Field(pattern=_SHA256)
    rejected_candidate_content_digest: str = Field(pattern=_SHA256)
    comparison_v1_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_v1_parquet_sha256: str = Field(pattern=_SHA256)
    comparison_v2_manifest_sha256: str = Field(pattern=_SHA256)
    comparison_v2_parquet_sha256: str = Field(pattern=_SHA256)
    comparison_v2_content_digest: str = Field(pattern=_SHA256)
    edge_map_sha256: str = Field(pattern=_SHA256)
    station_mapping_sha256: str = Field(pattern=_SHA256)
    synthetic_geometry_manifest_sha256: str = Field(pattern=_SHA256)
    synthetic_geometry_parquet_sha256: str = Field(pattern=_SHA256)
    synthetic_geometry_validation_summary_sha256: str = Field(pattern=_SHA256)
    synthetic_geometry_validation_provenance_sha256: str = Field(pattern=_SHA256)
    abc_diagnostic_summary_sha256: str = Field(pattern=_SHA256)
    abc_diagnostic_provenance_sha256: str = Field(pattern=_SHA256)
    unchanged_station_count: Literal[420]
    changed_station_count: Literal[6]
    changed_app_edge_ids: list[str] = Field(min_length=3, max_length=3)
    changed_station_ids: list[str] = Field(min_length=6, max_length=6)
    scientific_guards: StationCrossSectionScientificGuardsV2R3
    mapping_output: FileIdentity
    row_provenance_output: FileIdentity
    summary_output: FileIdentity
    report_output: FileIdentity
    e1_feasibility_output: FileIdentity
    row_content_sha256: str = Field(pattern=_SHA256)
    content_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> StationCrossSectionManifestV2R3:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if len(set(self.changed_app_edge_ids)) != 3:
            raise ValueError("changed APP edges must be unique")
        if len(set(self.changed_station_ids)) != 6:
            raise ValueError("changed station IDs must be unique")
        if self.mapping_output.row_count != 426:
            raise ValueError("station mapping output must contain 426 rows")
        if self.row_provenance_output.row_count != 426:
            raise ValueError("row provenance output must contain 426 rows")
        return self
