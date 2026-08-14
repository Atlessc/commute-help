# Phase 1.2b: Raw PORTAL to Calibration-V2 Importer

## Scope and source boundary

The importer consumes only a completed, manifest-verified `raw/*.csv` PORTAL
detector partition plus the manifest-pinned metadata. It never reads a
`normalized/*.csv` file. In that older station roll-up, `volume` is VPH; in a
raw row, `volume` is the 15-minute detector count. The importer writes
`volume_count = raw volume` and `flow_vph = raw volume * 4`.

This phase creates immutable observation shards and a corpus index. It does
not build weekday profiles, traffic demand, baseline worlds, scenario worlds,
or calibrated simulation results. Every emitted record remains
`historical_observation_input` / `not_calibrated`.

## Partition flow

```text
manifest-verified raw CSV (one row at a time)
  -> structural and Pacific-calendar validation
  -> manifest-pinned detector / station / highway enrichment
  -> inclusive next-Monday export-boundary suppression
  -> deterministic observation identity
  -> deterministic duplicate / conflict handling
  -> canonical-content-addressed observations.jsonl.gz + immutable shard manifest
  -> corpus-manifest.json index (validates shard stream hashes only)
```

The streaming importer retains only the current partition's ID/payload index,
not the campaign. The 71,626,056-row campaign must therefore be invoked as
separate chunk imports in Phase 1.2c. A completed shard is never modified in
place: an equivalent rerun reuses it; different recomputed content fails.
Promotion is an atomic directory rename.

## Verified normalization rules

- The raw source interval is 900 seconds. `starttime` is `interval_start` and
  `interval_end` is exactly 900 seconds later.
- The supplied Pacific `-08:00` or `-07:00` offset is preserved and validated
  against `America/Los_Angeles`; exact date and Monday–Friday weekday are
  derived only after that validation.
- Raw speed MPH is multiplied by exactly `1.609344`. Blank speed is missing,
  while speed zero stays numeric and has the
  `zero_speed_semantics_unresolved` quality flag.
- Raw volume zero and occupancy zero are valid measured zeroes. Occupancy is
  not clipped or rescaled. `countreadings` becomes `sample_count` without a
  quality threshold.
- Structurally valid records have `source_status=unknown`. PORTAL supplies no
  authoritative source quality status; local `good`, `low_sample`, and
  `profile_eligible` labels are never imported as source facts.

## Identity, overlap, and conflicts

The observation ID hashes this locked tuple only: provider endpoint, detector
ID, metadata station ID, highway ID, direction, offset-normalized source
timestamp, and 900-second resolution. It excludes filename, line number,
import order, graph/SUMO mappings, raw measurements, and derived values.

Rows outside their chunk's logical date window are suppressed before an ID is
made. This implements PORTAL's inclusive next-Monday-midnight export overlap;
it is an import-transport audit event, not a rejected traffic observation.

For an identical ID, identical canonical payload is suppressed. A differing
payload creates bounded conflict evidence and removes that identity from the
accepted shard. Legitimate different detector IDs at the same station/time
remain separate. Rejection and overlap diagnostics hold line number plus a
row hash, never a raw row dump, and are capped at 25 deterministic entries.

## Network associations

Only a station matcher row marked `accepted`, with the matcher report's graph
version, attaches an app-edge association. `review` and `unmatched` rows are
left unattached. No SUMO association is emitted: the available PORTAL-to-SUMO
work lacks the required versioned mapping contract. The raw metadata and
station-matcher source hashes remain in each shard for a later versioned SUMO
join.

## Manifest contracts

`CalibrationArtifactV2` remains strict for small complete artifacts. The
separate `CalibrationObservationShardV2` / `CalibrationObservationCorpusV2`
schemas carry canonical content hash, deterministic physical gzip hash,
content digest, source references, audit totals, mapping coverage, interval
bounds, and shard index data. Their reproducibility digests deliberately
exclude `generated_at`.

The immutable observation-shard envelope remains schema version 1. The
authoritative corpus envelope is schema version 2 because a finalized corpus
now requires separate typed provenance for:

```text
observation generator: portal_calibration_v2_importer / phase-1.2b
corpus finalizer: portal_calibration_v2_finalizer / phase-1.2c2
integrity reconciliation: ordered-stream / v1
```

The generator identity continues to describe raw-row normalization and shard
creation. Finalization provenance describes the implementation and integrity
algorithm that validated the immutable shard set, reconciled the global
ledger, and atomically promoted the corpus manifest. Incomplete corpus indexes
may omit finalization provenance; finalized states require it.

### Intentionally unresolved

This importer does not impose sample-day thresholds, outlier thresholds,
lane/station aggregation, speed weighting, flow inference beyond the raw
15-minute conversion, accepted SUMO mapping, or historical calibration.
Those remain Phase 1.2c/1.3+ quality and calibration decisions.
