"""Print a cheap structural summary for one Phase 2.1 telemetry artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from pyarrow import parquet

from backend.app.core.settings import Settings
from backend.app.schemas.sumo_edge_telemetry import SumoEdgeTelemetryManifestV1
from backend.app.services.sumo.edge_telemetry_service import (
    TELEMETRY_DIRECTORY,
    TELEMETRY_MANIFEST,
)


def inspect_telemetry(path: Path) -> dict[str, Any]:
    artifact_dir = path / TELEMETRY_DIRECTORY if (path / TELEMETRY_DIRECTORY).is_dir() else path
    manifest_path = artifact_dir / TELEMETRY_MANIFEST
    manifest = SumoEdgeTelemetryManifestV1.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    parquet_path = artifact_dir / manifest.output.relative_path
    parquet_file = parquet.ParquetFile(parquet_path)
    intervals: set[tuple[int, int]] = set()
    edge_ids: set[str] = set()
    previous_identity: tuple[str, int, str] | None = None
    duplicates = 0
    order_violations = 0
    negative_counts: dict[str, int] = {
        name: 0
        for name in (
            "entered_count",
            "departed_count",
            "left_count",
            "arrived_count",
            "flow_vph",
            "sampled_vehicle_seconds",
            "mean_speed_mps",
            "mean_travel_time_seconds",
            "density_veh_per_km",
            "occupancy_percent",
            "waiting_time_seconds",
            "time_loss_seconds",
        )
    }
    non_null_counts = {
        name: 0
        for name in (
            "mean_travel_time_seconds",
            "density_veh_per_km",
            "occupancy_percent",
            "waiting_time_seconds",
            "time_loss_seconds",
        )
    }
    flow_chunks: list[pa.Array] = []
    speed_chunks: list[pa.Array] = []
    columns = [
        "variant",
        "sumo_edge_id",
        "interval_start_seconds",
        "interval_end_seconds",
        *negative_counts,
        "mean_speed_mps",
        *non_null_counts,
    ]
    columns = list(dict.fromkeys(columns))
    for batch in parquet_file.iter_batches(columns=columns, batch_size=100_000):
        values = batch.to_pydict()
        for variant, start, end, edge_id in zip(
            values["variant"],
            values["interval_start_seconds"],
            values["interval_end_seconds"],
            values["sumo_edge_id"],
            strict=True,
        ):
            identity = (str(variant), int(start), str(edge_id))
            if identity == previous_identity:
                duplicates += 1
            elif previous_identity is not None and identity < previous_identity:
                order_violations += 1
            previous_identity = identity
            intervals.add((int(start), int(end)))
            edge_ids.add(str(edge_id))
        for name in negative_counts:
            negative_counts[name] += int(
                pc.sum(pc.cast(pc.less(batch.column(name), 0), pa.int64())).as_py()
            )
        for name in non_null_counts:
            non_null_counts[name] += batch.num_rows - batch.column(name).null_count
        flow_chunks.append(batch.column("flow_vph"))
        speed_chunks.append(batch.column("mean_speed_mps"))

    summary = {
        "telemetry_row_count": parquet_file.metadata.num_rows,
        "interval_count": len(intervals),
        "distinct_edge_count": len(edge_ids),
        "interval_boundaries": [list(value) for value in sorted(intervals)],
        "flow_vph": _summary(pa.chunked_array(flow_chunks)),
        "mean_speed_mps": _summary(pa.chunked_array(speed_chunks)),
        "non_null_metric_counts": non_null_counts,
        "duplicate_edge_interval_identities": duplicates,
        "deterministic_order_violations": order_violations,
        "negative_value_counts": negative_counts,
        "telemetry_file": {
            "bytes": parquet_path.stat().st_size,
            "sha256": _sha256(parquet_path),
            "manifest_sha256_matches": _sha256(parquet_path) == manifest.output.sha256,
        },
        "identity": {
            "application_run_id": manifest.application_run_id,
            "run_identity": manifest.run_identity,
            "network_version": manifest.source.network_version,
            "network_sha256": manifest.source.network_sha256,
            "sumo_version": manifest.source.sumo_version,
            "simulation_mode": manifest.source.simulation_mode,
            "content_digest": manifest.content_digest,
            "evidence_level": manifest.evidence_level,
            "calibration_status": manifest.calibration_status,
        },
    }
    return summary


def _summary(values: pa.ChunkedArray) -> dict[str, float | None]:
    present = pc.drop_null(values)
    if len(present) == 0:
        return {"min": None, "median": None, "max": None}
    quantile = pc.quantile(present, q=0.5, interpolation="linear")
    median = quantile[0].as_py() if isinstance(quantile, pa.Array) else quantile.as_py()
    return {
        "min": float(pc.min(present).as_py()),
        "median": float(median),
        "max": float(pc.max(present).as_py()),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="SUMO run directory or its edge-telemetry-15m directory",
    )
    selection.add_argument(
        "--latest",
        action="store_true",
        help="Inspect the newest completed local telemetry artifact",
    )
    args = parser.parse_args()
    path = _latest_telemetry_path() if args.latest else args.path.resolve()
    summary = inspect_telemetry(path)
    print(json.dumps(summary, indent=2, sort_keys=True))
    has_error = (
        summary["duplicate_edge_interval_identities"] > 0
        or summary["deterministic_order_violations"] > 0
        or any(summary["negative_value_counts"].values())
        or not summary["telemetry_file"]["manifest_sha256_matches"]
    )
    return 1 if has_error else 0


def _latest_telemetry_path() -> Path:
    runs_root = Settings().sumo_runs_path.resolve()
    manifests = sorted(
        runs_root.glob(f"*/{TELEMETRY_DIRECTORY}/{TELEMETRY_MANIFEST}"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )
    if not manifests:
        raise FileNotFoundError("No completed local edge-telemetry artifact exists")
    return manifests[0].parent


if __name__ == "__main__":
    raise SystemExit(main())
