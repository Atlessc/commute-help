# Phase 1.3 historical calibration-v2 compiler

## Scope

Phase 1.3 transforms the frozen, manifest-verified calibration-v2 observation
corpus into two bounded, deterministic Parquet artifacts:

```text
edge-day-15m.parquet
edge-time-distributions.parquet
```

It also writes:

```text
historical-calibration-manifest.json
validation-report.json
validation-report.md
```

The output remains `historical_input` / `not_calibrated`. It is not registered
as an application traffic profile, does not generate SUMO demand, and does not
promote any calibration result.

## Bounded processing architecture

The compiler consumes the finalized corpus manifest and campaign manifest,
then processes source shards by logical source week. Each compressed JSONL
stream is decoded through PyArrow streaming record batches with an explicit
nested schema.

Only one logical week's detector/station accumulators are retained at once.
Date-level edge rows are written immediately to the canonical Parquet writer
and to deterministic temporary edge partitions. Each temporary partition is
then reduced independently into weekday distributions. There is no
full-corpus pandas concatenation and no corpus-sized Python observation set.

The output directory is promoted with one atomic directory rename. An
interruption removes its non-authoritative staging directory and cannot leave
a successful manifest. Phase 1.3-v1 is bounded and atomic but is not
checkpoint-resumable; an interrupted compile must restart from the frozen
source corpus.

## Aggregation policy

The locked Phase 1.3-v1 policy is:

1. Raw detector counts and flows at one station/time are summed. This is the
   lane-count interpretation already established by the PORTAL pipeline.
2. Station speed is weighted by positive detector volume. When every detector
   volume is zero, the median available detector speed is retained.
3. Station occupancy is the arithmetic mean of present detector values. It is
   preserved without clipping or rescaling.
4. If multiple longitudinal stations map to one app edge at the same time,
   station metrics are combined by median. Their flows are deliberately not
   summed because doing so would double-count traffic passing successive
   detectors.
5. Exact-date edge rows are the input samples for weekday distributions. Each
   date therefore has equal weight regardless of detector count.
6. Quantiles are exact within deterministic bounded edge partitions.
7. Missing measurements remain null. No cyclic interpolation, cross-weekday
   fallback, or weekend fabrication is permitted.
8. No outlier filter or sample-day promotion threshold is applied. Profiles
   are emitted as `candidate_unvalidated`, with sample counts and quality flags
   exposed for the later quality gate.
9. Slowdown is graph reference speed divided by observed positive speed.
   Explicit speed zero is preserved but has no slowdown value.

This policy is an explicit compiler contract, not a claim that detector
quality, occupancy semantics, or profile eligibility has been validated.

## Identity and provenance

The compiler manifest records:

- compiler name/version;
- frozen source corpus and integrity digests;
- source campaign-manifest SHA-256;
- graph version and edge-artifact SHA-256;
- source date window and Pacific timezone;
- all five weekday identities and 900-second resolution;
- batch/partition settings;
- aggregation policy;
- mapped/unmapped accounting;
- row counts, byte counts, and SHA-256 for both Parquet artifacts;
- deterministic manifest content digest excluding generation time.

Changing observation content, graph content/version, compiler contract, or a
physical output hash changes the manifest content identity. Operational
generation time does not.

## Production command — Tyler acceptance gate

Do not run this concurrently with another compiler targeting the same output.
The output path must not already exist.

```bash
.venv/bin/python scripts/build_historical_calibration_v2.py \
  --corpus data/traffic/processed/calibration-v2/portal-raw-v1 \
  --campaign-manifest data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json \
  --graph-edges data/graphs/edges.parquet \
  --graph-version 2026-08-08-portland-vancouver-frozen-v2 \
  --output data/traffic/processed/calibration-v2/historical-edge-profiles-v1
```

Then inspect:

```bash
ls -lh data/traffic/processed/calibration-v2/historical-edge-profiles-v1
npm run test:backend
```

The production gate passes only if the validation report shows Monday,
Tuesday, Wednesday, Thursday, and Friday independently, all 96 possible
15-minute buckets, ordered quantiles, nonnegative physical fields, closed
mapped/unmapped accounting, reproducible hashes, and no weekend profile rows.

## Deferred beyond Phase 1.3

- empirical sample-day and outlier promotion thresholds;
- detector quality and zero-speed interpretation;
- authoritative occupancy semantics for values above 100;
- accepted versioned PORTAL-to-SUMO associations;
- SUMO demand generation and calibration;
- explanation of the legacy pooled P95 tail (Phase 1.4).
- compiler checkpoint/resume support if full-run measurements justify it.
