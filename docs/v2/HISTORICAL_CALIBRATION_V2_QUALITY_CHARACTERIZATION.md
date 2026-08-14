# Phase 1.3 candidate quality characterization

This step describes the evidence already present in the frozen Phase 1.3-v1
candidate tables. It does not select an eligibility policy, alter the source
artifact, revisit the raw observation corpus, or promote any profile.

## Data flow

```text
historical-edge-profiles-v1 (read only)
    edge-day-15m.parquet metadata + bounded batches
    edge-time-distributions.parquet candidate rows
        -> calendar/support/anomaly characterization
        -> isolated immutable characterization directory
```

The analyzer identity is
`historical_calibration_v2_quality_characterizer / phase-1.3-quality-v1`.
Its output grain is one row per candidate
`app_edge_id × weekday × 15-minute bucket`.

## Evidence semantics

- `possible_date_count` is calculated from the inclusive source window for
  the row's weekday. Weekends are neither synthesized nor exposed.
- `observed_date_count` is the exact number of date-level rows supporting the
  candidate identity.
- Edge diagnostics distinguish full dense calendar coverage (all five
  weekdays and all 96 buckets) from within-candidate date coverage. Missing
  profile identities therefore cannot make a sparse edge look more complete.
- Measurement-present counts are calculated from non-null date-level values.
  Missing counts include both absent dates and null measurements.
- Existing Phase 1.3 flow, speed, occupancy, and slowdown profile quantiles
  are copied rather than recomputed from raw observations.
- Per-profile `sample_count`, detector-support, station-support, and reference
  speed summaries use exact date-level values.
- Global integer support distributions use bounded exact histograms.
- The descriptive global occupancy upper quantiles use deterministic
  0.125-percentage-point histogram bins; the exact maximum is retained.
- Zero speed and occupancy above 100 are counted as preserved observations.
  They are not interpreted, clipped, rejected, or repaired.

## Bounded-memory and atomicity contract

The date table is scanned once in Arrow batches. Rows are routed into a fixed
number of temporary sorted-edge partitions while bounded aggregate counters
are updated. Each temporary partition is then reduced to candidate-profile
rows. Only the small candidate and characterization tables are held in full.

Output is built in a sibling staging directory and promoted with one atomic
directory rename after all outputs and the typed manifest validate. An
interruption leaves no authoritative characterization directory.

The analyzer refuses to overwrite an existing output. The production
`historical-edge-profiles-v1` directory is always read-only input.

## Outputs

- `profile-quality-characterization.parquet`: candidate-grain evidence rows.
- `quality-characterization.json`: aggregate distributions, support bands,
  edge diagnostics, time-of-day diagnostics, and derivability limits.
- `QUALITY_CHARACTERIZATION.md`: compact human-readable findings.
- `quality-characterization-manifest.json`: typed input/output provenance and
  deterministic content identity.

The content digest excludes the generation timestamp. Equivalent source
content and methodology therefore produce the same characterization identity.

## Explicit policy boundary

This analysis SHALL NOT define or emit minimum dates, minimum coverage,
minimum sample count, anomaly thresholds, repair rules, eligibility, trust,
acceptance, rejection, or historical calibration status. Those decisions
require a separate human-reviewed policy step.

The derived inputs do not preserve unique detector identities or raw
per-detector `countreadings` distributions after aggregation. They also do not
separate review-mapped from unmatched observation totals. Recovering those
facts would require a compiler/provenance change or a return to earlier source
artifacts and is outside this characterization pass.

## Command

```bash
.venv/bin/python scripts/characterize_historical_calibration_v2_quality.py \
  --source data/traffic/processed/calibration-v2/historical-edge-profiles-v1 \
  --output data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-characterization
```
