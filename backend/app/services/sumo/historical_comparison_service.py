"""Phase 2.2 deterministic PORTAL historical evidence to SUMO comparator."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_policy import (
    HistoricalQualityPolicyManifestV1,
)
from backend.app.schemas.sumo_edge_telemetry import SumoEdgeTelemetryManifestV1
from backend.app.schemas.sumo_historical_comparison import (
    SUMO_HISTORICAL_COMPARISON_PRODUCER_VERSION,
    SUMO_HISTORICAL_COMPARISON_SCHEMA_VERSION,
    SUMO_HISTORICAL_FLOW_SEMANTICS_VERSION,
    SUMO_HISTORICAL_MAPPING_CONTRACT_VERSION,
    SumoHistoricalComparisonManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json
from backend.app.services.sumo.edge_telemetry_service import (
    TELEMETRY_DIRECTORY,
    TELEMETRY_MANIFEST,
)

COMPARISON_DIRECTORY = "historical-comparison-v1"
COMPARISON_PARQUET = "comparison.parquet"
COMPARISON_SUMMARY = "comparison-summary.json"
COMPARISON_REPORT = "comparison-report.md"
COMPARISON_MANIFEST = "comparison-manifest.json"
PROFILE_PARQUET = "edge-time-distributions.parquet"
PROFILE_MANIFEST = "historical-calibration-manifest.json"
POLICY_PARQUET = "quality-policy-profile-status.parquet"
POLICY_MANIFEST = "quality-policy-manifest.json"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")


class SumoHistoricalComparisonError(RuntimeError):
    """Inputs cannot satisfy the versioned Phase 2.2 comparison contract."""


def resolve_local_interval(
    departure_time: datetime, interval_start_seconds: int
) -> tuple[str, int, str]:
    """Resolve one SUMO interval start to local calendar identity without coercion."""
    if departure_time.tzinfo is None:
        raise ValueError("simulation departure_time must be timezone-aware")
    if interval_start_seconds < 0:
        raise ValueError("interval_start_seconds cannot be negative")
    local = (departure_time + timedelta(seconds=interval_start_seconds)).astimezone(
        LOCAL_TIMEZONE
    )
    weekday = local.strftime("%A").lower()
    if weekday not in WEEKDAYS:
        raise ValueError("weekend telemetry has no Phase 1 historical profile target")
    if local.second or local.microsecond or local.minute % 15:
        raise ValueError("SUMO telemetry interval does not begin on a local 15-minute boundary")
    return weekday, local.hour * 60 + local.minute, local.date().isoformat()


def derive_sumo_comparable_flow(
    entered_count: float, departed_count: float, interval_seconds: int
) -> tuple[float, float]:
    """Return edge-inflow count and VPH, including vehicles emitted on the edge."""
    if entered_count < 0 or departed_count < 0 or interval_seconds <= 0:
        raise ValueError("flow inputs require nonnegative counts and positive duration")
    count = float(entered_count) + float(departed_count)
    return count, count * 3600.0 / interval_seconds


def calculate_primary_metrics(frame: pd.DataFrame) -> dict[str, float | int | None]:
    """Aggregate direct-evidence residuals with explicit metric-specific masks."""
    result: dict[str, float | int | None] = {}
    flow = frame.loc[frame["flow_primary_scoring"]]
    denominator = float(flow["historical_flow_vph_p50"].abs().sum())
    numerator = float(flow["flow_absolute_error_vph"].sum())
    result["flow_primary_row_count"] = len(flow)
    result["flow_wape_numerator_vph"] = numerator
    result["flow_wape_denominator_vph"] = denominator
    result["flow_wape"] = numerator / denominator if denominator else None

    speed = frame.loc[frame["speed_primary_scoring"]]
    result["speed_primary_row_count"] = len(speed)
    result["speed_mae_kph"] = _mean_or_none(speed["speed_absolute_error_kph"])
    result["speed_bias_kph"] = _mean_or_none(speed["speed_signed_error_kph"])

    slowdown = frame.loc[frame["slowdown_primary_scoring"]]
    result["slowdown_primary_row_count"] = len(slowdown)
    result["slowdown_p50_mae"] = _mean_abs_or_none(
        slowdown["slowdown_error_vs_p50"]
    )
    result["slowdown_p85_mae"] = _mean_abs_or_none(
        slowdown["slowdown_error_vs_p85"]
    )
    result["slowdown_p90_mae"] = _mean_abs_or_none(
        slowdown["slowdown_error_vs_p90"]
    )
    result["slowdown_p95_mae"] = _mean_abs_or_none(
        slowdown["slowdown_error_vs_p95"]
    )
    return result


def compare_sumo_to_historical(
    *,
    run_directory: Path,
    historical_directory: Path,
    policy_directory: Path,
    edge_map_path: Path,
    edge_map_report_path: Path,
    graph_edges_path: Path,
    output_directory: Path | None = None,
) -> SumoHistoricalComparisonManifestV1:
    """Create one atomic diagnostic comparison without mutating any source artifact."""
    run_directory = run_directory.resolve()
    output_directory = (output_directory or run_directory / COMPARISON_DIRECTORY).resolve()
    pending_directory = output_directory.parent / f".{output_directory.name}.pending"
    if output_directory.exists():
        return SumoHistoricalComparisonManifestV1.model_validate_json(
            (output_directory / COMPARISON_MANIFEST).read_text(encoding="utf-8")
        )
    if pending_directory.exists():
        shutil.rmtree(pending_directory)

    telemetry_dir = run_directory / TELEMETRY_DIRECTORY
    telemetry_manifest_path = telemetry_dir / TELEMETRY_MANIFEST
    telemetry_manifest = SumoEdgeTelemetryManifestV1.model_validate_json(
        telemetry_manifest_path.read_text(encoding="utf-8")
    )
    historical_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        (historical_directory / PROFILE_MANIFEST).read_text(encoding="utf-8")
    )
    policy_manifest = HistoricalQualityPolicyManifestV1.model_validate_json(
        (policy_directory / POLICY_MANIFEST).read_text(encoding="utf-8")
    )
    request_payload = json.loads((run_directory / "request.json").read_text(encoding="utf-8"))
    departure_time = datetime.fromisoformat(
        str(request_payload["request"]["departure_time"]).replace("Z", "+00:00")
    )
    if telemetry_manifest.source.graph_version != historical_manifest.graph_version:
        raise SumoHistoricalComparisonError("telemetry and historical graph versions differ")
    if policy_manifest.source_phase_1_3_content_digest != historical_manifest.content_digest:
        raise SumoHistoricalComparisonError("quality policy does not describe the profile input")
    edge_map_sha256 = _sha256(edge_map_path)
    edge_map_report = json.loads(edge_map_report_path.read_text(encoding="utf-8"))
    report_edge_map_sha256 = edge_map_report.get("artifacts", {}).get("edge_map", {}).get("sha256")
    if report_edge_map_sha256 != edge_map_sha256:
        raise SumoHistoricalComparisonError("edge-map report hash does not match edge map")
    if edge_map_report.get("graph_version") != historical_manifest.graph_version:
        raise SumoHistoricalComparisonError("edge map graph version differs")
    if edge_map_report.get("sumo_network_version") != telemetry_manifest.source.network_version:
        raise SumoHistoricalComparisonError("edge map SUMO network version differs")

    telemetry = pq.read_table(telemetry_dir / telemetry_manifest.output.relative_path).to_pandas()
    interval_catalog = _interval_catalog(telemetry, departure_time)
    historical = pq.read_table(historical_directory / PROFILE_PARQUET).to_pandas()
    policy = pq.read_table(policy_directory / POLICY_PARQUET).to_pandas()
    mapping, mapping_inventory = _mapping_inventory(edge_map_path)
    graph = pq.read_table(
        graph_edges_path,
        columns=["edge_id", "road_name", "road_class", "ref", "maxspeed_kph"],
    ).to_pandas()
    frame = build_comparison_frame(
        historical=historical,
        policy=policy,
        telemetry=telemetry,
        interval_catalog=interval_catalog,
        mapping=mapping,
        graph=graph,
    )
    summary = _build_summary(
        frame=frame,
        policy=policy,
        mapping=mapping,
        mapping_inventory=mapping_inventory,
        telemetry=telemetry,
        telemetry_manifest=telemetry_manifest,
        historical_manifest=historical_manifest,
        policy_manifest=policy_manifest,
        interval_catalog=interval_catalog,
    )
    report = _render_report(summary)

    pending_directory.mkdir(parents=True)
    try:
        output_parquet = pending_directory / COMPARISON_PARQUET
        output_summary = pending_directory / COMPARISON_SUMMARY
        output_report = pending_directory / COMPARISON_REPORT
        output_manifest = pending_directory / COMPARISON_MANIFEST
        table = pa.Table.from_pandas(frame, preserve_index=False)
        table = table.replace_schema_metadata(
            {
                b"schema_version": b"1",
                b"producer": b"portal_sumo_edge_time_comparator",
                b"producer_version": SUMO_HISTORICAL_COMPARISON_PRODUCER_VERSION.encode(),
                b"flow_semantics": SUMO_HISTORICAL_FLOW_SEMANTICS_VERSION.encode(),
                b"occupancy_role": b"secondary_not_primary_comparable",
            }
        )
        pq.write_table(table, output_parquet, compression="zstd", compression_level=9)
        output_summary.write_text(canonical_json(summary) + "\n", encoding="utf-8")
        output_report.write_text(report, encoding="utf-8")
        row_digest = _frame_digest(frame)
        manifest_payload: dict[str, Any] = {
            "schema_version": SUMO_HISTORICAL_COMPARISON_SCHEMA_VERSION,
            "artifact_type": "commute_help_portal_sumo_edge_time_comparison",
            "artifact_status": "complete_diagnostic",
            "evidence_level": "modeled_uncalibrated",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC).isoformat(),
            "producer": {
                "name": "portal_sumo_edge_time_comparator",
                "version": SUMO_HISTORICAL_COMPARISON_PRODUCER_VERSION,
            },
            "application_run_id": telemetry_manifest.application_run_id,
            "run_identity": telemetry_manifest.run_identity,
            "telemetry_content_digest": telemetry_manifest.content_digest,
            "historical_profile_content_digest": historical_manifest.content_digest,
            "quality_policy_content_digest": policy_manifest.content_digest,
            "mapping": {
                "contract_version": SUMO_HISTORICAL_MAPPING_CONTRACT_VERSION,
                "graph_version": historical_manifest.graph_version,
                "sumo_network_version": telemetry_manifest.source.network_version,
                "edge_map_sha256": edge_map_sha256,
                "accepted_relation_semantics": "exactly_one_distinct_accepted_sumo_edge_per_app_edge",
                "ambiguity_semantics": "exclude_without_selecting_a_winner",
            },
            "flow_contract": {
                "semantics_version": SUMO_HISTORICAL_FLOW_SEMANTICS_VERSION,
                "historical_source": "PORTAL_15_minute_detector_count_aggregated_to_edge",
                "sumo_comparable_count": "entered_count_plus_departed_count",
                "sumo_comparable_flow": "(entered_count + departed_count) * 3600 / interval_duration_seconds",
                "native_phase_2_1_flow_preserved": True,
                "limitation": "SUMO_edge_inflow_is_not_identical_to_a_PORTAL_point_detector_crossing",
            },
            "timezone": "America/Los_Angeles",
            "interval_seconds": 900,
            "compared_weekdays": sorted(set(interval_catalog["weekday"]), key=WEEKDAYS.index),
            "compared_bucket_start_minutes": sorted(set(interval_catalog["bucket_start_minute"])),
            "variants": sorted(set(telemetry["variant"])),
            "comparison_output": _file_identity(output_parquet, len(frame)),
            "summary_output": _file_identity(output_summary),
            "report_output": _file_identity(output_report),
            "row_content_sha256": row_digest,
        }
        manifest_payload["content_digest"] = _content_digest(manifest_payload)
        manifest = SumoHistoricalComparisonManifestV1.model_validate(manifest_payload)
        output_manifest.write_text(
            canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        _promote_completed_pending(pending_directory, output_directory)
        return manifest
    except BaseException:
        # A pending directory is deliberately non-authoritative; remove it on normal errors.
        if pending_directory.exists():
            shutil.rmtree(pending_directory)
        raise


def build_comparison_frame(
    *,
    historical: pd.DataFrame,
    policy: pd.DataFrame,
    telemetry: pd.DataFrame,
    interval_catalog: pd.DataFrame,
    mapping: pd.DataFrame,
    graph: pd.DataFrame,
) -> pd.DataFrame:
    """Join exact edge/weekday/bucket evidence while retaining non-primary rows."""
    identity = ["app_edge_id", "direction", "weekday", "bucket_start_minute"]
    if historical.duplicated(identity).any() or policy.duplicated(identity).any():
        raise SumoHistoricalComparisonError("duplicate historical edge/weekday/bucket identity")
    profile_columns = identity + [
        "flow_vph_mean", "flow_vph_p50", "flow_vph_p85", "flow_vph_p90", "flow_vph_p95",
        "speed_kph_mean", "speed_kph_p10", "speed_kph_p50", "speed_kph_p85", "speed_kph_p90", "speed_kph_p95",
        "slowdown_p50", "slowdown_p85", "slowdown_p90", "slowdown_p95",
    ]
    policy_columns = identity + [
        "date_support_class", "observed_date_count", "possible_date_count", "coverage_fraction",
        "flow_direct_calibration_eligible", "speed_direct_calibration_eligible",
        "speed_missing_within_observed_date_count", "speed_zero_diagnostic_date_count",
        "sample_count_p50", "detector_support_count_p50", "station_support_count_p50",
        "calibration_status",
    ]
    profiles = historical[profile_columns].merge(
        policy[policy_columns], on=identity, how="inner", validate="one_to_one"
    )
    profiles = interval_catalog.merge(
        profiles, on=["weekday", "bucket_start_minute"], how="inner", validate="many_to_many"
    )
    profiles = profiles.merge(mapping, on="app_edge_id", how="left", validate="many_to_one")
    profiles["mapping_status"] = profiles["mapping_status"].fillna("absent")
    graph = graph.rename(columns={"edge_id": "app_edge_id", "ref": "road_ref", "maxspeed_kph": "reference_speed_kph"})
    if graph.duplicated("app_edge_id").any():
        raise SumoHistoricalComparisonError("graph edge IDs are not unique")
    profiles = profiles.merge(graph, on="app_edge_id", how="left", validate="many_to_one")
    variants = pd.DataFrame({"variant": sorted(telemetry["variant"].unique())})
    profiles["_cross"] = 1
    variants["_cross"] = 1
    profiles = profiles.merge(variants, on="_cross").drop(columns="_cross")

    telemetry_identity = ["variant", "sumo_edge_id", "interval_start_seconds"]
    if telemetry.duplicated(telemetry_identity).any():
        raise SumoHistoricalComparisonError("duplicate SUMO edge/variant/interval identity")
    telemetry_columns = telemetry_identity + [
        "interval_end_seconds", "interval_duration_seconds", "entered_count", "departed_count",
        "left_count", "arrived_count", "flow_vph", "mean_speed_kph", "mean_travel_time_seconds",
        "density_veh_per_km", "occupancy_percent", "sampled_vehicle_seconds",
    ]
    frame = profiles.merge(
        telemetry[telemetry_columns], on=telemetry_identity, how="left", validate="many_to_one"
    )
    frame["sumo_comparable_inflow_count"] = frame["entered_count"] + frame["departed_count"]
    frame["sumo_comparable_flow_vph"] = (
        frame["sumo_comparable_inflow_count"] * 3600.0 / frame["interval_duration_seconds"]
    )
    frame["flow_comparator_derivation"] = "(entered_count + departed_count) * 3600 / interval_duration_seconds"
    frame["historical_flow_vph_target"] = frame["flow_vph_p50"]
    frame["historical_speed_kph_target"] = frame["speed_kph_p50"]
    frame["flow_signed_error_vph"] = frame["sumo_comparable_flow_vph"] - frame["historical_flow_vph_target"]
    frame["flow_absolute_error_vph"] = frame["flow_signed_error_vph"].abs()
    frame["flow_wape_denominator_vph"] = frame["historical_flow_vph_target"].abs()
    frame["speed_signed_error_kph"] = frame["mean_speed_kph"] - frame["historical_speed_kph_target"]
    frame["speed_absolute_error_kph"] = frame["speed_signed_error_kph"].abs()
    frame["sumo_slowdown"] = frame["reference_speed_kph"] / frame["mean_speed_kph"].where(frame["mean_speed_kph"] > 0)
    for quantile in ("p50", "p85", "p90", "p95"):
        frame[f"slowdown_error_vs_{quantile}"] = frame["sumo_slowdown"] - frame[f"slowdown_{quantile}"]
    has_telemetry = frame["interval_end_seconds"].notna()
    accepted = frame["mapping_status"].eq("accepted_unique")
    frame["flow_primary_scoring"] = frame["flow_direct_calibration_eligible"] & accepted & has_telemetry & frame["historical_flow_vph_target"].notna()
    frame["speed_primary_scoring"] = frame["speed_direct_calibration_eligible"] & accepted & has_telemetry & frame["historical_speed_kph_target"].notna() & frame["mean_speed_kph"].notna()
    frame["slowdown_primary_scoring"] = frame["speed_primary_scoring"] & frame["sumo_slowdown"].notna() & frame["slowdown_p50"].notna()
    frame["occupancy_primary_comparable"] = False
    frame["residual_rank_score"] = (
        (frame["flow_absolute_error_vph"] / frame["historical_flow_vph_target"].abs().clip(lower=1.0)).fillna(0)
        + frame["speed_absolute_error_kph"].fillna(0)
        + 10.0 * frame["slowdown_error_vs_p50"].abs().fillna(0)
    )
    rename = {
        "flow_vph_mean": "historical_flow_vph_mean", "flow_vph_p50": "historical_flow_vph_p50",
        "flow_vph_p85": "historical_flow_vph_p85", "flow_vph_p90": "historical_flow_vph_p90",
        "flow_vph_p95": "historical_flow_vph_p95", "speed_kph_mean": "historical_speed_kph_mean",
        "speed_kph_p10": "historical_speed_kph_p10", "speed_kph_p50": "historical_speed_kph_p50",
        "speed_kph_p85": "historical_speed_kph_p85", "speed_kph_p90": "historical_speed_kph_p90",
        "speed_kph_p95": "historical_speed_kph_p95", "slowdown_p50": "historical_slowdown_p50",
        "slowdown_p85": "historical_slowdown_p85", "slowdown_p90": "historical_slowdown_p90",
        "slowdown_p95": "historical_slowdown_p95", "flow_vph": "sumo_native_entered_flow_vph",
        "mean_speed_kph": "sumo_mean_speed_kph", "occupancy_percent": "sumo_edge_occupancy_percent",
    }
    frame = frame.rename(columns=rename)
    # Fix residual columns after their historical sources were renamed.
    ordered = ["variant", "app_edge_id", "sumo_edge_id", "direction", "weekday", "bucket_start_minute", "interval_start_seconds"]
    return frame.sort_values(ordered, kind="mergesort", na_position="last").reset_index(drop=True)


def _interval_catalog(telemetry: pd.DataFrame, departure: datetime) -> pd.DataFrame:
    intervals = telemetry[["interval_start_seconds", "interval_end_seconds"]].drop_duplicates().sort_values("interval_start_seconds")
    rows = []
    for row in intervals.itertuples(index=False):
        try:
            weekday, bucket, local_date = resolve_local_interval(departure, int(row.interval_start_seconds))
        except ValueError as exc:
            if "weekend" in str(exc):
                continue
            raise
        rows.append({"interval_start_seconds": int(row.interval_start_seconds), "interval_end_seconds_expected": int(row.interval_end_seconds), "weekday": weekday, "bucket_start_minute": bucket, "simulation_local_date": local_date})
    return pd.DataFrame(rows)


def _mapping_inventory(path: Path) -> tuple[pd.DataFrame, dict[str, int]]:
    raw = pq.read_table(path, columns=["app_edge_id", "sumo_edge_id", "status"]).to_pandas()
    edges = pd.DataFrame({"app_edge_id": sorted(raw["app_edge_id"].unique())})
    accepted = raw.loc[raw["status"].eq("accepted")].groupby("app_edge_id")["sumo_edge_id"].agg(
        accepted_sumo_edge_count="nunique", sumo_edge_id="min"
    )
    flags = pd.crosstab(raw["app_edge_id"], raw["status"]).astype(bool)
    mapping = edges.merge(accepted, on="app_edge_id", how="left").merge(
        flags.reset_index(), on="app_edge_id", how="left"
    )
    mapping["accepted_sumo_edge_count"] = mapping["accepted_sumo_edge_count"].fillna(0).astype(int)
    mapping["mapping_status"] = "unmatched"
    review_mask = (
        mapping["review"].fillna(False)
        if "review" in mapping
        else pd.Series(False, index=mapping.index)
    )
    mapping.loc[review_mask, "mapping_status"] = "review"
    mapping.loc[mapping["accepted_sumo_edge_count"].eq(1), "mapping_status"] = "accepted_unique"
    mapping.loc[mapping["accepted_sumo_edge_count"].gt(1), "mapping_status"] = "ambiguous_multiple_accepted"
    mapping.loc[~mapping["mapping_status"].eq("accepted_unique"), "sumo_edge_id"] = None
    result = mapping[["app_edge_id", "sumo_edge_id", "mapping_status", "accepted_sumo_edge_count"]].sort_values("app_edge_id").reset_index(drop=True)
    counts = Counter(result["mapping_status"])
    return result, dict(sorted(counts.items()))


def _build_summary(**values: Any) -> dict[str, Any]:
    frame: pd.DataFrame = values["frame"]
    policy: pd.DataFrame = values["policy"]
    mapping: pd.DataFrame = values["mapping"]
    telemetry: pd.DataFrame = values["telemetry"]
    intervals: pd.DataFrame = values["interval_catalog"]
    direct = policy["date_support_class"].eq("direct_calibration_evidence")
    profile_mapping = policy[["app_edge_id", "date_support_class"]].merge(
        mapping[["app_edge_id", "mapping_status"]],
        on="app_edge_id",
        how="left",
        validate="many_to_one",
    )
    profile_mapping["mapping_status"] = profile_mapping["mapping_status"].fillna("absent")
    all_profile_mapping_counts = profile_mapping["mapping_status"].value_counts().sort_index()
    direct_profile_mapping_counts = (
        profile_mapping.loc[
            profile_mapping["date_support_class"].eq("direct_calibration_evidence"),
            "mapping_status",
        ]
        .value_counts()
        .sort_index()
    )
    association = frame.drop_duplicates(["app_edge_id", "weekday", "bucket_start_minute"])
    coverage = association["mapping_status"].value_counts().sort_index().to_dict()
    accepted_edges = set(association.loc[association["mapping_status"] == "accepted_unique", "sumo_edge_id"].dropna())
    sumo_edges = set(telemetry["sumo_edge_id"])
    groupings = {}
    for name, columns in {
        "variant": ["variant"], "weekday": ["weekday"], "bucket": ["bucket_start_minute"],
        "direction": ["direction"], "corridor": ["road_ref", "road_name"],
    }.items():
        records = []
        grouper: str | list[str] = columns[0] if len(columns) == 1 else columns
        for key, group in frame.groupby(grouper, dropna=False, sort=True):
            key_values = key if isinstance(key, tuple) else (key,)
            records.append(
                {
                    **{
                        column: _json_value(value)
                        for column, value in zip(columns, key_values, strict=True)
                    },
                    **calculate_primary_metrics(group),
                }
            )
        groupings[name] = records
    scored = frame.loc[frame[["flow_primary_scoring", "speed_primary_scoring", "slowdown_primary_scoring"]].any(axis=1)]
    worst = scored.sort_values(["residual_rank_score", "variant", "app_edge_id", "interval_start_seconds"], ascending=[False, True, True, True], kind="mergesort").head(25)
    worst_columns = ["variant", "app_edge_id", "sumo_edge_id", "road_ref", "road_name", "direction", "weekday", "bucket_start_minute", "historical_flow_vph_target", "sumo_comparable_flow_vph", "flow_signed_error_vph", "historical_speed_kph_target", "sumo_mean_speed_kph", "speed_signed_error_kph", "historical_slowdown_p50", "sumo_slowdown", "slowdown_error_vs_p50", "date_support_class", "residual_rank_score"]
    return {
        "artifact_status": "complete_diagnostic",
        "calibration_status": "not_calibrated",
        "primary_metrics": calculate_primary_metrics(frame),
        "coverage": {
            "historical_profile_count": len(policy),
            "historical_direct_evidence_profile_count": int(direct.sum()),
            "all_historical_profile_mapping_status_counts": {
                str(key): int(value) for key, value in all_profile_mapping_counts.items()
            },
            "direct_evidence_profile_mapping_status_counts": {
                str(key): int(value) for key, value in direct_profile_mapping_counts.items()
            },
            "direct_evidence_profiles_with_accepted_sumo_association": int(
                direct_profile_mapping_counts.get("accepted_unique", 0)
            ),
            "direct_evidence_profiles_without_accepted_sumo_association": int(
                direct.sum() - direct_profile_mapping_counts.get("accepted_unique", 0)
            ),
            "window_profile_variant_row_count": len(frame),
            "window_profile_mapping_status_counts": {str(k): int(v) for k, v in coverage.items()},
            "sumo_telemetry_distinct_edge_count": len(sumo_edges),
            "sumo_only_distinct_edge_count": len(sumo_edges - accepted_edges),
            "mapped_but_missing_telemetry_row_count": int(((frame["mapping_status"] == "accepted_unique") & frame["interval_end_seconds"].isna()).sum()),
            "supplemental_or_insufficient_excluded_from_primary_row_count": int((frame["date_support_class"] != "direct_calibration_evidence").sum()),
            "mapping_inventory_app_edge_counts": values["mapping_inventory"],
        },
        "time": {"timezone": "America/Los_Angeles", "intervals": intervals.to_dict(orient="records")},
        "flow_semantics": {"native_phase_2_1": "entered_count * 4", "comparator": "(entered_count + departed_count) * 4", "direct_on_edge_departures_included": True},
        "occupancy": {"role": "secondary_not_primary_comparable", "reason": "SUMO edge-space occupancy and PORTAL point-detector occupancy are not aligned"},
        "breakdowns": groupings,
        "peak_timing": _peak_timing(frame),
        "worst_25_direct_evidence_residuals": [_clean_record(row) for row in worst[worst_columns].to_dict(orient="records")],
        "source": {"telemetry_content_digest": values["telemetry_manifest"].content_digest, "historical_profile_content_digest": values["historical_manifest"].content_digest, "quality_policy_content_digest": values["policy_manifest"].content_digest},
    }


def _peak_timing(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    eligible = frame.loc[frame["flow_primary_scoring"]]
    for (variant, edge, weekday), group in eligible.groupby(["variant", "app_edge_id", "weekday"], sort=True):
        if group["bucket_start_minute"].nunique() < 2:
            continue
        historical = group.sort_values(["historical_flow_vph_target", "bucket_start_minute"], ascending=[False, True]).iloc[0]
        simulated = group.sort_values(["sumo_comparable_flow_vph", "bucket_start_minute"], ascending=[False, True]).iloc[0]
        rows.append({"variant": variant, "app_edge_id": edge, "weekday": weekday, "historical_peak_bucket_start_minute": int(historical["bucket_start_minute"]), "sumo_peak_bucket_start_minute": int(simulated["bucket_start_minute"]), "peak_timing_offset_minutes": int(simulated["bucket_start_minute"] - historical["bucket_start_minute"])})
    return rows


def _render_report(summary: dict[str, Any]) -> str:
    p = summary["primary_metrics"]
    c = summary["coverage"]
    lines = [
        "# Phase 2.2 PORTAL ↔ SUMO comparison", "", "Diagnostic modeled-uncalibrated evidence only. No pass/fail threshold or calibration promotion is applied.", "",
        "## Primary direct-evidence metrics", "",
        f"- Flow WAPE: {_fmt(p['flow_wape'])} ({p['flow_primary_row_count']} rows)",
        f"- Speed MAE: {_fmt(p['speed_mae_kph'])} km/h", f"- Speed bias (SUMO − historical): {_fmt(p['speed_bias_kph'])} km/h",
        f"- Slowdown P50 MAE: {_fmt(p['slowdown_p50_mae'])}", "",
        "## Coverage", "", f"- All historical candidate profiles: {c['historical_profile_count']:,}",
        f"- Direct-evidence profiles: {c['historical_direct_evidence_profile_count']:,}", f"- Comparison rows in this run window: {c['window_profile_variant_row_count']:,}",
        f"- Mapping states in this run window: `{json.dumps(c['window_profile_mapping_status_counts'], sort_keys=True)}`", f"- SUMO-only telemetry edges: {c['sumo_only_distinct_edge_count']:,}", "",
        "## Semantics", "", "PORTAL flow is compared with SUMO edge inflow `(entered + departed) × 4`; this includes vehicles emitted directly onto an edge. Native Phase 2.1 entered-only flow remains present separately. Occupancy is secondary because edge-space and point-detector occupancy are not semantically aligned.", "",
        "The worst-25 diagnostic records and all variant/weekday/bucket/direction/corridor breakdowns are in `comparison-summary.json`.",
    ]
    return "\n".join(lines) + "\n"


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for record in frame.to_dict(orient="records"):
        digest.update(canonical_json(_clean_record(record)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(value) for key, value in record.items()}


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _mean_or_none(series: pd.Series) -> float | None:
    value = series.dropna().mean()
    return None if pd.isna(value) else float(value)


def _mean_abs_or_none(series: pd.Series) -> float | None:
    return _mean_or_none(series.abs())


def _fmt(value: Any) -> str:
    return "not derivable" if value is None else f"{float(value):.6f}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path, row_count: int | None = None) -> dict[str, Any]:
    return {"relative_path": path.name, "sha256": _sha256(path), "byte_count": path.stat().st_size, "row_count": row_count}


def _content_digest(payload: dict[str, Any]) -> str:
    identity = dict(payload)
    identity.pop("generated_at", None)
    identity.pop("content_digest", None)
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def _promote_completed_pending(pending: Path, output: Path) -> None:
    """Atomically promote only a pending directory with a valid success manifest."""
    manifest_path = pending / COMPARISON_MANIFEST
    if output.exists():
        raise SumoHistoricalComparisonError("comparison output already exists")
    if not manifest_path.is_file():
        raise SumoHistoricalComparisonError("partial comparison has no success manifest")
    SumoHistoricalComparisonManifestV1.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    os.replace(pending, output)
