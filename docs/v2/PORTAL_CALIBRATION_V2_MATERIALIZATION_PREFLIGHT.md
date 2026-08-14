# Phase 1.2c1: PORTAL Calibration-V2 Materialization Preflight

## Authorization boundary

This phase prepares the 3,240-partition raw PORTAL campaign but does not run it. Normal commands and `npm run dev` cannot materialize the campaign. The only campaign-wide execution path requires both flags:

```bash
.venv/bin/python scripts/build_portal_calibration_v2.py \
  --campaign data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day \
  --output data/traffic/processed/calibration-v2/portal-raw-v1 \
  --station-mappings data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-edge-matches.csv \
  --station-mapping-report data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-match-report.json \
  --materialize-campaign \
  --confirm-full-campaign
```

This is the **Phase 1.2c2 command**, not an instruction to run it now. It is allowed only after rerunning the storage preflight for the intended target and confirming it still passes.

No completed source import is calibrated. It remains `historical_input` / `not_calibrated`; it proves neither observation quality, profile eligibility, traffic accuracy, nor SUMO calibration.

## Planning, atomicity, and resume

`plan_portal_calibration_campaign` reads only the campaign manifest, output artifacts, and raw-file metadata. It lexicographically orders partition IDs, derives expected shard IDs from partition ID plus raw-file SHA, and returns each partition in one state:

| State | Meaning |
|---|---|
| `pending` | Complete source entry exists and its file size agrees with the manifest; no shard exists. |
| `reusable_verified` | Expected immutable shard, physical hash, canonical hash, and raw provenance validate. |
| `failed` | Atomic failure-state record names an independently retryable failed partition. |
| `conflict` | Existing expected shard cannot be reconciled with source identity. |
| `invalid` | Manifest entry or referenced source file is malformed, missing, or size-inconsistent. |

Directories under `shards/.*` count as incomplete temporary output but are never valid shards. Shard promotion uses an atomic directory rename. Restart state is reconstructed from source manifests, shard manifests/streams, the persistent integrity index, and a small atomic failure-state artifact; it is not process-memory state. The source campaign is never modified.

```bash
# Full inventory only: no raw detector rows are parsed.
.venv/bin/python scripts/build_portal_calibration_v2.py \
  --campaign data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day \
  --output <materialization-output> --plan

# Plan plus durable shard/integrity status.
.venv/bin/python scripts/build_portal_calibration_v2.py \
  --campaign data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day \
  --output <materialization-output> --status

# Project full-run disk use from a small materialized sample.
.venv/bin/python scripts/build_portal_calibration_v2.py \
  --campaign data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day \
  --output <intended-materialization-output> \
  --preflight <representative-output>
```

Dry-run validates every source entry, raw path, raw byte size, chunk identity, source calendar, and manifest structure. SHA-256 of a raw source is rechecked immediately before each actual partition import. This avoids a full raw-content read in the plan-only step while retaining manifest verification at the point of use.

## Compressed canonical shards

The measured uncompressed representation was too large for the current disk. Each new immutable shard therefore stores `observations.jsonl.gz`, using stdlib gzip level 6 with an empty filename and `mtime=0`. The manifest contains separate identities:

- `canonical_sha256` and `canonical_byte_count` for sorted, uncompressed canonical JSONL;
- `physical_sha256` and `physical_byte_count` for deterministic gzip bytes.

Validation streams decompression, re-hashes canonical bytes, and Pydantic-validates one `ObservationRecord` at a time. Compression does not alter any traffic semantics or weaken typed validation.

## Cross-shard identity ledger and corpus promotion

`calibration-v2-integrity.sqlite3` is a persistent local SQLite index. It stores compact binary observation IDs and payload hashes plus a compact shard key; shard rows retain source partition and raw-reference provenance. It never builds a Python set of all campaign IDs.

- Same ID + same payload becomes an explicit global duplicate claim. One logical observation is counted; physical claims remain auditable.
- Same ID + differing payload creates bounded, sorted evidence with IDs, payloads, owners, and raw provenance. No winner is chosen and the ID is omitted from logical count.
- Different detector IDs at the same station/time remain distinct identities.
- A next-Monday boundary identity is a defect signal: it should already have been suppressed before a shard record exists.

Corpus states are `incomplete`, `structurally_valid`, `duplicates_resolved_logically`, `conflicts_present`, and `complete_unvalidated`. Cross-shard conflicts prohibit a clean completion state. A complete, conflict-free corpus is still only historical input—not calibration.

## Phase 1.2b coverage audit

The following matrix maps every required behavior to concrete assertions in `backend/tests/test_portal_calibration_v2_importer.py`.

| Requirement | Assertion location |
|---|---|
| 1–3: count 10, flow 40 VPH, exactly `1.609344` MPH conversion | `test_raw_measurement_normalization_calendar_missing_and_mapping` |
| 4–6: Monday/Tuesday, `-08:00`, `-07:00` | normalization test and `test_pacific_offsets_and_identity_exclude_filename_and_mappings` |
| 7–11: blank/zero speed, zero volume, unscaled occupancy | normalization test |
| 12: `countreadings` to sample count | normalization test |
| 13–17: deterministic ID; filename/order/mapping/measurement independence | Pacific/identity test and cross-shard conflict test (equal ID, different payload) |
| 18–19: exact duplicate suppression and conflict detection | `test_exact_duplicate_suppression_and_conflict_exclusion` |
| 20: separate detectors at same station/time | normalization test asserts shared station/start and distinct IDs |
| 21: inclusive-midnight suppression | normalization test |
| 22–25: accepted attachment; review/unmatched non-promotion; no SUMO association | normalization test |
| 26: historical input remains not calibrated | normalization test |
| 27: deterministic compressed/canonical digest and shard reuse | `test_shards_are_deterministic_reusable_and_incremental_without_corpus_materialization` |
| 28: bounded streaming processing | importer `csv.DictReader` assertion plus independent partition imports |

The new tests additionally cover deterministic planning, temporary-output detection, reuse, global duplicate classification, global conflict classification, and partition-order-independent conflict evidence.

## Measured representative preflight

On 2026-08-11, two actual weekly raw partitions were regenerated outside the repository: I-5 NB and I-205 SB for 2024-01-01 through 2024-01-07. They contain 81,519 raw rows, 81,340 accepted observations, 179 already-suppressed boundary rows, zero duplicates, zero global conflicts, and zero malformed-row rejects.

| Measurement | Observed |
|---|---:|
| Canonical JSONL | 100,912,392 bytes / 1,240.62 bytes per accepted observation |
| Deterministic gzip streams | 7,208,685 bytes / 88.62 bytes per accepted observation |
| Shard manifests / serialized audit evidence | 19,757 / 13,604 bytes |
| Compact SQLite integrity index | 5,763,072 bytes / 70.85 bytes per accepted observation |
| Import throughput | 5,107 and 5,300 raw rows/sec |
| Integrity insertion/check throughput | 20,624 and 18,186 accepted rows/sec |
| Measured maximum RSS | 656,588,800 bytes for one approximately 44k-row partition |

Memory is bounded by one partition’s local duplicate/conflict materialization and canonical stream, rather than the full corpus. The runner is sequential by default.

The 71,626,056-row campaign projects to 71,468,779 accepted observations:

| Projection | Bytes | Approx. GiB |
|---|---:|---:|
| Compressed observation shards | 6,333,856,837 | 5.90 |
| Shard manifests and audit | 32,006,340 | 0.03 |
| Compact integrity index | 5,063,679,852 | 4.72 |
| Materialized corpus total | 11,429,543,029 | 10.64 |
| Largest projected atomic staging shard | 5,019,082 | 0.005 |
| Projected peak including staging | 11,434,562,111 | 10.65 |

Required free space is `projected total + largest staging shard + max(25% of projected total, 20 GiB)`. The 20 GiB floor deliberately protects retries, index growth, and unrelated local work; it is not a token margin. At measurement, 60,216,315,904 bytes (56.08 GiB) were free; 32,909,398,591 bytes (30.65 GiB) were required. **The measured preflight passes.** It must be rerun immediately before Phase 1.2c2.

## Full-manifest dry-run

The plan-only command completed with no raw-observation transformation. It found:

- 3,240 expected source partitions;
- manifest SHA-256 `114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2`;
- 3,238 pending partitions and two representative shards reusable;
- zero invalid entries, duplicate source IDs, and temporary output directories;
- 15-minute Monday–Friday acquisition only.

No weekend historical materialization is planned. Phase 1.2c2 remains the next gate; profiles, SUMO demand, calibration, worlds, and probes remain out of scope.

## Phase 1.2c2 finalization performance correction

The first full campaign runner originally recomputed the complete integrity
summary and digest after every indexed shard. That was logically correct but
pathologically cumulative over 3,240 shards. Per-shard processing now performs
only transactional incremental claim registration and updates persisted O(1)
progress counters. A promoted-but-not-yet-indexed shard is safely recognized
and registered on resume; a partially indexed shard transaction rolls back.

After all 3,240 shards were materialized, the first explicit finalization was
stopped during its SQLite reconciliation after more than five hours. Source
inspection showed that finalization still used separate full-ledger queries for
shard claim counts, physical count, logical count, duplicates, conflicts,
digest input, and conflict evidence. It also verified every shard stream during
planning and then hashed every stream again while building the corpus manifest.
No corpus manifest or `complete_unvalidated` promotion was written by that
interrupted run.

The corrected finalizer uses one deterministic traversal:

```sql
SELECT observation_id, payload_sha256, shard_key
FROM observation_claims
ORDER BY observation_id, shard_key
```

`observation_claims` is a `WITHOUT ROWID` table whose primary key is
`(observation_id, shard_key)`. SQLite's live `EXPLAIN QUERY PLAN` reports
`SCAN observation_claims`; it requires no temporary B-tree, grouped query, or
secondary index/table lookup. One bounded Python state machine computes the
physical, logical, duplicate, conflict, per-shard, evidence, and deterministic
digest results together. It retains only the current observation identity's
claims, one counter per indexed shard, bounded conflict evidence, and one
50,000-row fetch batch. Progress is emitted from that traversal every 20
seconds with claims processed, percentage, elapsed time, rate, and estimated
remaining time; it performs no separate status query.

The digest remains byte-compatible with the original definition: every
non-conflicting identity contributes the compact JSON serialization of
`(portal.obs.<hex ID>, <payload SHA-256 hex>)`, plus a newline, in observation
ID order. Synthetic regression tests compare the optimized output against the
old grouped-query definition for unique, duplicate, and conflicting claims,
including batch boundaries and shuffled shard registration.

Final shard validation now returns trusted typed shard manifests. Corpus
manifest construction consumes those validation results instead of reopening
and hashing all streams a second time. Promotion remains atomic: interruption
before reconciliation completes cannot write `corpus-manifest.json` or a
`complete_unvalidated` state.

A temporary 500,000-identity benchmark produced 500,834 claims. All batch sizes
matched the old/reference counts, conflict evidence, and digest exactly:

| Fetch batch | Claims/sec | Elapsed | Traced Python peak |
|---:|---:|---:|---:|
| 10,000 | 35,475 | 14.12 s | 5.05 MB |
| 50,000 | 39,309 | 12.74 s | 25.12 MB |
| 100,000 | 39,184 | 12.78 s | 50.02 MB |

The legacy grouped queries completed this small warm temporary database in
2.02 seconds. That micro-result does not justify their production read
amplification: the failed production path performed several separate grouped
passes over 71,482,755 claims. The 50,000-row batch is retained because it gave
the best measured optimized throughput without the doubled memory cost of the
100,000-row batch. No new multi-gigabyte covering index is recommended.

The optimized implementation has not yet been run against the complete ledger.
The authoritative materialized state remains 3,240 immutable/indexed shards,
71,482,755 persisted logical claims, zero persisted duplicate claims, zero
persisted conflicts, and no final corpus promotion. A reviewed future retry may
run `--finalize-corpus`; it must never rerun `--materialize-campaign` merely to
finalize this corpus.

## Final Phase 1.2c2 production result

The reviewed optimized `--finalize-corpus` retry completed successfully on
2026-08-12. It did not import or regenerate any shard. One-time validation
accepted all 3,240 immutable shard manifests and streams. The ordered SQLite
reconciliation then processed all 71,482,755 claims in 5 minutes 11 seconds at
an average 229,958 claims/second. Total command wall time was 6 minutes 37.40
seconds; approximately 1 minute 26 seconds was spent outside the ledger
traversal on startup, shard validation, and final manifest construction.

The streamed reconciliation independently reproduced the persisted state:

| Integrity measure | Final value |
|---|---:|
| Indexed shards | 3,240 |
| Physical claims | 71,482,755 |
| Logical observations | 71,482,755 |
| Same-payload duplicate claims | 0 |
| Conflicting identities | 0 |
| Integrity digest | `4aed0b90c157f49c76469aa5a00e23b2b99efa6f02a570a69602c7a485c18d31` |

Per-shard streamed claim counts agreed with the 3,240 validated shard
manifests, and the full result agreed with the transactionally persisted
incremental counters. No inclusive-Monday-midnight overlap reached the global
ledger as a duplicate identity.

Final raw-row accounting closes exactly:

```text
71,626,056 raw rows encountered
  = 71,482,755 accepted observations
  +    141,978 inclusive-boundary suppressions
  +        954 detector-not-in-manifest-metadata rejects
  +        369 negative-volume rejects
  +          0 local duplicate suppressions
  +          0 local conflicts
```

Mapping coverage in the immutable shard manifests is:

| Mapping disposition | Observation count |
|---|---:|
| Accepted app-edge attached | 62,895,977 |
| Review or unmatched, deliberately unattached | 8,586,778 |
| Absent matcher information | 0 |
| Accepted SUMO association | 0 |

The Phase 1.2c shard contract combines review and unmatched observation counts;
it does not preserve separate corpus-level totals for those two statuses. The
station matcher separately records 55 review stations and 21 unmatched
stations, but those station counts must not be misreported as observation
counts. A later reporting schema may separate them without changing immutable
observation content.

Calendar coverage remains 675 Monday-Friday dates from 2024-01-01 through
2026-07-31, all five weekdays, and all 96 15-minute start buckets. The accepted
interval bounds are 2024-01-01 00:00 Pacific through 2026-08-01 00:00 Pacific
(the exclusive end of the final 2026-07-31 interval). Existing campaign
evidence reports 502 observed stations. The selected metadata contains 1,336
detector definitions; the current corpus manifest does not persist an exact
corpus-wide distinct detector union, so finalization did not perform another
observation-stream scan merely to recreate that reporting statistic.

Final storage is:

| Component | Bytes | Approximate size |
|---|---:|---:|
| Compressed observation streams | 5,962,506,223 | 5.55 GiB |
| Shard manifests | 27,228,480 | 25.97 MiB |
| Integrity SQLite | 5,283,647,488 | 4.92 GiB |
| Total corpus footprint | 11,275,287,097 | 10.50 GiB |
| Filesystem free after finalization | 47,651,012,608 | 44.38 GiB |

The actual 10.50 GiB footprint is about 0.14 GiB (1.3%) below the Phase 1.2c1
10.64 GiB projection; no material divergence requires explanation.

The authoritative corpus manifest now records:

```text
source campaign SHA-256:
114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2

plan digest:
b3ba4d64e4c0e105b81bcb5cea76fb8f52d31689cad47da6491c2664850b3a37

original Phase 1.2c2 corpus digest:
9ad2a25aad48de194190449db5cb4e07805a43a694eb08b60b8c5952cebac50e

materialization state: complete_unvalidated
artifact status: historical_input
calibration status: not_calibrated
```

### Finalization-provenance refresh

The first successful optimized finalization correctly preserved the
observation generator as
`portal_calibration_v2_importer / phase-1.2b`, but the schema-v1 corpus
envelope did not identify the Phase 1.2c2 finalizer or its integrity
reconciliation algorithm. The immutable observations and integrity result
were valid; the defect was limited to the provenance envelope.

The corpus envelope is now schema version 2 and records:

```text
observation generator: portal_calibration_v2_importer / phase-1.2b
finalizer: portal_calibration_v2_finalizer / phase-1.2c2
integrity reconciliation: ordered-stream / v1
```

The 3,240 immutable shard envelopes remain schema version 1 and were not
regenerated. The optimized finalizer revalidated them and reconciled all
71,482,755 claims in 5 minutes 10 seconds at 230,227 claims/second. The whole
provenance-refresh command took approximately 5 minutes 57 seconds. It again
found 71,482,755 physical and logical observations, zero duplicate claims,
and zero conflicts.

The logical integrity digest remains unchanged:

```text
4aed0b90c157f49c76469aa5a00e23b2b99efa6f02a570a69602c7a485c18d31
```

The corpus digest covers the complete reproducible manifest envelope (except
generation time), so adding required finalization provenance correctly changed
it. The original schema-v1 digest remains recorded above; the authoritative
schema-v2 corpus digest is:

```text
09a80fc2e419db11d19925fa078e370afccba4469af476bc1da0e4707844a37b
```

`plan_digest` is a deterministic plan/status snapshot digest, not an immutable
source-campaign identity. It includes each partition's mutable state and the
incomplete-temporary-output count. Its change from
`d423e1b374ae897376a49ae72b15d71b534207f06b208adbe9793a82c7eb7737`
during materialization to
`b3ba4d64e4c0e105b81bcb5cea76fb8f52d31689cad47da6491c2664850b3a37`
with all partitions `reusable_verified` is expected. The stable source
provenance anchor remains campaign manifest SHA-256
`114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2`.

Post-run planning reports 3,240 `reusable_verified`, zero pending, failed,
invalid, or conflicting partitions, zero temporary outputs, and 3,240 indexed
shards. This state proves faithful structural materialization only. It does not
establish observation quality, detector representativeness, profile
eligibility, SUMO calibration, or `historically_calibrated` evidence.
