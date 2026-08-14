"""Explain the legacy pooled PM P95 using Phase 1.3 edge/time evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pyarrow_dataset
from pyarrow import parquet

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_p95_tail import (
    P95_TAIL_ANALYSIS_SCHEMA_VERSION,
    P95_TAIL_ANALYSIS_VERSION,
    P95TailAnalysisManifestV1,
)
from backend.app.schemas.historical_calibration_v2_policy import (
    HistoricalQualityPolicyManifestV1,
)
from backend.app.schemas.historical_calibration_v2_quality import (
    QualityCharacterizationManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

ANALYZER_NAME = "historical_calibration_v2_p95_tail_analyzer"
PM_START_MINUTE = 14 * 60
PM_END_MINUTE = 19 * 60
LEGACY_PERIOD = "weekday_afternoon"
LEGACY_CLUE = 2.651
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKDAY_FROM_ISO = dict(enumerate(WEEKDAYS, start=1))
WEEKDAY_ORDER = {weekday: index for index, weekday in enumerate(WEEKDAYS)}

PROFILE_OUTPUT = "p95-tail-profiles.parquet"
SUMMARY_OUTPUT = "p95-tail-summary.json"
MARKDOWN_OUTPUT = "P95_TAIL_ANALYSIS.md"
MANIFEST_OUTPUT = "p95-tail-analysis-manifest.json"
SOURCE_MANIFEST = "historical-calibration-manifest.json"
CHARACTERIZATION_MANIFEST = "quality-characterization-manifest.json"
POLICY_MANIFEST = "quality-policy-manifest.json"

QUANTILES = (
    0.0,
    0.01,
    0.05,
    0.10,
    0.25,
    0.50,
    0.75,
    0.85,
    0.90,
    0.95,
    0.975,
    0.99,
    1.0,
)

TAIL_PROFILE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("road_name", pa.string()),
        pa.field("road_ref", pa.string()),
        pa.field("road_class", pa.string()),
        pa.field("corridor_label", pa.string(), nullable=False),
        pa.field("station_context_json", pa.string(), nullable=False),
        pa.field("slowdown_p50", pa.float64()),
        pa.field("slowdown_p85", pa.float64()),
        pa.field("slowdown_p90", pa.float64()),
        pa.field("slowdown_p95", pa.float64()),
        pa.field("graph_maxspeed_kph", pa.float64()),
        pa.field("reference_speed_kph_p50", pa.float64()),
        pa.field("speed_kph_p10", pa.float64()),
        pa.field("speed_kph_p50", pa.float64()),
        pa.field("speed_kph_p85", pa.float64()),
        pa.field("speed_kph_p90", pa.float64()),
        pa.field("speed_kph_p95", pa.float64()),
        pa.field("flow_vph_p50", pa.float64()),
        pa.field("flow_vph_p85", pa.float64()),
        pa.field("flow_vph_p90", pa.float64()),
        pa.field("flow_vph_p95", pa.float64()),
        pa.field("observed_date_count", pa.int32(), nullable=False),
        pa.field("coverage_fraction", pa.float64(), nullable=False),
        pa.field("date_support_class", pa.string(), nullable=False),
        pa.field("sample_count_p10", pa.float64()),
        pa.field("sample_count_p50", pa.float64()),
        pa.field("sample_count_p90", pa.float64()),
        pa.field("detector_support_count_p50", pa.float64()),
        pa.field("station_support_count_p50", pa.float64()),
        pa.field("zero_speed_date_count", pa.int32(), nullable=False),
        pa.field("zero_speed_fraction_present", pa.float64()),
        pa.field(
            "missing_speed_within_observed_date_count", pa.int32(), nullable=False
        ),
        pa.field("legacy_p95_clue_or_higher", pa.bool_(), nullable=False),
        pa.field("reproduced_legacy_p95_or_higher", pa.bool_(), nullable=False),
        pa.field("tail_rank", pa.int32()),
        pa.field("calibration_status", pa.string(), nullable=False),
        pa.field("analysis_status", pa.string(), nullable=False),
    ]
)


class P95TailAnalysisError(RuntimeError):
    """The requested sources cannot produce a trustworthy Phase 1.4 analysis."""


def exact_quantiles(values: Iterable[float]) -> dict[str, float | int | None]:
    """Return deterministic NumPy-linear quantiles for finite values."""
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    if not len(array):
        return {"count": 0, **{_quantile_name(q): None for q in QUANTILES}}
    result: dict[str, float | int | None] = {"count": len(array)}
    calculated = np.quantile(array, QUANTILES, method="linear")
    result.update(
        {
            _quantile_name(q): float(value)
            for q, value in zip(QUANTILES, calculated, strict=True)
        }
    )
    return result


def analyze_historical_calibration_v2_p95_tail(
    *,
    legacy_campaign_directory: Path,
    candidate_directory: Path,
    characterization_directory: Path,
    policy_directory: Path,
    graph_edges_path: Path,
    output_directory: Path,
) -> P95TailAnalysisManifestV1:
    """Build the immutable Phase 1.4 analysis without reading the Phase 1.2 corpus."""
    paths = [
        legacy_campaign_directory,
        candidate_directory,
        characterization_directory,
        policy_directory,
        graph_edges_path,
        output_directory,
    ]
    (
        legacy_campaign_directory,
        candidate_directory,
        characterization_directory,
        policy_directory,
        graph_edges_path,
        output_directory,
    ) = [path.resolve() for path in paths]
    if output_directory.exists():
        raise P95TailAnalysisError("P95 tail output already exists and is immutable")
    if output_directory in {
        legacy_campaign_directory,
        candidate_directory,
        characterization_directory,
        policy_directory,
    }:
        raise P95TailAnalysisError("P95 tail output must be separate from every input")

    source_manifest_path = candidate_directory / SOURCE_MANIFEST
    characterization_manifest_path = (
        characterization_directory / CHARACTERIZATION_MANIFEST
    )
    policy_manifest_path = policy_directory / POLICY_MANIFEST
    source_manifest_bytes = source_manifest_path.read_bytes()
    characterization_manifest_bytes = characterization_manifest_path.read_bytes()
    policy_manifest_bytes = policy_manifest_path.read_bytes()
    source_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        source_manifest_bytes
    )
    characterization_manifest = QualityCharacterizationManifestV1.model_validate_json(
        characterization_manifest_bytes
    )
    policy_manifest = HistoricalQualityPolicyManifestV1.model_validate_json(
        policy_manifest_bytes
    )
    _validate_modern_provenance(
        source_manifest,
        source_manifest_bytes,
        characterization_manifest,
        characterization_manifest_bytes,
        policy_manifest,
        graph_edges_path,
    )

    legacy_report_path = (
        legacy_campaign_directory / "profiles/profile-build-report.json"
    )
    legacy_profiles_path = (
        legacy_campaign_directory / "profiles/edge-bucket-profiles.parquet"
    )
    legacy_matches_path = (
        legacy_campaign_directory / "station-matching/station-edge-matches.parquet"
    )
    legacy_finalization_path = legacy_campaign_directory / "finalization-manifest.json"
    legacy_report = json.loads(legacy_report_path.read_text(encoding="utf-8"))
    _validate_legacy_contract(legacy_report, legacy_profiles_path)

    graph = parquet.read_table(
        graph_edges_path,
        columns=["edge_id", "maxspeed_kph", "road_name", "road_class", "ref"],
    ).to_pandas()
    if graph["edge_id"].duplicated().any():
        raise P95TailAnalysisError("Graph edge IDs are not unique")
    matches = parquet.read_table(legacy_matches_path).to_pandas()
    accepted_matches = matches.loc[matches["status"].eq("accepted")].copy()
    if accepted_matches["station_id"].duplicated().any():
        raise P95TailAnalysisError("Accepted legacy station mappings are not unique")

    legacy_frame = _load_legacy_pm_observations(
        legacy_campaign_directory / "observations", accepted_matches, graph
    )
    legacy_analysis = _analyze_legacy(legacy_frame, legacy_report)

    profiles_path = candidate_directory / source_manifest.weekday_profiles.relative_path
    characterization_path = (
        characterization_directory
        / characterization_manifest.output_profile_characterization.relative_path
    )
    policy_path = policy_directory / policy_manifest.output_profile_status.relative_path
    _verify_file(profiles_path, source_manifest.weekday_profiles)
    _verify_file(
        characterization_path,
        characterization_manifest.output_profile_characterization,
    )
    _verify_file(policy_path, policy_manifest.output_profile_status)

    modern = parquet.read_table(profiles_path).to_pandas()
    quality = parquet.read_table(characterization_path).to_pandas()
    policy = parquet.read_table(policy_path).to_pandas()
    station_context = _station_context_map(accepted_matches)
    modern_frame = _build_modern_frame(
        modern,
        quality,
        policy,
        graph,
        station_context,
        reproduced_legacy_p95=float(legacy_analysis["distribution"]["p95"]),
    )
    modern_analysis = _analyze_modern(
        modern_frame,
        reproduced_legacy_p95=float(legacy_analysis["distribution"]["p95"]),
    )
    summary = {
        "schema_version": P95_TAIL_ANALYSIS_SCHEMA_VERSION,
        "artifact_status": "descriptive_analysis_only",
        "calibration_status": "not_calibrated",
        "analyzer": {
            "name": ANALYZER_NAME,
            "version": P95_TAIL_ANALYSIS_VERSION,
        },
        "pm_window": {
            "timezone": "America/Los_Angeles",
            "start_minute_inclusive": PM_START_MINUTE,
            "end_minute_exclusive": PM_END_MINUTE,
            "display": "14:00-19:00 Pacific",
            "source": "legacy PROFILE_PERIODS weekday_afternoon",
        },
        "legacy": legacy_analysis,
        "modern": modern_analysis,
        "interpretation": _interpretation(legacy_analysis, modern_analysis),
        "future_sumo_comparison_targets": _future_targets(modern_frame),
    }

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    try:
        profiles_output = staging / PROFILE_OUTPUT
        table = pa.Table.from_pandas(
            modern_frame[list(TAIL_PROFILE_SCHEMA.names)],
            schema=TAIL_PROFILE_SCHEMA,
            preserve_index=False,
        )
        parquet.write_table(
            table,
            profiles_output,
            compression="zstd",
            use_dictionary=True,
            row_group_size=32_768,
        )
        summary_path = staging / SUMMARY_OUTPUT
        summary_path.write_text(canonical_json(summary), encoding="utf-8")
        markdown_path = staging / MARKDOWN_OUTPUT
        markdown_path.write_text(_markdown(summary), encoding="utf-8")

        legacy_inputs = tuple(
            _file_identity(path, str(path.relative_to(legacy_campaign_directory)))
            for path in (
                legacy_finalization_path,
                legacy_report_path,
                legacy_profiles_path,
                legacy_matches_path,
            )
        )
        manifest_payload: dict[str, Any] = {
            "schema_version": P95_TAIL_ANALYSIS_SCHEMA_VERSION,
            "artifact_type": "commute_help_historical_p95_tail_analysis",
            "evidence_status": "descriptive_analysis_only",
            "calibration_status": "not_calibrated",
            "generated_at": datetime.now(UTC),
            "analyzer": {
                "name": ANALYZER_NAME,
                "code_version": P95_TAIL_ANALYSIS_VERSION,
                "model_version": None,
            },
            "methodology_version": P95_TAIL_ANALYSIS_VERSION,
            "legacy_methodology": {
                "weighting_grain": "accepted_station_interval_observation",
                "source_months": [9, 10],
                "weekdays": "monday_through_friday_pooled",
                "pm_window_start_minute": PM_START_MINUTE,
                "pm_window_end_minute_exclusive": PM_END_MINUTE,
                "slowdown_definition": "app_edge_maxspeed_kph_divided_by_station_speed_kph_clipped_1_to_5",
                "missing_speed": "omitted_from_multiplier_distribution",
                "zero_speed": "division_infinity_clipped_to_5_if_profile_eligible",
                "quantiles": "numpy_linear",
                "stored_reliability_samples": "up_to_10000_evenly_spaced_order_statistics_rounded_6_decimals",
            },
            "modern_methodology": {
                "grain": "app_edge_id_weekday_15_minute_bucket",
                "pm_window_start_minute": PM_START_MINUTE,
                "pm_window_end_minute_exclusive": PM_END_MINUTE,
                "comparison_measure": "within_profile_slowdown_p95",
                "cross_profile_quantiles": "numpy_linear_equal_profile_weight",
                "support_sensitivity": "all_candidate_direct_plus_supplemental_and_direct_only",
                "zero_speed_sensitivity": "compare_profiles_with_and_without_preserved_zero_speed_diagnostics",
                "calibration_effect": "none_descriptive_analysis_only",
            },
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_quality_policy_content_digest": policy_manifest.content_digest,
            "graph_version": source_manifest.graph_version,
            "legacy_inputs": legacy_inputs,
            "modern_profile_input": _file_identity(
                profiles_path,
                source_manifest.weekday_profiles.relative_path,
                len(modern),
            ),
            "quality_characterization_input": _file_identity(
                characterization_path,
                characterization_manifest.output_profile_characterization.relative_path,
                len(quality),
            ),
            "quality_policy_input": _file_identity(
                policy_path,
                policy_manifest.output_profile_status.relative_path,
                len(policy),
            ),
            "graph_edges_input": _file_identity(
                graph_edges_path, graph_edges_path.name, len(graph)
            ),
            "output_profiles": _file_identity(
                profiles_output, PROFILE_OUTPUT, len(modern_frame)
            ),
            "output_summary": _file_identity(summary_path, SUMMARY_OUTPUT),
            "output_markdown": _file_identity(markdown_path, MARKDOWN_OUTPUT),
        }
        manifest_payload["content_digest"] = _content_digest(manifest_payload)
        manifest = P95TailAnalysisManifestV1.model_validate(manifest_payload)
        (staging / MANIFEST_OUTPUT).write_text(
            canonical_json(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        os.replace(staging, output_directory)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_legacy_pm_observations(
    observations_directory: Path,
    accepted_matches: pd.DataFrame,
    graph: pd.DataFrame,
) -> pd.DataFrame:
    dataset = pyarrow_dataset.dataset(
        observations_directory, format="parquet", partitioning="hive"
    )
    expression = (
        pyarrow_dataset.field("local_month").isin([9, 10])
        & (pyarrow_dataset.field("iso_weekday") >= 1)
        & (pyarrow_dataset.field("iso_weekday") <= 5)
        & (pyarrow_dataset.field("minute_of_day") >= PM_START_MINUTE)
        & (pyarrow_dataset.field("minute_of_day") < PM_END_MINUTE)
        & (pyarrow_dataset.field("profile_eligible") == True)
    )
    if "month" in dataset.schema.names:
        expression &= pyarrow_dataset.field("month").isin([9, 10])
    match_columns = [
        "station_id",
        "edge_id",
        "direction",
        "road_name",
        "road_class",
        "edge_ref",
        "highway_name",
        "location_text",
    ]
    mapping = accepted_matches[match_columns]
    edge_speed = graph.set_index("edge_id")["maxspeed_kph"].to_dict()
    frames: list[pd.DataFrame] = []
    for batch in dataset.to_batches(
        columns=[
            "station_or_segment_id",
            "local_date",
            "iso_weekday",
            "minute_of_day",
            "speed_kph",
            "volume",
            "quality_flag",
        ],
        filter=expression,
        batch_size=131_072,
    ):
        frame = batch.to_pandas()
        frame = frame.merge(
            mapping,
            left_on="station_or_segment_id",
            right_on="station_id",
            how="inner",
            validate="many_to_one",
        )
        frame["maxspeed_kph"] = frame["edge_id"].map(edge_speed)
        if frame["maxspeed_kph"].isna().any():
            raise P95TailAnalysisError(
                "Accepted legacy mapping references a missing graph edge"
            )
        frame["multiplier"] = (
            pd.to_numeric(frame["maxspeed_kph"], errors="coerce")
            / pd.to_numeric(frame["speed_kph"], errors="coerce")
        ).clip(lower=1.0, upper=5.0)
        frames.append(frame)
    if not frames:
        raise P95TailAnalysisError("Legacy PM source produced no observations")
    return pd.concat(frames, ignore_index=True)


def _analyze_legacy(frame: pd.DataFrame, report: dict[str, Any]) -> dict[str, Any]:
    values = pd.to_numeric(frame["multiplier"], errors="coerce")
    distribution = exact_quantiles(values.dropna().to_numpy(dtype=float))
    profile = next(
        item for item in report["profiles"] if item["period"] == LEGACY_PERIOD
    )
    reported = profile["stats"]
    if int(distribution["count"]) != int(profile["observation_count"]):
        raise P95TailAnalysisError("Legacy PM observation count no longer reproduces")
    if not math.isclose(
        float(distribution["p95"]),
        float(reported["p95_multiplier"]),
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise P95TailAnalysisError("Legacy PM P95 no longer reproduces")
    threshold = float(distribution["p95"])
    analysis = frame.copy()
    analysis["tail"] = analysis["multiplier"].ge(threshold)
    analysis["weekday"] = analysis["iso_weekday"].map(WEEKDAY_FROM_ISO)
    by_weekday = _group_distribution(analysis, ["weekday"], threshold)
    by_bucket = _group_distribution(analysis, ["minute_of_day"], threshold)
    by_edge = _legacy_edge_summary(analysis, threshold)
    mon_thu = analysis.loc[analysis["iso_weekday"].between(1, 4), "multiplier"]
    friday = analysis.loc[analysis["iso_weekday"].eq(5), "multiplier"]
    positive = pd.to_numeric(analysis["speed_kph"], errors="coerce")
    station_per_edge = analysis.groupby("edge_id")["station_id"].nunique()
    tail_count = int(analysis["tail"].sum())
    multi_station_edges = set(station_per_edge.loc[station_per_edge.gt(1)].index)
    multi_station_tail = analysis["tail"] & analysis["edge_id"].isin(
        multi_station_edges
    )
    duplicated_edge_times = analysis.duplicated(
        ["edge_id", "local_date", "minute_of_day"], keep=False
    )
    duplicated_edge_time_groups = (
        analysis.loc[duplicated_edge_times]
        .groupby(["edge_id", "local_date", "minute_of_day"], dropna=False)
        .ngroups
    )
    return {
        "source": {
            "compiler": report["compiler_version"],
            "matcher": report["matcher_version"],
            "graph_version": report["graph_version"],
            "source_campaign": report["source_campaign"],
            "source_window": profile["source_window"],
            "source_months": [9, 10],
            "weekdays": list(WEEKDAYS),
            "weekend_or_modeled_values_included": False,
        },
        "weighting": {
            "grain": "one accepted normalized station-time observation equals one vote",
            "station_rows_are_downstream_rollups_not_raw_detector_rows": True,
            "accepted_station_count": int(analysis["station_id"].nunique()),
            "accepted_edge_count": int(analysis["edge_id"].nunique()),
            "edges_with_multiple_station_votes": int(station_per_edge.gt(1).sum()),
            "maximum_stations_per_edge": int(station_per_edge.max()),
            "edge_date_bucket_groups_with_multiple_station_rows": duplicated_edge_time_groups,
            "rows_in_multi_station_edge_date_buckets": int(duplicated_edge_times.sum()),
            "tail_samples_from_multi_station_edges": int(multi_station_tail.sum()),
            "tail_share_from_multi_station_edges": float(multi_station_tail.sum())
            / tail_count,
            "weekday_equal_weighting": False,
            "edge_equal_weighting": False,
            "date_equal_weighting": False,
        },
        "slowdown": {
            "definition": "graph maxspeed_kph / station speed_kph",
            "clip": [1.0, 5.0],
            "missing": "NaN omitted by dropna before quantiles",
            "zero": "would become infinity then clip to 5 when profile_eligible",
            "quantile_method": "numpy.quantile default linear",
        },
        "distribution": distribution,
        "historical_clue": LEGACY_CLUE,
        "reproduced_reported_p95": float(reported["p95_multiplier"]),
        "difference_from_clue": float(distribution["p95"]) - LEGACY_CLUE,
        "tail_sample_count_at_reproduced_p95": tail_count,
        "tail_sample_fraction": tail_count / int(distribution["count"]),
        "speed_diagnostics": {
            "zero_speed_rows": int(positive.eq(0).sum()),
            "missing_speed_rows": int(positive.isna().sum()),
            "speed_at_or_below_5_kph_rows": int(positive.le(5).sum()),
            "speed_at_or_below_10_kph_rows": int(positive.le(10).sum()),
        },
        "weekday": by_weekday,
        "time_bucket": by_bucket,
        "top_edges": by_edge[:25],
        "tail_concentration": _concentration(by_edge, tail_count),
        "monday_thursday_distribution": exact_quantiles(mon_thu),
        "friday_distribution": exact_quantiles(friday),
        "stored_reliability_sample_count": len(reported.get("multiplier_samples", [])),
    }


def _build_modern_frame(
    profiles: pd.DataFrame,
    quality: pd.DataFrame,
    policy: pd.DataFrame,
    graph: pd.DataFrame,
    station_context: dict[str, str],
    *,
    reproduced_legacy_p95: float,
) -> pd.DataFrame:
    key = ["app_edge_id", "weekday", "bucket_start_minute"]
    for name, frame in (
        ("profiles", profiles),
        ("quality", quality),
        ("policy", policy),
    ):
        if frame.duplicated(key).any():
            raise P95TailAnalysisError(
                f"Duplicate edge/weekday/bucket identity in {name}"
            )
        if not set(frame["weekday"]).issubset(WEEKDAYS):
            raise P95TailAnalysisError(f"Weekend or invalid weekday in {name}")
    profile_columns = [
        *key,
        "direction",
        "slowdown_p50",
        "slowdown_p85",
        "slowdown_p90",
        "slowdown_p95",
        "speed_kph_p10",
        "speed_kph_p50",
        "speed_kph_p85",
        "speed_kph_p90",
        "speed_kph_p95",
        "flow_vph_p50",
        "flow_vph_p85",
        "flow_vph_p90",
        "flow_vph_p95",
        "source_station_ids_json",
        "calibration_status",
    ]
    quality_columns = [
        *key,
        "reference_speed_kph_p50",
        "zero_speed_date_count",
        "zero_speed_fraction_present",
        "sample_count_p10",
        "sample_count_p50",
        "sample_count_p90",
        "detector_support_count_p50",
        "station_support_count_p50",
    ]
    policy_columns = [
        *key,
        "observed_date_count",
        "coverage_fraction",
        "date_support_class",
        "speed_missing_within_observed_date_count",
    ]
    frame = profiles[profile_columns].merge(
        quality[quality_columns], on=key, how="inner", validate="one_to_one"
    )
    frame = frame.merge(
        policy[policy_columns], on=key, how="inner", validate="one_to_one"
    )
    frame = frame.rename(
        columns={
            "speed_missing_within_observed_date_count": "missing_speed_within_observed_date_count"
        }
    )
    if (
        len(frame) != len(profiles)
        or len(frame) != len(quality)
        or len(frame) != len(policy)
    ):
        raise P95TailAnalysisError(
            "Modern profile/quality/policy identities do not align"
        )
    frame = frame.loc[
        frame["bucket_start_minute"].ge(PM_START_MINUTE)
        & frame["bucket_start_minute"].lt(PM_END_MINUTE)
    ].copy()
    graph_context = graph[
        ["edge_id", "maxspeed_kph", "road_name", "road_class", "ref"]
    ].rename(
        columns={
            "edge_id": "app_edge_id",
            "maxspeed_kph": "graph_maxspeed_kph",
            "ref": "road_ref",
        }
    )
    frame = frame.merge(
        graph_context, on="app_edge_id", how="left", validate="many_to_one"
    )
    frame["road_name"] = frame["road_name"].map(_clean_optional_text)
    frame["road_ref"] = frame["road_ref"].map(_clean_optional_text)
    frame["road_class"] = frame["road_class"].map(_clean_optional_text)
    frame["corridor_label"] = [
        _corridor_label(ref, road_name, edge)
        for ref, road_name, edge in zip(
            frame["road_ref"], frame["road_name"], frame["app_edge_id"], strict=True
        )
    ]
    frame["station_context_json"] = [
        canonical_json(
            sorted(
                {
                    station_context[station]
                    for station in json.loads(source_ids)
                    if station_context.get(station)
                }
            )
        )
        for source_ids in frame["source_station_ids_json"]
    ]
    frame["schema_version"] = P95_TAIL_ANALYSIS_SCHEMA_VERSION
    frame["record_id"] = [
        _record_id(str(edge), str(weekday), int(bucket))
        for edge, weekday, bucket in zip(
            frame["app_edge_id"],
            frame["weekday"],
            frame["bucket_start_minute"],
            strict=True,
        )
    ]
    frame["legacy_p95_clue_or_higher"] = frame["slowdown_p95"].ge(LEGACY_CLUE)
    frame["reproduced_legacy_p95_or_higher"] = frame["slowdown_p95"].ge(
        reproduced_legacy_p95
    )
    ranking = frame.loc[
        frame["slowdown_p95"].notna(), key + ["slowdown_p95"]
    ].sort_values(
        ["slowdown_p95", "app_edge_id", "weekday", "bucket_start_minute"],
        ascending=[False, True, True, True],
        kind="mergesort",
    )
    rank_by_identity = {
        (str(row.app_edge_id), str(row.weekday), int(row.bucket_start_minute)): rank
        for rank, row in enumerate(ranking.itertuples(index=False), start=1)
    }
    frame["tail_rank"] = pd.array(
        [
            rank_by_identity.get((str(edge), str(weekday), int(bucket)))
            for edge, weekday, bucket in zip(
                frame["app_edge_id"],
                frame["weekday"],
                frame["bucket_start_minute"],
                strict=True,
            )
        ],
        dtype="Int64",
    )
    frame["analysis_status"] = "descriptive_only"
    if set(frame["calibration_status"]) != {"not_calibrated"}:
        raise P95TailAnalysisError("Modern source was unexpectedly promoted")
    frame["_weekday_order"] = frame["weekday"].map(WEEKDAY_ORDER)
    return frame.sort_values(
        ["app_edge_id", "_weekday_order", "bucket_start_minute"], kind="mergesort"
    ).reset_index(drop=True)


def _analyze_modern(
    frame: pd.DataFrame, *, reproduced_legacy_p95: float
) -> dict[str, Any]:
    valid = frame.loc[frame["slowdown_p95"].notna()].copy()
    tail = valid.loc[valid["slowdown_p95"].ge(reproduced_legacy_p95)].copy()
    direct = valid.loc[valid["date_support_class"].eq("direct_calibration_evidence")]
    direct_supplemental = valid.loc[
        ~valid["date_support_class"].eq("insufficient_direct_evidence")
    ]
    without_zero = valid.loc[valid["zero_speed_date_count"].eq(0)]
    with_zero = valid.loc[valid["zero_speed_date_count"].gt(0)]
    edge_summary = _modern_group_summary(
        tail, ["app_edge_id", "corridor_label", "road_name", "road_ref", "direction"]
    )
    reference_comparable = frame.loc[
        frame["reference_speed_kph_p50"].notna() & frame["graph_maxspeed_kph"].notna()
    ].copy()
    reference_delta = (
        reference_comparable["reference_speed_kph_p50"]
        - reference_comparable["graph_maxspeed_kph"]
    ).abs()
    return {
        "grain": "one edge/weekday/15-minute profile equals one vote; value is that profile's within-date slowdown P95",
        "pm_profile_count": len(frame),
        "pm_profile_with_slowdown_count": len(valid),
        "pm_profile_missing_slowdown_count": int(len(frame) - len(valid)),
        "distribution": exact_quantiles(valid["slowdown_p95"]),
        "support_sensitivity": {
            "all_candidates": exact_quantiles(valid["slowdown_p95"]),
            "direct_plus_supplemental": exact_quantiles(
                direct_supplemental["slowdown_p95"]
            ),
            "direct_only": exact_quantiles(direct["slowdown_p95"]),
            "all_minus_direct_p95": float(
                exact_quantiles(valid["slowdown_p95"])["p95"]
                - exact_quantiles(direct["slowdown_p95"])["p95"]
            ),
        },
        "zero_speed_sensitivity": {
            "all_candidates": exact_quantiles(valid["slowdown_p95"]),
            "profiles_without_zero_speed_diagnostic": exact_quantiles(
                without_zero["slowdown_p95"]
            ),
            "profiles_with_zero_speed_diagnostic": exact_quantiles(
                with_zero["slowdown_p95"]
            ),
            "tail_profiles_with_zero_speed_diagnostic": int(
                tail["zero_speed_date_count"].gt(0).sum()
            ),
            "tail_profiles_total": len(tail),
        },
        "tail_threshold": reproduced_legacy_p95,
        "tail_profile_count": len(tail),
        "tail_profile_fraction": len(tail) / len(valid),
        "tail_concentration": _concentration(edge_summary, len(tail)),
        "top_edges": edge_summary[:25],
        "top_profiles": _top_profile_records(tail, 25),
        "corridor": _modern_group_summary(tail, ["corridor_label"]),
        "weekday": _modern_segment_summary(valid, tail, "weekday"),
        "time_bucket": _modern_segment_summary(valid, tail, "bucket_start_minute"),
        "direction": _modern_segment_summary(valid, tail, "direction"),
        "support_class": _modern_segment_summary(valid, tail, "date_support_class"),
        "support_context": {
            "all_profiles": _support_summary(valid),
            "tail_profiles": _support_summary(tail),
        },
        "artifact_checks": {
            "duplicate_profile_identities": int(
                frame.duplicated(
                    ["app_edge_id", "weekday", "bucket_start_minute"]
                ).sum()
            ),
            "weekend_profiles": int((~frame["weekday"].isin(WEEKDAYS)).sum()),
            "mapping_review_or_unmatched_profiles": 0,
            "graph_reference_speed_comparable_profile_count": len(reference_comparable),
            "graph_reference_speed_mismatch_profile_count": int(
                reference_delta.gt(1e-9).sum()
            ),
            "graph_reference_speed_max_absolute_delta_kph": float(
                reference_delta.max()
            ),
            "note": "Phase 1.3 emits only accepted app-edge associations; no review/unmatched rows can enter these profiles.",
        },
    }


def _group_distribution(
    frame: pd.DataFrame, group_columns: list[str], threshold: float
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    group_key: str | list[str] = (
        group_columns[0] if len(group_columns) == 1 else group_columns
    )
    for key, group in frame.groupby(group_key, sort=True, dropna=False):
        keys = (key,) if len(group_columns) == 1 else tuple(key)
        row = {
            column: _json_value(value)
            for column, value in zip(group_columns, keys, strict=True)
        }
        row.update(
            {
                "sample_count": int(group["multiplier"].notna().sum()),
                "tail_count": int(group["multiplier"].ge(threshold).sum()),
                "p50": float(group["multiplier"].quantile(0.50)),
                "p85": float(group["multiplier"].quantile(0.85)),
                "p90": float(group["multiplier"].quantile(0.90)),
                "p95": float(group["multiplier"].quantile(0.95)),
                "p99": float(group["multiplier"].quantile(0.99)),
            }
        )
        rows.append(row)
    return rows


def _legacy_edge_summary(frame: pd.DataFrame, threshold: float) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    columns = ["edge_id", "road_name", "edge_ref", "direction"]
    for key, group in frame.groupby(columns, sort=True, dropna=False):
        tail_count = int(group["multiplier"].ge(threshold).sum())
        if tail_count == 0:
            continue
        result.append(
            {
                **{
                    column: _json_value(value)
                    for column, value in zip(columns, key, strict=True)
                },
                "corridor_label": _corridor_label(key[2], key[1], key[0]),
                "sample_count": int(group["multiplier"].notna().sum()),
                "tail_count": tail_count,
                "station_count": int(group["station_id"].nunique()),
                "p95": float(group["multiplier"].quantile(0.95)),
            }
        )
    return sorted(result, key=lambda row: (-row["tail_count"], str(row["edge_id"])))


def _modern_group_summary(
    frame: pd.DataFrame, columns: list[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    group_key: str | list[str] = columns[0] if len(columns) == 1 else columns
    for key, group in frame.groupby(group_key, sort=True, dropna=False):
        keys = (key,) if len(columns) == 1 else tuple(key)
        result.append(
            {
                **{
                    column: _json_value(value)
                    for column, value in zip(columns, keys, strict=True)
                },
                "tail_count": len(group),
                "maximum_slowdown_p95": float(group["slowdown_p95"].max()),
                "median_slowdown_p95": float(group["slowdown_p95"].median()),
            }
        )
    return sorted(
        result,
        key=lambda row: (-row["tail_count"], canonical_json(row)),
    )


def _modern_segment_summary(
    all_profiles: pd.DataFrame, tail: pd.DataFrame, column: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    values = sorted(
        all_profiles[column].drop_duplicates(), key=lambda value: str(value)
    )
    if column == "weekday":
        values = sorted(values, key=lambda value: WEEKDAY_ORDER[str(value)])
    for value in values:
        group = all_profiles.loc[all_profiles[column].eq(value)]
        group_tail = tail.loc[tail[column].eq(value)]
        rows.append(
            {
                column: _json_value(value),
                "profile_count": len(group),
                "tail_count": len(group_tail),
                "tail_fraction": len(group_tail) / len(group),
                "slowdown_p95_distribution": exact_quantiles(group["slowdown_p95"]),
            }
        )
    return rows


def _support_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "profile_count": len(frame),
        "observed_date_count": exact_quantiles(frame["observed_date_count"]),
        "coverage_fraction": exact_quantiles(frame["coverage_fraction"]),
        "sample_count_p50": exact_quantiles(frame["sample_count_p50"].dropna()),
        "detector_support_count_p50": exact_quantiles(
            frame["detector_support_count_p50"].dropna()
        ),
        "station_support_count_p50": exact_quantiles(
            frame["station_support_count_p50"].dropna()
        ),
        "profiles_with_zero_speed": int(frame["zero_speed_date_count"].gt(0).sum()),
        "profiles_with_missing_speed": int(
            frame["missing_speed_within_observed_date_count"].gt(0).sum()
        ),
    }


def _top_profile_records(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    ordered = frame.sort_values(
        ["slowdown_p95", "app_edge_id", "weekday", "bucket_start_minute"],
        ascending=[False, True, True, True],
        kind="mergesort",
    )
    records: list[dict[str, Any]] = []
    for row in ordered.head(limit).itertuples(index=False):
        records.append(
            {
                "app_edge_id": row.app_edge_id,
                "corridor_label": row.corridor_label,
                "road_name": row.road_name,
                "road_ref": row.road_ref,
                "road_class": row.road_class,
                "direction": row.direction,
                "weekday": row.weekday,
                "bucket_start_minute": int(row.bucket_start_minute),
                "station_context": json.loads(row.station_context_json),
                "slowdown_p50": _optional_float(row.slowdown_p50),
                "slowdown_p85": _optional_float(row.slowdown_p85),
                "slowdown_p90": _optional_float(row.slowdown_p90),
                "slowdown_p95": _optional_float(row.slowdown_p95),
                "reference_speed_kph_p50": _optional_float(row.reference_speed_kph_p50),
                "speed_kph_p10": _optional_float(row.speed_kph_p10),
                "speed_kph_p50": _optional_float(row.speed_kph_p50),
                "flow_vph_p50": _optional_float(row.flow_vph_p50),
                "observed_date_count": int(row.observed_date_count),
                "coverage_fraction": float(row.coverage_fraction),
                "date_support_class": row.date_support_class,
                "sample_count_p50": _optional_float(row.sample_count_p50),
                "detector_support_count_p50": _optional_float(
                    row.detector_support_count_p50
                ),
                "station_support_count_p50": _optional_float(
                    row.station_support_count_p50
                ),
                "zero_speed_date_count": int(row.zero_speed_date_count),
                "missing_speed_within_observed_date_count": int(
                    row.missing_speed_within_observed_date_count
                ),
            }
        )
    return records


def _concentration(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    counts = [int(row["tail_count"]) for row in rows]
    return {
        f"top_{amount}_share": sum(counts[:amount]) / total if total else None
        for amount in (1, 5, 10, 25)
    }


def _interpretation(legacy: dict[str, Any], modern: dict[str, Any]) -> dict[str, Any]:
    direct = modern["support_sensitivity"]["direct_only"]
    all_profiles = modern["support_sensitivity"]["all_candidates"]
    zero = modern["zero_speed_sensitivity"]
    return {
        "old_p95_explanation": (
            "The value is the linear 95th percentile of 634,247 accepted Sep/Oct "
            "weekday 14:00-19:00 station-time slowdown samples after clipping to 1-5. "
            "It is not an edge-specific reliability percentile."
        ),
        "recurring_bottleneck_signal": (
            "The modern tail remains large among direct-support profiles and clusters "
            "on recurring freeway edge/time combinations, so a substantial part is "
            "real recurring congestion signal."
        ),
        "pooling_weighting_effect": (
            "The old pool gives repeated longitudinal stations and every station-time "
            "row separate votes, then applies the result route-wide. This overrepresents "
            "well-instrumented corridors and hides weekday/time heterogeneity."
        ),
        "possible_artifacts": (
            "Old station-level rollups, multiple stations per edge, clipping at 5, and "
            "reference-speed choice affect the pooled shape. No review/unmatched mapping "
            "rows or actual zero-speed rows entered the reproduced old PM pool."
        ),
        "direct_only_p95": direct["p95"],
        "all_candidate_p95": all_profiles["p95"],
        "sparse_support_p95_effect": modern["support_sensitivity"][
            "all_minus_direct_p95"
        ],
        "zero_speed_tail_profile_count": zero[
            "tail_profiles_with_zero_speed_diagnostic"
        ],
        "global_multiplier_limitation": (
            "One route-wide multiplier cannot say which edge, direction, weekday, or "
            "15-minute interval is slow and compounds neither segment exposure nor "
            "correlation correctly."
        ),
        "modern_comparison_caveat": (
            "The modern comparison distributes within-profile P95 values and does not "
            "apply the legacy 1-5 clip, so its upper quantiles are not a replacement "
            "route-wide multiplier or a like-for-like sample distribution."
        ),
    }


def _future_targets(frame: pd.DataFrame) -> list[dict[str, Any]]:
    direct = frame.loc[
        frame["date_support_class"].eq("direct_calibration_evidence")
        & frame["slowdown_p95"].notna()
    ].sort_values(
        ["slowdown_p95", "app_edge_id", "weekday", "bucket_start_minute"],
        ascending=[False, True, True, True],
        kind="mergesort",
    )
    targets: list[dict[str, Any]] = []
    for row in direct.head(25).itertuples(index=False):
        targets.append(
            {
                "app_edge_id": row.app_edge_id,
                "corridor_label": row.corridor_label,
                "direction": row.direction,
                "weekday": row.weekday,
                "bucket_start_minute": int(row.bucket_start_minute),
                "slowdown_p50": _optional_float(row.slowdown_p50),
                "slowdown_p95": _optional_float(row.slowdown_p95),
                "speed_kph_p50": _optional_float(row.speed_kph_p50),
                "flow_vph_p50": _optional_float(row.flow_vph_p50),
                "observed_date_count": int(row.observed_date_count),
            }
        )
    return targets


def _markdown(summary: dict[str, Any]) -> str:
    legacy = summary["legacy"]
    modern = summary["modern"]
    support = modern["support_sensitivity"]
    lines = [
        "# Phase 1.4 — Why the old PM P95 was about 2.651x",
        "",
        "> Descriptive analysis only. No profile is calibrated or promoted by this report.",
        "",
        "## Answer",
        "",
        legacy["source"]["source_window"]
        + " supplied **"
        + f"{legacy['distribution']['count']:,}** accepted station-time observations in the exact legacy 14:00–19:00 Pacific PM window. The compiler calculated `maxspeed / observed station speed`, floored values at 1, capped them at 5, pooled Monday–Friday and every mapped edge/direction together, and applied NumPy's linear P95. That reproduces **"
        + f"{legacy['reproduced_reported_p95']:.12f}x** (the remembered 2.651x clue).",
        "",
        "The upper tail is not one thing. It contains recurring freeway bottlenecks, plus a weighting effect from giving every station-time row a vote—including multiple longitudinal stations on some edges. Sparse modern profiles make the cross-profile tail heavier, but the tail remains substantial under the >=90% direct-evidence policy. Zero-speed rows did **not** create the old PM P95.",
        "",
        "## Old versus modern quantiles",
        "",
        "| Distribution | Votes | P50 | P85 | P90 | P95 | P99 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _quantile_markdown_row("Old station-time pool", legacy["distribution"]),
        _quantile_markdown_row("Modern profile P95: all", support["all_candidates"]),
        _quantile_markdown_row(
            "Modern profile P95: direct + supplemental",
            support["direct_plus_supplemental"],
        ),
        _quantile_markdown_row(
            "Modern profile P95: direct only", support["direct_only"]
        ),
        "",
        "These distributions answer different questions. The old row is the distribution of individual station-time slowdowns. Each modern row is a distribution across edge/weekday/bucket **within-profile P95s**.",
        "The modern ratios are not clipped to the legacy 1–5 range, so the modern cross-profile P95 is a tail-location diagnostic—not a candidate global multiplier.",
        "",
        "## Legacy mechanics and artifacts",
        "",
        "- PM window: **14:00 inclusive to 19:00 exclusive**, Pacific local time.",
        "- Calendar: September/October 2024–2025, Monday–Friday pooled. Friday was included; weekends and modeled fallback rows were not.",
        f"- Accepted stations / edges: **{legacy['weighting']['accepted_station_count']:,} / {legacy['weighting']['accepted_edge_count']:,}**.",
        f"- Edges with multiple accepted station votes: **{legacy['weighting']['edges_with_multiple_station_votes']:,}**; maximum **{legacy['weighting']['maximum_stations_per_edge']}** stations on one edge.",
        f"- Actual zero / missing speeds in the PM pool: **{legacy['speed_diagnostics']['zero_speed_rows']:,} / {legacy['speed_diagnostics']['missing_speed_rows']:,}**.",
        "- The old compact edge/bucket artifact later uses medians across station rows, but the pooled reliability distribution was collected **before** that collapse.",
        "- Reliability simulation resampled a deterministic 10,000-order-statistic approximation of the pool and applied one chosen multiplier to the whole route.",
        "",
        "## Tail concentration",
        "",
        f"Modern PM profiles at or above the reproduced legacy P95: **{modern['tail_profile_count']:,} / {modern['pm_profile_with_slowdown_count']:,}** ({modern['tail_profile_fraction']:.2%}).",
        "",
        "| Scope | Top 1 | Top 5 | Top 10 | Top 25 |",
        "|---|---:|---:|---:|---:|",
        _concentration_row("Old station samples by edge", legacy["tail_concentration"]),
        _concentration_row("Modern profiles by edge", modern["tail_concentration"]),
        "",
        "### Top modern edges",
        "",
        "| Edge | Corridor | Direction | Tail profiles | Median profile P95 | Maximum profile P95 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in modern["top_edges"][:25]:
        lines.append(
            f"| `{row['app_edge_id']}` | {row['corridor_label']} | {row['direction']} | {row['tail_count']:,} | {row['median_slowdown_p95']:.3f}x | {row['maximum_slowdown_p95']:.3f}x |"
        )
    lines.extend(
        [
            "",
            "### Top modern edge/time profiles",
            "",
            "| Edge | Corridor/context | Direction | Weekday | Time | P50 | P95 | Dates | Coverage | Support | Zero speeds |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|---:|",
        ]
    )
    for row in modern["top_profiles"]:
        context = "; ".join(row["station_context"][:2]) or row["corridor_label"]
        lines.append(
            f"| `{row['app_edge_id']}` | {context} | {row['direction']} | {row['weekday']} | {_minute_label(row['bucket_start_minute'])} | {row['slowdown_p50']:.3f}x | {row['slowdown_p95']:.3f}x | {row['observed_date_count']:,} | {row['coverage_fraction']:.1%} | {row['date_support_class']} | {row['zero_speed_date_count']:,} |"
        )
    lines.extend(["", "### Weekday", "", _segment_table(modern["weekday"], "weekday")])
    lines.extend(
        [
            "",
            "### 15-minute bucket",
            "",
            _segment_table(modern["time_bucket"], "bucket_start_minute"),
        ]
    )
    lines.extend(
        [
            "",
            "## Support and zero-speed sensitivity",
            "",
            f"Excluding profiles below 90% date support changes the cross-profile P95 from **{support['all_candidates']['p95']:.3f}x** to **{support['direct_only']['p95']:.3f}x** (difference **{modern['support_sensitivity']['all_minus_direct_p95']:.3f}x**). Sparse profiles amplify the tail, but do not explain it away.",
            f"Only **{modern['zero_speed_sensitivity']['tail_profiles_with_zero_speed_diagnostic']:,} / {modern['zero_speed_sensitivity']['tail_profiles_total']:,}** modern tail profiles contain any zero-speed date. Removing every profile with zero-speed diagnostics changes the all-candidate P95 from **{modern['zero_speed_sensitivity']['all_candidates']['p95']:.3f}x** to **{modern['zero_speed_sensitivity']['profiles_without_zero_speed_diagnostic']['p95']:.3f}x**.",
            "",
            "Low support is reported as context, not a rejection rule. The machine-readable summary contains observed-date, coverage, aggregated sample-count, detector-support, and station-support distributions for all profiles versus tail profiles.",
            "",
            "## What the old P95 means",
            "",
            "**Recurring signal:** repeated direct-support edge/time profiles on I-5, I-84/US-30, US-26, I-205, and OR-217 show severe slowdown across multiple weekdays and neighboring PM buckets.",
            "",
            "**Pooling effect:** instrument-rich and longitudinally duplicated corridors cast more votes. Monday–Friday and five hours of PM conditions are mixed, then one multiplier is imposed on an entire route.",
            "",
            "**Possible artifact:** the legacy input is a downstream station/time rollup, not raw detector observations; ratios are clipped at 5; graph reference speed defines the denominator; multiple station rows can represent the same edge/time. Review/unmatched mappings did not leak into either compared profile set.",
            "",
            "## Phase 2 comparison targets",
            "",
            "Later SUMO comparison should use direct-support edge/weekday/15-minute targets, comparing flow VPH, speed, slowdown P50/P85/P90/P95, and peak timing. Occupancy remains secondary until detector/SUMO semantics align. The top 25 direct-support target identities are in the JSON report.",
            "",
            "## Boundary",
            "",
            "This report changes no policy and promotes nothing. All sources and outputs remain `not_calibrated`.",
            "",
        ]
    )
    return "\n".join(lines)


def _segment_table(rows: list[dict[str, Any]], field: str) -> str:
    lines = [
        f"| {field.replace('_', ' ').title()} | Profiles | Tail | Tail share | P95 of profile P95 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        label = row[field]
        if field == "bucket_start_minute":
            label = _minute_label(int(label))
        lines.append(
            f"| {label} | {row['profile_count']:,} | {row['tail_count']:,} | {row['tail_fraction']:.2%} | {row['slowdown_p95_distribution']['p95']:.3f}x |"
        )
    return "\n".join(lines)


def _quantile_markdown_row(label: str, values: dict[str, Any]) -> str:
    return (
        f"| {label} | {values['count']:,} | {values['p50']:.3f}x | "
        f"{values['p85']:.3f}x | {values['p90']:.3f}x | "
        f"{values['p95']:.3f}x | {values['p99']:.3f}x |"
    )


def _concentration_row(label: str, values: dict[str, Any]) -> str:
    return (
        f"| {label} | {values['top_1_share']:.2%} | {values['top_5_share']:.2%} | "
        f"{values['top_10_share']:.2%} | {values['top_25_share']:.2%} |"
    )


def _validate_modern_provenance(
    source: HistoricalCalibrationCompilerManifestV1,
    source_bytes: bytes,
    characterization: QualityCharacterizationManifestV1,
    characterization_bytes: bytes,
    policy: HistoricalQualityPolicyManifestV1,
    graph_path: Path,
) -> None:
    if (
        source.calibration_status != "not_calibrated"
        or policy.calibration_status != "not_calibrated"
    ):
        raise P95TailAnalysisError("A source was promoted before Phase 1.4")
    if source.content_digest != characterization.source_phase_1_3_content_digest:
        raise P95TailAnalysisError(
            "Characterization does not describe this profile artifact"
        )
    if source.content_digest != policy.source_phase_1_3_content_digest:
        raise P95TailAnalysisError("Policy does not describe this profile artifact")
    if characterization.content_digest != policy.source_characterization_content_digest:
        raise P95TailAnalysisError("Policy does not describe this characterization")
    if (
        hashlib.sha256(source_bytes).hexdigest()
        != policy.source_phase_1_3_manifest_sha256
    ):
        raise P95TailAnalysisError("Phase 1.3 manifest hash changed")
    if (
        hashlib.sha256(characterization_bytes).hexdigest()
        != policy.source_characterization_manifest_sha256
    ):
        raise P95TailAnalysisError("Characterization manifest hash changed")
    if _sha256_file(graph_path) != source.graph_edges_sha256:
        raise P95TailAnalysisError("Graph edge artifact hash changed")


def _validate_legacy_contract(report: dict[str, Any], profiles_path: Path) -> None:
    if report.get("compiler_version") != "portal-background-profile-compiler-v1":
        raise P95TailAnalysisError("Unsupported legacy profile compiler")
    if _sha256_file(profiles_path) != report.get("artifact_sha256"):
        raise P95TailAnalysisError("Legacy compact profile artifact hash changed")
    periods = {item.get("period") for item in report.get("profiles", [])}
    if LEGACY_PERIOD not in periods:
        raise P95TailAnalysisError("Legacy PM profile is absent")


def _verify_file(path: Path, identity: Any) -> None:
    if not path.is_file() or path.stat().st_size != identity.byte_count:
        raise P95TailAnalysisError(f"Input metadata mismatch: {path.name}")
    if _sha256_file(path) != identity.sha256:
        raise P95TailAnalysisError(f"Input hash mismatch: {path.name}")
    if (
        identity.row_count is not None
        and parquet.read_metadata(path).num_rows != identity.row_count
    ):
        raise P95TailAnalysisError(f"Input row count mismatch: {path.name}")


def _station_context_map(matches: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in matches.itertuples(index=False):
        context = _clean_optional_text(row.location_text)
        if not context:
            continue
        station_id = str(row.station_id)
        result[station_id] = context
        if station_id.startswith("portal-station-"):
            result[station_id.removeprefix("portal-station-")] = context
    return result


def _corridor_label(ref: Any, road_name: Any, edge: Any) -> str:
    ref_text = _clean_optional_text(ref)
    name_text = _clean_optional_text(road_name)
    if ref_text and name_text and name_text.lower() != "unnamed road":
        return f"{ref_text} — {name_text}"
    return ref_text or name_text or f"edge {edge}"


def _clean_optional_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    return text if text and text.lower() not in {"nan", "none"} else None


def _record_id(edge: str, weekday: str, bucket: int) -> str:
    payload = "\x1f".join((P95_TAIL_ANALYSIS_VERSION, edge, weekday, str(bucket)))
    return f"portal.p95-tail.{hashlib.sha256(payload.encode()).hexdigest()[:40]}"


def _quantile_name(value: float) -> str:
    return {
        0.0: "min",
        0.01: "p01",
        0.05: "p05",
        0.10: "p10",
        0.25: "p25",
        0.50: "p50",
        0.75: "p75",
        0.85: "p85",
        0.90: "p90",
        0.95: "p95",
        0.975: "p97_5",
        0.99: "p99",
        1.0: "max",
    }[value]


def _minute_label(value: int) -> str:
    hour, minute = divmod(value, 60)
    return f"{hour:02d}:{minute:02d}"


def _optional_float(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _file_identity(
    path: Path, relative_path: str, row_count: int | None = None
) -> dict[str, Any]:
    return {
        "relative_path": relative_path,
        "sha256": _sha256_file(path),
        "byte_count": path.stat().st_size,
        "row_count": row_count,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_digest(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("generated_at", None)
    payload.pop("content_digest", None)
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
