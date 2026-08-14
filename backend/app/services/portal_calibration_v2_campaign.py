"""Deterministic planning and guarded execution for PORTAL calibration shards."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.schemas.calibration_v2_corpus import (
    CalibrationCampaignPlanV2,
    CalibrationObservationShardV2,
    CorpusFinalizationProvenance,
)
from backend.app.services.calibration_v2_integrity_service import (
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_BATCH_SIZE,
    INTEGRITY_RECONCILIATION_ALGORITHM,
    INTEGRITY_RECONCILIATION_ALGORITHM_VERSION,
    ProgressCallback,
    finalize_calibration_integrity_index,
    index_calibration_shard,
)
from backend.app.services.portal_calibration_v2_importer import (
    PORTAL_DATASET_ID,
    PortalImportError,
    ValidatedShardManifest,
    _canonical_stream_sha256,
    _sha256_file,
    build_portal_corpus_manifest,
    canonical_json,
    import_portal_partition,
    portal_shard_id,
)

CAMPAIGN_PLAN_SCHEMA_VERSION = 1
CAMPAIGN_FAILURE_STATE = "campaign-failure-state.json"
FINALIZER_NAME = "portal_calibration_v2_finalizer"
FINALIZER_CODE_VERSION = "phase-1.2c2"


def plan_portal_calibration_campaign(
    campaign_directory: Path,
    output_root: Path,
    *,
    verify_shard_content: bool = False,
) -> CalibrationCampaignPlanV2:
    """Inventory all source partitions without parsing a raw observation row."""
    campaign_directory, output_root = campaign_directory.resolve(), output_root.resolve()
    manifest_path = campaign_directory / "campaign-manifest.json"
    if not manifest_path.is_file():
        raise PortalImportError("Missing PORTAL campaign manifest")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 2 or not isinstance(manifest.get("chunks"), dict):
        raise PortalImportError("Expected PORTAL campaign manifest schema 2")
    config = manifest.get("config", {})
    if config.get("resolution") != "00:15:00" or config.get("days_of_week") != [2, 3, 4, 5, 6]:
        raise PortalImportError("Campaign calendar does not match approved Monday-Friday 15-minute source policy")
    failure_states = _failure_states(output_root)
    entries: list[dict[str, Any]] = []
    for partition_id, chunk in sorted(manifest["chunks"].items()):
        entries.append(
            _plan_entry(
                campaign_directory,
                output_root,
                partition_id,
                chunk,
                failure_states,
                verify_shard_content=verify_shard_content,
            )
        )
    payload: dict[str, Any] = {
        "plan_schema_version": CAMPAIGN_PLAN_SCHEMA_VERSION,
        "artifact_type": "commute_help_calibration_campaign_plan",
        "plan_id": "portal.plan." + manifest_sha[:40],
        "generated_at": datetime.now(UTC),
        "campaign_manifest": {
            "reference_kind": "manifest",
            "reference_id": "portal.campaign.manifest",
            "reference_version": "2",
            "relative_path": "campaign-manifest.json",
            "sha256": manifest_sha,
        },
        "source_dataset": {
            "dataset_id": PORTAL_DATASET_ID,
            "dataset_version": f"campaign-manifest-sha256:{manifest_sha}",
            "provider": "PORTAL",
            "synthetic": False,
        },
        "expected_partition_count": len(entries),
        "partitions": entries,
        "incomplete_temporary_output_count": len(list((output_root / "shards").glob(".*"))) if (output_root / "shards").exists() else 0,
    }
    digest_payload = dict(payload)
    digest_payload.pop("generated_at")
    payload["plan_digest"] = hashlib.sha256(canonical_json(digest_payload).encode()).hexdigest()
    return CalibrationCampaignPlanV2.model_validate(payload)


def materialize_portal_calibration_campaign(
    campaign_directory: Path,
    output_root: Path,
    *,
    station_mapping_path: Path | None,
    station_mapping_report_path: Path | None,
    allow_partial: bool = False,
) -> CalibrationCampaignPlanV2:
    """Run a preplanned campaign only when explicitly called by the guarded CLI."""
    plan = plan_portal_calibration_campaign(
        campaign_directory, output_root, verify_shard_content=True
    )
    blocked = [entry for entry in plan.partitions if entry.state in {"invalid", "conflict"}]
    if blocked:
        raise PortalImportError(f"Campaign precheck found {len(blocked)} invalid/conflicting partitions")
    if not allow_partial and any(entry.state == "failed" for entry in plan.partitions):
        raise PortalImportError("Campaign has recorded failed partitions; rerun them or use explicit partial mode")
    for entry in plan.partitions:
        if entry.state == "reusable_verified":
            shard_manifest_path = output_root / entry.relative_shard_manifest_path
            shard = CalibrationObservationShardV2.model_validate_json(shard_manifest_path.read_bytes())
            index_calibration_shard(
                output_root, shard_manifest_path.parent, shard, summarize=False
            )
            continue
        try:
            result = import_portal_partition(
                campaign_directory,
                entry.source_partition_id,
                output_root,
                station_mapping_path=station_mapping_path,
                station_mapping_report_path=station_mapping_report_path,
            )
            index_calibration_shard(
                output_root, result.shard_directory, result.manifest, summarize=False
            )
            _clear_failure(output_root, entry.source_partition_id)
        except Exception as error:
            _record_failure(output_root, entry.source_partition_id, str(error))
            if not allow_partial:
                raise
    updated = finalize_portal_calibration_corpus(campaign_directory, output_root)
    return updated


def finalize_portal_calibration_corpus(
    campaign_directory: Path,
    output_root: Path,
    *,
    fetch_batch_size: int = DEFAULT_RECONCILIATION_BATCH_SIZE,
    progress_callback: ProgressCallback | None = None,
    progress_interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
) -> CalibrationCampaignPlanV2:
    """Fully validate every shard, reconcile once, and promote the corpus manifest."""
    updated, validated_shards = _validate_finalization_shards(
        campaign_directory,
        output_root,
    )
    unresolved = [
        entry
        for entry in updated.partitions
        if entry.state != "reusable_verified"
    ]
    if unresolved:
        raise PortalImportError(
            f"Final corpus validation requires all partitions reusable; unresolved={len(unresolved)}"
        )
    expected_shards: dict[str, int] = {}
    for validated in validated_shards:
        shard = validated.manifest
        expected_shards[shard.shard_id] = shard.observation_stream.record_count
    integrity_result = finalize_calibration_integrity_index(
        output_root,
        expected_shards,
        fetch_batch_size=fetch_batch_size,
        progress_callback=progress_callback,
        progress_interval_seconds=progress_interval_seconds,
    )
    integrity = integrity_result.summary
    if integrity.global_conflict_identity_count:
        state = "conflicts_present"
    elif integrity.global_duplicate_claim_count:
        state = "duplicates_resolved_logically"
    else:
        state = "complete_unvalidated"
    build_portal_corpus_manifest(
        output_root,
        integrity=integrity,
        materialization_state=state,
        validated_shards=validated_shards,
        finalization=CorpusFinalizationProvenance(
            finalizer={
                "name": FINALIZER_NAME,
                "code_version": FINALIZER_CODE_VERSION,
                "model_version": None,
            },
            integrity_reconciliation={
                "algorithm": INTEGRITY_RECONCILIATION_ALGORITHM,
                "version": INTEGRITY_RECONCILIATION_ALGORITHM_VERSION,
            },
        ),
    )
    if integrity.global_conflict_identity_count:
        raise PortalImportError(
            "Global observation conflicts prevent clean corpus promotion; "
            f"conflicts={integrity.global_conflict_identity_count}, "
            f"evidence={integrity_result.conflict_evidence}"
        )
    return updated


def _validate_finalization_shards(
    campaign_directory: Path,
    output_root: Path,
) -> tuple[CalibrationCampaignPlanV2, tuple[ValidatedShardManifest, ...]]:
    """Verify every immutable stream once and retain its trusted manifest."""
    updated = plan_portal_calibration_campaign(
        campaign_directory,
        output_root,
        verify_shard_content=False,
    )
    unresolved = [
        entry for entry in updated.partitions if entry.state != "reusable_verified"
    ]
    if unresolved:
        raise PortalImportError(
            "Final corpus validation requires all partitions reusable; "
            f"unresolved={len(unresolved)}"
        )
    validated: list[ValidatedShardManifest] = []
    output_root = output_root.resolve()
    for entry in updated.partitions:
        manifest_path = output_root / entry.relative_shard_manifest_path
        shard = CalibrationObservationShardV2.model_validate_json(
            manifest_path.read_bytes()
        )
        stream_path = manifest_path.parent / shard.observation_stream.relative_path
        source_matches = any(
            reference.reference_id == entry.raw_source.reference_id
            and reference.sha256 == entry.raw_source.sha256
            for reference in shard.source_references
        )
        if (
            shard.shard_id != entry.expected_shard_id
            or shard.source_partition_id != entry.source_partition_id
            or not source_matches
            or not stream_path.is_file()
            or stream_path.stat().st_size
            != shard.observation_stream.physical_byte_count
            or _sha256_file(stream_path)
            != shard.observation_stream.physical_sha256
            or _canonical_stream_sha256(
                stream_path,
                shard.observation_stream.compression,
            )
            != shard.observation_stream.canonical_sha256
        ):
            raise PortalImportError(
                f"Final shard validation failed: {entry.expected_shard_id}"
            )
        validated.append(ValidatedShardManifest(manifest=shard))
    return updated, tuple(validated)


def _plan_entry(
    campaign_directory: Path,
    output_root: Path,
    partition_id: str,
    chunk: Any,
    failure_states: dict[str, str],
    *,
    verify_shard_content: bool,
) -> dict[str, Any]:
    if not isinstance(chunk, dict) or chunk.get("id") != partition_id:
        return _invalid_entry(partition_id, "manifest_chunk_identity_mismatch")
    raw = chunk.get("raw")
    if chunk.get("status") != "complete" or not isinstance(raw, dict):
        return _invalid_entry(partition_id, "manifest_chunk_not_complete")
    try:
        raw_path = str(raw["path"])
        raw_sha = str(raw["sha256"])
        raw_bytes = int(raw["bytes"])
        start_date = str(chunk["start_date"])
        end_date = str(chunk["end_date"])
    except (KeyError, TypeError, ValueError):
        return _invalid_entry(partition_id, "manifest_chunk_missing_required_field")
    source = campaign_directory / raw_path
    shard_id = portal_shard_id(partition_id, raw_sha)
    base: dict[str, Any] = {
        "source_partition_id": partition_id,
        "raw_source": {
            "reference_kind": "file",
            "reference_id": f"portal.raw.{partition_id}",
            "reference_version": None,
            "relative_path": raw_path,
            "sha256": raw_sha,
        },
        "raw_row_count": int(chunk.get("raw_rows", 0)),
        "logical_start_date": start_date,
        "logical_end_date": end_date,
        "expected_shard_id": shard_id,
        "relative_shard_manifest_path": f"shards/{shard_id}/shard-manifest.json",
    }
    if not source.is_file() or source.stat().st_size != raw_bytes:
        return dict(base, state="invalid", state_reason="raw_source_missing_or_size_mismatch")
    shard_path = output_root / "shards" / shard_id / "shard-manifest.json"
    if shard_path.exists():
        try:
            shard = CalibrationObservationShardV2.model_validate_json(shard_path.read_bytes())
            stream_path = shard_path.parent / shard.observation_stream.relative_path
            valid = (
                shard.shard_id == shard_id
                and shard.source_partition_id == partition_id
                and any(reference.sha256 == raw_sha for reference in shard.source_references if reference.reference_id == f"portal.raw.{partition_id}")
                and stream_path.is_file()
                and stream_path.stat().st_size
                == shard.observation_stream.physical_byte_count
            )
            if valid and verify_shard_content:
                valid = (
                    _sha256_file(stream_path)
                    == shard.observation_stream.physical_sha256
                    and _canonical_stream_sha256(
                        stream_path, shard.observation_stream.compression
                    )
                    == shard.observation_stream.canonical_sha256
                )
            return dict(base, state="reusable_verified" if valid else "conflict", state_reason=None if valid else "immutable_shard_validation_failed")
        except (OSError, ValueError):
            return dict(base, state="invalid", state_reason="unparseable_existing_shard")
    if partition_id in failure_states:
        return dict(base, state="failed", state_reason=failure_states[partition_id])
    return dict(base, state="pending", state_reason=None)


def _invalid_entry(partition_id: str, reason: str) -> dict[str, Any]:
    placeholder_sha = "0" * 64
    shard_id = "portal.shard.invalid." + hashlib.sha256(partition_id.encode()).hexdigest()[:32]
    return {
        "source_partition_id": partition_id,
        "raw_source": {"reference_kind": "file", "reference_id": f"portal.raw.{partition_id}", "reference_version": None, "relative_path": "invalid", "sha256": placeholder_sha},
        "raw_row_count": 0,
        "logical_start_date": "2024-01-01",
        "logical_end_date": "2024-01-01",
        "expected_shard_id": shard_id,
        "relative_shard_manifest_path": f"shards/{shard_id}/shard-manifest.json",
        "state": "invalid",
        "state_reason": reason,
    }


def _failure_states(output_root: Path) -> dict[str, str]:
    path = output_root / CAMPAIGN_FAILURE_STATE
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): str(reason) for key, reason in value.get("failures", {}).items()}


def _record_failure(output_root: Path, partition_id: str, reason: str) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    state = _failure_states(output_root)
    state[partition_id] = reason[:240]
    _write_failure_states(output_root, state)


def _clear_failure(output_root: Path, partition_id: str) -> None:
    state = _failure_states(output_root)
    if partition_id in state:
        del state[partition_id]
        _write_failure_states(output_root, state)


def _write_failure_states(output_root: Path, states: dict[str, str]) -> None:
    pending = output_root / f".{CAMPAIGN_FAILURE_STATE}.pending"
    pending.write_text(canonical_json({"schema_version": 1, "failures": dict(sorted(states.items()))}), encoding="utf-8")
    os.replace(pending, output_root / CAMPAIGN_FAILURE_STATE)
