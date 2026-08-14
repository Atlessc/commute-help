"""Phase 2.2g deterministic PORTAL-station to SUMO-station flow comparator."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.calibration_v2_corpus import CalibrationObservationCorpusV2
from backend.app.schemas.sumo_historical_comparison_v2 import (
    COMPARATOR_V2_ALGORITHM,
    COMPARATOR_V2_ELIGIBILITY_POLICY,
    COMPARATOR_V2_PRODUCER_NAME,
    COMPARATOR_V2_PRODUCER_VERSION,
    HISTORICAL_STATION_PROFILE_VERSION,
    ComparatorV2Manifest,
    HistoricalStationFlowManifestV1,
)
from backend.app.schemas.sumo_station_telemetry import (
    StationTelemetryManifestV1,
    StationTelemetryRecoveryManifestV1,
)
from backend.app.services.historical_calibration_v2_compiler import (
    WEEKDAYS,
    _accumulate_batch,
    _group_shards,
    _observation_batches,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

STATION_PROFILE_FILE = "historical-station-flow-profiles.parquet"
STATION_PROFILE_MANIFEST_FILE = "historical-station-flow-manifest.json"
STATION_ROUNDTRIP_FILE = "historical-station-flow-roundtrip.json"
STATION_COMPARISON_FILE = "station-comparisons.parquet"
APP_EDGE_COMPARISON_FILE = "app-edge-comparisons.parquet"
SCOREBOARD_FILE = "scoreboard.json"
REPORT_FILE = "COMPARATOR_V2_REPORT.md"
MANIFEST_FILE = "comparator-v2-manifest.json"
DEFAULT_OUTPUT_NAME = "historical-comparison-v2"
PACIFIC = ZoneInfo("America/Los_Angeles")
DIRECT = "direct_calibration_evidence"
SUPPLEMENTAL = "supplemental_evidence"
INSUFFICIENT = "insufficient_direct_evidence"
EXCLUSION_PRECEDENCE = (
    "provenance_mismatch",
    "time_profile_missing",
    "historical_missing",
    "historical_insufficient_support",
    "station_mapping_not_accepted",
    "sumo_station_missing",
    "sumo_interval_incomplete",
    "sumo_flow_incomplete",
    "unresolved_direct_departure",
)


class SumoHistoricalComparisonV2Error(RuntimeError):
    """Comparator-v2 inputs cannot produce trustworthy station-flow evidence."""


STATION_DATE_SCHEMA = pa.schema(
    [
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("local_date", pa.date32(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("volume_count", pa.float64(), nullable=False),
        pa.field("flow_vph", pa.float64(), nullable=False),
        pa.field("detector_count", pa.int32(), nullable=False),
        pa.field("sample_count", pa.int64(), nullable=False),
    ]
)

STATION_PROFILE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("interval_seconds", pa.int16(), nullable=False),
        pa.field("possible_date_count", pa.int32(), nullable=False),
        pa.field("observed_date_count", pa.int32(), nullable=False),
        pa.field("coverage_fraction", pa.float64(), nullable=False),
        pa.field("date_support_class", pa.string(), nullable=False),
        pa.field("flow_direct_calibration_eligible", pa.bool_(), nullable=False),
        pa.field("flow_vph_mean", pa.float64(), nullable=False),
        pa.field("flow_vph_p10", pa.float64(), nullable=False),
        pa.field("flow_vph_p50", pa.float64(), nullable=False),
        pa.field("flow_vph_p85", pa.float64(), nullable=False),
        pa.field("flow_vph_p90", pa.float64(), nullable=False),
        pa.field("flow_vph_p95", pa.float64(), nullable=False),
        pa.field("detector_support_count_min", pa.int32(), nullable=False),
        pa.field("detector_support_count_p50", pa.float64(), nullable=False),
        pa.field("detector_support_count_max", pa.int32(), nullable=False),
        pa.field("sample_count_min", pa.int64(), nullable=False),
        pa.field("sample_count_p50", pa.float64(), nullable=False),
        pa.field("sample_count_max", pa.int64(), nullable=False),
        pa.field("source_corpus_digest", pa.string(), nullable=False),
        pa.field("profile_version", pa.string(), nullable=False),
        pa.field("evidence_level", pa.string(), nullable=False),
        pa.field("calibration_status", pa.string(), nullable=False),
    ],
    metadata={b"producer": b"historical_station_flow_profile_deriver"},
)

ROUNDTRIP_DATE_COLUMNS = (
    "app_edge_id",
    "direction",
    "local_date",
    "weekday",
    "bucket_start_minute",
    "volume_count",
    "flow_vph",
    "station_count",
    "detector_count",
    "sample_count",
    "source_station_ids_json",
)


def canonical_station_id(value: object) -> str:
    text = str(value)
    return text if text.startswith("portal-station-") else f"portal-station-{text}"


def _canonical_station_json(encoded: object) -> str:
    values = json.loads(str(encoded))
    return json.dumps(
        sorted(canonical_station_id(value) for value in values), separators=(",", ":")
    )


def resolve_local_interval(
    departure_time: datetime, interval_start_seconds: int
) -> tuple[str, int, str]:
    """Resolve a SUMO offset to Portland weekday/bucket with DST-safe timezone rules."""
    if departure_time.tzinfo is None:
        raise ValueError("simulation departure time must be timezone-aware")
    local = (departure_time + timedelta(seconds=interval_start_seconds)).astimezone(
        PACIFIC
    )
    if local.second or local.microsecond or local.minute % 15:
        raise ValueError(
            "simulation interval does not begin on a local 15-minute bucket"
        )
    if local.weekday() >= 5:
        raise ValueError("weekend historical profiles are not synthesized")
    return (
        WEEKDAYS[local.weekday()],
        local.hour * 60 + local.minute,
        local.date().isoformat(),
    )


def date_support_class(observed: int, possible: int) -> str:
    if possible <= 0 or observed < 0 or observed > possible:
        raise ValueError("invalid historical date support")
    coverage = observed / possible
    if coverage >= 0.90:
        return DIRECT
    if coverage >= 0.75:
        return SUPPLEMENTAL
    return INSUFFICIENT


def aggregate_station_profiles(
    station_dates: pd.DataFrame,
    *,
    source_window_start: date,
    source_window_end: date,
    source_corpus_digest: str,
) -> pd.DataFrame:
    """Aggregate exact-date station flow using Phase 1 equal-date methodology."""
    identity = ["station_id", "app_edge_id", "local_date", "bucket_start_minute"]
    if station_dates.duplicated(identity).any():
        raise SumoHistoricalComparisonV2Error("duplicate station/date/bucket identity")
    possible = _possible_weekday_counts(source_window_start, source_window_end)
    rows: list[dict[str, Any]] = []
    grouped = station_dates.groupby(
        ["station_id", "app_edge_id", "direction", "weekday", "bucket_start_minute"],
        sort=True,
        observed=True,
    )
    for (station_id, edge_id, direction, weekday, bucket), values in grouped:
        flow = pd.to_numeric(values["flow_vph"], errors="raise")
        detectors = pd.to_numeric(values["detector_count"], errors="raise")
        samples = pd.to_numeric(values["sample_count"], errors="raise")
        observed = int(values["local_date"].nunique())
        possible_dates = possible[str(weekday)]
        support = date_support_class(observed, possible_dates)
        key = "\x1f".join(
            (
                HISTORICAL_STATION_PROFILE_VERSION,
                source_corpus_digest,
                str(station_id),
                str(edge_id),
                str(weekday),
                str(int(bucket)),
            )
        )
        rows.append(
            {
                "schema_version": 1,
                "record_id": f"portal.station-profile.{hashlib.sha256(key.encode()).hexdigest()[:48]}",
                "station_id": canonical_station_id(station_id),
                "app_edge_id": str(edge_id),
                "direction": str(direction),
                "weekday": str(weekday),
                "bucket_start_minute": int(bucket),
                "interval_seconds": 900,
                "possible_date_count": possible_dates,
                "observed_date_count": observed,
                "coverage_fraction": observed / possible_dates,
                "date_support_class": support,
                "flow_direct_calibration_eligible": support == DIRECT,
                "flow_vph_mean": float(flow.mean()),
                "flow_vph_p10": float(flow.quantile(0.10)),
                "flow_vph_p50": float(flow.quantile(0.50)),
                "flow_vph_p85": float(flow.quantile(0.85)),
                "flow_vph_p90": float(flow.quantile(0.90)),
                "flow_vph_p95": float(flow.quantile(0.95)),
                "detector_support_count_min": int(detectors.min()),
                "detector_support_count_p50": float(detectors.quantile(0.50)),
                "detector_support_count_max": int(detectors.max()),
                "sample_count_min": int(samples.min()),
                "sample_count_p50": float(samples.quantile(0.50)),
                "sample_count_max": int(samples.max()),
                "source_corpus_digest": source_corpus_digest,
                "profile_version": HISTORICAL_STATION_PROFILE_VERSION,
                "evidence_level": "derived_historical_station_profile_input",
                "calibration_status": "not_calibrated",
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=[field.name for field in STATION_PROFILE_SCHEMA])
    return frame.sort_values(
        ["station_id", "weekday", "bucket_start_minute", "app_edge_id"],
        kind="mergesort",
    ).reset_index(drop=True)


def reconstruct_app_edge_date_flow(station_dates: pd.DataFrame) -> pd.DataFrame:
    """Rebuild Phase 1 app-edge/date FLOW from the station/date intermediate."""
    rows: list[dict[str, Any]] = []
    grouped = station_dates.groupby(
        ["app_edge_id", "direction", "local_date", "weekday", "bucket_start_minute"],
        sort=True,
        observed=True,
    )
    for (edge, direction, local_date, weekday, bucket), values in grouped:
        station_ids = tuple(sorted(values["station_id"].map(canonical_station_id)))
        rows.append(
            {
                "app_edge_id": str(edge),
                "direction": str(direction),
                "local_date": local_date,
                "weekday": str(weekday),
                "bucket_start_minute": int(bucket),
                "volume_count": float(statistics.median(values["volume_count"])),
                "flow_vph": float(statistics.median(values["flow_vph"])),
                "station_count": len(values),
                "detector_count": int(values["detector_count"].sum()),
                "sample_count": int(values["sample_count"].sum()),
                "source_station_ids_json": json.dumps(
                    station_ids, separators=(",", ":")
                ),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values(
            ["app_edge_id", "local_date", "bucket_start_minute"], kind="mergesort"
        )
        .reset_index(drop=True)
    )


def reconstruct_app_edge_flow_profiles(edge_dates: pd.DataFrame) -> pd.DataFrame:
    """Rebuild Phase 1 weekday FLOW distributions after date-level station medians."""
    rows: list[dict[str, Any]] = []
    grouped = edge_dates.groupby(
        ["app_edge_id", "direction", "weekday", "bucket_start_minute"],
        sort=True,
        observed=True,
    )
    for (edge, direction, weekday, bucket), values in grouped:
        flow = pd.to_numeric(values["flow_vph"], errors="raise")
        volume = pd.to_numeric(values["volume_count"], errors="raise")
        station_ids = sorted(
            {
                canonical_station_id(station)
                for encoded in values["source_station_ids_json"]
                for station in json.loads(encoded)
            }
        )
        rows.append(
            {
                "app_edge_id": str(edge),
                "direction": str(direction),
                "weekday": str(weekday),
                "bucket_start_minute": int(bucket),
                "sample_days": len(values),
                "volume_sample_days": len(volume),
                "volume_count_mean": float(volume.mean()),
                "volume_count_p50": float(volume.quantile(0.50)),
                "volume_count_p85": float(volume.quantile(0.85)),
                "volume_count_p90": float(volume.quantile(0.90)),
                "volume_count_p95": float(volume.quantile(0.95)),
                "flow_vph_mean": float(flow.mean()),
                "flow_vph_p50": float(flow.quantile(0.50)),
                "flow_vph_p85": float(flow.quantile(0.85)),
                "flow_vph_p90": float(flow.quantile(0.90)),
                "flow_vph_p95": float(flow.quantile(0.95)),
                "source_station_ids_json": json.dumps(
                    station_ids, separators=(",", ":")
                ),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values(
            ["app_edge_id", "weekday", "bucket_start_minute"], kind="mergesort"
        )
        .reset_index(drop=True)
    )


def compare_roundtrip_rows(
    reconstructed: pd.DataFrame,
    accepted: pd.DataFrame,
    *,
    identity: list[str],
    numeric_fields: list[str],
    exact_fields: list[str],
) -> dict[str, Any]:
    """Compare a bounded round-trip partition without mutating either input."""
    left = reconstructed.copy()
    right = accepted.copy()
    for frame in (left, right):
        if "source_station_ids_json" in frame:
            frame["source_station_ids_json"] = frame["source_station_ids_json"].map(
                _canonical_station_json
            )
        if "local_date" in frame:
            frame["local_date"] = pd.to_datetime(frame["local_date"]).dt.date
    if left.duplicated(identity).any() or right.duplicated(identity).any():
        raise SumoHistoricalComparisonV2Error(
            "duplicate round-trip comparison identity"
        )
    merged = left.merge(
        right,
        on=identity,
        how="outer",
        suffixes=("_reconstructed", "_accepted"),
        indicator=True,
        validate="one_to_one",
    )
    reason_counts: Counter[str] = Counter()
    max_difference = {field: 0.0 for field in numeric_fields}
    mismatch_rows = 0
    matched_rows = 0
    for row in merged.to_dict(orient="records"):
        reasons: set[str] = set()
        if row["_merge"] == "left_only":
            reasons.add("missing_from_accepted")
        elif row["_merge"] == "right_only":
            reasons.add("missing_from_reconstructed")
        else:
            for field in numeric_fields:
                reconstructed_value = float(row[f"{field}_reconstructed"])
                accepted_value = float(row[f"{field}_accepted"])
                difference = abs(reconstructed_value - accepted_value)
                max_difference[field] = max(max_difference[field], difference)
                if not math.isclose(
                    reconstructed_value,
                    accepted_value,
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    reasons.add(f"numeric_mismatch:{field}")
            for field in exact_fields:
                if row[f"{field}_reconstructed"] != row[f"{field}_accepted"]:
                    reasons.add(f"exact_mismatch:{field}")
        if reasons:
            mismatch_rows += 1
            reason_counts.update(reasons)
        else:
            matched_rows += 1
    return {
        "reconstructed_row_count": len(left),
        "accepted_row_count": len(right),
        "exact_or_numerically_equivalent_match_count": matched_rows,
        "mismatch_row_count": mismatch_rows,
        "missing_from_reconstructed_count": int(
            (merged["_merge"] == "right_only").sum()
        ),
        "missing_from_accepted_count": int((merged["_merge"] == "left_only").sum()),
        "maximum_numeric_difference": max_difference,
        "mismatch_reason_counts": dict(sorted(reason_counts.items())),
    }


def build_station_comparison_frame(
    *,
    station_profiles: pd.DataFrame,
    edge_profiles: pd.DataFrame,
    quality_policy: pd.DataFrame,
    station_policy: pd.DataFrame,
    telemetry: pd.DataFrame,
    intervals: pd.DataFrame,
    expected_policy_digest: str,
    expected_observation_plan_digest: str,
) -> pd.DataFrame:
    """Build retained station candidates and explicit primary-flow eligibility."""
    station_profiles = station_profiles.copy()
    station_policy = station_policy.copy()
    telemetry = telemetry.copy()
    station_profiles["station_id"] = station_profiles["station_id"].map(
        canonical_station_id
    )
    station_policy["station_id"] = station_policy["station_id"].map(
        canonical_station_id
    )
    telemetry["station_id"] = telemetry["station_id"].map(canonical_station_id)
    station_identity = [
        "station_id",
        "app_edge_id",
        "weekday",
        "bucket_start_minute",
    ]
    if station_profiles.duplicated(station_identity).any():
        raise SumoHistoricalComparisonV2Error("duplicate historical station profile")
    if station_policy["station_id"].duplicated().any():
        raise SumoHistoricalComparisonV2Error("duplicate station policy identity")
    telemetry_identity = ["variant", "station_id", "interval_start_seconds"]
    if telemetry.duplicated(telemetry_identity).any():
        raise SumoHistoricalComparisonV2Error(
            "duplicate SUMO station telemetry identity"
        )

    edge_identity = ["app_edge_id", "weekday", "bucket_start_minute"]
    if (
        edge_profiles.duplicated(edge_identity).any()
        or quality_policy.duplicated(edge_identity).any()
    ):
        raise SumoHistoricalComparisonV2Error("duplicate historical app-edge profile")
    edge_evidence = edge_profiles[
        edge_identity + ["record_id", "source_station_ids_json"]
    ].rename(columns={"record_id": "historical_app_edge_profile_id"})
    edge_evidence = edge_evidence.merge(
        quality_policy[
            edge_identity
            + [
                "date_support_class",
                "flow_direct_calibration_eligible",
                "observed_date_count",
                "possible_date_count",
                "coverage_fraction",
            ]
        ].rename(
            columns={
                "date_support_class": "app_edge_date_support_class",
                "flow_direct_calibration_eligible": "app_edge_flow_direct_eligible",
                "observed_date_count": "app_edge_observed_date_count",
                "possible_date_count": "app_edge_possible_date_count",
                "coverage_fraction": "app_edge_coverage_fraction",
            }
        ),
        on=edge_identity,
        how="inner",
        validate="one_to_one",
    )

    variants = pd.DataFrame({"variant": sorted(telemetry["variant"].dropna().unique())})
    if variants.empty:
        raise SumoHistoricalComparisonV2Error("station telemetry contains no variants")
    candidate = station_policy.copy()
    candidate["_join"] = 1
    catalog = intervals.rename(
        columns={"interval_end_seconds": "interval_end_seconds_expected"}
    ).copy()
    catalog["_join"] = 1
    variants["_join"] = 1
    candidate = candidate.merge(catalog, on="_join", validate="many_to_many")
    candidate = candidate.merge(variants, on="_join", validate="many_to_many").drop(
        columns="_join"
    )
    candidate = candidate.merge(
        station_profiles,
        on=station_identity,
        how="left",
        validate="many_to_one",
        suffixes=("_mapping", "_historical"),
    )
    candidate = candidate.merge(
        edge_evidence,
        on=edge_identity,
        how="left",
        validate="many_to_one",
    )
    telemetry_columns = telemetry_identity + [
        "app_edge_id",
        "interval_end_seconds",
        "interval_duration_seconds",
        "crossing_count",
        "flow_vph",
        "contributing_vehicle_count",
        "resolved_direct_departure_count",
        "unresolved_direct_departure_count",
        "flow_measurement_complete",
        "policy_content_digest",
        "observation_plan_digest",
        "run_identity",
        "network_version",
        "sumo_version",
        "evidence_level",
        "calibration_status",
    ]
    telemetry_selected = telemetry[telemetry_columns].rename(
        columns={"app_edge_id": "telemetry_app_edge_id"}
    )
    candidate = candidate.merge(
        telemetry_selected,
        on=telemetry_identity,
        how="left",
        validate="one_to_one",
    )
    candidate["historical_flow_vph"] = candidate["flow_vph_p50"]
    candidate["direction"] = candidate["direction_historical"]
    candidate["sumo_flow_vph"] = candidate["crossing_count"] * 4.0
    candidate["signed_error_vph"] = (
        candidate["sumo_flow_vph"] - candidate["historical_flow_vph"]
    )
    candidate["absolute_error_vph"] = candidate["signed_error_vph"].abs()
    positive = candidate["historical_flow_vph"].gt(0)
    candidate["absolute_percentage_error"] = (
        candidate["absolute_error_vph"] / candidate["historical_flow_vph"]
    ).where(positive)
    candidate["flow_ratio"] = (
        candidate["sumo_flow_vph"] / candidate["historical_flow_vph"]
    ).where(positive)
    candidate["sumo_within_historical_p10_p90"] = (
        candidate["sumo_flow_vph"].ge(candidate["flow_vph_p10"])
        & candidate["sumo_flow_vph"].le(candidate["flow_vph_p90"])
    ).where(candidate["sumo_flow_vph"].notna() & candidate["flow_vph_p10"].notna())
    candidate["provenance_matches"] = (
        candidate["policy_content_digest"].eq(expected_policy_digest)
        & candidate["observation_plan_digest"].eq(expected_observation_plan_digest)
        & candidate["telemetry_app_edge_id"].eq(candidate["app_edge_id"])
    )

    reasons: list[list[str]] = []
    eligible: list[bool] = []
    for row in candidate.to_dict(orient="records"):
        row_reasons: list[str] = []
        telemetry_exists = not _missing(row.get("interval_end_seconds"))
        if telemetry_exists and not bool(row.get("provenance_matches")):
            row_reasons.append("provenance_mismatch")
        if _missing(row.get("historical_app_edge_profile_id")):
            row_reasons.append("time_profile_missing")
        if _missing(row.get("record_id")):
            row_reasons.append("historical_missing")
        elif (
            row.get("date_support_class") != DIRECT
            or not bool(row.get("flow_direct_calibration_eligible"))
            or row.get("app_edge_date_support_class") != DIRECT
            or not bool(row.get("app_edge_flow_direct_eligible"))
        ):
            row_reasons.append("historical_insufficient_support")
        if not bool(row.get("flow_measurement_eligible")):
            row_reasons.append("station_mapping_not_accepted")
        if not telemetry_exists:
            row_reasons.append("sumo_station_missing")
        else:
            duration = row.get("interval_duration_seconds")
            if duration != 900:
                row_reasons.append("sumo_interval_incomplete")
            if not bool(row.get("flow_measurement_complete")):
                row_reasons.append("sumo_flow_incomplete")
            if int(row.get("unresolved_direct_departure_count") or 0) > 0:
                row_reasons.append("unresolved_direct_departure")
        normalized = [
            reason for reason in EXCLUSION_PRECEDENCE if reason in row_reasons
        ]
        reasons.append(normalized)
        eligible.append(not normalized)
    candidate["flow_evidence_eligible"] = eligible
    candidate["primary_flow_scoring"] = candidate["flow_evidence_eligible"] & candidate[
        "variant"
    ].eq("baseline")
    candidate["primary_exclusion_reason"] = [
        values[0] if values else None for values in reasons
    ]
    candidate["primary_exclusion_reasons_json"] = [
        json.dumps(values, separators=(",", ":")) for values in reasons
    ]
    candidate["point_speed_primary_eligible"] = False
    candidate["comparison_record_id"] = [
        _record_id(
            "station",
            str(row.variant),
            str(row.station_id),
            str(int(row.interval_start_seconds)),
        )
        for row in candidate.itertuples(index=False)
    ]
    ordered = ["variant", "interval_start_seconds", "station_id"]
    return candidate.sort_values(ordered, kind="mergesort").reset_index(drop=True)


def build_app_edge_comparison_frame(
    *,
    station_comparisons: pd.DataFrame,
    edge_profiles: pd.DataFrame,
    quality_policy: pd.DataFrame,
    intervals: pd.DataFrame,
    variants: list[str],
    graph_edges: pd.DataFrame,
) -> pd.DataFrame:
    """Apply the all-contributing-stations rule and Phase 1 edge-flow target."""
    identity = ["app_edge_id", "weekday", "bucket_start_minute"]
    if (
        edge_profiles.duplicated(identity).any()
        or quality_policy.duplicated(identity).any()
    ):
        raise SumoHistoricalComparisonV2Error("duplicate app-edge historical identity")
    historical = edge_profiles[
        identity
        + [
            "record_id",
            "direction",
            "flow_vph_mean",
            "flow_vph_p50",
            "flow_vph_p85",
            "flow_vph_p90",
            "flow_vph_p95",
            "source_station_ids_json",
        ]
    ].merge(
        quality_policy[
            identity
            + [
                "date_support_class",
                "flow_direct_calibration_eligible",
                "observed_date_count",
                "possible_date_count",
                "coverage_fraction",
            ]
        ],
        on=identity,
        how="inner",
        validate="one_to_one",
    )
    catalog = intervals[
        [
            "interval_start_seconds",
            "interval_end_seconds",
            "weekday",
            "bucket_start_minute",
            "simulation_local_date",
        ]
    ]
    candidates = catalog.merge(
        historical,
        on=["weekday", "bucket_start_minute"],
        how="inner",
        validate="many_to_many",
    )
    variant_frame = pd.DataFrame({"variant": sorted(variants), "_join": 1})
    candidates["_join"] = 1
    candidates = candidates.merge(variant_frame, on="_join").drop(columns="_join")
    graph = graph_edges.rename(
        columns={"edge_id": "app_edge_id", "ref": "road_ref", "road_name": "road_name"}
    )
    if graph["app_edge_id"].duplicated().any():
        raise SumoHistoricalComparisonV2Error("graph edge IDs are not unique")
    candidates = candidates.merge(
        graph[["app_edge_id", "road_ref", "road_name", "road_class"]],
        on="app_edge_id",
        how="left",
        validate="many_to_one",
    )
    rows: list[dict[str, Any]] = []
    lookup = station_comparisons.groupby(
        ["variant", "interval_start_seconds", "app_edge_id"], sort=False
    )
    for raw in candidates.sort_values(
        ["variant", "interval_start_seconds", "app_edge_id"], kind="mergesort"
    ).to_dict(orient="records"):
        required = tuple(
            sorted(
                canonical_station_id(value)
                for value in json.loads(raw["source_station_ids_json"])
            )
        )
        group_key = (raw["variant"], raw["interval_start_seconds"], raw["app_edge_id"])
        group = (
            lookup.get_group(group_key)
            if group_key in lookup.groups
            else pd.DataFrame()
        )
        by_station = {str(row.station_id): row for row in group.itertuples(index=False)}
        present = [station for station in required if station in by_station]
        eligible_stations = [
            station
            for station in required
            if station in by_station
            and bool(by_station[station].flow_evidence_eligible)
        ]
        complete = len(eligible_stations) == len(required) and bool(required)
        partial_values = [
            float(by_station[station].sumo_flow_vph)
            for station in required
            if station in by_station and not _missing(by_station[station].sumo_flow_vph)
        ]
        simulated = statistics.median(partial_values) if complete else None
        reasons: set[str] = set()
        if (
            raw["date_support_class"] != DIRECT
            or not raw["flow_direct_calibration_eligible"]
        ):
            reasons.add("historical_insufficient_support")
        if not required:
            reasons.add("historical_missing")
        if len(present) != len(required):
            reasons.add("sumo_station_missing")
        for station in required:
            station_row = by_station.get(station)
            if station_row is None:
                continue
            reasons.update(json.loads(station_row.primary_exclusion_reasons_json))
        if not complete and not reasons:
            reasons.add("sumo_flow_incomplete")
        normalized = [reason for reason in EXCLUSION_PRECEDENCE if reason in reasons]
        evidence_eligible = not normalized and complete
        historical_target = float(raw["flow_vph_p50"])
        signed = simulated - historical_target if simulated is not None else None
        row = {
            **raw,
            "comparison_record_id": _record_id(
                "app-edge",
                str(raw["variant"]),
                str(raw["app_edge_id"]),
                str(raw["interval_start_seconds"]),
            ),
            "required_station_ids_json": json.dumps(required, separators=(",", ":")),
            "required_station_count": len(required),
            "present_station_count": len(present),
            "eligible_station_count": len(eligible_stations),
            "all_contributing_stations_complete": complete,
            "historical_flow_vph": historical_target,
            "sumo_flow_vph": simulated,
            "diagnostic_partial_sumo_flow_vph": (
                statistics.median(partial_values) if partial_values else None
            ),
            "signed_error_vph": signed,
            "absolute_error_vph": abs(signed) if signed is not None else None,
            "absolute_percentage_error": (
                abs(signed) / historical_target
                if signed is not None and historical_target > 0
                else None
            ),
            "flow_ratio": (
                simulated / historical_target
                if simulated is not None and historical_target > 0
                else None
            ),
            "flow_evidence_eligible": evidence_eligible,
            "primary_flow_scoring": evidence_eligible and raw["variant"] == "baseline",
            "primary_exclusion_reason": normalized[0] if normalized else None,
            "primary_exclusion_reasons_json": json.dumps(
                normalized, separators=(",", ":")
            ),
            "corridor": corridor_label(raw.get("road_ref"), raw.get("road_name")),
            "point_speed_primary_eligible": False,
        }
        rows.append(row)
    result = pd.DataFrame(rows)
    identity_columns = ["variant", "app_edge_id", "interval_start_seconds"]
    if result.duplicated(identity_columns).any():
        raise SumoHistoricalComparisonV2Error("duplicate app-edge comparison identity")
    return result.sort_values(identity_columns, kind="mergesort").reset_index(drop=True)


def calculate_flow_metrics(
    frame: pd.DataFrame, *, use_primary_flag: bool = True
) -> dict[str, Any]:
    """Return canonical WAPE components plus unweighted flow diagnostics."""
    flag = "primary_flow_scoring" if use_primary_flag else "flow_evidence_eligible"
    eligible = frame.loc[frame[flag].fillna(False)].copy()
    errors = pd.to_numeric(eligible["signed_error_vph"], errors="coerce").dropna()
    historical = pd.to_numeric(
        eligible.loc[errors.index, "historical_flow_vph"], errors="coerce"
    )
    numerator = float(errors.abs().sum())
    denominator = float(historical.abs().sum())
    ratios = pd.to_numeric(eligible["flow_ratio"], errors="coerce").dropna()
    absolute = errors.abs()
    return {
        "contributing_row_count": len(errors),
        "wape_numerator_vph": numerator,
        "wape_denominator_vph": denominator,
        "flow_wape": numerator / denominator if denominator > 0 else None,
        "flow_wape_undefined_reason": (
            None if denominator > 0 else "historical_denominator_is_zero"
        ),
        "flow_mae_vph": float(absolute.mean()) if len(absolute) else None,
        "flow_signed_bias_vph": float(errors.mean()) if len(errors) else None,
        "absolute_error_vph_distribution": _quantile_distribution(absolute),
        "signed_error_vph_distribution": _quantile_distribution(
            errors, quantiles=(0.15, 0.50, 0.85)
        ),
        "flow_ratio_distribution": _quantile_distribution(
            ratios, quantiles=(0.50, 0.85, 0.90, 0.95)
        ),
        "zero_historical_flow_row_count": int(
            (eligible["historical_flow_vph"] == 0).sum()
        ),
    }


def build_scoreboard(
    *,
    stations: pd.DataFrame,
    app_edges: pd.DataFrame,
    comparator_v1_summary: dict[str, Any] | None,
    source_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create baseline primary metrics and variant-separated diagnostic slices."""

    def all_exclusions(frame: pd.DataFrame) -> dict[str, int]:
        values: Counter[str] = Counter()
        for encoded in frame.loc[
            ~frame["flow_evidence_eligible"], "primary_exclusion_reasons_json"
        ]:
            values.update(json.loads(encoded))
        return dict(sorted(values.items()))

    def primary_exclusions(frame: pd.DataFrame) -> dict[str, int]:
        values = frame.loc[
            ~frame["flow_evidence_eligible"], "primary_exclusion_reason"
        ].dropna()
        return {
            str(reason): int(count)
            for reason, count in values.value_counts(sort=False).sort_index().items()
        }

    def breakdown(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        grouper: str | list[str] = columns[0] if len(columns) == 1 else columns
        for key, group in frame.groupby(grouper, sort=True, dropna=False):
            values = key if isinstance(key, tuple) else (key,)
            records.append(
                {
                    **{
                        column: _json_value(value)
                        for column, value in zip(columns, values, strict=True)
                    },
                    **calculate_flow_metrics(group, use_primary_flag=False),
                }
            )
        return records

    station_baseline = stations.loc[stations["variant"].eq("baseline")]
    edge_baseline = app_edges.loc[app_edges["variant"].eq("baseline")]
    high_volume: dict[str, Any] = {}
    for threshold in (1000.0, 2000.0):
        subset = edge_baseline.loc[edge_baseline["historical_flow_vph"].ge(threshold)]
        high_volume[f"historical_flow_at_least_{int(threshold)}_vph"] = (
            calculate_flow_metrics(subset)
        )
    primary_edges = edge_baseline.loc[edge_baseline["primary_flow_scoring"]]
    if len(primary_edges):
        threshold = float(primary_edges["historical_flow_vph"].quantile(0.75))
        high_volume["top_historical_flow_quartile"] = {
            "minimum_historical_flow_vph": threshold,
            **calculate_flow_metrics(
                primary_edges.loc[primary_edges["historical_flow_vph"].ge(threshold)]
            ),
        }
    station_worst = (
        station_baseline.loc[station_baseline["primary_flow_scoring"]]
        .sort_values(
            ["absolute_error_vph", "station_id"],
            ascending=[False, True],
            kind="mergesort",
        )
        .head(25)
    )
    edge_worst = primary_edges.sort_values(
        ["absolute_error_vph", "app_edge_id"],
        ascending=[False, True],
        kind="mergesort",
    ).head(25)
    return {
        "schema_version": 1,
        "artifact_status": "complete_diagnostic",
        "evidence_level": "modeled_uncalibrated",
        "calibration_status": "not_calibrated",
        "methodology": {
            "comparator_algorithm": COMPARATOR_V2_ALGORITHM,
            "eligibility_policy": COMPARATOR_V2_ELIGIBILITY_POLICY,
            "primary_variant": "baseline",
            "station_historical_target": "station exact-date equal-weight flow_vph_p50",
            "station_sumo_target": "physical crossing_count * 4",
            "app_edge_historical_target": "authoritative Phase 1 app-edge exact-date-median distribution flow_vph_p50",
            "app_edge_sumo_target": "median across the identical contributing station identities",
            "all_contributing_stations_required": True,
            "point_speed_primary_eligible": False,
        },
        "station": {
            "candidate_row_count": len(stations),
            "baseline_candidate_row_count": len(station_baseline),
            "primary_eligible_row_count": int(
                station_baseline["primary_flow_scoring"].sum()
            ),
            "diagnostic_eligible_row_count_all_variants": int(
                stations["flow_evidence_eligible"].sum()
            ),
            "baseline_excluded_row_count": int(
                (~station_baseline["flow_evidence_eligible"]).sum()
            ),
            "baseline_primary_exclusions_by_reason": primary_exclusions(
                station_baseline
            ),
            "all_exclusion_reasons_all_variants": all_exclusions(stations),
            "primary_metrics": calculate_flow_metrics(stations),
            "worst_25_primary_residuals": _records(
                station_worst,
                [
                    "station_id",
                    "app_edge_id",
                    "direction_historical",
                    "weekday",
                    "bucket_start_minute",
                    "historical_flow_vph",
                    "sumo_flow_vph",
                    "signed_error_vph",
                    "absolute_error_vph",
                    "flow_ratio",
                ],
            ),
        },
        "app_edge": {
            "candidate_row_count": len(app_edges),
            "baseline_candidate_row_count": len(edge_baseline),
            "primary_eligible_row_count": int(
                edge_baseline["primary_flow_scoring"].sum()
            ),
            "diagnostic_eligible_row_count_all_variants": int(
                app_edges["flow_evidence_eligible"].sum()
            ),
            "baseline_excluded_row_count": int(
                (~edge_baseline["flow_evidence_eligible"]).sum()
            ),
            "baseline_primary_exclusions_by_reason": primary_exclusions(edge_baseline),
            "all_exclusion_reasons_all_variants": all_exclusions(app_edges),
            "primary_metrics": calculate_flow_metrics(app_edges),
            "high_volume_diagnostics": high_volume,
            "worst_25_primary_residuals": _records(
                edge_worst,
                [
                    "app_edge_id",
                    "road_ref",
                    "road_name",
                    "corridor",
                    "direction",
                    "weekday",
                    "bucket_start_minute",
                    "historical_flow_vph",
                    "sumo_flow_vph",
                    "signed_error_vph",
                    "absolute_error_vph",
                    "flow_ratio",
                ],
            ),
        },
        "breakdowns": {
            "variant": breakdown(app_edges, ["variant"]),
            "weekday": breakdown(app_edges, ["variant", "weekday"]),
            "time_bucket": breakdown(app_edges, ["variant", "bucket_start_minute"]),
            "direction": breakdown(app_edges, ["variant", "direction"]),
            "corridor": breakdown(app_edges, ["variant", "corridor"]),
        },
        "integrity": integrity_checks(stations, app_edges),
        "comparator_v1": comparator_v1_summary,
        "source_coverage": source_coverage,
        "comparator_v1_difference": {
            "v1_flow_semantics": "whole-edge (entered + departed) inflow approximation",
            "v2_flow_semantics": "physical station cross-section crossing count",
            "expected_numeric_equivalence": False,
        },
    }


def integrity_checks(stations: pd.DataFrame, app_edges: pd.DataFrame) -> dict[str, int]:
    station_identity = ["variant", "station_id", "interval_start_seconds"]
    edge_identity = ["variant", "app_edge_id", "interval_start_seconds"]
    station_primary = stations.loc[stations["primary_flow_scoring"]]
    edge_primary = app_edges.loc[app_edges["primary_flow_scoring"]]
    return {
        "duplicate_station_comparison_identity_count": int(
            stations.duplicated(station_identity).sum()
        ),
        "duplicate_app_edge_comparison_identity_count": int(
            app_edges.duplicated(edge_identity).sum()
        ),
        "missing_station_id_count": int(stations["station_id"].isna().sum()),
        "unknown_variant_count": int(
            (~stations["variant"].isin(["baseline", "scenario"])).sum()
        ),
        "non_900_second_interval_count": int(
            stations["interval_duration_seconds"].dropna().ne(900).sum()
        ),
        "unresolved_departure_primary_count": int(
            station_primary["unresolved_direct_departure_count"].fillna(0).gt(0).sum()
        ),
        "unaccepted_mapping_primary_count": int(
            (~station_primary["flow_measurement_eligible"].fillna(False)).sum()
        ),
        "scenario_in_primary_count": int(
            (
                stations["primary_flow_scoring"] & stations["variant"].eq("scenario")
            ).sum()
        ),
        "missing_historical_coerced_to_zero_count": int(
            station_primary["historical_flow_vph"].isna().sum()
            + edge_primary["historical_flow_vph"].isna().sum()
        ),
        "weekend_primary_count": int(
            station_primary["weekday"].isin(["saturday", "sunday"]).sum()
        ),
        "point_speed_primary_eligibility_count": int(
            stations["point_speed_primary_eligible"].sum()
            + app_edges["point_speed_primary_eligible"].sum()
        ),
        "partial_station_primary_edge_count": int(
            edge_primary["all_contributing_stations_complete"].eq(False).sum()
        ),
    }


def compare_sumo_station_flows_to_historical(
    *,
    run_directory: Path,
    corpus_root: Path,
    campaign_manifest_path: Path,
    historical_directory: Path,
    quality_policy_directory: Path,
    station_policy_directory: Path,
    graph_edges_path: Path,
    output_directory: Path | None = None,
    comparator_v1_summary_path: Path | None = None,
    station_partition_count: int = 32,
    json_block_size: int = 4 * 1024 * 1024,
    log: Any = print,
) -> ComparatorV2Manifest:
    """Build the atomic Comparator-v2 artifact from accepted immutable inputs."""
    run_directory = run_directory.resolve()
    output_directory = (
        output_directory or run_directory / DEFAULT_OUTPUT_NAME
    ).resolve()
    if output_directory.exists():
        raise SumoHistoricalComparisonV2Error("Comparator-v2 output already exists")
    if station_partition_count < 1:
        raise ValueError("station_partition_count must be positive")

    inputs = _load_and_validate_inputs(
        run_directory=run_directory,
        corpus_root=corpus_root.resolve(),
        historical_directory=historical_directory.resolve(),
        quality_policy_directory=quality_policy_directory.resolve(),
        station_policy_directory=station_policy_directory.resolve(),
        graph_edges_path=graph_edges_path.resolve(),
    )
    pending = output_directory.parent / f".{output_directory.name}.pending"
    if (
        _sha256(campaign_manifest_path.resolve())
        != inputs["historical_manifest"]["source_campaign_manifest_sha256"]
    ):
        raise SumoHistoricalComparisonV2Error("source campaign manifest hash mismatch")
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    try:
        station_profiles, roundtrip = _derive_station_profiles_from_corpus(
            corpus_root=corpus_root.resolve(),
            campaign_manifest_path=campaign_manifest_path.resolve(),
            corpus=inputs["corpus"],
            source_window_start=date.fromisoformat(
                inputs["historical_manifest"]["source_window_start"]
            ),
            source_window_end=date.fromisoformat(
                inputs["historical_manifest"]["source_window_end"]
            ),
            staging_directory=pending,
            partition_count=station_partition_count,
            json_block_size=json_block_size,
            graph_version=inputs["historical_manifest"]["graph_version"],
            expected_mapped_observation_count=int(
                inputs["historical_manifest"]["mapped_observation_count"]
            ),
            accepted_date_level_path=historical_directory.resolve()
            / inputs["historical_manifest"]["date_level"]["relative_path"],
            accepted_profile_path=historical_directory.resolve()
            / inputs["historical_manifest"]["weekday_profiles"]["relative_path"],
            log=log,
        )
        station_profile_path = pending / STATION_PROFILE_FILE
        pq.write_table(
            pa.Table.from_pandas(
                station_profiles,
                schema=STATION_PROFILE_SCHEMA,
                preserve_index=False,
            ),
            station_profile_path,
            compression="zstd",
            use_dictionary=True,
        )
        roundtrip_path = pending / STATION_ROUNDTRIP_FILE
        roundtrip_path.write_text(
            json.dumps(roundtrip, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        station_profile_row_digest = frame_digest(station_profiles)
        station_manifest_payload: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "commute_help_historical_station_flow_profiles",
            "artifact_status": "complete_validated_intermediate",
            "evidence_level": "derived_historical_station_profile_input",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC),
            "producer_name": "historical_station_flow_profile_deriver",
            "producer_version": HISTORICAL_STATION_PROFILE_VERSION,
            "aggregation_semantics": "sum_detectors_within_station_then_equal_date_weekday_distribution",
            "source_corpus_digest": inputs["corpus"].corpus_digest,
            "source_integrity_digest": inputs["corpus"].integrity.index_digest,
            "source_historical_profile_digest": inputs["historical_manifest"][
                "content_digest"
            ],
            "source_date_level_sha256": inputs["historical_manifest"]["date_level"][
                "sha256"
            ],
            "source_weekday_profile_sha256": inputs["historical_manifest"][
                "weekday_profiles"
            ]["sha256"],
            "source_mapped_observation_count": int(
                inputs["historical_manifest"]["mapped_observation_count"]
            ),
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "support_policy_version": inputs["quality_policy_manifest"][
                "policy_version"
            ],
            "profiles_output": file_identity(
                station_profile_path, len(station_profiles)
            ),
            "roundtrip_output": file_identity(roundtrip_path),
            "row_content_sha256": station_profile_row_digest,
        }
        station_manifest_payload["content_digest"] = content_digest(
            station_manifest_payload
        )
        station_manifest = HistoricalStationFlowManifestV1.model_validate(
            station_manifest_payload
        )
        station_manifest_path = pending / STATION_PROFILE_MANIFEST_FILE
        station_manifest_path.write_text(
            canonical_json(station_manifest.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )
        request = inputs["request"]
        departure = datetime.fromisoformat(
            request["request"]["departure_time"].replace("Z", "+00:00")
        )
        intervals = _interval_catalog(inputs["telemetry"], departure)
        stations = build_station_comparison_frame(
            station_profiles=station_profiles,
            edge_profiles=inputs["edge_profiles"],
            quality_policy=inputs["quality_policy"],
            station_policy=inputs["station_policy"],
            telemetry=inputs["telemetry"],
            intervals=intervals,
            expected_policy_digest=inputs["station_policy_manifest"]["content_digest"],
            expected_observation_plan_digest=inputs[
                "telemetry_manifest"
            ].observation_plan_digest,
        )
        app_edges = build_app_edge_comparison_frame(
            station_comparisons=stations,
            edge_profiles=inputs["edge_profiles"],
            quality_policy=inputs["quality_policy"],
            intervals=intervals,
            variants=sorted(inputs["telemetry"]["variant"].unique()),
            graph_edges=inputs["graph_edges"],
        )
        checks = integrity_checks(stations, app_edges)
        if any(checks.values()):
            raise SumoHistoricalComparisonV2Error(
                f"Comparator-v2 integrity checks failed: {checks}"
            )
        v1_summary = _optional_v1_summary(
            run_directory, comparator_v1_summary_path=comparator_v1_summary_path
        )
        scoreboard = build_scoreboard(
            stations=stations,
            app_edges=app_edges,
            comparator_v1_summary=v1_summary,
            source_coverage=_source_coverage(
                edge_profiles=inputs["edge_profiles"],
                quality_policy=inputs["quality_policy"],
                station_policy=inputs["station_policy"],
                station_profiles=station_profiles,
            ),
        )
        station_path = pending / STATION_COMPARISON_FILE
        edge_path = pending / APP_EDGE_COMPARISON_FILE
        pq.write_table(
            pa.Table.from_pandas(stations, preserve_index=False),
            station_path,
            compression="zstd",
            use_dictionary=True,
        )
        pq.write_table(
            pa.Table.from_pandas(app_edges, preserve_index=False),
            edge_path,
            compression="zstd",
            use_dictionary=True,
        )
        scoreboard_path = pending / SCOREBOARD_FILE
        scoreboard_path.write_text(
            json.dumps(_json_value(scoreboard), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path = pending / REPORT_FILE
        report_path.write_text(_render_report(scoreboard), encoding="utf-8")
        station_digest = frame_digest(stations)
        edge_digest = frame_digest(app_edges)
        manifest_payload: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "commute_help_portal_sumo_station_flow_comparison",
            "artifact_status": "complete_diagnostic",
            "evidence_level": "modeled_uncalibrated",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC),
            "producer_name": COMPARATOR_V2_PRODUCER_NAME,
            "producer_version": COMPARATOR_V2_PRODUCER_VERSION,
            "comparator_algorithm": COMPARATOR_V2_ALGORITHM,
            "eligibility_policy_version": COMPARATOR_V2_ELIGIBILITY_POLICY,
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "historical_corpus_digest": inputs["corpus"].corpus_digest,
            "historical_integrity_digest": inputs["corpus"].integrity.index_digest,
            "historical_profile_content_digest": inputs["historical_manifest"][
                "content_digest"
            ],
            "historical_quality_policy_digest": inputs["quality_policy_manifest"][
                "content_digest"
            ],
            "historical_station_profile_version": HISTORICAL_STATION_PROFILE_VERSION,
            "station_policy_content_digest": inputs["station_policy_manifest"][
                "content_digest"
            ],
            "observation_plan_digest": inputs[
                "telemetry_manifest"
            ].observation_plan_digest,
            "station_telemetry_content_digest": inputs[
                "telemetry_manifest"
            ].content_digest,
            "comparator_v1_content_digest": (
                v1_summary.get("content_digest") if v1_summary else None
            ),
            "comparator_v1_summary_sha256": (
                v1_summary.get("summary_sha256") if v1_summary else None
            ),
            "application_run_id": inputs["telemetry_manifest"].application_run_id,
            "run_identity": inputs["telemetry_manifest"].run_identity,
            "graph_version": inputs["telemetry_manifest"].graph_version,
            "network_version": inputs["telemetry_manifest"].network_version,
            "sumo_version": inputs["telemetry_manifest"].sumo_version,
            "variants": tuple(sorted(inputs["telemetry"]["variant"].unique())),
            "station_profiles_output": file_identity(
                station_profile_path, len(station_profiles)
            ),
            "station_profiles_manifest": file_identity(station_manifest_path),
            "station_roundtrip_output": file_identity(roundtrip_path),
            "station_profile_content_digest": station_manifest.content_digest,
            "station_comparisons_output": file_identity(station_path, len(stations)),
            "app_edge_comparisons_output": file_identity(edge_path, len(app_edges)),
            "scoreboard_output": file_identity(scoreboard_path),
            "report_output": file_identity(report_path),
            "station_row_content_sha256": station_digest,
            "app_edge_row_content_sha256": edge_digest,
        }
        manifest_payload["content_digest"] = content_digest(manifest_payload)
        manifest = ComparatorV2Manifest.model_validate(manifest_payload)
        (pending / MANIFEST_FILE).write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        promote_completed_pending(pending, output_directory)
        return manifest
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def _load_and_validate_inputs(
    *,
    run_directory: Path,
    corpus_root: Path,
    historical_directory: Path,
    quality_policy_directory: Path,
    station_policy_directory: Path,
    graph_edges_path: Path,
) -> dict[str, Any]:
    corpus = CalibrationObservationCorpusV2.model_validate_json(
        (corpus_root / "corpus-manifest.json").read_bytes()
    )
    if (
        corpus.materialization_state != "complete_unvalidated"
        or corpus.integrity is None
    ):
        raise SumoHistoricalComparisonV2Error(
            "historical corpus is not frozen and reconciled"
        )
    historical_manifest = _read_json(
        historical_directory / "historical-calibration-manifest.json"
    )
    quality_manifest = _read_json(
        quality_policy_directory / "quality-policy-manifest.json"
    )
    station_policy_manifest = _read_json(
        station_policy_directory / "station-cross-section-policy-manifest.json"
    )
    telemetry_path = (
        run_directory / "station-telemetry-15m" / "station-telemetry-15m.parquet"
    )
    telemetry_manifest_path = (
        run_directory / "station-telemetry-15m" / "station-telemetry-manifest.json"
    )
    telemetry_manifest = StationTelemetryManifestV1.model_validate_json(
        telemetry_manifest_path.read_bytes()
    )
    request = _read_json(run_directory / "request.json")
    run_result = _read_json(run_directory / "result.json")
    if request.get("run_identity") != telemetry_manifest.run_identity:
        raise SumoHistoricalComparisonV2Error(
            "request and station telemetry run identity differ"
        )
    if historical_manifest["source_corpus_digest"] != corpus.corpus_digest:
        raise SumoHistoricalComparisonV2Error(
            "historical profile source corpus mismatch"
        )
    if (
        quality_manifest["source_phase_1_3_content_digest"]
        != historical_manifest["content_digest"]
    ):
        raise SumoHistoricalComparisonV2Error(
            "historical quality policy lineage mismatch"
        )
    if (
        station_policy_manifest["content_digest"]
        != telemetry_manifest.policy_content_digest
    ):
        raise SumoHistoricalComparisonV2Error(
            "station policy and telemetry provenance mismatch"
        )
    if station_policy_manifest["graph_version"] != telemetry_manifest.graph_version:
        raise SumoHistoricalComparisonV2Error(
            "station policy and telemetry graph versions differ"
        )
    if (
        station_policy_manifest["sumo_network_version"]
        != telemetry_manifest.network_version
    ):
        raise SumoHistoricalComparisonV2Error(
            "station policy and telemetry network versions differ"
        )
    if (
        telemetry_manifest.direct_departure_policy
        != "native_departure_child_or_station_local_incomplete"
    ):
        raise SumoHistoricalComparisonV2Error(
            "station telemetry predates Phase 2.2f station-local completeness"
        )
    if not telemetry_manifest.departure_provenance_version:
        raise SumoHistoricalComparisonV2Error(
            "station telemetry lacks departure provenance version"
        )
    if run_result.get("status") == "failed":
        recovery_path = (
            run_directory / "station-telemetry-15m" / "station-telemetry-recovery.json"
        )
        if not recovery_path.is_file():
            raise SumoHistoricalComparisonV2Error(
                "failed wrapper run lacks validated station telemetry recovery lineage"
            )
        recovery = StationTelemetryRecoveryManifestV1.model_validate_json(
            recovery_path.read_bytes()
        )
        recovery_semantic = recovery.model_dump(mode="json")
        recovery_claimed_digest = recovery_semantic.pop("content_digest")
        recovery_semantic.pop("generated_at")
        if (
            recovery.run_identity != telemetry_manifest.run_identity
            or recovery.source_run_wrapper_error != run_result.get("error")
            or recovery.station_telemetry_content_digest
            != telemetry_manifest.content_digest
            or recovery.station_telemetry_output_sha256
            != telemetry_manifest.output.sha256
            or hashlib.sha256(canonical_json(recovery_semantic).encode()).hexdigest()
            != recovery_claimed_digest
        ):
            raise SumoHistoricalComparisonV2Error(
                "station telemetry recovery lineage does not match failed run evidence"
            )
    elif run_result.get("status") != "completed":
        raise SumoHistoricalComparisonV2Error(
            "station telemetry source run has no accepted terminal status"
        )
    _verify_file(
        historical_directory / historical_manifest["weekday_profiles"]["relative_path"],
        historical_manifest["weekday_profiles"],
    )
    _verify_file(
        quality_policy_directory
        / quality_manifest["output_profile_status"]["relative_path"],
        quality_manifest["output_profile_status"],
    )
    _verify_file(
        station_policy_directory
        / station_policy_manifest["policy_output"]["relative_path"],
        station_policy_manifest["policy_output"],
    )
    _verify_file(telemetry_path, telemetry_manifest.output.model_dump())
    if _sha256(graph_edges_path) != historical_manifest["graph_edges_sha256"]:
        raise SumoHistoricalComparisonV2Error("graph edge artifact hash mismatch")

    telemetry = pq.read_table(telemetry_path).to_pandas()
    station_policy = pq.read_table(
        station_policy_directory / "station-cross-section-policy.parquet"
    ).to_pandas()
    required_telemetry = {
        "resolved_direct_departure_count",
        "unresolved_direct_departure_count",
        "flow_measurement_complete",
    }
    if not required_telemetry.issubset(telemetry.columns):
        raise SumoHistoricalComparisonV2Error(
            "station telemetry lacks Phase 2.2f completeness columns"
        )
    variants = set(telemetry["variant"].unique())
    if variants != {"baseline", "scenario"}:
        raise SumoHistoricalComparisonV2Error(
            "station telemetry must preserve baseline and scenario separately"
        )
    station_identity = ["variant", "station_id", "interval_start_seconds"]
    if telemetry.duplicated(station_identity).any():
        raise SumoHistoricalComparisonV2Error(
            "station telemetry contains duplicate station interval identities"
        )
    if telemetry["interval_duration_seconds"].ne(900).any():
        raise SumoHistoricalComparisonV2Error(
            "station telemetry contains a partial interval"
        )
    expected_flow = telemetry["crossing_count"] * 4.0
    if not telemetry["flow_vph"].sub(expected_flow).abs().le(1e-9).all():
        raise SumoHistoricalComparisonV2Error(
            "station telemetry flow does not equal crossings times four"
        )
    complete_expected = telemetry["unresolved_direct_departure_count"].eq(0)
    if not telemetry["flow_measurement_complete"].eq(complete_expected).all():
        raise SumoHistoricalComparisonV2Error(
            "station telemetry completeness contradicts unresolved counts"
        )
    if station_policy["station_id"].duplicated().any():
        raise SumoHistoricalComparisonV2Error(
            "station cross-section policy contains duplicate station identities"
        )
    expected_stations = set(
        station_policy.loc[
            station_policy["flow_measurement_eligible"], "station_id"
        ].map(canonical_station_id)
    )
    policy_edge_by_station = {
        canonical_station_id(row.station_id): str(row.app_edge_id)
        for row in station_policy.loc[
            station_policy["flow_measurement_eligible"]
        ].itertuples(index=False)
    }
    for (variant, interval_start), group in telemetry.groupby(
        ["variant", "interval_start_seconds"], sort=True
    ):
        observed_stations = set(group["station_id"].map(canonical_station_id))
        if observed_stations != expected_stations:
            raise SumoHistoricalComparisonV2Error(
                "station telemetry does not contain exactly the accepted station "
                f"plan for {variant} interval {interval_start}"
            )
        mismatched_edges = group.loc[
            group.apply(
                lambda row: (
                    policy_edge_by_station[canonical_station_id(row["station_id"])]
                    != str(row["app_edge_id"])
                ),
                axis=1,
            )
        ]
        if not mismatched_edges.empty:
            raise SumoHistoricalComparisonV2Error(
                "station telemetry app-edge identities disagree with policy"
            )
    return {
        "corpus": corpus,
        "historical_manifest": historical_manifest,
        "quality_policy_manifest": quality_manifest,
        "station_policy_manifest": station_policy_manifest,
        "telemetry_manifest": telemetry_manifest,
        "request": request,
        "edge_profiles": pq.read_table(
            historical_directory / "edge-time-distributions.parquet"
        ).to_pandas(),
        "quality_policy": pq.read_table(
            quality_policy_directory / "quality-policy-profile-status.parquet"
        ).to_pandas(),
        "station_policy": station_policy,
        "telemetry": telemetry,
        "graph_edges": pq.read_table(
            graph_edges_path,
            columns=["edge_id", "ref", "road_name", "road_class"],
        ).to_pandas(),
    }


def _derive_station_profiles_from_corpus(
    *,
    corpus_root: Path,
    campaign_manifest_path: Path,
    corpus: CalibrationObservationCorpusV2,
    source_window_start: date,
    source_window_end: date,
    staging_directory: Path,
    partition_count: int,
    json_block_size: int,
    graph_version: str,
    expected_mapped_observation_count: int,
    accepted_date_level_path: Path,
    accepted_profile_path: Path,
    log: Any,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    campaign = _read_json(campaign_manifest_path)
    groups, _, _ = _group_shards(corpus_root, corpus, campaign["chunks"])
    work = Path(
        tempfile.mkdtemp(prefix=".station-profile-work-", dir=staging_directory)
    )
    writers: dict[int, pq.ParquetWriter] = {}
    paths: dict[int, Path] = {}
    mapped_count = 0
    try:
        for number, (week, shards) in enumerate(sorted(groups.items()), start=1):
            station_groups: dict[tuple[Any, ...], Any] = {}
            for shard_path, shard in sorted(shards, key=lambda item: item[1].shard_id):
                stream_path = shard_path.parent / shard.observation_stream.relative_path
                for batch in _observation_batches(stream_path, json_block_size):
                    _, mapped, _ = _accumulate_batch(
                        batch, station_groups, graph_version=graph_version
                    )
                    mapped_count += mapped
            partition_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
            seen: set[tuple[Any, ...]] = set()
            for key, accumulator in sorted(station_groups.items()):
                observed_date, weekday, minute, edge_id, direction, station_id = key
                canonical_station = canonical_station_id(station_id)
                identity = (canonical_station, edge_id, observed_date, minute)
                if identity in seen:
                    raise SumoHistoricalComparisonV2Error(
                        "duplicate station/date/bucket while deriving profiles"
                    )
                seen.add(identity)
                row = {
                    "station_id": canonical_station,
                    "app_edge_id": str(edge_id),
                    "direction": str(direction),
                    "local_date": observed_date,
                    "weekday": str(weekday),
                    "bucket_start_minute": int(minute),
                    "volume_count": float(accumulator.volume_count),
                    "flow_vph": float(accumulator.flow_vph),
                    "detector_count": len(accumulator.detector_ids),
                    "sample_count": int(accumulator.sample_count),
                }
                partition = (
                    int.from_bytes(
                        hashlib.sha256(str(edge_id).encode()).digest()[:8], "big"
                    )
                    % partition_count
                )
                partition_rows[partition].append(row)
            for partition, rows in partition_rows.items():
                writer = writers.get(partition)
                if writer is None:
                    path = work / f"station-{partition:03d}.parquet"
                    writer = pq.ParquetWriter(
                        path, STATION_DATE_SCHEMA, compression="zstd"
                    )
                    writers[partition] = writer
                    paths[partition] = path
                writer.write_table(
                    pa.Table.from_pylist(rows, schema=STATION_DATE_SCHEMA)
                )
            log(
                f"STATION PROFILES source weeks={number:,}/{len(groups):,} "
                f"week={week} mapped_observations={mapped_count:,}"
            )
        for writer in writers.values():
            writer.close()
        writers.clear()
        if mapped_count != expected_mapped_observation_count:
            raise SumoHistoricalComparisonV2Error(
                "station-profile derivation mapped-observation accounting mismatch"
            )
        accepted_paths = _partition_accepted_date_level(
            accepted_date_level_path=accepted_date_level_path,
            work=work,
            partition_count=partition_count,
        )
        accepted_profiles = pq.read_table(
            accepted_profile_path,
            columns=[
                "app_edge_id",
                "direction",
                "weekday",
                "bucket_start_minute",
                "sample_days",
                "volume_sample_days",
                "volume_count_mean",
                "volume_count_p50",
                "volume_count_p85",
                "volume_count_p90",
                "volume_count_p95",
                "flow_vph_mean",
                "flow_vph_p50",
                "flow_vph_p85",
                "flow_vph_p90",
                "flow_vph_p95",
                "source_station_ids_json",
            ],
        ).to_pandas()
        profiles: list[pd.DataFrame] = []
        date_summaries: list[dict[str, Any]] = []
        reconstructed_profile_frames: list[pd.DataFrame] = []
        all_partitions = sorted(set(paths) | set(accepted_paths))
        for number, partition in enumerate(all_partitions, start=1):
            path = paths.get(partition)
            if path is None:
                frame = pd.DataFrame(columns=STATION_DATE_SCHEMA.names)
                reconstructed_dates = pd.DataFrame(columns=list(ROUNDTRIP_DATE_COLUMNS))
            else:
                frame = pq.read_table(path).to_pandas()
                profiles.append(
                    aggregate_station_profiles(
                        frame,
                        source_window_start=source_window_start,
                        source_window_end=source_window_end,
                        source_corpus_digest=corpus.corpus_digest,
                    )
                )
                reconstructed_dates = reconstruct_app_edge_date_flow(frame)
                reconstructed_profile_frames.append(
                    reconstruct_app_edge_flow_profiles(reconstructed_dates)
                )
            accepted_path = accepted_paths.get(partition)
            accepted_dates = (
                pq.read_table(accepted_path).to_pandas()
                if accepted_path is not None
                else pd.DataFrame(columns=list(ROUNDTRIP_DATE_COLUMNS))
            )
            date_summaries.append(
                compare_roundtrip_rows(
                    reconstructed_dates,
                    accepted_dates,
                    identity=[
                        "app_edge_id",
                        "direction",
                        "local_date",
                        "weekday",
                        "bucket_start_minute",
                    ],
                    numeric_fields=["volume_count", "flow_vph"],
                    exact_fields=[
                        "station_count",
                        "detector_count",
                        "sample_count",
                        "source_station_ids_json",
                    ],
                )
            )
            log(f"STATION ROUNDTRIP partitions={number:,}/{len(all_partitions):,}")
        result = pd.concat(profiles, ignore_index=True)
        identity = ["station_id", "app_edge_id", "weekday", "bucket_start_minute"]
        if result.duplicated(identity).any():
            raise SumoHistoricalComparisonV2Error("duplicate derived station profile")
        reconstructed_profiles = pd.concat(
            reconstructed_profile_frames, ignore_index=True
        )
        profile_roundtrip = compare_roundtrip_rows(
            reconstructed_profiles,
            accepted_profiles,
            identity=["app_edge_id", "direction", "weekday", "bucket_start_minute"],
            numeric_fields=[
                "volume_count_mean",
                "volume_count_p50",
                "volume_count_p85",
                "volume_count_p90",
                "volume_count_p95",
                "flow_vph_mean",
                "flow_vph_p50",
                "flow_vph_p85",
                "flow_vph_p90",
                "flow_vph_p95",
            ],
            exact_fields=[
                "sample_days",
                "volume_sample_days",
                "source_station_ids_json",
            ],
        )
        roundtrip = {
            "schema_version": 1,
            "algorithm": "phase-1-flow-roundtrip-v1",
            "source_mapped_observation_count": mapped_count,
            "expected_mapped_observation_count": expected_mapped_observation_count,
            "date_level": _combine_roundtrip_summaries(date_summaries),
            "weekday_profiles": profile_roundtrip,
            "passed": False,
        }
        roundtrip["passed"] = _roundtrip_passed(roundtrip)
        if not roundtrip["passed"]:
            raise SumoHistoricalComparisonV2Error(
                "historical station FLOW round-trip failed: "
                + canonical_json(roundtrip)
            )
        return (
            result.sort_values(identity, kind="mergesort").reset_index(drop=True),
            roundtrip,
        )
    finally:
        for writer in writers.values():
            writer.close()
        shutil.rmtree(work, ignore_errors=True)


def _partition_accepted_date_level(
    *, accepted_date_level_path: Path, work: Path, partition_count: int
) -> dict[int, Path]:
    writers: dict[int, pq.ParquetWriter] = {}
    paths: dict[int, Path] = {}
    schema = pq.read_schema(accepted_date_level_path)
    selected_schema = pa.schema([schema.field(name) for name in ROUNDTRIP_DATE_COLUMNS])
    try:
        parquet_file = pq.ParquetFile(accepted_date_level_path)
        for batch in parquet_file.iter_batches(
            batch_size=100_000, columns=list(ROUNDTRIP_DATE_COLUMNS)
        ):
            frame = pa.Table.from_batches([batch]).to_pandas()
            partition_ids = frame["app_edge_id"].map(
                lambda edge: (
                    int.from_bytes(
                        hashlib.sha256(str(edge).encode()).digest()[:8], "big"
                    )
                    % partition_count
                )
            )
            for partition, values in frame.groupby(partition_ids, sort=False):
                partition_id = int(partition)
                writer = writers.get(partition_id)
                if writer is None:
                    path = work / f"accepted-edge-{partition_id:03d}.parquet"
                    writer = pq.ParquetWriter(path, selected_schema, compression="zstd")
                    writers[partition_id] = writer
                    paths[partition_id] = path
                writer.write_table(
                    pa.Table.from_pandas(
                        values[list(ROUNDTRIP_DATE_COLUMNS)],
                        schema=selected_schema,
                        preserve_index=False,
                    )
                )
    finally:
        for writer in writers.values():
            writer.close()
    return paths


def _combine_roundtrip_summaries(
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    numeric_fields = sorted(
        {
            field
            for summary in summaries
            for field in summary["maximum_numeric_difference"]
        }
    )
    reason_counts: Counter[str] = Counter()
    for summary in summaries:
        reason_counts.update(summary["mismatch_reason_counts"])
    return {
        "reconstructed_row_count": sum(
            summary["reconstructed_row_count"] for summary in summaries
        ),
        "accepted_row_count": sum(
            summary["accepted_row_count"] for summary in summaries
        ),
        "exact_or_numerically_equivalent_match_count": sum(
            summary["exact_or_numerically_equivalent_match_count"]
            for summary in summaries
        ),
        "mismatch_row_count": sum(
            summary["mismatch_row_count"] for summary in summaries
        ),
        "missing_from_reconstructed_count": sum(
            summary["missing_from_reconstructed_count"] for summary in summaries
        ),
        "missing_from_accepted_count": sum(
            summary["missing_from_accepted_count"] for summary in summaries
        ),
        "maximum_numeric_difference": {
            field: max(
                summary["maximum_numeric_difference"].get(field, 0.0)
                for summary in summaries
            )
            for field in numeric_fields
        },
        "mismatch_reason_counts": dict(sorted(reason_counts.items())),
    }


def _roundtrip_passed(roundtrip: dict[str, Any]) -> bool:
    if (
        roundtrip["source_mapped_observation_count"]
        != roundtrip["expected_mapped_observation_count"]
    ):
        return False
    return all(
        section["mismatch_row_count"] == 0
        and section["missing_from_reconstructed_count"] == 0
        and section["missing_from_accepted_count"] == 0
        and section["reconstructed_row_count"] == section["accepted_row_count"]
        for section in (roundtrip["date_level"], roundtrip["weekday_profiles"])
    )


def _interval_catalog(telemetry: pd.DataFrame, departure: datetime) -> pd.DataFrame:
    intervals = telemetry[
        ["interval_start_seconds", "interval_end_seconds"]
    ].drop_duplicates()
    if intervals.duplicated("interval_start_seconds").any():
        raise SumoHistoricalComparisonV2Error("interval start maps to multiple ends")
    rows = []
    for row in intervals.sort_values("interval_start_seconds").itertuples(index=False):
        weekday, bucket, local_date = resolve_local_interval(
            departure, int(row.interval_start_seconds)
        )
        rows.append(
            {
                "interval_start_seconds": int(row.interval_start_seconds),
                "interval_end_seconds": int(row.interval_end_seconds),
                "weekday": weekday,
                "bucket_start_minute": bucket,
                "simulation_local_date": local_date,
            }
        )
    return pd.DataFrame(rows)


def _source_coverage(
    *,
    edge_profiles: pd.DataFrame,
    quality_policy: pd.DataFrame,
    station_policy: pd.DataFrame,
    station_profiles: pd.DataFrame,
) -> dict[str, Any]:
    identity = ["app_edge_id", "weekday", "bucket_start_minute"]
    profiles = edge_profiles[identity + ["source_station_ids_json"]].merge(
        quality_policy[identity + ["date_support_class"]],
        on=identity,
        how="inner",
        validate="one_to_one",
    )
    accepted = {
        canonical_station_id(value)
        for value in station_policy.loc[
            station_policy["flow_measurement_eligible"], "station_id"
        ]
    }
    direct = profiles.loc[profiles["date_support_class"].eq(DIRECT)].copy()
    direct["all_station_mappings_accepted"] = direct["source_station_ids_json"].map(
        lambda encoded: all(
            canonical_station_id(value) in accepted for value in json.loads(encoded)
        )
    )
    return {
        "historical_candidate_app_edge_profile_count": len(profiles),
        "historical_direct_app_edge_profile_count": len(direct),
        "direct_profiles_all_station_mappings_accepted": int(
            direct["all_station_mappings_accepted"].sum()
        ),
        "direct_profiles_station_mapping_incomplete": int(
            (~direct["all_station_mappings_accepted"]).sum()
        ),
        "historical_station_profile_count": len(station_profiles),
        "historical_direct_station_profile_count": int(
            station_profiles["flow_direct_calibration_eligible"].sum()
        ),
        "accepted_flow_station_count": len(accepted),
    }


def corridor_label(ref: Any, road_name: Any) -> str:
    ref_text = "" if _missing(ref) else str(ref)
    name_text = "" if _missing(road_name) else str(road_name)
    if "Interstate Bridge" in name_text or "Interstate Br" in name_text:
        return "Interstate Bridge"
    if "Glenn L. Jackson Memorial Bridge" in name_text:
        return "Glenn L. Jackson Memorial Bridge"
    if "Marquam Bridge" in name_text:
        return "Marquam Bridge"
    refs = {value.strip() for value in ref_text.split(";")}
    if "I 5" in refs:
        return "I-5"
    if "I 205" in refs:
        return "I-205"
    if "OR 217" in refs:
        return "OR-217"
    if "I 84" in refs or "US 30" in refs:
        return "I-84 / US-30"
    if "US 26" in refs:
        return "US-26"
    return ref_text or name_text or "unknown"


def _possible_weekday_counts(start: date, end: date) -> dict[str, int]:
    if end < start:
        raise ValueError("historical source window is reversed")
    counts = {weekday: 0 for weekday in WEEKDAYS}
    current = start
    while current <= end:
        if current.weekday() < 5:
            counts[WEEKDAYS[current.weekday()]] += 1
        current += timedelta(days=1)
    return counts


def _quantile_distribution(
    values: pd.Series, *, quantiles: tuple[float, ...] = (0.50, 0.85, 0.90, 0.95)
) -> dict[str, float | None]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return {
        f"p{round(value * 100):02d}": (
            float(numeric.quantile(value)) if len(numeric) else None
        )
        for value in quantiles
    }


def _render_report(scoreboard: dict[str, Any]) -> str:
    station = scoreboard["station"]
    edge = scoreboard["app_edge"]
    station_metrics = station["primary_metrics"]
    edge_metrics = edge["primary_metrics"]
    source = scoreboard.get("source_coverage") or {}
    v1 = scoreboard.get("comparator_v1") or {}
    v1_metrics = v1.get("primary_metrics") or {}
    return "\n".join(
        [
            "# Phase 2.2g PORTAL ↔ SUMO Comparator v2",
            "",
            "Diagnostic modeled-uncalibrated evidence only. No promotion threshold or demand tuning is applied.",
            "",
            "## Physical measurement contract",
            "",
            "PORTAL detector/lane flow is summed within each historical station. SUMO flow is the accepted virtual station's physical crossing count multiplied by four for a complete 900-second interval. App-edge simulated flow is the median over the exact station identities contributing to the authoritative Phase 1 app-edge profile; partial station sets are never primary evidence.",
            "",
            "Point speed remains ineligible. Scenario evidence is retained but excluded from the baseline calibration scoreboard.",
            "",
            "## Source-wide historical coverage",
            "",
            f"- Candidate app-edge profiles: {int(source.get('historical_candidate_app_edge_profile_count', 0)):,}",
            f"- Direct-evidence app-edge profiles: {int(source.get('historical_direct_app_edge_profile_count', 0)):,}",
            f"- Direct profiles with every station mapping accepted: {int(source.get('direct_profiles_all_station_mappings_accepted', 0)):,}",
            f"- Accepted FLOW stations: {int(source.get('accepted_flow_station_count', 0)):,}",
            f"- Reconstructed station profiles: {int(source.get('historical_station_profile_count', 0)):,}",
            "",
            "## Baseline primary scoreboard",
            "",
            f"- Station rows: {station['primary_eligible_row_count']:,} eligible of {station['baseline_candidate_row_count']:,}",
            f"- Station FLOW WAPE: {_format_metric(station_metrics['flow_wape'])}",
            f"- Station FLOW MAE: {_format_metric(station_metrics['flow_mae_vph'])} VPH",
            f"- Station FLOW bias (SUMO − PORTAL): {_format_metric(station_metrics['flow_signed_bias_vph'])} VPH",
            f"- App-edge rows: {edge['primary_eligible_row_count']:,} eligible of {edge['baseline_candidate_row_count']:,}",
            f"- App-edge FLOW WAPE: {_format_metric(edge_metrics['flow_wape'])}",
            f"- App-edge FLOW MAE: {_format_metric(edge_metrics['flow_mae_vph'])} VPH",
            f"- App-edge FLOW bias (SUMO − PORTAL): {_format_metric(edge_metrics['flow_signed_bias_vph'])} VPH",
            "",
            "## Comparator-v1 diagnostic context",
            "",
            f"- Comparator-v1 whole-edge FLOW WAPE: {_format_metric(v1_metrics.get('flow_wape'))}",
            f"- Comparator-v2 station-cross-section FLOW WAPE: {_format_metric(station_metrics['flow_wape'])}",
            "- The values are not expected to match: v1 used whole-edge inflow approximation; v2 uses physical station crossings and station-local completeness.",
            "",
            "Full weekday, time-bucket, direction, corridor, high-volume, exclusion, and integrity details are in `scoreboard.json`.",
            "",
        ]
    )


def _optional_v1_summary(
    run_directory: Path, *, comparator_v1_summary_path: Path | None
) -> dict[str, Any] | None:
    path = comparator_v1_summary_path or (
        run_directory / "historical-comparison-v1" / "comparison-summary.json"
    )
    if not path.is_file():
        return None
    value = _read_json(path)
    manifest_path = path.with_name("comparison-manifest.json")
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    return {
        "source_path": str(path),
        "summary_sha256": _sha256(path),
        "content_digest": manifest.get("content_digest"),
        "primary_metrics": value.get("primary_metrics"),
        "coverage": value.get("coverage"),
    }


def frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in frame.to_dict(orient="records"):
        digest.update(canonical_json(_json_value(row)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def content_digest(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("generated_at", None)
    value.pop("content_digest", None)
    return hashlib.sha256(canonical_json(_json_value(value)).encode()).hexdigest()


def file_identity(path: Path, row_count: int | None = None) -> dict[str, Any]:
    return {
        "relative_path": path.name,
        "sha256": _sha256(path),
        "byte_count": path.stat().st_size,
        "row_count": row_count,
    }


def _verify_file(path: Path, identity: dict[str, Any]) -> None:
    if not path.is_file() or path.stat().st_size != int(identity["byte_count"]):
        raise SumoHistoricalComparisonV2Error(f"input file metadata mismatch: {path}")
    if _sha256(path) != identity["sha256"]:
        raise SumoHistoricalComparisonV2Error(f"input file hash mismatch: {path}")
    if identity.get("row_count") is not None and pq.read_metadata(path).num_rows != int(
        identity["row_count"]
    ):
        raise SumoHistoricalComparisonV2Error(f"input row count mismatch: {path}")


def _record_id(kind: str, *parts: str) -> str:
    value = "\x1f".join((COMPARATOR_V2_PRODUCER_VERSION, kind, *parts))
    return f"portal.sumo.comparison-v2.{kind}.{hashlib.sha256(value.encode()).hexdigest()[:40]}"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SumoHistoricalComparisonV2Error(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if _missing(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _format_metric(value: Any) -> str:
    return "not derivable" if value is None else f"{float(value):.6f}"


def _records(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    available = [column for column in columns if column in frame.columns]
    return [_json_value(row) for row in frame[available].to_dict(orient="records")]


def promote_completed_pending(pending: Path, output: Path) -> None:
    """Promote only an atomically complete Comparator-v2 directory."""
    if output.exists():
        raise SumoHistoricalComparisonV2Error("Comparator-v2 output already exists")
    manifest = pending / MANIFEST_FILE
    if not manifest.is_file():
        raise SumoHistoricalComparisonV2Error(
            "partial Comparator-v2 output has no manifest"
        )
    ComparatorV2Manifest.model_validate_json(manifest.read_bytes())
    os.replace(pending, output)
