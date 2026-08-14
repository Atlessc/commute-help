"""Bounded-memory global observation identity integrity for calibration shards."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.schemas.calibration_v2_corpus import (
    CalibrationObservationShardV2,
    CorpusIntegrityProgress,
    CorpusIntegritySummary,
)
from backend.app.services.portal_calibration_v2_importer import (
    PortalImportError,
    iter_validated_shard_observations,
)

INTEGRITY_INDEX_SCHEMA_VERSION = 2
INTEGRITY_RECONCILIATION_ALGORITHM = "ordered-stream"
INTEGRITY_RECONCILIATION_ALGORITHM_VERSION = "v1"
INTEGRITY_INDEX_FILENAME = "calibration-v2-integrity.sqlite3"
MAX_CONFLICT_EVIDENCE = 25
DEFAULT_RECONCILIATION_BATCH_SIZE = 50_000
DEFAULT_PROGRESS_INTERVAL_SECONDS = 20.0
ORDERED_CLAIMS_QUERY = """
    SELECT observation_id, payload_sha256, shard_key
    FROM observation_claims
    ORDER BY observation_id, shard_key
"""


@dataclass(frozen=True)
class IntegrityIndexResult:
    summary: CorpusIntegritySummary
    reused_shard: bool
    conflict_evidence: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class IntegrityReconciliationProgress:
    processed_claim_count: int
    total_claim_count: int
    elapsed_seconds: float
    claims_per_second: float
    estimated_remaining_seconds: float | None


@dataclass(frozen=True)
class _LedgerReconciliation:
    summary: CorpusIntegritySummary
    conflict_evidence: tuple[dict[str, object], ...]
    shard_claim_counts: dict[str, int]


ProgressCallback = Callable[[IntegrityReconciliationProgress], None]


def inspect_calibration_integrity_progress(output_root: Path) -> CorpusIntegrityProgress:
    """Return transactionally persisted O(1) campaign progress counters."""
    index_path = integrity_index_path(output_root)
    if not index_path.is_file():
        raise PortalImportError("Calibration integrity index is missing")
    connection = _connect(index_path)
    try:
        _initialize(connection)
        return _progress(connection)
    finally:
        connection.close()


def integrity_index_path(output_root: Path) -> Path:
    return output_root / INTEGRITY_INDEX_FILENAME


def index_calibration_shard(
    output_root: Path,
    shard_directory: Path,
    shard: CalibrationObservationShardV2,
    *,
    summarize: bool = True,
) -> IntegrityIndexResult | None:
    """Add one validated shard to the persistent global identity ledger.

    SQLite owns the O(N) ID index. Python sees one compressed JSONL record at a
    time, so adding a campaign shard cannot require an all-corpus ID set.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    connection = _connect(integrity_index_path(output_root))
    try:
        _initialize(connection)
        existing = connection.execute(
            "SELECT shard_key, content_digest, stream_sha256 FROM indexed_shards WHERE shard_id = ?",
            (shard.shard_id,),
        ).fetchone()
        expected = (shard.content_digest, shard.observation_stream.canonical_sha256)
        if existing is not None:
            if tuple(existing)[1:] != expected:
                raise PortalImportError(
                    f"Integrity index already has incompatible shard {shard.shard_id}"
                )
            if not summarize:
                return None
            summary, evidence = _summary(connection)
            return IntegrityIndexResult(summary, True, evidence)
        source_reference = next(
            (reference for reference in shard.source_references if reference.reference_id.startswith("portal.raw.")),
            None,
        )
        if source_reference is None:
            raise PortalImportError(f"Shard {shard.shard_id} lacks raw source provenance")
        try:
            connection.execute("BEGIN IMMEDIATE")
            physical_delta = 0
            logical_delta = 0
            duplicate_delta = 0
            conflict_delta = 0
            shard_key = connection.execute(
                """
                INSERT INTO indexed_shards (
                    shard_id, content_digest, stream_sha256, source_partition_id,
                    source_reference_id, source_reference_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    shard.shard_id,
                    *expected,
                    shard.source_partition_id,
                    source_reference.reference_id,
                    source_reference.sha256,
                ),
            ).lastrowid
            for record, payload_sha256 in iter_validated_shard_observations(shard_directory, shard):
                observation_blob = _observation_blob(record.record_id)
                payload_blob = bytes.fromhex(payload_sha256)
                existing_payloads = {
                    bytes(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT payload_sha256 FROM observation_claims WHERE observation_id = ?",
                        (observation_blob,),
                    )
                }
                try:
                    connection.execute(
                        """
                            INSERT INTO observation_claims (
                                observation_id, payload_sha256, shard_key
                            ) VALUES (?, ?, ?)
                        """,
                        (
                            observation_blob,
                            payload_blob,
                            shard_key,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise PortalImportError(
                        f"Shard {shard.shard_id} repeats observation identity {record.record_id} internally"
                    ) from error
                physical_delta += 1
                if not existing_payloads:
                    logical_delta += 1
                else:
                    if payload_blob in existing_payloads:
                        duplicate_delta += 1
                    if payload_blob not in existing_payloads and len(existing_payloads) == 1:
                        logical_delta -= 1
                        conflict_delta += 1
            connection.execute(
                """
                UPDATE integrity_progress SET
                    indexed_shard_count = indexed_shard_count + 1,
                    physical_observation_count = physical_observation_count + ?,
                    logical_observation_count = logical_observation_count + ?,
                    global_duplicate_claim_count = global_duplicate_claim_count + ?,
                    global_conflict_identity_count = global_conflict_identity_count + ?
                WHERE singleton = 1
                """,
                (
                    physical_delta,
                    logical_delta,
                    duplicate_delta,
                    conflict_delta,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if not summarize:
            return None
        summary, evidence = _summary(connection)
        return IntegrityIndexResult(summary, False, evidence)
    finally:
        connection.close()


def rebuild_calibration_integrity_index(output_root: Path) -> IntegrityIndexResult:
    """Reconstruct the index solely from immutable shards in deterministic order."""
    index_path = integrity_index_path(output_root)
    for path in (index_path, index_path.with_name(index_path.name + "-wal"), index_path.with_name(index_path.name + "-shm")):
        if path.exists():
            path.unlink()
    indexed = False
    for manifest_path in sorted((output_root / "shards").glob("*/shard-manifest.json")):
        shard = CalibrationObservationShardV2.model_validate_json(manifest_path.read_bytes())
        index_calibration_shard(
            output_root, manifest_path.parent, shard, summarize=False
        )
        indexed = True
    if not indexed:
        raise PortalImportError("Cannot build integrity index without promoted shards")
    return inspect_calibration_integrity_index(output_root)


def inspect_calibration_integrity_index(output_root: Path) -> IntegrityIndexResult:
    """Explicitly perform complete reconciliation and deterministic digesting."""
    index_path = integrity_index_path(output_root)
    if not index_path.is_file():
        raise PortalImportError("Calibration integrity index is missing")
    connection = _connect(index_path)
    try:
        _initialize(connection)
        summary, evidence = _summary(connection)
        return IntegrityIndexResult(summary, True, evidence)
    finally:
        connection.close()


def finalize_calibration_integrity_index(
    output_root: Path,
    expected_shards: dict[str, int],
    *,
    fetch_batch_size: int = DEFAULT_RECONCILIATION_BATCH_SIZE,
    progress_callback: ProgressCallback | None = None,
    progress_interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
) -> IntegrityIndexResult:
    """Perform one ordered full-ledger reconciliation before corpus promotion."""
    index_path = integrity_index_path(output_root)
    if not index_path.is_file():
        raise PortalImportError("Calibration integrity index is missing")
    connection = _connect(index_path)
    try:
        _initialize(connection)
        progress = _progress(connection)
        reconciled = _reconcile_ordered_claims(
            connection,
            total_claim_count=progress.physical_observation_count,
            fetch_batch_size=fetch_batch_size,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )
        actual_shards = reconciled.shard_claim_counts
        if actual_shards != dict(sorted(expected_shards.items())):
            missing = sorted(set(expected_shards) - set(actual_shards))[:10]
            extra = sorted(set(actual_shards) - set(expected_shards))[:10]
            wrong = sorted(
                shard_id
                for shard_id in set(expected_shards) & set(actual_shards)
                if expected_shards[shard_id] != actual_shards[shard_id]
            )[:10]
            raise PortalImportError(
                "Integrity index shard coverage mismatch: "
                f"missing={missing}, extra={extra}, wrong_claim_counts={wrong}"
            )
        _assert_progress_matches(reconciled.summary, progress)
        return IntegrityIndexResult(
            reconciled.summary,
            True,
            reconciled.conflict_evidence,
        )
    finally:
        connection.close()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    version = connection.execute(
        "SELECT value FROM integrity_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None:
        connection.execute(
            "INSERT INTO integrity_meta (key, value) VALUES ('schema_version', ?)",
            (str(INTEGRITY_INDEX_SCHEMA_VERSION),),
        )
    elif version[0] != str(INTEGRITY_INDEX_SCHEMA_VERSION):
        raise PortalImportError("Unsupported calibration integrity index schema version")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS indexed_shards (
            shard_key INTEGER PRIMARY KEY,
            shard_id TEXT NOT NULL UNIQUE,
            content_digest TEXT NOT NULL,
            stream_sha256 TEXT NOT NULL,
            source_partition_id TEXT NOT NULL,
            source_reference_id TEXT NOT NULL,
            source_reference_sha256 TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_claims (
            observation_id BLOB NOT NULL,
            payload_sha256 BLOB NOT NULL,
            shard_key INTEGER NOT NULL REFERENCES indexed_shards(shard_key),
            PRIMARY KEY (observation_id, shard_key)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_progress (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            indexed_shard_count INTEGER NOT NULL,
            physical_observation_count INTEGER NOT NULL,
            logical_observation_count INTEGER NOT NULL,
            global_duplicate_claim_count INTEGER NOT NULL,
            global_conflict_identity_count INTEGER NOT NULL
        )
        """
    )
    if connection.execute(
        "SELECT 1 FROM integrity_progress WHERE singleton = 1"
    ).fetchone() is None:
        indexed_shards = int(
            connection.execute("SELECT COUNT(*) FROM indexed_shards").fetchone()[0]
        )
        if indexed_shards:
            summary = _reconcile_ordered_claims(
                connection,
                total_claim_count=None,
                fetch_batch_size=DEFAULT_RECONCILIATION_BATCH_SIZE,
                progress_callback=None,
                progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
            ).summary
        else:
            summary = CorpusIntegritySummary(
                indexed_shard_count=0,
                physical_observation_count=0,
                logical_observation_count=0,
                global_duplicate_claim_count=0,
                global_conflict_identity_count=0,
                index_schema_version=INTEGRITY_INDEX_SCHEMA_VERSION,
                index_digest=hashlib.sha256().hexdigest(),
            )
        connection.execute(
            """
            INSERT INTO integrity_progress (
                singleton, indexed_shard_count, physical_observation_count,
                logical_observation_count, global_duplicate_claim_count,
                global_conflict_identity_count
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                summary.indexed_shard_count,
                summary.physical_observation_count,
                summary.logical_observation_count,
                summary.global_duplicate_claim_count,
                summary.global_conflict_identity_count,
            ),
        )
    connection.commit()


def _summary(
    connection: sqlite3.Connection,
) -> tuple[CorpusIntegritySummary, tuple[dict[str, object], ...]]:
    progress = _progress(connection)
    reconciled = _reconcile_ordered_claims(
        connection,
        total_claim_count=progress.physical_observation_count,
        fetch_batch_size=DEFAULT_RECONCILIATION_BATCH_SIZE,
        progress_callback=None,
        progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS,
    )
    _assert_progress_matches(reconciled.summary, progress)
    return reconciled.summary, reconciled.conflict_evidence


def _reconcile_ordered_claims(
    connection: sqlite3.Connection,
    *,
    total_claim_count: int | None,
    fetch_batch_size: int,
    progress_callback: ProgressCallback | None,
    progress_interval_seconds: float,
) -> _LedgerReconciliation:
    """Reconcile counts, conflicts, shard ownership, and digest in one pass."""
    if fetch_batch_size < 1:
        raise ValueError("Reconciliation fetch batch size must be positive")
    if progress_interval_seconds < 0:
        raise ValueError("Progress interval cannot be negative")

    shard_metadata: dict[int, dict[str, str]] = {}
    for row in connection.execute(
        """
        SELECT shard_key, shard_id, source_partition_id,
               source_reference_id, source_reference_sha256
        FROM indexed_shards
        ORDER BY shard_id
        """
    ):
        shard_metadata[int(row["shard_key"])] = {
            "shard_id": str(row["shard_id"]),
            "source_partition_id": str(row["source_partition_id"]),
            "source_reference_id": str(row["source_reference_id"]),
            "source_reference_sha256": str(row["source_reference_sha256"]),
        }
    shard_counts_by_key = {shard_key: 0 for shard_key in shard_metadata}

    physical_count = 0
    logical_count = 0
    duplicate_count = 0
    conflict_count = 0
    digest = hashlib.sha256()
    evidence: list[dict[str, object]] = []
    current_observation: bytes | None = None
    current_claims: list[tuple[bytes, int]] = []

    def finish_identity() -> None:
        nonlocal logical_count, duplicate_count, conflict_count
        if current_observation is None:
            return
        payload_counts: dict[bytes, int] = {}
        for payload, _shard_key in current_claims:
            payload_counts[payload] = payload_counts.get(payload, 0) + 1
        duplicate_count += sum(count - 1 for count in payload_counts.values())
        if len(payload_counts) == 1:
            logical_count += 1
            payload = next(iter(payload_counts))
            digest.update(
                json.dumps(
                    (_observation_text(current_observation), payload.hex()),
                    separators=(",", ":"),
                ).encode()
            )
            digest.update(b"\n")
            return
        conflict_count += 1
        if len(evidence) >= MAX_CONFLICT_EVIDENCE:
            return
        sorted_claims = sorted(
            current_claims,
            key=lambda claim: (
                claim[0],
                shard_metadata[claim[1]]["shard_id"],
            ),
        )
        evidence.append(
            {
                "observation_id": _observation_text(current_observation),
                "claims": [
                    {
                        "payload_sha256": payload.hex(),
                        **shard_metadata[shard_key],
                    }
                    for payload, shard_key in sorted_claims
                ],
            }
        )

    started_at = time.monotonic()
    last_progress_at = started_at
    cursor = connection.execute(ORDERED_CLAIMS_QUERY)
    while batch := cursor.fetchmany(fetch_batch_size):
        for row in batch:
            observation_id = bytes(row["observation_id"])
            payload_sha256 = bytes(row["payload_sha256"])
            shard_key = int(row["shard_key"])
            if shard_key not in shard_counts_by_key:
                raise PortalImportError(
                    f"Observation claim references unknown shard key {shard_key}"
                )
            if current_observation is not None and observation_id < current_observation:
                raise PortalImportError("Integrity claim traversal is not deterministically ordered")
            if current_observation is not None and observation_id != current_observation:
                finish_identity()
                current_claims = []
            current_observation = observation_id
            current_claims.append((payload_sha256, shard_key))
            shard_counts_by_key[shard_key] += 1
            physical_count += 1
        now = time.monotonic()
        if (
            progress_callback is not None
            and now - last_progress_at >= progress_interval_seconds
        ):
            progress_callback(
                _reconciliation_progress(
                    physical_count,
                    total_claim_count,
                    started_at,
                    now,
                )
            )
            last_progress_at = now
    finish_identity()
    if progress_callback is not None:
        progress_callback(
            _reconciliation_progress(
                physical_count,
                total_claim_count,
                started_at,
                time.monotonic(),
            )
        )

    summary = CorpusIntegritySummary(
        indexed_shard_count=len(shard_metadata),
        physical_observation_count=physical_count,
        logical_observation_count=logical_count,
        global_duplicate_claim_count=duplicate_count,
        global_conflict_identity_count=conflict_count,
        index_schema_version=INTEGRITY_INDEX_SCHEMA_VERSION,
        index_digest=digest.hexdigest(),
    )
    shard_claim_counts = {
        shard_metadata[key]["shard_id"]: count
        for key, count in shard_counts_by_key.items()
    }
    return _LedgerReconciliation(
        summary=summary,
        conflict_evidence=tuple(evidence),
        shard_claim_counts=dict(sorted(shard_claim_counts.items())),
    )


def _reconciliation_progress(
    processed: int,
    expected_total: int | None,
    started_at: float,
    now: float,
) -> IntegrityReconciliationProgress:
    elapsed = max(now - started_at, 0.0)
    rate = processed / elapsed if elapsed else 0.0
    total = expected_total if expected_total is not None else processed
    remaining = None
    if expected_total is not None and rate > 0:
        remaining = max(expected_total - processed, 0) / rate
    return IntegrityReconciliationProgress(
        processed_claim_count=processed,
        total_claim_count=total,
        elapsed_seconds=elapsed,
        claims_per_second=rate,
        estimated_remaining_seconds=remaining,
    )


def _assert_progress_matches(
    reconciled: CorpusIntegritySummary,
    progress: CorpusIntegrityProgress,
) -> None:
    if reconciled.model_dump(exclude={"index_digest"}) != progress.model_dump():
        raise PortalImportError(
            "Persisted integrity progress does not match full ledger reconciliation"
        )


def _progress(connection: sqlite3.Connection) -> CorpusIntegrityProgress:
    row = connection.execute(
        """
        SELECT indexed_shard_count, physical_observation_count,
               logical_observation_count, global_duplicate_claim_count,
               global_conflict_identity_count
        FROM integrity_progress WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        raise PortalImportError("Integrity progress counters are missing")
    return CorpusIntegrityProgress(
        indexed_shard_count=row[0],
        physical_observation_count=row[1],
        logical_observation_count=row[2],
        global_duplicate_claim_count=row[3],
        global_conflict_identity_count=row[4],
        index_schema_version=INTEGRITY_INDEX_SCHEMA_VERSION,
    )


def _observation_blob(value: str) -> bytes:
    prefix = "portal.obs."
    if not value.startswith(prefix):
        raise PortalImportError(f"Unexpected calibration observation ID: {value}")
    return bytes.fromhex(value.removeprefix(prefix))


def _observation_text(value: bytes) -> str:
    return "portal.obs." + bytes(value).hex()
