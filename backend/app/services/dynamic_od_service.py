"""Validate and atomically persist Phase 3.1 dynamic OD contract artifacts."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from backend.app.schemas.dynamic_od import (
    DynamicOdArtifactV1,
    DynamicOdBucketConservation,
    DynamicOdMovementTotal,
    DynamicOdRow,
    EvidenceRole,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json


class DynamicOdContractError(RuntimeError):
    """A dynamic OD artifact violates Phase 3.1 semantics or lineage."""


_WEEKDAY_ORDER = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
}


def load_dynamic_od_artifact(path: Path) -> DynamicOdArtifactV1:
    """Load and fully verify one immutable dynamic OD contract artifact."""

    artifact = DynamicOdArtifactV1.model_validate_json(path.read_bytes())
    validate_dynamic_od_artifact(artifact)
    if dynamic_od_content_digest(artifact.model_dump(mode="json")) != artifact.content_digest:
        raise DynamicOdContractError("dynamic OD content digest is invalid")
    return artifact


def validate_dynamic_od_artifact(artifact: DynamicOdArtifactV1) -> None:
    """Enforce identity, evidence, conservation, and temporal contracts."""

    evidence = {source.evidence_id: source for source in artifact.evidence_sources}
    if len(evidence) != len(artifact.evidence_sources):
        raise DynamicOdContractError("evidence source IDs must be unique")
    roles = {source.role for source in artifact.evidence_sources}
    required_roles = {
        EvidenceRole.HISTORICAL_OBSERVATION_CORPUS,
        EvidenceRole.HISTORICAL_FLOW_PROFILE,
        EvidenceRole.HISTORICAL_QUALITY_POLICY,
    }
    if not required_roles.issubset(roles):
        raise DynamicOdContractError(
            "dynamic OD lineage requires the frozen corpus, flow profiles, and quality policy"
        )

    rows = sorted(artifact.rows, key=_row_sort_key)
    if list(artifact.rows) != rows:
        raise DynamicOdContractError("dynamic OD rows must use deterministic ordering")
    identities = [row.row_identity for row in rows]
    if len(identities) != len(set(identities)):
        raise DynamicOdContractError("dynamic OD row identities must be unique")
    canonical_keys = [_row_key(row) for row in rows]
    if len(canonical_keys) != len(set(canonical_keys)):
        raise DynamicOdContractError("dynamic OD canonical row keys must be unique")

    for row in rows:
        expected_identity = dynamic_od_row_identity(row.model_dump(mode="json"))
        if row.row_identity != expected_identity:
            raise DynamicOdContractError("dynamic OD row identity is invalid")
        unknown = (set(row.evidence_ids) | set(row.adjacent_change_evidence_ids)) - set(
            evidence
        )
        if unknown:
            raise DynamicOdContractError(
                "dynamic OD row references unknown evidence: " + ", ".join(sorted(unknown))
            )
        if any(
            evidence[item].role != EvidenceRole.BUCKET_SPECIFIC_DETECTOR_EVIDENCE
            for item in row.adjacent_change_evidence_ids
        ):
            raise DynamicOdContractError(
                "adjacent changes require bucket-specific detector evidence"
            )

    declared_weekdays = tuple(
        sorted({row.weekday for row in rows}, key=lambda value: _WEEKDAY_ORDER[value.value])
    )
    declared_buckets = tuple(sorted({row.bucket_start_minute for row in rows}))
    if artifact.weekdays != declared_weekdays:
        raise DynamicOdContractError("artifact weekdays do not match row weekdays")
    if artifact.bucket_start_minutes != declared_buckets:
        raise DynamicOdContractError("artifact buckets do not match row buckets")
    if any(
        current - previous != 15
        for previous, current in pairwise(declared_buckets)
    ):
        raise DynamicOdContractError(
            "dynamic OD bucket sequence must be contiguous; missing buckets are not interpolated"
        )

    _validate_complete_matrices(rows, artifact.bucket_start_minutes)
    _validate_adjacent_changes(rows)
    expected_conservation = _conservation(rows)
    if artifact.conservation != expected_conservation:
        raise DynamicOdContractError("dynamic OD conservation audit does not match rows")


def finalize_dynamic_od_payload(payload: dict[str, Any]) -> DynamicOdArtifactV1:
    """Fill deterministic row, conservation, and artifact identities."""

    working = dict(payload)
    rows: list[dict[str, Any]] = []
    for source_row in working["rows"]:
        row = dict(source_row)
        row["row_identity"] = dynamic_od_row_identity(row)
        rows.append(row)
    parsed_rows = tuple(sorted(
        (DynamicOdRow.model_validate(row) for row in rows), key=_row_sort_key
    ))
    working["rows"] = [row.model_dump(mode="json") for row in parsed_rows]
    working["conservation"] = [
        item.model_dump(mode="json") for item in _conservation(parsed_rows)
    ]
    working["content_digest"] = "0" * 64
    normalized = DynamicOdArtifactV1.model_validate(working).model_dump(mode="json")
    normalized["content_digest"] = dynamic_od_content_digest(normalized)
    artifact = DynamicOdArtifactV1.model_validate(normalized)
    validate_dynamic_od_artifact(artifact)
    return artifact


def write_dynamic_od_artifact_atomic(
    output_path: Path, artifact: DynamicOdArtifactV1
) -> None:
    """Write one validated JSON artifact using atomic file replacement."""

    validate_dynamic_od_artifact(artifact)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pending = output_path.with_name(f".{output_path.name}.pending")
    pending.write_text(
        canonical_json(artifact.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    os.replace(pending, output_path)


def dynamic_od_row_identity(payload: dict[str, Any]) -> str:
    """Hash only the physical regional OD row identity, never a selected trip."""

    identity = {
        "weekday": payload["weekday"],
        "bucket_start_minute": payload["bucket_start_minute"],
        "origin_zone_id": payload["origin_zone_id"],
        "origin_zone_type": payload["origin_zone_type"],
        "destination_zone_id": payload["destination_zone_id"],
        "destination_zone_type": payload["destination_zone_type"],
        "movement_class": payload["movement_class"],
        "vehicle_class": payload["vehicle_class"],
    }
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def dynamic_od_content_digest(payload: dict[str, Any]) -> str:
    """Hash reproducible demand semantics while excluding wall-clock metadata."""

    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_digest"}
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _row_key(row: DynamicOdRow) -> tuple[object, ...]:
    return (
        row.weekday,
        row.bucket_start_minute,
        row.origin_zone_id,
        row.destination_zone_id,
        row.vehicle_class,
    )


def _row_sort_key(row: DynamicOdRow) -> tuple[object, ...]:
    return (
        _WEEKDAY_ORDER[row.weekday.value],
        row.bucket_start_minute,
        row.vehicle_class,
        row.origin_zone_id,
        row.destination_zone_id,
    )


def _matrix_key(row: DynamicOdRow) -> tuple[str, str, str]:
    return (row.origin_zone_id, row.destination_zone_id, row.vehicle_class)


def _validate_complete_matrices(
    rows: list[DynamicOdRow], bucket_starts: tuple[int, ...]
) -> None:
    by_weekday_bucket: dict[tuple[object, int], set[tuple[str, str, str]]] = defaultdict(set)
    for row in rows:
        by_weekday_bucket[(row.weekday, row.bucket_start_minute)].add(_matrix_key(row))
    for weekday in {row.weekday for row in rows}:
        matrices = [by_weekday_bucket[(weekday, bucket)] for bucket in bucket_starts]
        if any(matrix != matrices[0] for matrix in matrices[1:]):
            raise DynamicOdContractError(
                f"{weekday.value} OD matrices must retain the same explicit zone-pair rows"
            )


def _validate_adjacent_changes(rows: list[DynamicOdRow]) -> None:
    groups: dict[tuple[object, str, str, str], list[DynamicOdRow]] = defaultdict(list)
    for row in rows:
        groups[(row.weekday, *_matrix_key(row))].append(row)
    for group in groups.values():
        group.sort(key=lambda row: row.bucket_start_minute)
        for previous, current in pairwise(group):
            changed = abs(current.target_vehicle_trips - previous.target_vehicle_trips) > 1e-9
            if changed and not current.adjacent_change_evidence_ids:
                raise DynamicOdContractError(
                    "changed adjacent OD targets require bucket-specific detector evidence"
                )


def _conservation(rows: tuple[DynamicOdRow, ...] | list[DynamicOdRow]) -> tuple[DynamicOdBucketConservation, ...]:
    groups: dict[tuple[object, int], list[DynamicOdRow]] = defaultdict(list)
    for row in rows:
        groups[(row.weekday, row.bucket_start_minute)].append(row)
    audits: list[DynamicOdBucketConservation] = []
    for (weekday, bucket), group in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        movement_groups: dict[object, list[DynamicOdRow]] = defaultdict(list)
        for row in group:
            movement_groups[row.movement_class].append(row)
        movement_totals = tuple(
            DynamicOdMovementTotal(
                movement_class=movement,
                target_vehicle_trips=sum(row.target_vehicle_trips for row in movement_rows),
                target_flow_vph=sum(row.target_flow_vph for row in movement_rows),
            )
            for movement, movement_rows in sorted(
                movement_groups.items(), key=lambda item: item[0].value
            )
        )
        audits.append(
            DynamicOdBucketConservation(
                weekday=weekday,
                bucket_start_minute=bucket,
                row_count=len(group),
                target_vehicle_trips=sum(row.target_vehicle_trips for row in group),
                target_flow_vph=sum(row.target_flow_vph for row in group),
                movement_totals=movement_totals,
            )
        )
    return tuple(audits)
