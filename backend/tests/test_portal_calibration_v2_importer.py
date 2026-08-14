from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

import backend.app.services.calibration_v2_integrity_service as integrity_service
import backend.app.services.historical_calibration_v2_compiler as historical_compiler
import backend.app.services.portal_calibration_v2_campaign as campaign_service
import backend.app.services.portal_calibration_v2_importer as importer_service
from backend.app.schemas.calibration_v2_corpus import (
    CALIBRATION_V2_CORPUS_SCHEMA_VERSION,
    CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION,
    CalibrationObservationCorpusV2,
)
from backend.app.services.calibration_v2_integrity_service import (
    finalize_calibration_integrity_index,
    index_calibration_shard,
    inspect_calibration_integrity_index,
    inspect_calibration_integrity_progress,
)
from backend.app.services.historical_calibration_v2_compiler import (
    HistoricalCalibrationCompileError,
    compile_historical_calibration_v2,
)
from backend.app.services.portal_calibration_v2_campaign import (
    finalize_portal_calibration_corpus,
    plan_portal_calibration_campaign,
)
from backend.app.services.portal_calibration_v2_importer import (
    PortalImportError,
    build_portal_corpus_manifest,
    import_portal_partition,
    portal_observation_id,
)
from backend.app.services.portal_calibration_v2_preflight import (
    measure_materialization_preflight,
)

FIXTURES = Path(__file__).parent / "fixtures" / "portal_calibration_v2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _campaign(
    root: Path,
    *,
    raws: dict[str, tuple[str, str, str, str]],
    mapping: bool = True,
) -> tuple[Path, Path | None, Path | None]:
    """Build a tiny manifest-verified campaign around committed raw fixtures."""
    campaign = root / "campaign"
    raw_dir, metadata_dir = campaign / "raw", campaign / "metadata"
    raw_dir.mkdir(parents=True)
    metadata_dir.mkdir()
    highways = [{"highwayid": 1, "direction": "NORTH", "highwayname": "Synthetic I-5"}]
    detectors = [
        {"detectorid": 101, "stationid": 10, "highwayid": 1, "lanenumber": 1},
        {"detectorid": 102, "stationid": 10, "highwayid": 1, "lanenumber": 2},
        {"detectorid": 103, "stationid": 11, "highwayid": 1, "lanenumber": 1},
    ]
    stations = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.6, 45.5]}, "properties": {"stationid": 10, "highwayid": 1, "locationtext": "Synthetic Ten"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.7, 45.6]}, "properties": {"stationid": 11, "highwayid": 1, "locationtext": "Synthetic Eleven"}},
    ]}
    for name, value in (("highways", highways), ("detectors", detectors), ("stations", stations)):
        (metadata_dir / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    chunks = {}
    for chunk_id, (fixture_name, start, end, target_name) in raws.items():
        target = raw_dir / target_name
        shutil.copyfile(FIXTURES / fixture_name, target)
        chunks[chunk_id] = {"id": chunk_id, "highway_id": 1, "start_date": start, "end_date": end, "status": "complete", "raw_rows": len(target.read_text(encoding="utf-8").splitlines()) - 1, "raw": {"path": f"raw/{target_name}", "bytes": target.stat().st_size, "sha256": _sha(target)}}
    metadata = {
        name: {
            "path": f"metadata/{name}.json",
            "bytes": (metadata_dir / f"{name}.json").stat().st_size,
            "sha256": _sha(metadata_dir / f"{name}.json"),
        }
        for name in ("highways", "detectors", "stations")
    }
    manifest = {
        "schema_version": 2,
        "config": {
            "dataset_version": "synthetic-source-v1",
            "resolution": "00:15:00",
            "days_of_week": [2, 3, 4, 5, 6],
        },
        "metadata": metadata,
        "chunks": chunks,
    }
    (campaign / "campaign-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if not mapping:
        return campaign, None, None
    mapping_path = root / "matches.csv"
    mapping_path.write_text(
        "station_id,direction,status,edge_id\n"
        "portal-station-10,NORTH,accepted,app-edge-10\n"
        "portal-station-11,NORTH,review,app-edge-11\n",
        encoding="utf-8",
    )
    report_path = root / "matches.json"
    report_path.write_text(json.dumps({"graph_version": "synthetic-graph-v1"}), encoding="utf-8")
    return campaign, mapping_path, report_path


def _import_week(tmp_path: Path, *, mapping: bool = True):
    campaign, matches, report = _campaign(tmp_path, raws={"week-a": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv")}, mapping=mapping)
    return import_portal_partition(campaign, "week-a", tmp_path / "out", station_mapping_path=matches, station_mapping_report_path=report)


def _records(result) -> list[dict]:
    with gzip.open(result.shard_directory / "observations.jsonl.gz", "rt") as source:
        return [json.loads(line) for line in source]


def _reference_integrity(output: Path) -> tuple[dict[str, int | str], tuple[dict, ...]]:
    """The pre-optimization grouped-query semantics, retained only in tests."""
    connection = sqlite3.connect(integrity_service.integrity_index_path(output))
    connection.row_factory = sqlite3.Row
    try:
        physical = int(connection.execute("SELECT COUNT(*) FROM observation_claims").fetchone()[0])
        indexed = int(connection.execute("SELECT COUNT(*) FROM indexed_shards").fetchone()[0])
        logical = int(connection.execute("""
            SELECT COUNT(*) FROM (
                SELECT observation_id FROM observation_claims
                GROUP BY observation_id
                HAVING COUNT(DISTINCT payload_sha256) = 1
            )
        """).fetchone()[0])
        duplicates = int(connection.execute("""
            SELECT COALESCE(SUM(claim_count - 1), 0) FROM (
                SELECT observation_id, payload_sha256, COUNT(*) AS claim_count
                FROM observation_claims
                GROUP BY observation_id, payload_sha256
                HAVING claim_count > 1
            )
        """).fetchone()[0])
        conflicts = int(connection.execute("""
            SELECT COUNT(*) FROM (
                SELECT observation_id FROM observation_claims
                GROUP BY observation_id
                HAVING COUNT(DISTINCT payload_sha256) > 1
            )
        """).fetchone()[0])
        digest = hashlib.sha256()
        for observation_id, payload in connection.execute("""
            SELECT observation_id, MIN(payload_sha256)
            FROM observation_claims
            GROUP BY observation_id
            HAVING COUNT(DISTINCT payload_sha256) = 1
            ORDER BY observation_id
        """):
            digest.update(json.dumps(
                ("portal.obs." + bytes(observation_id).hex(), bytes(payload).hex()),
                separators=(",", ":"),
            ).encode())
            digest.update(b"\n")
        evidence = []
        for (observation_id,) in connection.execute("""
            SELECT observation_id FROM observation_claims
            GROUP BY observation_id
            HAVING COUNT(DISTINCT payload_sha256) > 1
            ORDER BY observation_id LIMIT 25
        """):
            claims = connection.execute("""
                SELECT c.payload_sha256, s.shard_id, s.source_partition_id,
                       s.source_reference_id, s.source_reference_sha256
                FROM observation_claims AS c
                JOIN indexed_shards AS s ON s.shard_key = c.shard_key
                WHERE c.observation_id = ?
                ORDER BY c.payload_sha256, s.shard_id
            """, (observation_id,)).fetchall()
            evidence.append({
                "observation_id": "portal.obs." + bytes(observation_id).hex(),
                "claims": [
                    {
                        "payload_sha256": bytes(claim["payload_sha256"]).hex(),
                        "shard_id": claim["shard_id"],
                        "source_partition_id": claim["source_partition_id"],
                        "source_reference_id": claim["source_reference_id"],
                        "source_reference_sha256": claim["source_reference_sha256"],
                    }
                    for claim in claims
                ],
            })
        return ({
            "indexed_shard_count": indexed,
            "physical_observation_count": physical,
            "logical_observation_count": logical,
            "global_duplicate_claim_count": duplicates,
            "global_conflict_identity_count": conflicts,
            "index_schema_version": 2,
            "index_digest": digest.hexdigest(),
        }, tuple(evidence))
    finally:
        connection.close()


def test_raw_measurement_normalization_calendar_missing_and_mapping(tmp_path: Path) -> None:
    result = _import_week(tmp_path)
    records = _records(result)
    assert len(records) == 4  # next Monday boundary is suppressed before identity
    volume_ten = next(record for record in records if record["detector"]["detector_id"] == "101" and record["calendar"]["weekday"] == "monday")
    assert volume_ten["measurements"]["volume_count"] == 10
    assert volume_ten["measurements"]["flow_vph"] == 40
    assert volume_ten["measurements"]["speed_kph"] == pytest.approx(50 * 1.609344)
    assert volume_ten["measurements"]["occupancy_percent"] == 150.5
    assert volume_ten["measurements"]["sample_count"] == 45
    assert volume_ten["road_association"]["app_edge_id"] == "app-edge-10"
    assert volume_ten["road_association"]["accepted_sumo_association"] is None
    assert volume_ten["evidence"]["calibration_status"] == "not_calibrated"
    assert volume_ten["quality"]["source_status"] == "unknown"
    tuesday = next(record for record in records if record["detector"]["detector_id"] == "101" and record["calendar"]["weekday"] == "tuesday")
    assert tuesday["measurements"]["volume_count"] == 0
    assert tuesday["measurements"]["flow_vph"] == 0
    assert tuesday["measurements"]["occupancy_percent"] == 0
    assert tuesday["measurements"]["speed_kph"] is None
    assert tuesday["quality"]["missing_fields"] == ["speed_kph"]
    zero_speed = next(record for record in records if record["detector"]["detector_id"] == "102")
    assert zero_speed["measurements"]["speed_kph"] == 0
    assert zero_speed["quality"]["quality_flags"] == ["zero_speed_semantics_unresolved"]
    assert volume_ten["record_id"] != zero_speed["record_id"]
    assert volume_ten["detector"]["station_id"] == zero_speed["detector"]["station_id"]
    assert volume_ten["calendar"]["interval_start"] == zero_speed["calendar"]["interval_start"]
    review = next(record for record in records if record["detector"]["detector_id"] == "103")
    assert review["road_association"] is None
    assert result.manifest.audit.export_boundary_suppressed_count == 1
    assert result.manifest.mapping_coverage.accepted_app_edge_attached_count == 3
    assert result.manifest.mapping_coverage.review_or_unmatched_not_attached_count == 1
    matches = tmp_path / "matches.csv"
    matches.write_text(
        matches.read_text(encoding="utf-8").replace(",review,app-edge-11", ",unmatched,app-edge-11"),
        encoding="utf-8",
    )
    unmatched = import_portal_partition(
        tmp_path / "campaign",
        "week-a",
        tmp_path / "out-unmatched",
        station_mapping_path=matches,
        station_mapping_report_path=tmp_path / "matches.json",
    )
    assert next(record for record in _records(unmatched) if record["detector"]["detector_id"] == "103")["road_association"] is None


def test_pacific_offsets_and_identity_exclude_filename_and_mappings(tmp_path: Path) -> None:
    campaign_a, matches, report = _campaign(tmp_path / "a", raws={"summer": ("raw-summer.csv", "2026-07-06", "2026-07-06", "first-name.csv")})
    campaign_b, _, _ = _campaign(tmp_path / "b", raws={"summer": ("raw-summer.csv", "2026-07-06", "2026-07-06", "renamed.csv")}, mapping=False)
    first = import_portal_partition(campaign_a, "summer", tmp_path / "out-a", station_mapping_path=matches, station_mapping_report_path=report)
    second = import_portal_partition(campaign_b, "summer", tmp_path / "out-b")
    first_record, second_record = _records(first)[0], _records(second)[0]
    assert first_record["record_id"] == second_record["record_id"]
    assert first_record["calendar"]["interval_start"].endswith("-07:00")
    assert first_record["calendar"]["weekday"] == "monday"
    assert first_record["road_association"] is not None
    assert second_record["road_association"] is None
    assert portal_observation_id(
        detector_id="101", station_id="10", highway_id="1", direction="northbound",
        interval_start=datetime.fromisoformat("2026-01-05T08:00:00-08:00"),
    ) == portal_observation_id(
        detector_id="101", station_id="10", highway_id="1", direction="northbound",
        interval_start=datetime.fromisoformat("2026-01-05T08:00:00-08:00"),
    )
    ordered_campaign, ordered_matches, ordered_report = _campaign(
        tmp_path / "ordered",
        raws={"week": ("raw-week.csv", "2026-01-05", "2026-01-11", "ordered.csv")},
    )
    reordered_campaign, reordered_matches, reordered_report = _campaign(
        tmp_path / "reordered",
        raws={"week": ("raw-week.csv", "2026-01-05", "2026-01-11", "reordered.csv")},
    )
    reordered_raw = reordered_campaign / "raw" / "reordered.csv"
    header, *rows = reordered_raw.read_text(encoding="utf-8").splitlines()
    reordered_raw.write_text("\n".join([header, *reversed(rows)]) + "\n", encoding="utf-8")
    reordered_manifest = json.loads((reordered_campaign / "campaign-manifest.json").read_text(encoding="utf-8"))
    reordered_manifest["chunks"]["week"]["raw"].update(bytes=reordered_raw.stat().st_size, sha256=_sha(reordered_raw))
    (reordered_campaign / "campaign-manifest.json").write_text(json.dumps(reordered_manifest), encoding="utf-8")
    ordered = import_portal_partition(ordered_campaign, "week", tmp_path / "out-ordered", station_mapping_path=ordered_matches, station_mapping_report_path=ordered_report)
    reordered = import_portal_partition(reordered_campaign, "week", tmp_path / "out-reordered", station_mapping_path=reordered_matches, station_mapping_report_path=reordered_report)
    assert [record["record_id"] for record in _records(ordered)] == [record["record_id"] for record in _records(reordered)]
    assert portal_observation_id(
        detector_id="101", station_id="10", highway_id="1", direction="northbound",
        interval_start=datetime.fromisoformat("2026-01-05T08:00:00-08:00"),
    ) == portal_observation_id(
        detector_id="101", station_id="10", highway_id="1", direction="northbound",
        interval_start=datetime.fromisoformat("2026-01-05T08:00:00-08:00"),
    )


def test_exact_duplicate_suppression_and_conflict_exclusion(tmp_path: Path) -> None:
    campaign, matches, report = _campaign(tmp_path / "duplicate", raws={"week-a": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv")})
    raw = campaign / "raw/week.csv"
    lines = raw.read_text().splitlines()
    raw.write_text("\n".join(lines + [lines[1]]) + "\n")
    manifest = json.loads((campaign / "campaign-manifest.json").read_text())
    manifest["chunks"]["week-a"]["raw"].update(bytes=raw.stat().st_size, sha256=_sha(raw))
    (campaign / "campaign-manifest.json").write_text(json.dumps(manifest))
    duplicate = import_portal_partition(campaign, "week-a", tmp_path / "out-duplicate", station_mapping_path=matches, station_mapping_report_path=report)
    assert duplicate.manifest.audit.duplicate_suppressed_count == 1
    assert duplicate.manifest.audit.accepted_count == 4
    conflict_campaign, conflict_matches, conflict_report = _campaign(tmp_path / "conflict", raws={"conflict": ("raw-conflict.csv", "2026-01-05", "2026-01-05", "conflict.csv")})
    conflict = import_portal_partition(conflict_campaign, "conflict", tmp_path / "out-conflict", station_mapping_path=conflict_matches, station_mapping_report_path=conflict_report)
    assert conflict.manifest.audit.conflict_count == 1
    assert conflict.manifest.audit.accepted_count == 0
    assert conflict.manifest.audit.diagnostics[0].reason == "conflicting_observation_identity"


def test_shards_are_deterministic_reusable_and_incremental_without_corpus_materialization(tmp_path: Path) -> None:
    campaign, matches, report = _campaign(tmp_path, raws={"week-a": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv"), "summer": ("raw-summer.csv", "2026-07-06", "2026-07-06", "summer.csv")})
    output = tmp_path / "out"
    week = import_portal_partition(campaign, "week-a", output, station_mapping_path=matches, station_mapping_report_path=report)
    retry = import_portal_partition(campaign, "week-a", output, station_mapping_path=matches, station_mapping_report_path=report)
    independent = import_portal_partition(campaign, "week-a", tmp_path / "independent-output", station_mapping_path=matches, station_mapping_report_path=report)
    summer = import_portal_partition(campaign, "summer", output, station_mapping_path=matches, station_mapping_report_path=report)
    assert retry.reused_existing is True
    assert retry.manifest.content_digest == week.manifest.content_digest
    assert independent.manifest.content_digest == week.manifest.content_digest
    assert independent.manifest.observation_stream.canonical_sha256 == week.manifest.observation_stream.canonical_sha256
    assert (independent.shard_directory / "observations.jsonl.gz").read_bytes() == (week.shard_directory / "observations.jsonl.gz").read_bytes()
    assert summer.reused_existing is False
    corpus = build_portal_corpus_manifest(output)
    assert len(corpus.shards) == 2
    assert corpus.observation_count == week.manifest.audit.accepted_count + summer.manifest.audit.accepted_count
    assert "DictReader" in Path(import_portal_partition.__code__.co_filename).read_text()


def test_campaign_plan_resume_and_cross_shard_global_integrity(tmp_path: Path) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "part-a": ("raw-summer.csv", "2026-07-06", "2026-07-06", "a.csv"),
            "part-b": ("raw-summer.csv", "2026-07-06", "2026-07-06", "b.csv"),
        },
    )
    output = tmp_path / "out"
    initial = plan_portal_calibration_campaign(campaign, output)
    assert [entry.source_partition_id for entry in initial.partitions] == ["part-a", "part-b"]
    assert {entry.state for entry in initial.partitions} == {"pending"}
    first = import_portal_partition(campaign, "part-a", output, station_mapping_path=matches, station_mapping_report_path=report)
    first_index = index_calibration_shard(output, first.shard_directory, first.manifest)
    assert first_index.summary.logical_observation_count == 1
    assert index_calibration_shard(output, first.shard_directory, first.manifest, summarize=False) is None
    assert inspect_calibration_integrity_index(output).summary.logical_observation_count == 1
    second = import_portal_partition(campaign, "part-b", output, station_mapping_path=matches, station_mapping_report_path=report)
    second_index = index_calibration_shard(output, second.shard_directory, second.manifest)
    assert second_index.summary.physical_observation_count == 2
    assert second_index.summary.logical_observation_count == 1
    assert second_index.summary.global_duplicate_claim_count == 1
    assert second_index.summary.global_conflict_identity_count == 0
    retry = index_calibration_shard(output, second.shard_directory, second.manifest)
    assert retry.reused_shard is True
    (output / "shards" / ".abandoned-pending").mkdir(parents=True)
    resumed = plan_portal_calibration_campaign(campaign, output)
    assert {entry.state for entry in resumed.partitions} == {"reusable_verified"}
    assert resumed.incomplete_temporary_output_count == 1
    future_output = tmp_path / "not-created-yet" / "future-output"
    preflight = measure_materialization_preflight(campaign, output, future_output)
    assert preflight.representative_accepted_observations == 2
    assert preflight.expected_raw_rows == 2
    assert preflight.projected_accepted_observations == 2
    assert preflight.physical_observation_bytes < preflight.canonical_observation_bytes
    assert not future_output.parent.exists()


def test_incremental_registration_is_quiet_and_finalization_reconciles_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "part-a": ("raw-summer.csv", "2026-07-06", "2026-07-06", "a.csv"),
            "part-b": ("raw-summer.csv", "2026-07-06", "2026-07-06", "b.csv"),
        },
    )
    output = tmp_path / "out"
    first = import_portal_partition(
        campaign, "part-a", output,
        station_mapping_path=matches, station_mapping_report_path=report,
    )
    second = import_portal_partition(
        campaign, "part-b", output,
        station_mapping_path=matches, station_mapping_report_path=report,
    )
    original_reconciliation = integrity_service._reconcile_ordered_claims
    reconciliation_calls = 0

    def count_reconciliation(*args, **kwargs):
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        return original_reconciliation(*args, **kwargs)

    monkeypatch.setattr(
        integrity_service,
        "_reconcile_ordered_claims",
        count_reconciliation,
    )
    index_calibration_shard(
        output, first.shard_directory, first.manifest, summarize=False
    )
    index_calibration_shard(
        output, second.shard_directory, second.manifest, summarize=False
    )
    assert reconciliation_calls == 0
    progress = inspect_calibration_integrity_progress(output)
    assert progress.indexed_shard_count == 2
    assert progress.physical_observation_count == 2
    assert progress.logical_observation_count == 1
    assert progress.global_duplicate_claim_count == 1
    assert reconciliation_calls == 0
    expected = {
        first.manifest.shard_id: 1,
        second.manifest.shard_id: 1,
    }
    finalized = finalize_calibration_integrity_index(output, expected)
    assert reconciliation_calls == 1
    assert finalized.summary.logical_observation_count == 1
    corpus_plan = finalize_portal_calibration_corpus(campaign, output)
    assert reconciliation_calls == 2
    assert all(entry.state == "reusable_verified" for entry in corpus_plan.partitions)
    corpus = json.loads((output / "corpus-manifest.json").read_text())
    assert corpus["corpus_schema_version"] == CALIBRATION_V2_CORPUS_SCHEMA_VERSION == 2
    assert corpus["generator"] == {
        "name": "portal_calibration_v2_importer",
        "code_version": "phase-1.2b",
        "model_version": None,
    }
    assert corpus["finalization"] == {
        "finalizer": {
            "name": "portal_calibration_v2_finalizer",
            "code_version": "phase-1.2c2",
            "model_version": None,
        },
        "integrity_reconciliation": {
            "algorithm": "ordered-stream",
            "version": "v1",
        },
    }
    assert corpus["materialization_state"] == "duplicates_resolved_logically"
    assert corpus["integrity"]["index_digest"] == finalized.summary.index_digest
    reverse_output = tmp_path / "reverse-output"
    first_reverse = import_portal_partition(
        campaign, "part-a", reverse_output,
        station_mapping_path=matches, station_mapping_report_path=report,
    )
    second_reverse = import_portal_partition(
        campaign, "part-b", reverse_output,
        station_mapping_path=matches, station_mapping_report_path=report,
    )
    index_calibration_shard(
        reverse_output,
        second_reverse.shard_directory,
        second_reverse.manifest,
        summarize=False,
    )
    index_calibration_shard(
        reverse_output,
        first_reverse.shard_directory,
        first_reverse.manifest,
        summarize=False,
    )
    finalize_portal_calibration_corpus(campaign, reverse_output)
    reverse_corpus = json.loads(
        (reverse_output / "corpus-manifest.json").read_text()
    )
    assert reverse_corpus["integrity"]["index_digest"] == corpus["integrity"]["index_digest"]
    assert reverse_corpus["corpus_digest"] == corpus["corpus_digest"]


def test_corpus_envelope_v2_requires_finalization_provenance_for_final_state(
    tmp_path: Path,
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "week": ("raw-summer.csv", "2026-07-06", "2026-07-06", "week.csv"),
        },
    )
    output = tmp_path / "out"
    shard = import_portal_partition(
        campaign,
        "week",
        output,
        station_mapping_path=matches,
        station_mapping_report_path=report,
    )
    assert (
        shard.manifest.corpus_schema_version
        == CALIBRATION_V2_SHARD_ENVELOPE_SCHEMA_VERSION
        == 1
    )
    index_calibration_shard(
        output,
        shard.shard_directory,
        shard.manifest,
        summarize=False,
    )
    finalize_portal_calibration_corpus(campaign, output)
    payload = json.loads((output / "corpus-manifest.json").read_text())

    missing_provenance = dict(payload)
    missing_provenance.pop("finalization")
    with pytest.raises(ValidationError, match="finalization provenance"):
        CalibrationObservationCorpusV2.model_validate(missing_provenance)

    legacy_envelope = dict(payload)
    legacy_envelope["corpus_schema_version"] = 1
    with pytest.raises(ValidationError, match="corpus_schema_version"):
        CalibrationObservationCorpusV2.model_validate(legacy_envelope)


def test_one_pass_reconciliation_matches_reference_across_batch_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "part-a": ("raw-week.csv", "2026-01-05", "2026-01-11", "a.csv"),
            "part-b": ("raw-week.csv", "2026-01-05", "2026-01-11", "b.csv"),
            "part-c": ("raw-week.csv", "2026-01-05", "2026-01-11", "c.csv"),
        },
    )
    changed = campaign / "raw" / "c.csv"
    changed.write_text(
        changed.read_text(encoding="utf-8").replace(
            "2026-01-05T08:00:00-08:00,00:15:00,101,50,10,150.5,45,0,0,0,0",
            "2026-01-05T08:00:00-08:00,00:15:00,101,51,10,150.5,45,0,0,0,0",
        ),
        encoding="utf-8",
    )
    manifest_path = campaign / "campaign-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["chunks"]["part-c"]["raw"].update(
        bytes=changed.stat().st_size,
        sha256=_sha(changed),
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    output = tmp_path / "out"
    imported = {
        partition: import_portal_partition(
            campaign,
            partition,
            output,
            station_mapping_path=matches,
            station_mapping_report_path=report,
        )
        for partition in ("part-a", "part-b", "part-c")
    }
    for partition in ("part-c", "part-a", "part-b"):
        shard = imported[partition]
        index_calibration_shard(
            output,
            shard.shard_directory,
            shard.manifest,
            summarize=False,
        )

    reference_summary, reference_evidence = _reference_integrity(output)
    expected = {
        shard.manifest.shard_id: shard.manifest.audit.accepted_count
        for shard in imported.values()
    }
    queries: list[str] = []
    original_connect = integrity_service._connect

    def traced_connect(path: Path) -> sqlite3.Connection:
        connection = original_connect(path)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(integrity_service, "_connect", traced_connect)
    progress = []
    optimized = finalize_calibration_integrity_index(
        output,
        expected,
        fetch_batch_size=2,
        progress_callback=progress.append,
        progress_interval_seconds=0,
    )
    assert optimized.summary.model_dump(mode="json") == reference_summary
    assert optimized.conflict_evidence == reference_evidence
    assert optimized.summary.physical_observation_count == 12
    assert optimized.summary.logical_observation_count == 3
    assert optimized.summary.global_duplicate_claim_count == 7
    assert optimized.summary.global_conflict_identity_count == 1
    assert progress[-1].processed_claim_count == 12
    assert progress[-1].total_claim_count == 12
    claim_queries = [
        query
        for query in queries
        if "FROM observation_claims" in query
    ]
    assert len(claim_queries) == 1
    assert "ORDER BY observation_id, shard_key" in claim_queries[0]


def test_finalization_reuses_verified_hashes_and_interruption_cannot_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "week": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv"),
        },
    )
    output = tmp_path / "out"
    shard = import_portal_partition(
        campaign,
        "week",
        output,
        station_mapping_path=matches,
        station_mapping_report_path=report,
    )
    index_calibration_shard(
        output,
        shard.shard_directory,
        shard.manifest,
        summarize=False,
    )

    physical_calls = 0
    canonical_calls = 0
    original_physical = campaign_service._sha256_file
    original_canonical = campaign_service._canonical_stream_sha256
    original_importer_physical = importer_service._sha256_file
    original_importer_canonical = importer_service._canonical_stream_sha256

    def count_physical(path: Path) -> str:
        nonlocal physical_calls
        physical_calls += 1
        return original_physical(path)

    def count_canonical(path: Path, compression: str) -> str:
        nonlocal canonical_calls
        canonical_calls += 1
        return original_canonical(path, compression)

    monkeypatch.setattr(campaign_service, "_sha256_file", count_physical)
    monkeypatch.setattr(
        campaign_service,
        "_canonical_stream_sha256",
        count_canonical,
    )

    def unexpected_rehash(*_args, **_kwargs):
        raise AssertionError("corpus manifest builder rehashed a validated stream")

    monkeypatch.setattr(importer_service, "_sha256_file", unexpected_rehash)
    monkeypatch.setattr(
        importer_service,
        "_canonical_stream_sha256",
        unexpected_rehash,
    )
    finalize_portal_calibration_corpus(
        campaign,
        output,
        fetch_batch_size=1,
    )
    assert physical_calls == 1
    assert canonical_calls == 1
    assert (output / "corpus-manifest.json").is_file()

    monkeypatch.setattr(
        importer_service,
        "_sha256_file",
        original_importer_physical,
    )
    monkeypatch.setattr(
        importer_service,
        "_canonical_stream_sha256",
        original_importer_canonical,
    )

    interrupted_output = tmp_path / "interrupted"
    interrupted = import_portal_partition(
        campaign,
        "week",
        interrupted_output,
        station_mapping_path=matches,
        station_mapping_report_path=report,
    )
    index_calibration_shard(
        interrupted_output,
        interrupted.shard_directory,
        interrupted.manifest,
        summarize=False,
    )

    def interrupt_reconciliation(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic final reconciliation interruption")

    monkeypatch.setattr(
        campaign_service,
        "finalize_calibration_integrity_index",
        interrupt_reconciliation,
    )
    with pytest.raises(KeyboardInterrupt, match="synthetic final"):
        finalize_portal_calibration_corpus(campaign, interrupted_output)
    assert not (interrupted_output / "corpus-manifest.json").exists()
    assert not (interrupted_output / ".corpus-manifest.pending").exists()


def test_interrupted_integrity_transaction_rolls_back_and_resume_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "week": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv"),
        },
    )
    output = tmp_path / "out"
    shard = import_portal_partition(
        campaign, "week", output,
        station_mapping_path=matches, station_mapping_report_path=report,
    )
    original_iterator = integrity_service.iter_validated_shard_observations

    def interrupted_iterator(shard_directory, shard_manifest):
        iterator = original_iterator(shard_directory, shard_manifest)
        yield next(iterator)
        raise RuntimeError("synthetic interruption inside shard transaction")

    monkeypatch.setattr(
        integrity_service,
        "iter_validated_shard_observations",
        interrupted_iterator,
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        index_calibration_shard(
            output, shard.shard_directory, shard.manifest, summarize=False
        )
    progress = inspect_calibration_integrity_progress(output)
    assert progress.indexed_shard_count == 0
    assert progress.physical_observation_count == 0
    monkeypatch.setattr(
        integrity_service,
        "iter_validated_shard_observations",
        original_iterator,
    )
    index_calibration_shard(
        output, shard.shard_directory, shard.manifest, summarize=False
    )
    resumed = inspect_calibration_integrity_progress(output)
    assert resumed.indexed_shard_count == 1
    assert resumed.physical_observation_count == shard.manifest.audit.accepted_count
    retry = index_calibration_shard(
        output, shard.shard_directory, shard.manifest
    )
    assert retry.reused_shard is True
    assert retry.summary.physical_observation_count == shard.manifest.audit.accepted_count


def test_cross_shard_conflict_is_order_independent_and_blocks_clean_state(tmp_path: Path) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "part-a": ("raw-summer.csv", "2026-07-06", "2026-07-06", "a.csv"),
            "part-b": ("raw-summer.csv", "2026-07-06", "2026-07-06", "b.csv"),
        },
    )
    changed = campaign / "raw" / "b.csv"
    changed.write_text(changed.read_text(encoding="utf-8").replace(",50,10,20,", ",51,10,20,"), encoding="utf-8")
    manifest_path = campaign / "campaign-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["chunks"]["part-b"]["raw"].update(bytes=changed.stat().st_size, sha256=_sha(changed))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "out"
    second = import_portal_partition(campaign, "part-b", output, station_mapping_path=matches, station_mapping_report_path=report)
    index_calibration_shard(output, second.shard_directory, second.manifest)
    first = import_portal_partition(campaign, "part-a", output, station_mapping_path=matches, station_mapping_report_path=report)
    result = index_calibration_shard(output, first.shard_directory, first.manifest)
    assert result.summary.physical_observation_count == 2
    assert result.summary.logical_observation_count == 0
    assert result.summary.global_conflict_identity_count == 1
    assert result.summary.global_duplicate_claim_count == 0
    assert _records(first)[0]["record_id"] == _records(second)[0]["record_id"]
    assert result.conflict_evidence[0]["observation_id"] == _records(first)[0]["record_id"]
    reverse_output = tmp_path / "out-reverse"
    first_reverse = import_portal_partition(campaign, "part-a", reverse_output, station_mapping_path=matches, station_mapping_report_path=report)
    second_reverse = import_portal_partition(campaign, "part-b", reverse_output, station_mapping_path=matches, station_mapping_report_path=report)
    index_calibration_shard(reverse_output, first_reverse.shard_directory, first_reverse.manifest)
    reverse = index_calibration_shard(reverse_output, second_reverse.shard_directory, second_reverse.manifest)
    assert reverse.summary == result.summary
    assert reverse.conflict_evidence == result.conflict_evidence
    with pytest.raises(PortalImportError, match="Global observation conflicts"):
        finalize_portal_calibration_corpus(campaign, output)
    conflict_manifest = json.loads((output / "corpus-manifest.json").read_text())
    assert conflict_manifest["materialization_state"] == "conflicts_present"


def test_phase_1_3_compiler_preserves_five_weekdays_and_is_reproducible(
    tmp_path: Path,
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "week": ("raw-week.csv", "2026-01-05", "2026-01-11", "week.csv"),
        },
    )
    raw = campaign / "raw" / "week.csv"
    lines = raw.read_text(encoding="utf-8").splitlines()
    additions = [
        "2026-01-07T08:15:00-08:00,00:15:00,101,45,4,8,43,0,0,0,0",
        "2026-01-08T08:15:00-08:00,00:15:00,101,40,5,9,42,0,0,0,0",
        "2026-01-09T08:15:00-08:00,00:15:00,101,35,6,10,41,0,0,0,0",
    ]
    raw.write_text("\n".join([*lines[:-1], *additions, lines[-1]]) + "\n", encoding="utf-8")
    campaign_payload = json.loads((campaign / "campaign-manifest.json").read_text())
    campaign_payload["chunks"]["week"]["raw"].update(
        bytes=raw.stat().st_size,
        sha256=_sha(raw),
    )
    campaign_payload["chunks"]["week"]["raw_rows"] = len(lines) - 1 + len(additions)
    (campaign / "campaign-manifest.json").write_text(
        json.dumps(campaign_payload), encoding="utf-8"
    )
    assert matches is not None
    matches.write_text(
        "station_id,direction,status,edge_id\n"
        "portal-station-10,NORTH,accepted,app-edge-10\n"
        "portal-station-11,NORTH,accepted,app-edge-10\n",
        encoding="utf-8",
    )
    corpus_root = tmp_path / "corpus"
    shard = import_portal_partition(
        campaign,
        "week",
        corpus_root,
        station_mapping_path=matches,
        station_mapping_report_path=report,
    )
    index_calibration_shard(
        corpus_root,
        shard.shard_directory,
        shard.manifest,
        summarize=False,
    )
    finalize_portal_calibration_corpus(campaign, corpus_root)
    edges = tmp_path / "edges.parquet"
    pd.DataFrame(
        [{"edge_id": "app-edge-10", "maxspeed_kph": 100.0}]
    ).to_parquet(edges, index=False)

    first = compile_historical_calibration_v2(
        corpus_root=corpus_root,
        campaign_manifest_path=campaign / "campaign-manifest.json",
        graph_edges_path=edges,
        graph_version="synthetic-graph-v1",
        output_directory=tmp_path / "compiled-first",
        edge_partition_count=2,
        json_block_size=64 * 1024,
        require_complete_calendar=False,
        log=lambda _message: None,
    )
    second = compile_historical_calibration_v2(
        corpus_root=corpus_root,
        campaign_manifest_path=campaign / "campaign-manifest.json",
        graph_edges_path=edges,
        graph_version="synthetic-graph-v1",
        output_directory=tmp_path / "compiled-second",
        edge_partition_count=2,
        json_block_size=64 * 1024,
        require_complete_calendar=False,
        log=lambda _message: None,
    )

    date_level = pd.read_parquet(tmp_path / "compiled-first" / "edge-day-15m.parquet")
    profiles = pd.read_parquet(
        tmp_path / "compiled-first" / "edge-time-distributions.parquet"
    )
    assert set(date_level["weekday"]) == {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    }
    assert set(profiles["weekday"]) == set(date_level["weekday"])
    assert not {"saturday", "sunday"} & set(profiles["weekday"])
    monday = date_level[date_level["weekday"] == "monday"].iloc[0]
    tuesday = date_level[date_level["weekday"] == "tuesday"].iloc[0]
    assert monday["volume_count"] == 10
    assert monday["flow_vph"] == 40
    assert monday["speed_kph"] == pytest.approx(50 * 1.609344)
    assert monday["occupancy_percent"] == pytest.approx((150.5 + 0) / 2)
    assert tuesday["station_count"] == 2
    assert tuesday["volume_count"] == pytest.approx(1.5)
    assert tuesday["flow_vph"] == pytest.approx(6)
    assert tuesday["speed_kph"] == pytest.approx(35 * 1.609344)
    assert tuesday["sample_count"] == 84
    assert all(profiles["sample_days"] == 1)
    assert set(profiles["profile_status"]) == {"candidate"}
    assert set(profiles["calibration_status"]) == {"not_calibrated"}
    assert first.source_observation_count == 7
    assert first.mapped_observation_count == 7
    assert first.unmapped_observation_count == 0
    assert first.content_digest == second.content_digest
    assert first.date_level.sha256 == second.date_level.sha256
    assert first.weekday_profiles.sha256 == second.weekday_profiles.sha256


def test_phase_1_3_compiler_does_not_promote_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign, matches, report = _campaign(
        tmp_path,
        raws={
            "week": ("raw-summer.csv", "2026-07-06", "2026-07-06", "week.csv"),
        },
    )
    corpus_root = tmp_path / "corpus"
    shard = import_portal_partition(
        campaign,
        "week",
        corpus_root,
        station_mapping_path=matches,
        station_mapping_report_path=report,
    )
    index_calibration_shard(
        corpus_root,
        shard.shard_directory,
        shard.manifest,
        summarize=False,
    )
    finalize_portal_calibration_corpus(campaign, corpus_root)
    edges = tmp_path / "edges.parquet"
    pd.DataFrame(
        [{"edge_id": "app-edge-10", "maxspeed_kph": 100.0}]
    ).to_parquet(edges, index=False)
    output = tmp_path / "compiled"

    with pytest.raises(
        HistoricalCalibrationCompileError, match="all five weekdays"
    ):
        compile_historical_calibration_v2(
            corpus_root=corpus_root,
            campaign_manifest_path=campaign / "campaign-manifest.json",
            graph_edges_path=edges,
            graph_version="synthetic-graph-v1",
            output_directory=output,
            edge_partition_count=2,
            json_block_size=64 * 1024,
            log=lambda _message: None,
        )
    assert not output.exists()

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("synthetic aggregate interruption")

    monkeypatch.setattr(historical_compiler, "_aggregate_partition", interrupt)
    with pytest.raises(RuntimeError, match="synthetic aggregate interruption"):
        compile_historical_calibration_v2(
            corpus_root=corpus_root,
            campaign_manifest_path=campaign / "campaign-manifest.json",
            graph_edges_path=edges,
            graph_version="synthetic-graph-v1",
            output_directory=output,
            edge_partition_count=2,
            json_block_size=64 * 1024,
            require_complete_calendar=False,
            log=lambda _message: None,
        )
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*"))
