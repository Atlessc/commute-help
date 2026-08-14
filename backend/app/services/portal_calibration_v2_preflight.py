"""Measured storage and runtime preflight for a future PORTAL campaign run."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.schemas.calibration_v2_corpus import CalibrationObservationShardV2
from backend.app.services.calibration_v2_integrity_service import integrity_index_path
from backend.app.services.portal_calibration_v2_campaign import (
    plan_portal_calibration_campaign,
)
from backend.app.services.portal_calibration_v2_importer import PortalImportError

GIB = 1024**3
MINIMUM_SAFETY_MARGIN_BYTES = 20 * GIB
SAFETY_MARGIN_FRACTION = 0.25


@dataclass(frozen=True)
class MaterializationPreflight:
    representative_shards: int
    representative_raw_rows: int
    representative_accepted_observations: int
    canonical_observation_bytes: int
    physical_observation_bytes: int
    shard_manifest_bytes: int
    audit_evidence_bytes: int
    integrity_index_bytes: int
    canonical_bytes_per_accepted_observation: float
    physical_bytes_per_accepted_observation: float
    integrity_bytes_per_accepted_observation: float
    expected_raw_rows: int
    projected_accepted_observations: int
    projected_observation_bytes: int
    projected_manifest_and_audit_bytes: int
    projected_integrity_index_bytes: int
    largest_observed_atomic_staging_bytes: int
    projected_atomic_staging_bytes: int
    projected_total_bytes: int
    projected_peak_bytes: int
    required_safety_margin_bytes: int
    required_free_bytes: int
    current_free_bytes: int
    safe_to_materialize: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)


def measure_materialization_preflight(
    campaign_directory: Path,
    representative_output_root: Path,
    target_output_root: Path,
) -> MaterializationPreflight:
    """Project one campaign from real materialized shards, without importing more rows."""
    plan = plan_portal_calibration_campaign(campaign_directory, target_output_root)
    manifests = [
        CalibrationObservationShardV2.model_validate_json(path.read_bytes())
        for path in sorted((representative_output_root / "shards").glob("*/shard-manifest.json"))
    ]
    if not manifests:
        raise PortalImportError("Preflight requires at least one representative materialized shard")
    raw_rows = sum(shard.audit.rows_encountered for shard in manifests)
    accepted = sum(shard.audit.accepted_count for shard in manifests)
    if not raw_rows or not accepted:
        raise PortalImportError("Preflight requires representative accepted observations")
    canonical_bytes = sum(shard.observation_stream.canonical_byte_count for shard in manifests)
    physical_bytes = sum(shard.observation_stream.physical_byte_count for shard in manifests)
    manifest_paths = sorted((representative_output_root / "shards").glob("*/shard-manifest.json"))
    manifest_bytes = sum(path.stat().st_size for path in manifest_paths)
    audit_bytes = sum(
        len(json.dumps(json.loads(path.read_text())["audit"], separators=(",", ":"), sort_keys=True).encode())
        for path in manifest_paths
    )
    index = integrity_index_path(representative_output_root)
    index_bytes = sum(
        path.stat().st_size
        for path in (
            index,
            index.with_name(index.name + "-wal"),
            index.with_name(index.name + "-shm"),
        )
        if path.exists()
    )
    expected_raw_rows = sum(entry.raw_row_count for entry in plan.partitions)
    expected_partitions = plan.expected_partition_count
    projected_accepted = round(expected_raw_rows * accepted / raw_rows)
    physical_per_observation = physical_bytes / accepted
    canonical_per_observation = canonical_bytes / accepted
    integrity_per_observation = index_bytes / accepted
    projected_observations = round(projected_accepted * physical_per_observation)
    projected_integrity = round(projected_accepted * integrity_per_observation)
    projected_manifest = round(expected_partitions * manifest_bytes / len(manifests))
    largest_observed = max(shard.observation_stream.physical_byte_count for shard in manifests)
    max_raw_rows = max(entry.raw_row_count for entry in plan.partitions)
    projected_staging = round(max_raw_rows * physical_bytes / raw_rows)
    projected_total = projected_observations + projected_integrity + projected_manifest
    peak = projected_total + max(projected_staging, largest_observed)
    safety_margin = max(round(projected_total * SAFETY_MARGIN_FRACTION), MINIMUM_SAFETY_MARGIN_BYTES)
    free = shutil.disk_usage(_existing_disk_ancestor(target_output_root)).free
    required_free = peak + safety_margin
    return MaterializationPreflight(
        representative_shards=len(manifests),
        representative_raw_rows=raw_rows,
        representative_accepted_observations=accepted,
        canonical_observation_bytes=canonical_bytes,
        physical_observation_bytes=physical_bytes,
        shard_manifest_bytes=manifest_bytes,
        audit_evidence_bytes=audit_bytes,
        integrity_index_bytes=index_bytes,
        canonical_bytes_per_accepted_observation=canonical_per_observation,
        physical_bytes_per_accepted_observation=physical_per_observation,
        integrity_bytes_per_accepted_observation=integrity_per_observation,
        expected_raw_rows=expected_raw_rows,
        projected_accepted_observations=projected_accepted,
        projected_observation_bytes=projected_observations,
        projected_manifest_and_audit_bytes=projected_manifest,
        projected_integrity_index_bytes=projected_integrity,
        largest_observed_atomic_staging_bytes=largest_observed,
        projected_atomic_staging_bytes=projected_staging,
        projected_total_bytes=projected_total,
        projected_peak_bytes=peak,
        required_safety_margin_bytes=safety_margin,
        required_free_bytes=required_free,
        current_free_bytes=free,
        safe_to_materialize=free >= required_free,
    )


def _existing_disk_ancestor(path: Path) -> Path:
    """Preflight must inspect a future destination without creating it."""
    candidate = path.resolve()
    while not candidate.exists():
        if candidate.parent == candidate:
            raise PortalImportError(f"Cannot locate a filesystem ancestor for {path}")
        candidate = candidate.parent
    return candidate
