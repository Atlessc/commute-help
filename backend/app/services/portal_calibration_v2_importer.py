"""Streaming raw-PORTAL detector importer for calibration-v2 partitions.

This module deliberately does not import ``normalized/*.csv``.  Those files
are an older station/VPH roll-up.  The only measurement input here is a
manifest-verified raw detector CSV, one 15-minute row at a time.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from backend.app.schemas.calibration_v2 import (
    CALIBRATION_V2_INTERVAL_SECONDS,
    CALIBRATION_V2_SCHEMA_VERSION,
    ObservationRecord,
)
from backend.app.schemas.calibration_v2_corpus import (
    CALIBRATION_V2_CORPUS_SCHEMA_VERSION,
    CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION,
    CalibrationObservationCorpusV2,
    CalibrationObservationShardV2,
    CorpusFinalizationProvenance,
    CorpusIntegritySummary,
    CorpusShardIndexEntry,
    ImportDiagnostic,
)

PORTAL_PROVIDER_ENDPOINT = "https://new.portal.its.pdx.edu/highways/api/freewaydata/"
PORTAL_DATASET_ID = "portal.freeway.detector-observations"
IMPORTER_NAME = "portal_calibration_v2_importer"
IMPORTER_CODE_VERSION = "phase-1.2b"
PACIFIC = ZoneInfo("America/Los_Angeles")
RAW_COLUMNS = {
    "starttime", "resolution", "detector_id", "speed", "volume", "occupancy",
    "countreadings", "delay", "traveltime", "vht", "vmt",
}
MAX_DIAGNOSTICS = 25


class PortalImportError(ValueError):
    """A selected raw partition cannot be promoted as an immutable shard."""


@dataclass(frozen=True)
class ShardImportResult:
    shard_directory: Path
    manifest: CalibrationObservationShardV2
    reused_existing: bool


@dataclass(frozen=True)
class ValidatedShardManifest:
    """One immutable shard whose physical and canonical hashes were verified."""

    manifest: CalibrationObservationShardV2


@dataclass(frozen=True)
class _Mapping:
    graph_version: str | None
    app_edge_id: str | None
    status: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item),
    ) + "\n"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_canonical_stream(value: bytes) -> bytes:
    """Use fixed gzip metadata; content identity remains the raw canonical JSONL."""
    destination = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=destination, compresslevel=6, mtime=0
    ) as compressed:
        compressed.write(value)
    return destination.getvalue()


def _canonical_stream_sha256(path: Path, compression: str) -> str:
    digest = hashlib.sha256()
    with _open_observation_stream(path, compression) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_observation_stream(path: Path, compression: str):
    if compression == "none":
        return path.open("rb")
    if compression == "gzip":
        return gzip.open(path, "rb")
    raise PortalImportError(f"Unsupported observation stream compression: {compression}")


def iter_validated_shard_observations(
    shard_directory: Path,
    shard: CalibrationObservationShardV2,
) -> Iterator[tuple[ObservationRecord, str]]:
    """Validate and yield one canonical record at a time from an immutable shard."""
    stream_path = shard_directory / shard.observation_stream.relative_path
    if (
        not stream_path.is_file()
        or _sha256_file(stream_path) != shard.observation_stream.physical_sha256
    ):
        raise PortalImportError(f"Shard physical stream validation failed: {shard.shard_id}")
    canonical_digest = hashlib.sha256()
    record_count = 0
    with _open_observation_stream(stream_path, shard.observation_stream.compression) as source:
        for line in source:
            canonical_digest.update(line)
            record = ObservationRecord.model_validate_json(line)
            record_count += 1
            yield record, _sha256_bytes(canonical_json(record.model_dump(mode="json", exclude_none=False)).encode("utf-8"))
    if (
        record_count != shard.observation_stream.record_count
        or canonical_digest.hexdigest() != shard.observation_stream.canonical_sha256
    ):
        raise PortalImportError(f"Shard canonical stream validation failed: {shard.shard_id}")


def _safe_id(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._:-" else "-" for character in value.lower())


def _reference(reference_kind: str, reference_id: str, relative_path: str, sha256: str, version: str | None = None) -> dict[str, Any]:
    return {
        "reference_kind": reference_kind,
        "reference_id": _safe_id(reference_id),
        "reference_version": version,
        "relative_path": relative_path,
        "sha256": sha256,
    }


def _manifest_content_digest(value: dict[str, Any]) -> str:
    """Digest reproducible content only; generation time is audit metadata."""
    payload = dict(value)
    payload.pop("generated_at", None)
    payload.pop("content_digest", None)
    payload.pop("corpus_digest", None)
    return _sha256_bytes(canonical_json(payload).encode("utf-8"))


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_missing_offset")
    local = parsed.astimezone(PACIFIC)
    if parsed.utcoffset() != local.utcoffset():
        raise ValueError("timestamp_not_pacific_offset")
    return parsed


def _number(value: str, field: str) -> float:
    if not value.strip():
        raise ValueError(f"missing_{field}")
    number = float(value)
    if number < 0:
        raise ValueError(f"negative_{field}")
    return number


def _integer(value: str, field: str) -> int:
    number = _number(value, field)
    if not number.is_integer():
        raise ValueError(f"nonintegral_{field}")
    return int(number)


def portal_observation_id(
    *,
    detector_id: str,
    station_id: str,
    highway_id: str,
    direction: str,
    interval_start: datetime,
) -> str:
    """Stable source identity.  It intentionally excludes file and measurements."""
    local = interval_start.astimezone(PACIFIC)
    # Seconds are normalized to the fixed source resolution rather than to a
    # CSV spelling, and the source UTC offset remains represented explicitly.
    if local.minute % 15 or local.second or local.microsecond:
        raise ValueError("timestamp_not_15_minute_boundary")
    identity = "\x1f".join((
        PORTAL_PROVIDER_ENDPOINT,
        str(detector_id), str(station_id), str(highway_id), direction,
        local.isoformat(timespec="seconds"), str(CALIBRATION_V2_INTERVAL_SECONDS),
    ))
    return "portal.obs." + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]


def portal_shard_id(chunk_id: str, raw_sha256: str) -> str:
    return "portal.shard." + _sha256_bytes(f"{chunk_id}\x1f{raw_sha256}".encode())[:40]


def _campaign_manifest(campaign_directory: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = campaign_directory / "campaign-manifest.json"
    if not manifest_path.is_file():
        raise PortalImportError(f"Missing campaign manifest: {manifest_path}")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 2 or not isinstance(value.get("chunks"), dict):
        raise PortalImportError("Expected PORTAL campaign manifest schema_version 2 with chunks")
    return value, manifest_path


def _load_metadata(campaign_directory: Path, manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    metadata = manifest.get("metadata", {})
    expected = {"highways", "detectors", "stations"}
    if not expected.issubset(metadata):
        raise PortalImportError("Campaign manifest lacks required PORTAL metadata references")
    loaded: dict[str, Any] = {}
    references: list[dict[str, Any]] = []
    for name in sorted(expected):
        entry = metadata[name]
        path = campaign_directory / entry["path"]
        if (
            not path.is_file()
            or path.stat().st_size != entry["bytes"]
            or _sha256_file(path) != entry["sha256"]
        ):
            raise PortalImportError(f"Manifest verification failed for metadata {name}")
        loaded[name] = json.loads(path.read_text(encoding="utf-8"))
        references.append(_reference("file", f"portal.metadata.{name}", entry["path"], entry["sha256"]))
    highways = {str(item["highwayid"]): item for item in loaded["highways"]}
    detectors = {str(item["detectorid"]): item for item in loaded["detectors"]}
    stations = {}
    for feature in loaded["stations"].get("features", []):
        properties = feature.get("properties", {})
        coordinates = (feature.get("geometry") or {}).get("coordinates", [])
        if len(coordinates) >= 2:
            properties = dict(properties, coordinates=coordinates)
        stations[str(properties.get("stationid"))] = properties
    return highways, detectors, stations, references + [_reference("manifest", "portal.campaign.manifest", "campaign-manifest.json", _sha256_file(campaign_directory / "campaign-manifest.json"), "2")]


def _load_accepted_mappings(mapping_path: Path | None, report_path: Path | None) -> tuple[dict[tuple[str, str], _Mapping], list[dict[str, Any]]]:
    if mapping_path is None:
        return {}, []
    if not mapping_path.is_file():
        raise PortalImportError(f"Mapping file is not readable: {mapping_path}")
    graph_version = None
    refs = [_reference("file", "portal.station.app-edge-matches", mapping_path.name, _sha256_file(mapping_path))]
    if report_path is not None:
        if not report_path.is_file():
            raise PortalImportError(f"Mapping report is not readable: {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        graph_version = report.get("graph_version")
        refs.append(_reference("file", "portal.station.app-edge-match-report", report_path.name, _sha256_file(report_path)))
    mappings: dict[tuple[str, str], _Mapping] = {}
    with mapping_path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            station_id = (row.get("station_id") or "").strip()
            if station_id.startswith("portal-station-"):
                station_id = station_id.removeprefix("portal-station-")
            direction = (row.get("direction") or "").strip().lower()
            if station_id and direction:
                mappings[(station_id, direction)] = _Mapping(
                    graph_version=graph_version,
                    app_edge_id=(row.get("edge_id") or "").strip() or None,
                    status=(row.get("status") or "").strip().lower(),
                )
    return mappings, refs


def _local_coordinates(station: dict[str, Any]) -> tuple[float | None, float | None]:
    coordinates = station.get("coordinates") or []
    if len(coordinates) < 2:
        return None, None
    x, y = float(coordinates[0]), float(coordinates[1])
    # PORTAL station metadata is normally Web Mercator. Synthetic/source data
    # already in WGS84 remains usable without another dependency.
    if abs(x) <= 180 and abs(y) <= 90:
        return y, x
    longitude = x * 180 / 20037508.34
    latitude = 180 / math.pi * (2 * math.atan(math.exp(y * math.pi / 20037508.34)) - math.pi / 2)
    return latitude, longitude


def _diagnostic(reason: str, raw: dict[str, str], source_ref: str, line: int, **extra: str | None) -> ImportDiagnostic:
    digest = _sha256_bytes(canonical_json(raw).encode("utf-8"))
    return ImportDiagnostic(reason=reason, source_reference_id=source_ref, line_number=line, row_sha256=digest, **extra)


def _record_from_row(
    raw: dict[str, str], *, highways: dict[str, dict[str, Any]], detectors: dict[str, dict[str, Any]], stations: dict[str, dict[str, Any]], mappings: dict[tuple[str, str], _Mapping],
) -> tuple[ObservationRecord, str]:
    if set(raw) != RAW_COLUMNS:
        raise ValueError("raw_header_schema_mismatch")
    start = _parse_timestamp(raw["starttime"])
    if raw["resolution"].strip() != "00:15:00":
        raise ValueError("unsupported_resolution")
    detector_id = str(_integer(raw["detector_id"], "detector_id"))
    detector = detectors.get(detector_id)
    if detector is None:
        raise ValueError("detector_not_in_manifest_metadata")
    station_id, highway_id = str(detector["stationid"]), str(detector["highwayid"])
    station, highway = stations.get(station_id), highways.get(highway_id)
    if station is None or highway is None:
        raise ValueError("metadata_join_missing")
    source_direction = str(highway.get("direction", "")).strip().lower()
    direction = {"north": "northbound", "south": "southbound", "east": "eastbound", "west": "westbound"}.get(source_direction)
    if direction is None:
        raise ValueError("unsupported_highway_direction")
    volume = _number(raw["volume"], "volume")
    countreadings = _integer(raw["countreadings"], "countreadings")
    occupancy = _number(raw["occupancy"], "occupancy")
    missing: list[str] = []
    flags: list[str] = []
    speed_raw = raw["speed"].strip()
    if speed_raw:
        speed_mph = _number(speed_raw, "speed")
        speed_kph: float | None = speed_mph * 1.609344
        if speed_mph == 0:
            flags.append("zero_speed_semantics_unresolved")
    else:
        speed_kph = None
        missing.append("speed_kph")
    latitude, longitude = _local_coordinates(station)
    road_association = None
    mapping_direction = direction.removesuffix("bound")
    mapping = mappings.get((station_id, mapping_direction))
    if mapping and mapping.status == "accepted" and mapping.app_edge_id and mapping.graph_version:
        road_association = {"graph_version": mapping.graph_version, "app_edge_id": mapping.app_edge_id, "osm_way_ids": [], "accepted_sumo_association": None}
    record_id = portal_observation_id(detector_id=detector_id, station_id=station_id, highway_id=highway_id, direction=direction, interval_start=start)
    local = start.astimezone(PACIFIC)
    record = ObservationRecord.model_validate({
        "record_id": record_id, "record_kind": "observation", "direction": direction,
        "evidence": {"input_status": "historical_observation_input", "calibration_status": "not_calibrated"},
        "detector": {"station_id": station_id, "detector_id": detector_id, "lane_id": str(detector.get("lanenumber")) if detector.get("lanenumber") is not None else None, "source_direction_code": source_direction},
        "source_location": {"source_location_id": station_id, "latitude": latitude, "longitude": longitude, "route_id": highway_id, "road_name": highway.get("highwayname")},
        "calendar": {"exact_date": local.date(), "weekday": local.strftime("%A").lower(), "timezone": "America/Los_Angeles", "interval_start": start, "interval_end": start + timedelta(seconds=900), "interval_seconds": 900},
        "road_association": road_association,
        "measurements": {"volume_count": volume, "flow_vph": volume * 4, "speed_kph": speed_kph, "occupancy_percent": occupancy, "sample_count": countreadings, "units": {"volume_count": "vehicles_per_interval", "flow_vph": "vehicles_per_hour", "speed": "kilometers_per_hour", "occupancy": "percent"}},
        "quality": {"disposition": "accepted", "source_status": "unknown", "confidence": "unknown", "missing_fields": missing, "quality_flags": flags, "exclusion_reasons": []},
    })
    return record, "accepted" if road_association else (mapping.status if mapping else "absent")


def import_portal_partition(
    campaign_directory: Path, chunk_id: str, output_root: Path, *, station_mapping_path: Path | None = None, station_mapping_report_path: Path | None = None,
) -> ShardImportResult:
    """Validate and promote one manifest-verified raw chunk as an immutable shard."""
    campaign_directory, output_root = campaign_directory.resolve(), output_root.resolve()
    campaign, _campaign_path = _campaign_manifest(campaign_directory)
    chunk = campaign["chunks"].get(chunk_id)
    if not isinstance(chunk, dict) or chunk.get("status") != "complete":
        raise PortalImportError(f"Chunk {chunk_id!r} is not a completed manifest partition")
    raw_meta = chunk.get("raw", {})
    raw_relative = raw_meta.get("path")
    raw_path = campaign_directory / str(raw_relative)
    if (
        not raw_path.is_file()
        or raw_path.stat().st_size != raw_meta.get("bytes")
        or _sha256_file(raw_path) != raw_meta.get("sha256")
    ):
        raise PortalImportError("Raw PORTAL file does not match its campaign manifest digest")
    highways, detectors, stations, metadata_refs = _load_metadata(campaign_directory, campaign)
    mappings, mapping_refs = _load_accepted_mappings(station_mapping_path, station_mapping_report_path)
    raw_ref = _reference("file", f"portal.raw.{chunk_id}", str(raw_relative), raw_meta["sha256"])
    source_refs = sorted(metadata_refs + mapping_refs + [raw_ref], key=lambda ref: ref["reference_id"])
    source_ref_id = raw_ref["reference_id"]
    start_date, end_date = date.fromisoformat(chunk["start_date"]), date.fromisoformat(chunk["end_date"])
    records: dict[str, tuple[str, str, ObservationRecord, str, int]] = {}
    conflicts: set[str] = set()
    conflict_payloads: dict[str, set[str]] = {}
    conflict_rows: dict[str, list[tuple[str, int]]] = {}
    rejections: Counter[str] = Counter()
    diagnostics: list[ImportDiagnostic] = []
    audit = Counter()
    with raw_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if set(reader.fieldnames or []) != RAW_COLUMNS:
            raise PortalImportError("Raw PORTAL CSV headers do not match verified source structure")
        for line_number, raw in enumerate(reader, start=2):
            audit["rows"] += 1
            try:
                start = _parse_timestamp(raw["starttime"])
                local_date = start.astimezone(PACIFIC).date()
                # PORTAL's endpoint returns the following Monday at 00:00 as an
                # inclusive transport boundary. Suppress it before identity.
                if local_date < start_date or local_date > end_date:
                    audit["boundary"] += 1
                    diagnostics.append(_diagnostic("export_boundary_overlap", raw, source_ref_id, line_number))
                    continue
                record, _ = _record_from_row(raw, highways=highways, detectors=detectors, stations=stations, mappings=mappings)
            except ValidationError:
                reason = "calibration_v2_structural_validation_failed"
                rejections[reason] += 1
                diagnostics.append(_diagnostic(reason, raw, source_ref_id, line_number))
                continue
            except ValueError as error:
                reason = str(error).split("\n", 1)[0]
                rejections[reason] += 1
                diagnostics.append(_diagnostic(reason, raw, source_ref_id, line_number))
                continue
            payload = canonical_json(record.model_dump(mode="json", exclude_none=False))
            payload_digest = _sha256_bytes(payload.encode("utf-8"))
            row_digest = _sha256_bytes(canonical_json(raw).encode("utf-8"))
            existing = records.get(record.record_id)
            if record.record_id in conflicts:
                conflict_payloads[record.record_id].add(payload_digest)
                conflict_rows[record.record_id].append((row_digest, line_number))
                continue
            if existing is None:
                records[record.record_id] = (
                    payload_digest,
                    payload,
                    record,
                    row_digest,
                    line_number,
                )
            elif existing[0] == payload_digest:
                audit["duplicates"] += 1
            else:
                conflicts.add(record.record_id)
                conflict_payloads[record.record_id] = {existing[0], payload_digest}
                conflict_rows[record.record_id] = [
                    (existing[3], existing[4]),
                    (row_digest, line_number),
                ]
                del records[record.record_id]
                audit["conflicts"] += 1
    for record_id in sorted(conflicts):
        row_digest, line_number = min(conflict_rows[record_id])
        conflict_fingerprint = _sha256_bytes(
            "\x1f".join(sorted(conflict_payloads[record_id])).encode()
        )
        diagnostics.append(ImportDiagnostic(
            reason="conflicting_observation_identity",
            source_reference_id=source_ref_id,
            line_number=line_number,
            row_sha256=row_digest,
            observation_id=record_id,
            related_payload_sha256=conflict_fingerprint,
        ))
    ordered = [records[key] for key in sorted(records)]
    attached = sum(item[2].road_association is not None for item in ordered)
    review_or_unmatched = sum(
        item[2].road_association is None
        and (mapping := mappings.get((item[2].detector.station_id, item[2].direction.removesuffix("bound")))) is not None
        and mapping.status != "accepted"
        for item in ordered
    )
    absent = len(ordered) - attached - review_or_unmatched
    canonical_stream_bytes = "".join(item[1] for item in ordered).encode("utf-8")
    canonical_stream_sha = _sha256_bytes(canonical_stream_bytes)
    physical_stream_bytes = _gzip_canonical_stream(canonical_stream_bytes)
    physical_stream_sha = _sha256_bytes(physical_stream_bytes)
    shard_id = portal_shard_id(chunk_id, raw_meta["sha256"])
    diagnostics = sorted(diagnostics, key=lambda value: (value.reason, value.row_sha256, value.line_number))[:MAX_DIAGNOSTICS]
    starts = [item[2].calendar.interval_start for item in ordered]
    ends = [item[2].calendar.interval_end for item in ordered]
    payload: dict[str, Any] = {
        "corpus_schema_version": CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION, "shard_schema_version": 1,
        "artifact_type": "commute_help_calibration_observation_shard", "shard_id": shard_id,
        "source_partition_id": _safe_id(chunk_id), "calibration_schema_version": CALIBRATION_V2_SCHEMA_VERSION,
        "artifact_status": "historical_input", "calibration_status": "not_calibrated", "generated_at": datetime.now(UTC),
        "generator": {"name": IMPORTER_NAME, "code_version": IMPORTER_CODE_VERSION, "model_version": None},
        "source_dataset": {"dataset_id": PORTAL_DATASET_ID, "dataset_version": f"campaign-manifest-sha256:{_sha256_file(campaign_directory / 'campaign-manifest.json')}", "provider": "PORTAL", "synthetic": False},
        "source_references": source_refs,
        "observation_stream": {"relative_path": "observations.jsonl.gz", "compression": "gzip", "canonical_sha256": canonical_stream_sha, "physical_sha256": physical_stream_sha, "canonical_byte_count": len(canonical_stream_bytes), "physical_byte_count": len(physical_stream_bytes), "record_count": len(ordered)},
        "audit": {"rows_encountered": audit["rows"], "accepted_count": len(ordered), "duplicate_suppressed_count": audit["duplicates"], "export_boundary_suppressed_count": audit["boundary"], "conflict_count": audit["conflicts"], "rejected_by_reason": dict(rejections), "diagnostics": [item.model_dump(mode="json") for item in diagnostics]},
        "earliest_interval_start": min(starts) if starts else None, "latest_interval_end": max(ends) if ends else None,
        "weekdays_represented": sorted({item[2].calendar.weekday for item in ordered}), "station_count": len({item[2].detector.station_id for item in ordered}), "detector_count": len({item[2].detector.detector_id for item in ordered}),
        "mapping_coverage": {"accepted_app_edge_attached_count": attached, "review_or_unmatched_not_attached_count": review_or_unmatched, "absent_mapping_count": absent},
    }
    payload["content_digest"] = _manifest_content_digest(payload)
    manifest = CalibrationObservationShardV2.model_validate(payload)
    shard_directory = output_root / "shards" / shard_id
    if shard_directory.exists():
        existing = CalibrationObservationShardV2.model_validate_json((shard_directory / "shard-manifest.json").read_bytes())
        existing_stream = shard_directory / existing.observation_stream.relative_path
        if (
            existing.content_digest != manifest.content_digest
            or not existing_stream.is_file()
            or _sha256_file(existing_stream) != physical_stream_sha
            or _canonical_stream_sha256(existing_stream, existing.observation_stream.compression) != canonical_stream_sha
        ):
            raise PortalImportError("Existing immutable shard conflicts with recomputed partition content")
        return ShardImportResult(shard_directory, existing, True)
    shard_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{shard_id}.", dir=shard_directory.parent))
    try:
        (staging / "observations.jsonl.gz").write_bytes(physical_stream_bytes)
        (staging / "shard-manifest.json").write_text(canonical_json(manifest.model_dump(mode="json")), encoding="utf-8")
        os.replace(staging, shard_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ShardImportResult(shard_directory, manifest, False)


def build_portal_corpus_manifest(
    output_root: Path,
    *,
    corpus_id: str = "portal.calibration-v2",
    integrity: CorpusIntegritySummary | None = None,
    materialization_state: str = "incomplete",
    validated_shards: tuple[ValidatedShardManifest, ...] | None = None,
    finalization: CorpusFinalizationProvenance | None = None,
) -> CalibrationObservationCorpusV2:
    """Index immutable shards, reusing trusted validation results when supplied."""
    output_root = output_root.resolve()
    entries: list[CorpusShardIndexEntry] = []
    dataset = None
    if validated_shards is None:
        trusted_manifests: list[ValidatedShardManifest] = []
        for manifest_path in sorted(
            (output_root / "shards").glob("*/shard-manifest.json")
        ):
            shard = CalibrationObservationShardV2.model_validate_json(
                manifest_path.read_bytes()
            )
            stream_path = (
                manifest_path.parent / shard.observation_stream.relative_path
            )
            if (
                not stream_path.is_file()
                or _sha256_file(stream_path)
                != shard.observation_stream.physical_sha256
                or _canonical_stream_sha256(
                    stream_path,
                    shard.observation_stream.compression,
                )
                != shard.observation_stream.canonical_sha256
            ):
                raise PortalImportError(
                    f"Shard stream validation failed: {manifest_path.parent.name}"
                )
            trusted_manifests.append(ValidatedShardManifest(manifest=shard))
    else:
        trusted_manifests = list(validated_shards)
    trusted_manifests.sort(key=lambda item: item.manifest.shard_id)
    if len({item.manifest.shard_id for item in trusted_manifests}) != len(
        trusted_manifests
    ):
        raise PortalImportError("Validated shard manifests must have unique shard IDs")
    for validated in trusted_manifests:
        shard = validated.manifest
        if dataset is None:
            dataset = shard.source_dataset
        elif dataset != shard.source_dataset:
            raise PortalImportError("Cannot combine different source datasets in one PORTAL corpus")
        entries.append(CorpusShardIndexEntry(shard_id=shard.shard_id, relative_path=f"shards/{shard.shard_id}/shard-manifest.json", content_digest=shard.content_digest, observation_stream_canonical_sha256=shard.observation_stream.canonical_sha256, observation_stream_physical_sha256=shard.observation_stream.physical_sha256, accepted_count=shard.audit.accepted_count, earliest_interval_start=shard.earliest_interval_start, latest_interval_end=shard.latest_interval_end))
    if dataset is None:
        raise PortalImportError("No promoted shards available for corpus manifest")
    payload: dict[str, Any] = {"corpus_schema_version": CALIBRATION_V2_CORPUS_SCHEMA_VERSION, "artifact_type": "commute_help_calibration_observation_corpus", "corpus_id": corpus_id, "calibration_schema_version": CALIBRATION_V2_SCHEMA_VERSION, "artifact_status": "historical_input", "calibration_status": "not_calibrated", "generated_at": datetime.now(UTC), "generator": {"name": IMPORTER_NAME, "code_version": IMPORTER_CODE_VERSION, "model_version": None}, "finalization": finalization.model_dump(mode="json") if finalization is not None else None, "source_dataset": dataset.model_dump(mode="json"), "shards": [entry.model_dump(mode="json") for entry in entries], "observation_count": sum(entry.accepted_count for entry in entries), "materialization_state": materialization_state, "integrity": integrity.model_dump(mode="json") if integrity is not None else None}
    payload["corpus_digest"] = _manifest_content_digest(payload)
    corpus = CalibrationObservationCorpusV2.model_validate(payload)
    staging = output_root / ".corpus-manifest.pending"
    staging.write_text(canonical_json(corpus.model_dump(mode="json")), encoding="utf-8")
    os.replace(staging, output_root / "corpus-manifest.json")
    return corpus
