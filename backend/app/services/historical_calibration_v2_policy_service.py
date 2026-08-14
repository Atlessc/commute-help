"""Apply the human-reviewed Phase 1.3 policy to candidate evidence profiles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
from pyarrow import parquet

from backend.app.schemas.historical_calibration_v2 import (
    HistoricalCalibrationCompilerManifestV1,
)
from backend.app.schemas.historical_calibration_v2_policy import (
    HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION,
    HISTORICAL_QUALITY_POLICY_VERSION,
    DateSupportClass,
    HistoricalQualityPolicyManifestV1,
    HistoricalQualityPolicyV1,
    MeasurementEvidenceStatus,
)
from backend.app.schemas.historical_calibration_v2_quality import (
    QualityCharacterizationManifestV1,
)
from backend.app.services.portal_calibration_v2_importer import canonical_json

POLICY_COMPILER_NAME = "historical_calibration_v2_quality_policy"
PROFILE_STATUS_OUTPUT = "quality-policy-profile-status.parquet"
REPORT_JSON_OUTPUT = "quality-policy-report.json"
REPORT_MARKDOWN_OUTPUT = "QUALITY_POLICY_REPORT.md"
MANIFEST_OUTPUT = "quality-policy-manifest.json"
SOURCE_MANIFEST = "historical-calibration-manifest.json"
CHARACTERIZATION_MANIFEST = "quality-characterization-manifest.json"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKDAY_ORDER = {weekday: index for index, weekday in enumerate(WEEKDAYS)}

SUPPORT_PREFIXES = (
    "sample_count",
    "detector_support_count",
    "station_support_count",
)
SUPPORT_SUFFIXES = (
    "min",
    "p01",
    "p05",
    "p10",
    "p25",
    "p50",
    "p75",
    "p90",
    "p95",
    "p99",
    "max",
)
SUPPORT_COLUMNS = tuple(
    f"{prefix}_{suffix}" for prefix in SUPPORT_PREFIXES for suffix in SUPPORT_SUFFIXES
)
INPUT_COLUMNS = (
    "record_id",
    "app_edge_id",
    "direction",
    "weekday",
    "bucket_start_minute",
    "possible_date_count",
    "observed_date_count",
    "missing_date_count",
    "coverage_fraction",
    "present_flow_date_count",
    "missing_flow_date_count",
    "zero_flow_date_count",
    "zero_flow_fraction_present",
    "present_speed_date_count",
    "missing_speed_date_count",
    "zero_speed_date_count",
    "zero_speed_fraction_present",
    "present_occupancy_date_count",
    "missing_occupancy_date_count",
    "occupancy_above_100_date_count",
    "occupancy_above_100_fraction_present",
    "sample_count_present_date_count",
    "sample_count_missing_date_count",
    "sample_count_zero_date_count",
    *SUPPORT_COLUMNS,
    "source_profile_status",
    "source_evidence_level",
    "calibration_status",
    "characterization_status",
)

POLICY = HistoricalQualityPolicyV1.model_validate(
    {
        "grain": "app_edge_id_weekday_15_minute_bucket",
        "date_support": {
            "possible_date_semantics": "inclusive_source_window_calendar_by_weekday",
            "direct_minimum_coverage_fraction": 0.9,
            "supplemental_minimum_coverage_fraction": 0.75,
            "threshold_evaluation": "exact_observed_over_possible_integer_ratio",
            "sparse_profiles": "retain_never_delete",
        },
        "flow": {
            "direct_requirement": "direct_date_support_and_present_flow_evidence",
            "zero_flow": "preserve_as_valid_numeric_evidence",
            "cross_measurement_anomalies": "do_not_invalidate_flow",
        },
        "speed": {
            "direct_requirement": "direct_date_support_and_at_least_one_nonzero_numeric_speed",
            "missing_speed": "preserve_and_account",
            "zero_speed": "preserve_as_diagnostic_no_automatic_profile_rejection",
            "slowdown_for_zero_speed": "undefined",
            "zero_speed_threshold": "none",
        },
        "occupancy": {
            "direct_requirement": "direct_date_support_and_at_least_one_occupancy_not_above_100",
            "above_100": "preserve_but_exclude_only_from_occupancy_calibration",
            "repair": "none_no_clipping",
            "cross_measurement_anomalies": "do_not_invalidate_flow_or_speed",
        },
        "sample_count": {
            "use": "descriptive_support_and_anomaly_context_only",
            "global_minimum_threshold": None,
            "rationale": "aggregated_edge_date_bucket_countreadings_not_raw_detector_distribution",
        },
        "network_fallback": {
            "insufficient_history_means": "insufficient_for_direct_calibration_not_zero_traffic_or_closed_edge",
            "downstream_requirement": "use_separate_prior_or_model_fallback",
        },
    }
)

POLICY_STATUS_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("record_id", pa.string(), nullable=False),
        pa.field("source_characterization_record_id", pa.string(), nullable=False),
        pa.field("app_edge_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("weekday", pa.string(), nullable=False),
        pa.field("bucket_start_minute", pa.int16(), nullable=False),
        pa.field("possible_date_count", pa.int32(), nullable=False),
        pa.field("observed_date_count", pa.int32(), nullable=False),
        pa.field("coverage_fraction", pa.float64(), nullable=False),
        pa.field("date_support_class", pa.string(), nullable=False),
        pa.field("flow_evidence_status", pa.string(), nullable=False),
        pa.field("flow_direct_calibration_eligible", pa.bool_(), nullable=False),
        pa.field("flow_present_date_count", pa.int32(), nullable=False),
        pa.field("flow_missing_date_count", pa.int32(), nullable=False),
        pa.field("flow_zero_date_count", pa.int32(), nullable=False),
        pa.field("flow_zero_fraction_present", pa.float64()),
        pa.field("speed_evidence_status", pa.string(), nullable=False),
        pa.field("speed_direct_calibration_eligible", pa.bool_(), nullable=False),
        pa.field("speed_present_date_count", pa.int32(), nullable=False),
        pa.field("speed_nonzero_eligible_date_count", pa.int32(), nullable=False),
        pa.field("speed_missing_date_count", pa.int32(), nullable=False),
        pa.field(
            "speed_missing_within_observed_date_count", pa.int32(), nullable=False
        ),
        pa.field("speed_zero_diagnostic_date_count", pa.int32(), nullable=False),
        pa.field("speed_zero_fraction_present", pa.float64()),
        pa.field("zero_speed_slowdown_policy", pa.string(), nullable=False),
        pa.field("occupancy_evidence_status", pa.string(), nullable=False),
        pa.field("occupancy_direct_calibration_eligible", pa.bool_(), nullable=False),
        pa.field("occupancy_present_date_count", pa.int32(), nullable=False),
        pa.field("occupancy_eligible_date_count", pa.int32(), nullable=False),
        pa.field("occupancy_missing_date_count", pa.int32(), nullable=False),
        pa.field("occupancy_above_100_excluded_date_count", pa.int32(), nullable=False),
        pa.field("occupancy_above_100_fraction_present", pa.float64()),
        *[pa.field(column, pa.float64()) for column in SUPPORT_COLUMNS],
        pa.field("sample_count_policy", pa.string(), nullable=False),
        pa.field("source_profile_status", pa.string(), nullable=False),
        pa.field("source_evidence_level", pa.string(), nullable=False),
        pa.field("calibration_status", pa.string(), nullable=False),
        pa.field("policy_application_status", pa.string(), nullable=False),
        pa.field("policy_version", pa.string(), nullable=False),
    ]
)


class HistoricalQualityPolicyError(RuntimeError):
    """The source evidence cannot safely receive policy-v1."""


def classify_date_support(observed: int, possible: int) -> DateSupportClass:
    """Classify exact date support without a rounded or hardcoded day threshold."""
    if possible <= 0 or observed < 0 or observed > possible:
        raise ValueError(
            "Date support requires 0 <= observed <= possible and possible > 0"
        )
    if observed * 10 >= possible * 9:
        return "direct_calibration_evidence"
    if observed * 4 >= possible * 3:
        return "supplemental_evidence"
    return "insufficient_direct_evidence"


def apply_historical_calibration_v2_quality_policy(
    *,
    candidate_directory: Path,
    characterization_directory: Path,
    output_directory: Path,
) -> HistoricalQualityPolicyManifestV1:
    """Apply policy-v1 atomically without modifying either source artifact."""
    candidate_directory = candidate_directory.resolve()
    characterization_directory = characterization_directory.resolve()
    output_directory = output_directory.resolve()
    if output_directory.exists():
        raise HistoricalQualityPolicyError(
            "Policy output already exists and is immutable"
        )
    if output_directory in (candidate_directory, characterization_directory):
        raise HistoricalQualityPolicyError(
            "Policy output must be separate from its inputs"
        )

    source_manifest_path = candidate_directory / SOURCE_MANIFEST
    characterization_manifest_path = (
        characterization_directory / CHARACTERIZATION_MANIFEST
    )
    source_manifest_bytes = source_manifest_path.read_bytes()
    characterization_manifest_bytes = characterization_manifest_path.read_bytes()
    source_manifest = HistoricalCalibrationCompilerManifestV1.model_validate_json(
        source_manifest_bytes
    )
    characterization_manifest = QualityCharacterizationManifestV1.model_validate_json(
        characterization_manifest_bytes
    )
    _validate_provenance(
        source_manifest, source_manifest_bytes, characterization_manifest
    )

    input_path = (
        characterization_directory
        / characterization_manifest.output_profile_characterization.relative_path
    )
    report_input_path = (
        characterization_directory / characterization_manifest.output_json.relative_path
    )
    _verify_file(input_path, characterization_manifest.output_profile_characterization)
    _verify_file(report_input_path, characterization_manifest.output_json)
    characterization_report = json.loads(report_input_path.read_text(encoding="utf-8"))

    frame = parquet.read_table(input_path, columns=list(INPUT_COLUMNS)).to_pandas()
    policy_frame = _apply_policy(frame, characterization_manifest.content_digest)
    report = _build_report(
        source_manifest=source_manifest,
        characterization_manifest=characterization_manifest,
        characterization_report=characterization_report,
        frame=policy_frame,
    )

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    try:
        output_path = staging / PROFILE_STATUS_OUTPUT
        table = pa.Table.from_pandas(
            policy_frame[list(POLICY_STATUS_SCHEMA.names)],
            schema=POLICY_STATUS_SCHEMA,
            preserve_index=False,
        )
        parquet.write_table(
            table,
            output_path,
            compression="zstd",
            use_dictionary=True,
            row_group_size=32_768,
        )
        report_json_path, report_markdown_path = _write_report_files(staging, report)
        manifest_payload: dict[str, Any] = {
            "schema_version": HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION,
            "artifact_type": "commute_help_historical_calibration_quality_policy_application",
            "artifact_status": "quality_policy_applied_unvalidated",
            "calibration_status": "not_calibrated",
            "source_profile_status": "candidate_unvalidated",
            "generated_at": datetime.now(UTC),
            "compiler": {
                "name": POLICY_COMPILER_NAME,
                "code_version": HISTORICAL_QUALITY_POLICY_VERSION,
                "model_version": None,
            },
            "policy_version": HISTORICAL_QUALITY_POLICY_VERSION,
            "policy": POLICY.model_dump(mode="json"),
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_phase_1_3_manifest_sha256": hashlib.sha256(
                source_manifest_bytes
            ).hexdigest(),
            "source_characterization_content_digest": characterization_manifest.content_digest,
            "source_characterization_manifest_sha256": hashlib.sha256(
                characterization_manifest_bytes
            ).hexdigest(),
            "graph_version": source_manifest.graph_version,
            "input_profile_characterization": _file_identity(
                input_path,
                characterization_manifest.output_profile_characterization.relative_path,
                len(frame),
            ),
            "input_characterization_report": _file_identity(
                report_input_path,
                characterization_manifest.output_json.relative_path,
            ),
            "output_profile_status": _file_identity(
                output_path, PROFILE_STATUS_OUTPUT, len(policy_frame)
            ),
            "output_report_json": _file_identity(report_json_path, REPORT_JSON_OUTPUT),
            "output_report_markdown": _file_identity(
                report_markdown_path, REPORT_MARKDOWN_OUTPUT
            ),
        }
        manifest_payload["content_digest"] = _content_digest(manifest_payload)
        manifest = HistoricalQualityPolicyManifestV1.model_validate(manifest_payload)
        (staging / MANIFEST_OUTPUT).write_text(
            canonical_json(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        os.replace(staging, output_directory)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_provenance(
    source: HistoricalCalibrationCompilerManifestV1,
    source_bytes: bytes,
    characterization: QualityCharacterizationManifestV1,
) -> None:
    if source.calibration_status != "not_calibrated":
        raise HistoricalQualityPolicyError("Candidate source is not not_calibrated")
    if characterization.calibration_status != "not_calibrated":
        raise HistoricalQualityPolicyError("Characterization source was promoted")
    if characterization.evidence_status != "descriptive_characterization_only":
        raise HistoricalQualityPolicyError("Characterization evidence status changed")
    if source.content_digest != characterization.source_phase_1_3_content_digest:
        raise HistoricalQualityPolicyError(
            "Source and characterization digests disagree"
        )
    if source.graph_version != characterization.graph_version:
        raise HistoricalQualityPolicyError(
            "Source and characterization graphs disagree"
        )
    if (
        hashlib.sha256(source_bytes).hexdigest()
        != characterization.source_phase_1_3_manifest_sha256
    ):
        raise HistoricalQualityPolicyError(
            "Characterization source manifest hash changed"
        )


def _verify_file(path: Path, identity: Any) -> None:
    if not path.is_file() or path.stat().st_size != identity.byte_count:
        raise HistoricalQualityPolicyError(f"Input metadata mismatch: {path.name}")
    if _sha256_file(path) != identity.sha256:
        raise HistoricalQualityPolicyError(f"Input hash mismatch: {path.name}")
    if (
        identity.row_count is not None
        and parquet.read_metadata(path).num_rows != identity.row_count
    ):
        raise HistoricalQualityPolicyError(f"Input row count mismatch: {path.name}")


def _apply_policy(frame: pd.DataFrame, characterization_digest: str) -> pd.DataFrame:
    required_weekdays = set(WEEKDAYS)
    if set(frame["weekday"]) != required_weekdays:
        raise HistoricalQualityPolicyError(
            "Policy input must contain Monday-Friday only"
        )
    if frame.duplicated(["app_edge_id", "weekday", "bucket_start_minute"]).any():
        raise HistoricalQualityPolicyError(
            "Characterization profile identity is not unique"
        )
    expected_statuses = {
        "source_profile_status": {"candidate_unvalidated"},
        "calibration_status": {"not_calibrated"},
        "characterization_status": {"descriptive_only"},
    }
    for column, expected in expected_statuses.items():
        if set(frame[column]) != expected:
            raise HistoricalQualityPolicyError(f"Unexpected source status in {column}")

    frame = frame.copy()
    possible = frame["possible_date_count"].astype("int64")
    observed = frame["observed_date_count"].astype("int64")
    if (possible <= 0).any() or (observed < 0).any() or (observed > possible).any():
        raise HistoricalQualityPolicyError(
            "Invalid possible/observed date relationship"
        )
    computed_coverage = observed / possible
    if not all(
        math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12)
        for actual, expected in zip(
            frame["coverage_fraction"], computed_coverage, strict=True
        )
    ):
        raise HistoricalQualityPolicyError("Stored coverage disagrees with date counts")

    frame["_weekday_order"] = frame["weekday"].map(WEEKDAY_ORDER)
    frame = frame.sort_values(
        ["app_edge_id", "_weekday_order", "bucket_start_minute"], kind="mergesort"
    ).reset_index(drop=True)
    computed_coverage = frame["observed_date_count"].astype("int64") / frame[
        "possible_date_count"
    ].astype("int64")
    date_classes = [
        classify_date_support(int(observed_count), int(possible_count))
        for observed_count, possible_count in zip(
            frame["observed_date_count"], frame["possible_date_count"], strict=True
        )
    ]
    flow_present = frame["present_flow_date_count"].astype("int64")
    speed_nonzero = (
        frame["present_speed_date_count"] - frame["zero_speed_date_count"]
    ).astype("int64")
    occupancy_eligible = (
        frame["present_occupancy_date_count"] - frame["occupancy_above_100_date_count"]
    ).astype("int64")
    speed_missing_within_observed = (
        frame["missing_speed_date_count"] - frame["missing_date_count"]
    ).astype("int64")
    if (
        (flow_present < 0).any()
        or (speed_nonzero < 0).any()
        or (occupancy_eligible < 0).any()
        or (speed_missing_within_observed < 0).any()
    ):
        raise HistoricalQualityPolicyError(
            "Measurement evidence counts are inconsistent"
        )

    result = pd.DataFrame(
        {
            "schema_version": HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION,
            "record_id": [
                _record_id(
                    characterization_digest,
                    str(edge),
                    str(weekday),
                    int(bucket),
                )
                for edge, weekday, bucket in zip(
                    frame["app_edge_id"],
                    frame["weekday"],
                    frame["bucket_start_minute"],
                    strict=True,
                )
            ],
            "source_characterization_record_id": frame["record_id"],
            "app_edge_id": frame["app_edge_id"],
            "direction": frame["direction"],
            "weekday": frame["weekday"],
            "bucket_start_minute": frame["bucket_start_minute"],
            "possible_date_count": frame["possible_date_count"],
            "observed_date_count": frame["observed_date_count"],
            "coverage_fraction": computed_coverage,
            "date_support_class": date_classes,
            "flow_evidence_status": [
                _measurement_status(date_class, count > 0)
                for date_class, count in zip(date_classes, flow_present, strict=True)
            ],
            "flow_present_date_count": flow_present,
            "flow_missing_date_count": frame["missing_flow_date_count"],
            "flow_zero_date_count": frame["zero_flow_date_count"],
            "flow_zero_fraction_present": frame["zero_flow_fraction_present"],
            "speed_evidence_status": [
                _measurement_status(date_class, count > 0)
                for date_class, count in zip(date_classes, speed_nonzero, strict=True)
            ],
            "speed_present_date_count": frame["present_speed_date_count"],
            "speed_nonzero_eligible_date_count": speed_nonzero,
            "speed_missing_date_count": frame["missing_speed_date_count"],
            "speed_missing_within_observed_date_count": speed_missing_within_observed,
            "speed_zero_diagnostic_date_count": frame["zero_speed_date_count"],
            "speed_zero_fraction_present": frame["zero_speed_fraction_present"],
            "zero_speed_slowdown_policy": "undefined_preserved_diagnostic",
            "occupancy_evidence_status": [
                _measurement_status(date_class, count > 0)
                for date_class, count in zip(
                    date_classes, occupancy_eligible, strict=True
                )
            ],
            "occupancy_present_date_count": frame["present_occupancy_date_count"],
            "occupancy_eligible_date_count": occupancy_eligible,
            "occupancy_missing_date_count": frame["missing_occupancy_date_count"],
            "occupancy_above_100_excluded_date_count": frame[
                "occupancy_above_100_date_count"
            ],
            "occupancy_above_100_fraction_present": frame[
                "occupancy_above_100_fraction_present"
            ],
            "sample_count_policy": "diagnostic_only_no_global_threshold",
            "source_profile_status": frame["source_profile_status"],
            "source_evidence_level": frame["source_evidence_level"],
            "calibration_status": "not_calibrated",
            "policy_application_status": "quality_policy_applied_unvalidated",
            "policy_version": HISTORICAL_QUALITY_POLICY_VERSION,
        }
    )
    for column in SUPPORT_COLUMNS:
        result[column] = frame[column]
    result["flow_direct_calibration_eligible"] = result["flow_evidence_status"].eq(
        "direct_calibration_evidence"
    )
    result["speed_direct_calibration_eligible"] = result["speed_evidence_status"].eq(
        "direct_calibration_evidence"
    )
    result["occupancy_direct_calibration_eligible"] = result[
        "occupancy_evidence_status"
    ].eq("direct_calibration_evidence")
    return result.drop(columns=[], errors="ignore")


def _measurement_status(
    date_class: DateSupportClass, has_evidence: bool
) -> MeasurementEvidenceStatus:
    if not has_evidence:
        return "no_eligible_measurement_evidence"
    return date_class


def _build_report(
    *,
    source_manifest: HistoricalCalibrationCompilerManifestV1,
    characterization_manifest: QualityCharacterizationManifestV1,
    characterization_report: dict[str, Any],
    frame: pd.DataFrame,
) -> dict[str, Any]:
    total = len(frame)
    date_counts = _value_counts(frame["date_support_class"])
    flow_counts = _value_counts(frame["flow_evidence_status"])
    speed_counts = _value_counts(frame["speed_evidence_status"])
    occupancy_counts = _value_counts(frame["occupancy_evidence_status"])
    direct = frame["date_support_class"].eq("direct_calibration_evidence")
    return {
        "schema_version": HISTORICAL_QUALITY_POLICY_SCHEMA_VERSION,
        "artifact_status": "quality_policy_applied_unvalidated",
        "calibration_status": "not_calibrated",
        "policy_version": HISTORICAL_QUALITY_POLICY_VERSION,
        "input": {
            "source_phase_1_3_content_digest": source_manifest.content_digest,
            "source_characterization_content_digest": characterization_manifest.content_digest,
            "graph_version": source_manifest.graph_version,
            "candidate_profile_count": total,
            "weekdays": list(WEEKDAYS),
        },
        "policy": POLICY.model_dump(mode="json"),
        "date_support": {
            "class_counts": _with_percentages(date_counts, total),
            "retained_profile_count": total,
            "deleted_profile_count": 0,
        },
        "flow": {
            "evidence_status_counts": _with_percentages(flow_counts, total),
            "direct_calibration_profile_count": int(
                frame["flow_direct_calibration_eligible"].sum()
            ),
            "direct_calibration_sample_count": int(
                frame.loc[direct, "flow_present_date_count"].sum()
            ),
            "zero_flow_sample_count": int(frame["flow_zero_date_count"].sum()),
            "occupancy_or_speed_anomalies_do_not_invalidate_flow": True,
        },
        "speed": {
            "evidence_status_counts": _with_percentages(speed_counts, total),
            "direct_calibration_profile_count": int(
                frame["speed_direct_calibration_eligible"].sum()
            ),
            "direct_nonzero_sample_count": int(
                frame.loc[direct, "speed_nonzero_eligible_date_count"].sum()
            ),
            "total_nonzero_sample_count": int(
                frame["speed_nonzero_eligible_date_count"].sum()
            ),
            "profiles_with_missing_speed_evidence": int(
                frame["speed_missing_date_count"].gt(0).sum()
            ),
            "profiles_with_null_speed_inside_observed_dates": int(
                frame["speed_missing_within_observed_date_count"].gt(0).sum()
            ),
            "missing_speed_within_observed_date_count": int(
                frame["speed_missing_within_observed_date_count"].sum()
            ),
            "profiles_with_zero_speed_evidence": int(
                frame["speed_zero_diagnostic_date_count"].gt(0).sum()
            ),
            "zero_speed_diagnostic_sample_count": int(
                frame["speed_zero_diagnostic_date_count"].sum()
            ),
            "zero_speed_rejection_threshold": None,
            "zero_speed_slowdown": "undefined",
        },
        "occupancy": {
            "evidence_status_counts": _with_percentages(occupancy_counts, total),
            "direct_calibration_profile_count": int(
                frame["occupancy_direct_calibration_eligible"].sum()
            ),
            "direct_eligible_sample_count": int(
                frame.loc[direct, "occupancy_eligible_date_count"].sum()
            ),
            "total_eligible_sample_count": int(
                frame["occupancy_eligible_date_count"].sum()
            ),
            "profiles_with_above_100_evidence": int(
                frame["occupancy_above_100_excluded_date_count"].gt(0).sum()
            ),
            "above_100_excluded_sample_count": int(
                frame["occupancy_above_100_excluded_date_count"].sum()
            ),
            "above_100_values_clipped_or_repaired": False,
        },
        "source_support": {
            "sample_count_policy": "diagnostic_only_no_global_threshold",
            "global_minimum_sample_count": None,
            "distribution_carried_from_characterization": characterization_report[
                "sample_count"
            ]["distribution"],
            "bands_carried_from_characterization": characterization_report[
                "sample_count"
            ]["bands"],
            "detector_and_station_support_preserved_per_profile": True,
        },
        "fallback_semantics": {
            "insufficient_history_does_not_mean": [
                "zero_traffic",
                "closed_road",
                "unusable_network_edge",
            ],
            "downstream_requirement": "use_separate_prior_or_model_fallback",
        },
    }


def _value_counts(series: pd.Series) -> dict[str, int]:
    counts = Counter(str(value) for value in series)
    return dict(sorted(counts.items()))


def _with_percentages(counts: dict[str, int], total: int) -> list[dict[str, Any]]:
    return [
        {
            "status": status,
            "profile_count": count,
            "profile_fraction": count / total,
            "profile_percent": count / total * 100,
        }
        for status, count in counts.items()
    ]


def _write_report_files(staging: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    json_path = staging / REPORT_JSON_OUTPUT
    markdown_path = staging / REPORT_MARKDOWN_OUTPUT
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_report_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _report_markdown(report: dict[str, Any]) -> str:
    date_rows = report["date_support"]["class_counts"]
    flow = report["flow"]
    speed = report["speed"]
    occupancy = report["occupancy"]
    sample = report["source_support"]
    lines = [
        "# Phase 1.3 historical quality policy-v1 application",
        "",
        "**Policy status:** quality policy applied, unvalidated  ",
        "**Calibration status:** not calibrated  ",
        "**Policy version:** phase-1.3-policy-v1",
        "",
        "## Date-support evidence classes",
        "",
        "| Evidence use | Profiles | Percent |",
        "|---|---:|---:|",
        *[
            f"| {row['status']} | {row['profile_count']:,} | {row['profile_percent']:.4f}% |"
            for row in date_rows
        ],
        "",
        f"All **{report['date_support']['retained_profile_count']:,}** candidate profiles remain present. Sparse profiles were classified, not deleted.",
        "",
        "## Measurement-specific direct evidence",
        "",
        "| Family | Direct profiles | Direct eligible samples |",
        "|---|---:|---:|",
        f"| Flow | {flow['direct_calibration_profile_count']:,} | {flow['direct_calibration_sample_count']:,} |",
        f"| Speed | {speed['direct_calibration_profile_count']:,} | {speed['direct_nonzero_sample_count']:,} |",
        f"| Occupancy | {occupancy['direct_calibration_profile_count']:,} | {occupancy['direct_eligible_sample_count']:,} |",
        "",
        "The three measurement families are evaluated independently. An occupancy anomaly does not invalidate flow or valid speed evidence.",
        "",
        "## Preserved diagnostics",
        "",
        f"- Profiles with missing-speed evidence: **{speed['profiles_with_missing_speed_evidence']:,}**",
        f"- Null speed samples inside observed date rows: **{speed['missing_speed_within_observed_date_count']:,}**",
        f"- Profiles containing zero speed: **{speed['profiles_with_zero_speed_evidence']:,}**",
        f"- Zero-speed samples retained as diagnostics: **{speed['zero_speed_diagnostic_sample_count']:,}**",
        f"- Profiles containing occupancy above 100: **{occupancy['profiles_with_above_100_evidence']:,}**",
        f"- Occupancy-above-100 samples excluded only from occupancy calibration: **{occupancy['above_100_excluded_sample_count']:,}**",
        "- Occupancy values were not clipped or repaired.",
        "",
        "## Aggregated sample-count policy",
        "",
        f"- Policy: **{sample['sample_count_policy']}**",
        "- Global minimum threshold: **none**",
        f"- Carried distribution: min {sample['distribution_carried_from_characterization']['min']:.0f}, median {sample['distribution_carried_from_characterization']['p50']:.0f}, p95 {sample['distribution_carried_from_characterization']['p95']:.0f}, max {sample['distribution_carried_from_characterization']['max']:.0f}.",
        "",
        "## Network fallback boundary",
        "",
        "Insufficient historical evidence does not mean zero traffic, a closed road, or an unusable edge. Later world building must use a separately governed prior/model fallback.",
        "",
        "## Honesty boundary",
        "",
        "This artifact applies an evidence-use policy. It does not establish historical calibration, traffic accuracy, SUMO calibration, or promotion.",
        "",
    ]
    return "\n".join(lines)


def _record_id(digest: str, edge: str, weekday: str, bucket: int) -> str:
    payload = "\x1f".join(
        (digest, HISTORICAL_QUALITY_POLICY_VERSION, edge, weekday, str(bucket))
    )
    return f"portal.profile-policy.{hashlib.sha256(payload.encode()).hexdigest()[:40]}"


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
