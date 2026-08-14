"""SUMO-native edgeData capture and deterministic 15-minute materialization."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

import pyarrow as pa
from pyarrow import parquet

from backend.app.schemas.sumo_edge_telemetry import (
    SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS,
    SUMO_EDGE_TELEMETRY_METRIC_SEMANTICS_VERSION,
    SUMO_EDGE_TELEMETRY_PRODUCER_VERSION,
    SUMO_EDGE_TELEMETRY_SCHEMA_VERSION,
    SumoEdgeTelemetryManifestV1,
)

TELEMETRY_DIRECTORY = "edge-telemetry-15m"
TELEMETRY_PARQUET = "edge-telemetry-15m.parquet"
TELEMETRY_MANIFEST = "edge-telemetry-manifest.json"
NATIVE_FRAGMENT = "native-edge-data.xml.gz"
NATIVE_DEFINITION = "native-edge-data.add.xml"
_VARIANTS: tuple[Literal["baseline", "scenario"], ...] = (
    "baseline",
    "scenario",
)
_COUNT_ATTRIBUTES = ("entered", "departed", "left", "arrived")
_OPTIONAL_ATTRIBUTES = (
    "sampledSeconds",
    "traveltime",
    "density",
    "occupancy",
    "waitingTime",
    "timeLoss",
    "speed",
)

TELEMETRY_ARROW_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("run_identity", pa.string(), nullable=False),
        pa.field("network_version", pa.string(), nullable=False),
        pa.field("sumo_version", pa.string(), nullable=False),
        pa.field("simulation_mode", pa.string(), nullable=False),
        pa.field("seed", pa.int64(), nullable=False),
        pa.field("demand_version", pa.string(), nullable=False),
        pa.field("variant", pa.string(), nullable=False),
        pa.field("sumo_edge_id", pa.string(), nullable=False),
        pa.field("interval_start_seconds", pa.int64(), nullable=False),
        pa.field("interval_end_seconds", pa.int64(), nullable=False),
        pa.field("interval_duration_seconds", pa.int32(), nullable=False),
        pa.field("entered_count", pa.float64(), nullable=False),
        pa.field("departed_count", pa.float64(), nullable=False),
        pa.field("left_count", pa.float64(), nullable=False),
        pa.field("arrived_count", pa.float64(), nullable=False),
        pa.field("flow_vph", pa.float64(), nullable=False),
        pa.field("flow_derivation", pa.string(), nullable=False),
        pa.field("sampled_vehicle_seconds", pa.float64(), nullable=False),
        pa.field("mean_speed_mps", pa.float64()),
        pa.field("mean_speed_kph", pa.float64()),
        pa.field("mean_travel_time_seconds", pa.float64()),
        pa.field("density_veh_per_km", pa.float64()),
        pa.field("occupancy_percent", pa.float64()),
        pa.field("waiting_time_seconds", pa.float64()),
        pa.field("time_loss_seconds", pa.float64()),
    ],
    metadata={
        b"schema_version": b"1",
        b"producer": b"sumo_native_edge_telemetry",
        b"producer_version": SUMO_EDGE_TELEMETRY_PRODUCER_VERSION.encode(),
        b"native_source": b"SUMO edgeData",
        b"interval_semantics": b"[start,end)",
        b"flow_derivation": b"entered_count * 3600 / interval_duration_seconds",
        b"occupancy_unit": b"percent of edge space occupied; not detector occupancy",
    },
)


class SumoEdgeTelemetryError(RuntimeError):
    """Raised when native fragments cannot satisfy the telemetry contract."""


@dataclass(frozen=True)
class NativeFragmentIdentity:
    path: Path
    variant: Literal["baseline", "scenario"]
    begin_seconds: int
    end_seconds: int
    catalog_included: bool


@dataclass
class _EdgeAccumulator:
    entered: float = 0.0
    departed: float = 0.0
    left: float = 0.0
    arrived: float = 0.0
    sampled_seconds: float = 0.0
    speed_sample_seconds: float = 0.0
    speed_weighted_sum: float = 0.0
    edge_length_weighted_sum: float = 0.0
    edge_length_sample_seconds: float = 0.0
    density_duration_sum: float = 0.0
    density_present: bool = False
    occupancy_duration_sum: float = 0.0
    occupancy_present: bool = False
    waiting_time_sum: float = 0.0
    waiting_time_present: bool = False
    time_loss_sum: float = 0.0
    time_loss_present: bool = False


def write_native_edge_data_definition(
    *,
    definition_path: Path,
    output_path: Path,
    begin_seconds: int,
    end_seconds: int,
    include_empty_edge_catalog: bool,
) -> None:
    """Configure one checkpoint-local native edgeData fragment."""

    if begin_seconds < 0 or end_seconds <= begin_seconds:
        raise ValueError("Native edgeData fragment requires a positive time window")
    root = ET.Element("additional")
    ET.SubElement(
        root,
        "edgeData",
        {
            "id": "commute-help-native-edge-telemetry",
            "file": str(output_path.resolve()),
            "begin": str(begin_seconds),
            "end": str(end_seconds),
            "period": str(SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS),
            "excludeEmpty": "false" if include_empty_edge_catalog else "true",
            "withInternal": "false",
            "writeAttributes": " ".join((*_COUNT_ATTRIBUTES, *_OPTIONAL_ATTRIBUTES)),
        },
    )
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(definition_path, encoding="utf-8", xml_declaration=True)


def validate_native_fragment(
    path: Path,
    *,
    expected_begin_seconds: int,
    expected_end_seconds: int,
) -> int:
    """Validate one promoted fragment without retaining its edge rows."""

    edge_count = 0

    def count_edge(_attributes: dict[str, str]) -> None:
        nonlocal edge_count
        edge_count += 1

    begin, end = _consume_native_fragment(path, count_edge)
    if begin != expected_begin_seconds or end != expected_end_seconds:
        raise SumoEdgeTelemetryError(
            f"Native edgeData fragment {path.name} covers [{begin},{end}), "
            f"expected [{expected_begin_seconds},{expected_end_seconds})"
        )
    return edge_count


def collect_checkpoint_fragments(
    run_dir: Path,
    variant: Literal["baseline", "scenario"],
) -> list[NativeFragmentIdentity]:
    """Return validated promoted fragments in simulation-time order."""

    checkpoint_root = run_dir / "checkpoints" / variant
    fragments: list[NativeFragmentIdentity] = []
    if not checkpoint_root.is_dir():
        raise SumoEdgeTelemetryError(f"Missing {variant} checkpoint directory")
    for checkpoint_dir in sorted(checkpoint_root.iterdir(), key=lambda path: path.name):
        if not checkpoint_dir.is_dir() or not checkpoint_dir.name.isdigit():
            continue
        metadata_path = checkpoint_dir / "checkpoint.json"
        fragment_path = checkpoint_dir / NATIVE_FRAGMENT
        if not metadata_path.is_file() or not fragment_path.is_file():
            raise SumoEdgeTelemetryError(
                f"Promoted telemetry checkpoint {checkpoint_dir.name} is incomplete"
            )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("completed") is not True or metadata.get("variant") != variant:
            raise SumoEdgeTelemetryError(
                f"Promoted telemetry checkpoint {checkpoint_dir.name} is invalid"
            )
        begin = int(metadata["chunk_start_second"])
        end = int(metadata["chunk_end_second"])
        if (
            metadata.get("telemetry_interval_seconds")
            != SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS
            or metadata.get("telemetry_native_edge_count") is None
        ):
            raise SumoEdgeTelemetryError(
                f"Promoted telemetry checkpoint {checkpoint_dir.name} lacks validation metadata"
            )
        fragments.append(
            NativeFragmentIdentity(
                path=fragment_path,
                variant=variant,
                begin_seconds=begin,
                end_seconds=end,
                catalog_included=bool(metadata.get("telemetry_edge_catalog_included")),
            )
        )
    if not fragments:
        raise SumoEdgeTelemetryError(f"No promoted {variant} telemetry fragments exist")
    _validate_fragment_sequence(fragments)
    return fragments


def materialize_sumo_edge_telemetry(
    *,
    run_dir: Path,
    application_run_id: str,
    run_identity: str,
    network_manifest_path: Path,
    graph_manifest_path: Path,
    demand_manifest: dict[str, Any],
    seed: int,
    real_vehicles_per_simulated_vehicle: float,
    simulation_start_seconds: int,
    simulation_end_seconds: int,
    interval_seconds: int,
) -> SumoEdgeTelemetryManifestV1:
    """Merge checkpoint-native edgeData into one atomic dense Parquet artifact."""

    if interval_seconds != SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS:
        raise SumoEdgeTelemetryError("Phase 2.1 telemetry interval must be exactly 900 seconds")
    if simulation_start_seconds < 0 or simulation_end_seconds <= simulation_start_seconds:
        raise SumoEdgeTelemetryError("Invalid telemetry simulation window")

    network_manifest = json.loads(network_manifest_path.read_text(encoding="utf-8"))
    graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    fragments = {
        variant: collect_checkpoint_fragments(run_dir, variant) for variant in _VARIANTS
    }
    for variant in _VARIANTS:
        if fragments[variant][0].begin_seconds != simulation_start_seconds:
            raise SumoEdgeTelemetryError(
                f"{variant} telemetry fragments do not begin at the simulation start"
            )
        if fragments[variant][-1].end_seconds != simulation_end_seconds:
            raise SumoEdgeTelemetryError(
                f"{variant} telemetry fragments do not reach the simulation end"
            )
        if not fragments[variant][0].catalog_included:
            raise SumoEdgeTelemetryError(
                f"{variant} first telemetry fragment lacks the native edge catalog"
            )

    output_dir = run_dir / TELEMETRY_DIRECTORY
    pending_dir = run_dir / f".{TELEMETRY_DIRECTORY}.pending"
    if output_dir.exists():
        existing = _load_completed_artifact(output_dir)
        if existing.run_identity != run_identity:
            raise SumoEdgeTelemetryError(
                "Completed telemetry artifact belongs to another run identity"
            )
        return existing
    if pending_dir.exists():
        shutil.rmtree(pending_dir)
    pending_dir.mkdir(parents=True)
    parquet_path = pending_dir / TELEMETRY_PARQUET
    manifest_path = pending_dir / TELEMETRY_MANIFEST

    row_digest = hashlib.sha256()
    row_count = 0
    edge_catalog: tuple[str, ...] | None = None
    complete_counts: dict[str, int] = {}
    writer = parquet.ParquetWriter(
        parquet_path,
        TELEMETRY_ARROW_SCHEMA,
        compression="zstd",
        compression_level=9,
        use_dictionary=[
            "run_identity",
            "network_version",
            "sumo_version",
            "simulation_mode",
            "demand_version",
            "variant",
            "flow_derivation",
        ],
        write_statistics=True,
    )
    try:
        for variant in _VARIANTS:
            variant_rows, variant_catalog, complete_count = _write_variant_rows(
                writer=writer,
                row_digest=row_digest,
                run_identity=run_identity,
                network_version=str(network_manifest["network_version"]),
                sumo_version=str(network_manifest["sumo_version"]),
                seed=int(seed),
                demand_version=str(demand_manifest["demand_version"]),
                variant=variant,
                fragments=fragments[variant],
                simulation_start_seconds=simulation_start_seconds,
                simulation_end_seconds=simulation_end_seconds,
                interval_seconds=interval_seconds,
            )
            if edge_catalog is None:
                edge_catalog = variant_catalog
            elif variant_catalog != edge_catalog:
                raise SumoEdgeTelemetryError(
                    "Baseline and scenario native edge catalogs do not match"
                )
            row_count += variant_rows
            complete_counts[variant] = complete_count
    finally:
        writer.close()

    if edge_catalog is None or not edge_catalog:
        raise SumoEdgeTelemetryError("SUMO telemetry edge catalog is empty")
    expected_network_edges = network_manifest.get("validation", {}).get("edges")
    if (
        expected_network_edges is not None
        and len(edge_catalog) != int(expected_network_edges)
    ):
        raise SumoEdgeTelemetryError(
            "Native telemetry edge catalog does not match the network manifest"
        )
    if len(set(complete_counts.values())) != 1:
        raise SumoEdgeTelemetryError("Baseline and scenario interval counts do not match")
    complete_interval_count = complete_counts["baseline"]
    aligned_start = _next_complete_boundary(simulation_start_seconds, interval_seconds)
    aligned_end = simulation_end_seconds - (simulation_end_seconds % interval_seconds)
    omitted_start = max(0, aligned_start - simulation_start_seconds)
    omitted_end = max(0, simulation_end_seconds - aligned_end)

    output_sha256 = _sha256(parquet_path)
    source = {
        "graph_version": str(graph_manifest["graph_version"]),
        "graph_manifest_sha256": _sha256(graph_manifest_path),
        "network_version": str(network_manifest["network_version"]),
        "network_sha256": str(network_manifest["artifact"]["sha256"]),
        "network_manifest_sha256": _sha256(network_manifest_path),
        "sumo_version": str(network_manifest["sumo_version"]),
        "simulation_mode": "mesoscopic",
        "seed": int(seed),
        "demand_version": str(demand_manifest["demand_version"]),
        "demand_routes_sha256": str(demand_manifest["artifacts"]["routes"]["sha256"]),
        "real_vehicles_per_simulated_vehicle": float(
            real_vehicles_per_simulated_vehicle
        ),
    }
    interval = {
        "interval_seconds": interval_seconds,
        "interval_semantics": "half_open_start_inclusive_end_exclusive",
        "simulation_start_seconds": simulation_start_seconds,
        "simulation_end_seconds": simulation_end_seconds,
        "complete_interval_count_per_variant": complete_interval_count,
        "omitted_partial_seconds_at_start": omitted_start,
        "omitted_partial_seconds_at_end": omitted_end,
    }
    deterministic_identity = {
        "schema_version": SUMO_EDGE_TELEMETRY_SCHEMA_VERSION,
        "producer": {
            "name": "sumo_native_edge_telemetry",
            "version": SUMO_EDGE_TELEMETRY_PRODUCER_VERSION,
            "metric_semantics_version": SUMO_EDGE_TELEMETRY_METRIC_SEMANTICS_VERSION,
        },
        "run_identity": run_identity,
        "source": source,
        "interval": interval,
        "variants": list(_VARIANTS),
        "edge_count_per_interval": len(edge_catalog),
        "row_count": row_count,
        "row_content_sha256": row_digest.hexdigest(),
    }
    content_digest = hashlib.sha256(_canonical_json(deterministic_identity)).hexdigest()
    manifest = SumoEdgeTelemetryManifestV1.model_validate(
        {
            "schema_version": SUMO_EDGE_TELEMETRY_SCHEMA_VERSION,
            "producer": deterministic_identity["producer"],
            "run_identity": run_identity,
            "source": source,
            "interval": interval,
            "variants": list(_VARIANTS),
            "edge_count_per_interval": len(edge_catalog),
            "row_content_sha256": row_digest.hexdigest(),
            "artifact_type": "commute_help_sumo_native_edge_telemetry",
            "artifact_status": "complete",
            "evidence_level": "modeled_uncalibrated",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC),
            "application_run_id": application_run_id,
            "native_fragment_count": sum(len(value) for value in fragments.values()),
            "native_source": "SUMO edgeData",
            "flow_derivation": (
                "entered_count * 3600 / interval_duration_seconds"
            ),
            "output": {
                "relative_path": TELEMETRY_PARQUET,
                "sha256": output_sha256,
                "byte_count": parquet_path.stat().st_size,
                "row_count": row_count,
            },
            "content_digest": content_digest,
        }
    )
    _atomic_json(manifest_path, manifest.model_dump(mode="json"))
    os.replace(pending_dir, output_dir)
    return manifest


def _write_variant_rows(
    *,
    writer: parquet.ParquetWriter,
    row_digest: Any,
    run_identity: str,
    network_version: str,
    sumo_version: str,
    seed: int,
    demand_version: str,
    variant: Literal["baseline", "scenario"],
    fragments: list[NativeFragmentIdentity],
    simulation_start_seconds: int,
    simulation_end_seconds: int,
    interval_seconds: int,
) -> tuple[int, tuple[str, ...], int]:
    catalog: set[str] = set()
    accumulator: dict[str, _EdgeAccumulator] = {}
    bucket_start = (simulation_start_seconds // interval_seconds) * interval_seconds
    covered_seconds = 0
    row_count = 0
    complete_count = 0

    for fragment_index, fragment in enumerate(fragments):
        fragment_bucket = (fragment.begin_seconds // interval_seconds) * interval_seconds
        if fragment_bucket != bucket_start:
            if covered_seconds:
                # A non-aligned simulation start may leave a deliberately omitted prefix.
                if bucket_start + covered_seconds != fragment.begin_seconds:
                    raise SumoEdgeTelemetryError("Telemetry fragment coverage has a gap")
                accumulator.clear()
            bucket_start = fragment_bucket
            covered_seconds = 0
        bucket_end = bucket_start + interval_seconds
        if fragment.end_seconds > bucket_end:
            raise SumoEdgeTelemetryError(
                "A compute checkpoint crosses a 900-second telemetry boundary"
            )
        duration = fragment.end_seconds - fragment.begin_seconds

        def consume_edge(
            attributes: dict[str, str],
            fragment_index: int = fragment_index,
            duration: int = duration,
        ) -> None:
            edge_id = attributes.get("id", "")
            if not edge_id or edge_id.startswith(":"):
                raise SumoEdgeTelemetryError("Native telemetry emitted an invalid edge ID")
            if fragment_index == 0:
                catalog.add(edge_id)
            elif edge_id not in catalog:
                raise SumoEdgeTelemetryError(
                    f"Native fragment emitted uncatalogued SUMO edge ID {edge_id!r}"
                )
            _accumulate_native_edge(accumulator, edge_id, attributes, duration)

        begin, end = _consume_native_fragment(fragment.path, consume_edge)
        if begin != fragment.begin_seconds or end != fragment.end_seconds:
            raise SumoEdgeTelemetryError("Native fragment metadata changed after validation")
        covered_seconds += duration
        if fragment.end_seconds == bucket_end:
            if covered_seconds == interval_seconds:
                if not catalog:
                    raise SumoEdgeTelemetryError("Native edge catalog was not captured")
                row_count += _write_dense_interval(
                    writer=writer,
                    row_digest=row_digest,
                    run_identity=run_identity,
                    network_version=network_version,
                    sumo_version=sumo_version,
                    seed=seed,
                    demand_version=demand_version,
                    variant=variant,
                    edge_ids=sorted(catalog),
                    interval_start_seconds=bucket_start,
                    accumulator=accumulator,
                )
                complete_count += 1
            accumulator.clear()
            bucket_start = bucket_end
            covered_seconds = 0

    if fragments[-1].end_seconds != simulation_end_seconds:
        raise SumoEdgeTelemetryError("Telemetry fragment sequence ended unexpectedly")
    return row_count, tuple(sorted(catalog)), complete_count


def _accumulate_native_edge(
    accumulators: dict[str, _EdgeAccumulator],
    edge_id: str,
    attributes: dict[str, str],
    fragment_duration_seconds: int,
) -> None:
    values = {
        name: _nonnegative_native_float(attributes, name)
        for name in (*_COUNT_ATTRIBUTES, *_OPTIONAL_ATTRIBUTES)
    }
    if not any(value not in (None, 0.0) for value in values.values()):
        return
    record = accumulators.setdefault(edge_id, _EdgeAccumulator())
    record.entered += values["entered"] or 0.0
    record.departed += values["departed"] or 0.0
    record.left += values["left"] or 0.0
    record.arrived += values["arrived"] or 0.0
    sampled_seconds = values["sampledSeconds"] or 0.0
    record.sampled_seconds += sampled_seconds
    speed = values["speed"]
    if speed is not None and sampled_seconds > 0:
        record.speed_weighted_sum += speed * sampled_seconds
        record.speed_sample_seconds += sampled_seconds
        travel_time = values["traveltime"]
        if speed > 0 and travel_time is not None:
            record.edge_length_weighted_sum += speed * travel_time * sampled_seconds
            record.edge_length_sample_seconds += sampled_seconds
    density = values["density"]
    if density is not None:
        record.density_duration_sum += density * fragment_duration_seconds
        record.density_present = True
    occupancy = values["occupancy"]
    if occupancy is not None:
        record.occupancy_duration_sum += occupancy * fragment_duration_seconds
        record.occupancy_present = True
    waiting = values["waitingTime"]
    if waiting is not None:
        record.waiting_time_sum += waiting
        record.waiting_time_present = True
    time_loss = values["timeLoss"]
    if time_loss is not None:
        record.time_loss_sum += time_loss
        record.time_loss_present = True


def _write_dense_interval(
    *,
    writer: parquet.ParquetWriter,
    row_digest: Any,
    run_identity: str,
    network_version: str,
    sumo_version: str,
    seed: int,
    demand_version: str,
    variant: Literal["baseline", "scenario"],
    edge_ids: list[str],
    interval_start_seconds: int,
    accumulator: dict[str, _EdgeAccumulator],
) -> int:
    batch_size = 25_000
    for start in range(0, len(edge_ids), batch_size):
        rows = [
            _telemetry_row(
                run_identity=run_identity,
                network_version=network_version,
                sumo_version=sumo_version,
                seed=seed,
                demand_version=demand_version,
                variant=variant,
                edge_id=edge_id,
                interval_start_seconds=interval_start_seconds,
                value=accumulator.get(edge_id),
            )
            for edge_id in edge_ids[start : start + batch_size]
        ]
        for row in rows:
            row_digest.update(_canonical_json(row))
            row_digest.update(b"\n")
        writer.write_table(pa.Table.from_pylist(rows, schema=TELEMETRY_ARROW_SCHEMA))
    return len(edge_ids)


def _telemetry_row(
    *,
    run_identity: str,
    network_version: str,
    sumo_version: str,
    seed: int,
    demand_version: str,
    variant: Literal["baseline", "scenario"],
    edge_id: str,
    interval_start_seconds: int,
    value: _EdgeAccumulator | None,
) -> dict[str, Any]:
    data = value or _EdgeAccumulator()
    mean_speed = (
        data.speed_weighted_sum / data.speed_sample_seconds
        if data.speed_sample_seconds > 0
        else None
    )
    travel_time = None
    if (
        mean_speed is not None
        and mean_speed > 0
        and data.edge_length_sample_seconds > 0
    ):
        edge_length = data.edge_length_weighted_sum / data.edge_length_sample_seconds
        travel_time = edge_length / mean_speed
    entered = _rounded(data.entered)
    return {
        "schema_version": SUMO_EDGE_TELEMETRY_SCHEMA_VERSION,
        "run_identity": run_identity,
        "network_version": network_version,
        "sumo_version": sumo_version,
        "simulation_mode": "mesoscopic",
        "seed": seed,
        "demand_version": demand_version,
        "variant": variant,
        "sumo_edge_id": edge_id,
        "interval_start_seconds": interval_start_seconds,
        "interval_end_seconds": (
            interval_start_seconds + SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS
        ),
        "interval_duration_seconds": SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS,
        "entered_count": entered,
        "departed_count": _rounded(data.departed),
        "left_count": _rounded(data.left),
        "arrived_count": _rounded(data.arrived),
        "flow_vph": _rounded(entered * 4.0),
        "flow_derivation": (
            "entered_count * 3600 / interval_duration_seconds"
        ),
        "sampled_vehicle_seconds": _rounded(data.sampled_seconds),
        "mean_speed_mps": _rounded_or_none(mean_speed),
        "mean_speed_kph": _rounded_or_none(
            mean_speed * 3.6 if mean_speed is not None else None
        ),
        "mean_travel_time_seconds": _rounded_or_none(travel_time),
        "density_veh_per_km": _rounded_or_none(
            data.density_duration_sum / SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS
            if data.density_present
            else None
        ),
        "occupancy_percent": _rounded_or_none(
            data.occupancy_duration_sum / SUMO_EDGE_TELEMETRY_INTERVAL_SECONDS
            if data.occupancy_present
            else None
        ),
        "waiting_time_seconds": _rounded_or_none(
            data.waiting_time_sum if data.waiting_time_present else None
        ),
        "time_loss_seconds": _rounded_or_none(
            data.time_loss_sum if data.time_loss_present else None
        ),
    }


def _consume_native_fragment(
    path: Path,
    consume_edge: Callable[[dict[str, str]], None],
) -> tuple[int, int]:
    if not path.is_file():
        raise SumoEdgeTelemetryError(f"Missing native edgeData fragment: {path}")
    begin: int | None = None
    end: int | None = None
    seen_edges: set[str] = set()
    opener: Callable[..., Any] = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rb") as source:
        for event, element in ET.iterparse(source, events=("start", "end")):
            if event == "start" and element.tag == "interval":
                if begin is not None:
                    raise SumoEdgeTelemetryError("Native fragment contains multiple intervals")
                begin = _exact_second(element.attrib.get("begin"), "begin")
                end = _exact_second(element.attrib.get("end"), "end")
            elif event == "end" and element.tag == "edge":
                edge_id = element.attrib.get("id", "")
                if edge_id in seen_edges:
                    raise SumoEdgeTelemetryError(
                        f"Native fragment repeats SUMO edge ID {edge_id!r}"
                    )
                seen_edges.add(edge_id)
                consume_edge(dict(element.attrib))
                element.clear()
    if begin is None or end is None or end <= begin:
        raise SumoEdgeTelemetryError("Native fragment has no valid interval")
    return begin, end


def _validate_fragment_sequence(fragments: list[NativeFragmentIdentity]) -> None:
    expected = fragments[0].begin_seconds
    for fragment in fragments:
        if fragment.begin_seconds != expected:
            raise SumoEdgeTelemetryError("Native telemetry fragments overlap or have a gap")
        if fragment.end_seconds <= fragment.begin_seconds:
            raise SumoEdgeTelemetryError("Native telemetry fragment duration is invalid")
        expected = fragment.end_seconds


def _nonnegative_native_float(
    attributes: dict[str, str],
    name: str,
) -> float | None:
    raw = attributes.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise SumoEdgeTelemetryError(f"SUMO edgeData {name} is not numeric") from error
    if not math.isfinite(value) or value < 0:
        raise SumoEdgeTelemetryError(f"SUMO edgeData {name} must be nonnegative")
    return value


def _exact_second(raw: str | None, name: str) -> int:
    if raw is None:
        raise SumoEdgeTelemetryError(f"Native fragment omits interval {name}")
    value = float(raw)
    rounded = round(value)
    if not math.isfinite(value) or abs(value - rounded) > 1e-7:
        raise SumoEdgeTelemetryError(f"Native fragment {name} is not an exact second")
    return rounded


def _next_complete_boundary(start_seconds: int, interval_seconds: int) -> int:
    if start_seconds % interval_seconds == 0:
        return start_seconds
    return ((start_seconds // interval_seconds) + 1) * interval_seconds


def _rounded(value: float) -> float:
    return round(float(value), 9)


def _rounded_or_none(value: float | None) -> float | None:
    return None if value is None else _rounded(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _load_completed_artifact(output_dir: Path) -> SumoEdgeTelemetryManifestV1:
    manifest_path = output_dir / TELEMETRY_MANIFEST
    if not manifest_path.is_file():
        raise SumoEdgeTelemetryError(
            "Telemetry output directory exists without an authoritative manifest"
        )
    try:
        manifest = SumoEdgeTelemetryManifestV1.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise SumoEdgeTelemetryError(
            "Existing telemetry manifest is invalid"
        ) from error
    output_path = output_dir / manifest.output.relative_path
    if (
        not output_path.is_file()
        or output_path.stat().st_size != manifest.output.byte_count
        or _sha256(output_path) != manifest.output.sha256
    ):
        raise SumoEdgeTelemetryError(
            "Existing telemetry Parquet does not match its authoritative manifest"
        )
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
