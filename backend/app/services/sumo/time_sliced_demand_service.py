"""Phase 3.2 deterministic bucket-contained SUMO trip-demand generation."""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import random
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.schemas.dynamic_od import DynamicOdArtifactV1, DynamicOdRow
from backend.app.schemas.time_sliced_demand import (
    TIME_SLICED_DEMAND_ALGORITHM,
    TIME_SLICED_DEMAND_FILE,
    TIME_SLICED_DEMAND_PRODUCER_NAME,
    TIME_SLICED_DEMAND_PRODUCER_VERSION,
    TIME_SLICED_DEMAND_SCHEMA_VERSION,
    TimeSlicedBucketAudit,
    TimeSlicedClassConservation,
    TimeSlicedDemandManifestV1,
    TimeSlicedDemandOutputIdentity,
    TimeSlicedDepartureAudit,
    TimeSlicedRoundingAudit,
    TimeSlicedRowRoundingAudit,
    TimeSlicedSeedLineage,
)
from backend.app.services.dynamic_od_service import load_dynamic_od_artifact
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.demand_service import VTYPE_ATTRIBUTES

TIME_SLICED_DEMAND_MANIFEST = "time-sliced-demand-manifest.json"


class TimeSlicedDemandError(RuntimeError):
    """Phase 3.1 demand cannot be safely materialized as Phase 3.2 trips."""


def build_time_sliced_demand(
    *,
    source_artifact_path: Path,
    output_directory: Path,
    weekday: str | None = None,
    seed_override: int | None = None,
    generated_at: datetime | None = None,
) -> TimeSlicedDemandManifestV1:
    """Build one weekday's deterministic unrouted SUMO zone-pair trip file."""

    source = load_dynamic_od_artifact(source_artifact_path)
    selected_weekday = _select_weekday(source, weekday)
    rows = tuple(row for row in source.rows if row.weekday.value == selected_weekday)
    if not rows:
        raise TimeSlicedDemandError(
            f"dynamic OD artifact contains no rows for weekday {selected_weekday}"
        )
    effective_seed = source.seed_policy.base_seed if seed_override is None else seed_override
    if not 0 <= effective_seed <= 2_147_483_647:
        raise TimeSlicedDemandError("effective seed is outside the supported range")

    if output_directory.exists():
        raise TimeSlicedDemandError("time-sliced demand output already exists")
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        events, bucket_audits = _generate_events(
            rows=rows,
            demand_version=source.demand_version,
            effective_seed=effective_seed,
        )
        route_bytes = _render_sumo_demand(events)
        compressed_path = pending / TIME_SLICED_DEMAND_FILE
        _write_deterministic_gzip(compressed_path, route_bytes)
        output_identity = TimeSlicedDemandOutputIdentity(
            relative_path=TIME_SLICED_DEMAND_FILE,
            sha256=_sha256(compressed_path),
            uncompressed_sha256=hashlib.sha256(route_bytes).hexdigest(),
            byte_count=compressed_path.stat().st_size,
        )
        manifest_payload = _manifest_payload(
            source=source,
            weekday=selected_weekday,
            effective_seed=effective_seed,
            seed_override_applied=seed_override is not None,
            bucket_audits=bucket_audits,
            events=events,
            output_identity=output_identity,
            generated_at=generated_at or datetime.now(UTC),
        )
        manifest_payload["content_digest"] = time_sliced_demand_content_digest(
            manifest_payload
        )
        manifest = TimeSlicedDemandManifestV1.model_validate(manifest_payload)
        _validate_materialized_output(pending, manifest)
        (pending / TIME_SLICED_DEMAND_MANIFEST).write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        os.replace(pending, output_directory)
        return manifest
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def load_time_sliced_demand_artifact(
    output_directory: Path,
) -> TimeSlicedDemandManifestV1:
    """Load and independently verify a Phase 3.2 artifact directory."""

    manifest = TimeSlicedDemandManifestV1.model_validate_json(
        (output_directory / TIME_SLICED_DEMAND_MANIFEST).read_bytes()
    )
    if (
        time_sliced_demand_content_digest(manifest.model_dump(mode="json"))
        != manifest.content_digest
    ):
        raise TimeSlicedDemandError("time-sliced demand content digest is invalid")
    _validate_materialized_output(output_directory, manifest)
    return manifest


def balanced_stochastic_round(
    rows: tuple[DynamicOdRow, ...], *, seed: int
) -> tuple[tuple[int, ...], tuple[float, ...], float]:
    """Round one bucket jointly, bounding every row and aggregate to <1 trip."""

    exact = tuple(float(row.target_vehicle_trips) for row in rows)
    floors = [math.floor(value) for value in exact]
    exact_total = sum(exact)
    rng = random.Random(seed)
    target_total = math.floor(exact_total)
    if rng.random() < exact_total - target_total:
        target_total += 1
    remaining = target_total - sum(floors)
    fractional = [
        (index, value - math.floor(value))
        for index, value in enumerate(exact)
        if value - math.floor(value) > 0
    ]
    ranked = sorted(
        fractional,
        key=lambda item: (
            -math.log(max(rng.random(), 1e-15)) / item[1],
            rows[item[0]].row_identity,
        ),
    )
    if remaining < 0 or remaining > len(ranked):
        raise TimeSlicedDemandError("balanced rounding could not allocate bucket target")
    counts = floors
    for index, _ in ranked[:remaining]:
        counts[index] += 1
    errors = tuple(float(count) - value for count, value in zip(counts, exact, strict=True))
    aggregate_error = float(sum(counts) - exact_total)
    if any(abs(error) >= 1.0 + 1e-12 for error in errors):
        raise TimeSlicedDemandError("balanced rounding exceeded per-row error bound")
    if abs(aggregate_error) >= 1.0 + 1e-12:
        raise TimeSlicedDemandError("balanced rounding exceeded bucket error bound")
    return tuple(counts), errors, aggregate_error


def time_sliced_demand_content_digest(payload: dict[str, Any]) -> str:
    """Hash physical demand semantics while excluding wall-clock metadata."""

    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_digest"}
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _select_weekday(source: DynamicOdArtifactV1, requested: str | None) -> str:
    available = tuple(value.value for value in source.weekdays)
    if requested is None:
        if len(available) != 1:
            raise TimeSlicedDemandError(
                "weekday is required when a dynamic OD artifact contains multiple weekdays"
            )
        return available[0]
    if requested not in available:
        raise TimeSlicedDemandError(
            f"requested weekday {requested} is unavailable; substitution is prohibited"
        )
    return requested


def _derive_seed(
    effective_seed: int,
    demand_version: str,
    weekday: str,
    bucket_start_minute: int,
    scope: str,
) -> int:
    payload = canonical_json(
        [effective_seed, demand_version, weekday, bucket_start_minute, scope]
    )
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def _generate_events(
    *, rows: tuple[DynamicOdRow, ...], demand_version: str, effective_seed: int
) -> tuple[list[dict[str, Any]], tuple[TimeSlicedBucketAudit, ...]]:
    grouped: dict[int, list[DynamicOdRow]] = defaultdict(list)
    for row in rows:
        grouped[row.bucket_start_minute].append(row)
    events: list[dict[str, Any]] = []
    audits: list[TimeSlicedBucketAudit] = []
    for bucket, unsorted_rows in sorted(grouped.items()):
        bucket_rows = tuple(
            sorted(
                unsorted_rows,
                key=lambda row: (
                    row.vehicle_class,
                    row.origin_zone_id,
                    row.destination_zone_id,
                ),
            )
        )
        weekday = bucket_rows[0].weekday.value
        rounding_seed = _derive_seed(
            effective_seed, demand_version, weekday, bucket, "balanced-rounding"
        )
        counts, errors, aggregate_error = balanced_stochastic_round(
            bucket_rows, seed=rounding_seed
        )
        bucket_start_seconds = bucket * 60
        bucket_end_seconds = bucket_start_seconds + 900
        row_audits: list[TimeSlicedRowRoundingAudit] = []
        bucket_events: list[dict[str, Any]] = []
        for row, count, error in zip(bucket_rows, counts, errors, strict=True):
            placement_seed = _derive_seed(
                effective_seed,
                demand_version,
                weekday,
                bucket,
                f"departure-placement:{row.vehicle_class}:{row.row_identity}",
            )
            placement_rng = random.Random(placement_seed)
            if row.vehicle_class not in VTYPE_ATTRIBUTES:
                raise TimeSlicedDemandError(
                    f"vehicle class {row.vehicle_class!r} has no reviewed SUMO type"
                )
            for vehicle_index in range(count):
                depart = (
                    bucket_start_seconds
                    + placement_rng.randrange(900_000_000) / 1_000_000
                )
                vehicle_id = (
                    f"dyn-{weekday}-{bucket:04d}-{row.row_identity[:16]}-"
                    f"{vehicle_index:06d}"
                )
                bucket_events.append(
                    {
                        "id": vehicle_id,
                        "depart": depart,
                        "weekday": weekday,
                        "bucket_start_minute": bucket,
                        "source_row_identity": row.row_identity,
                        "from_taz": row.origin_zone_id,
                        "to_taz": row.destination_zone_id,
                        "movement_class": row.movement_class.value,
                        "vehicle_class": row.vehicle_class,
                    }
                )
            row_audits.append(
                TimeSlicedRowRoundingAudit(
                    source_row_identity=row.row_identity,
                    origin_zone_id=row.origin_zone_id,
                    destination_zone_id=row.destination_zone_id,
                    movement_class=row.movement_class.value,
                    vehicle_class=row.vehicle_class,
                    target_vehicle_trips=row.target_vehicle_trips,
                    generated_vehicle_count=count,
                    represented_vehicle_trips=float(count),
                    rounding_error_vehicle_trips=error,
                    allowed_absolute_error_vehicle_trips=1.0,
                    error_within_bound=abs(error) < 1.0 + 1e-12,
                )
            )
        bucket_events.sort(key=lambda event: (event["depart"], event["id"]))
        events.extend(bucket_events)
        leakage = sum(
            not (bucket_start_seconds <= event["depart"] < bucket_end_seconds)
            for event in bucket_events
        )
        movement_audit = _class_conservation(
            bucket_rows, row_audits, field="movement_class"
        )
        vehicle_audit = _class_conservation(
            bucket_rows, row_audits, field="vehicle_class"
        )
        audits.append(
            TimeSlicedBucketAudit(
                weekday=weekday,
                bucket_start_minute=bucket,
                interval_start_seconds=bucket_start_seconds,
                interval_end_seconds=bucket_end_seconds,
                source_row_count=len(bucket_rows),
                target_vehicle_trips=sum(row.target_vehicle_trips for row in bucket_rows),
                generated_vehicle_count=len(bucket_events),
                represented_vehicle_trips=float(len(bucket_events)),
                rounding_error_vehicle_trips=aggregate_error,
                allowed_absolute_aggregate_error_vehicle_trips=1.0,
                maximum_absolute_row_error_vehicle_trips=max(
                    abs(error) for error in errors
                ),
                rounding_gate_passed=(
                    abs(aggregate_error) < 1.0 + 1e-12
                    and all(abs(error) < 1.0 + 1e-12 for error in errors)
                ),
                departure_leakage_count=leakage,
                minimum_departure_seconds=(
                    min(event["depart"] for event in bucket_events)
                    if bucket_events
                    else None
                ),
                maximum_departure_seconds=(
                    max(event["depart"] for event in bucket_events)
                    if bucket_events
                    else None
                ),
                movement_class_conservation=movement_audit,
                vehicle_class_conservation=vehicle_audit,
                rows=tuple(row_audits),
            )
        )
    events.sort(key=lambda event: (event["depart"], event["id"]))
    return events, tuple(audits)


def _class_conservation(
    rows: tuple[DynamicOdRow, ...],
    audits: list[TimeSlicedRowRoundingAudit],
    *,
    field: str,
) -> tuple[TimeSlicedClassConservation, ...]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        value = getattr(row, field)
        grouped[value.value if hasattr(value, "value") else str(value)].append(index)
    return tuple(
        TimeSlicedClassConservation(
            class_name=class_name,
            target_vehicle_trips=sum(rows[index].target_vehicle_trips for index in indexes),
            represented_vehicle_trips=sum(
                audits[index].represented_vehicle_trips for index in indexes
            ),
            rounding_error_vehicle_trips=sum(
                audits[index].rounding_error_vehicle_trips for index in indexes
            ),
        )
        for class_name, indexes in sorted(grouped.items())
    )


def _render_sumo_demand(events: list[dict[str, Any]]) -> bytes:
    root = ET.Element("routes")
    for type_id in sorted({str(event["vehicle_class"]) for event in events}):
        ET.SubElement(root, "vType", {"id": type_id, **VTYPE_ATTRIBUTES[type_id]})
    for event in events:
        ET.SubElement(
            root,
            "trip",
            {
                "id": str(event["id"]),
                "type": str(event["vehicle_class"]),
                "depart": f"{float(event['depart']):.6f}",
                "fromTaz": str(event["from_taz"]),
                "toTaz": str(event["to_taz"]),
                "departLane": "best",
                "departPos": "random_free",
                "arrivalPos": "max",
            },
        )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _write_deterministic_gzip(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as raw, gzip.GzipFile(
        fileobj=raw, mode="wb", filename="", mtime=0
    ) as compressed:
        compressed.write(payload)
    os.replace(temporary, path)


def _manifest_payload(
    *,
    source: DynamicOdArtifactV1,
    weekday: str,
    effective_seed: int,
    seed_override_applied: bool,
    bucket_audits: tuple[TimeSlicedBucketAudit, ...],
    events: list[dict[str, Any]],
    output_identity: TimeSlicedDemandOutputIdentity,
    generated_at: datetime,
) -> dict[str, Any]:
    target = sum(audit.target_vehicle_trips for audit in bucket_audits)
    represented = float(len(events))
    row_errors = [
        abs(row.rounding_error_vehicle_trips)
        for bucket in bucket_audits
        for row in bucket.rows
    ]
    vehicle_ids = [str(event["id"]) for event in events]
    departure_at_end = sum(
        event["depart"] == event["bucket_start_minute"] * 60 + 900
        for event in events
    )
    leakage = sum(audit.departure_leakage_count for audit in bucket_audits)
    return {
        "schema_version": TIME_SLICED_DEMAND_SCHEMA_VERSION,
        "artifact_type": "commute_help_time_sliced_sumo_demand",
        "artifact_status": "candidate_unvalidated",
        "producer_name": TIME_SLICED_DEMAND_PRODUCER_NAME,
        "producer_version": TIME_SLICED_DEMAND_PRODUCER_VERSION,
        "algorithm": TIME_SLICED_DEMAND_ALGORITHM,
        "generated_at": generated_at.isoformat(),
        "source_dynamic_od_content_digest": source.content_digest,
        "source_demand_version": source.demand_version,
        "source_evidence_content_digests": sorted(
            evidence.content_digest for evidence in source.evidence_sources
        ),
        "demand_version": f"{source.demand_version}:phase-3.2:{weekday}",
        "weekday": weekday,
        "timezone": source.timezone,
        "interval_seconds": source.interval_seconds,
        "bucket_end_semantics": source.bucket_end_semantics,
        "bucket_start_minutes": [audit.bucket_start_minute for audit in bucket_audits],
        "evidence_level": "modeled_uncalibrated",
        "calibration_status": "not_calibrated",
        "routing_status": "unrouted_zone_pair_demand",
        "assignment_policy_name": source.assignment_policy.policy_name,
        "seed_lineage": TimeSlicedSeedLineage(
            source_base_seed=source.seed_policy.base_seed,
            effective_seed=effective_seed,
            seed_override_applied=seed_override_applied,
            source_rng_algorithm=source.seed_policy.rng_algorithm,
            source_seed_derivation=source.seed_policy.seed_derivation,
            generation_seed_derivation=(
                "sha256-effective-seed-demand-version-weekday-bucket-scope-v1"
            ),
        ).model_dump(mode="json"),
        "real_vehicles_per_modeled_vehicle": 1.0,
        "generated_vehicle_count": len(events),
        "represented_target_vehicle_trips": represented,
        "bucket_audits": [audit.model_dump(mode="json") for audit in bucket_audits],
        "rounding_audit": TimeSlicedRoundingAudit(
            policy="balanced-stochastic-rounding-per-weekday-bucket-v1",
            scale_real_vehicles_per_sumo_vehicle=1.0,
            per_row_absolute_error_bound_vehicle_trips=1.0,
            per_bucket_absolute_error_bound_vehicle_trips=1.0,
            target_vehicle_trips=target,
            represented_vehicle_trips=represented,
            aggregate_rounding_error_vehicle_trips=represented - target,
            maximum_absolute_row_error_vehicle_trips=max(row_errors, default=0.0),
            gate_passed=(
                all(audit.rounding_gate_passed for audit in bucket_audits)
                and all(error < 1.0 + 1e-12 for error in row_errors)
            ),
        ).model_dump(mode="json"),
        "departure_audit": TimeSlicedDepartureAudit(
            interval_seconds=900,
            generated_vehicle_count=len(events),
            leakage_count=leakage,
            departure_at_interval_end_count=departure_at_end,
            duplicate_vehicle_identity_count=len(vehicle_ids) - len(set(vehicle_ids)),
            gate_passed=(
                leakage == 0
                and departure_at_end == 0
                and len(vehicle_ids) == len(set(vehicle_ids))
            ),
        ).model_dump(mode="json"),
        "output": output_identity.model_dump(mode="json"),
    }


def _validate_materialized_output(
    directory: Path, manifest: TimeSlicedDemandManifestV1
) -> None:
    output = directory / manifest.output.relative_path
    if _sha256(output) != manifest.output.sha256:
        raise TimeSlicedDemandError("time-sliced demand output hash is invalid")
    if output.stat().st_size != manifest.output.byte_count:
        raise TimeSlicedDemandError("time-sliced demand output size is invalid")
    with gzip.open(output, "rb") as compressed:
        payload = compressed.read()
    if hashlib.sha256(payload).hexdigest() != manifest.output.uncompressed_sha256:
        raise TimeSlicedDemandError("uncompressed demand hash is invalid")
    root = ET.fromstring(payload)
    trips = root.findall("trip")
    if len(trips) != manifest.generated_vehicle_count:
        raise TimeSlicedDemandError("generated trip count does not match manifest")
    identities = [str(trip.get("id")) for trip in trips]
    if len(identities) != len(set(identities)):
        raise TimeSlicedDemandError("generated vehicle identities are not unique")
    allowed_buckets = set(manifest.bucket_start_minutes)
    leakage = 0
    previous: tuple[float, str] | None = None
    for trip in trips:
        vehicle_id = str(trip.get("id"))
        parts = vehicle_id.split("-")
        if len(parts) != 5 or parts[0] != "dyn" or parts[1] != manifest.weekday:
            raise TimeSlicedDemandError("generated vehicle identity is malformed")
        bucket = int(parts[2])
        depart = float(str(trip.get("depart")))
        if bucket not in allowed_buckets or not (bucket * 60 <= depart < bucket * 60 + 900):
            leakage += 1
        current = (depart, vehicle_id)
        if previous is not None and current < previous:
            raise TimeSlicedDemandError("generated trips are not deterministically ordered")
        previous = current
    if leakage or manifest.departure_audit.leakage_count:
        raise TimeSlicedDemandError("generated demand contains bucket leakage")
    if not manifest.rounding_audit.gate_passed or not manifest.departure_audit.gate_passed:
        raise TimeSlicedDemandError("time-sliced demand acceptance audit failed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
