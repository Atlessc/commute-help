# Phase 1.4 legacy P95 tail analysis

## Scope

Phase 1.4 explains the old pooled PM slowdown P95 without changing any
Phase 1.2 observation, Phase 1.3 candidate, characterization, or quality-policy
artifact. It produces descriptive evidence only and leaves every source and
output `not_calibrated`.

The analysis reads:

```text
legacy normalized PORTAL station/time observations
legacy accepted station-to-edge matches
legacy background profile report
Phase 1.3 edge/time distributions
Phase 1.3 quality characterization
Phase 1.3 policy-v1 statuses
frozen app graph edge metadata
```

It does not read the 71,482,755-record Phase 1.2 corpus.

## Reproduced legacy contract

The legacy `portal-background-profile-compiler-v1` defines
`weekday_afternoon` as 14:00 inclusive through 19:00 exclusive in Pacific
local time. It selects profile-eligible September/October Monday–Friday rows,
joins only accepted station mappings, calculates:

```text
graph maxspeed_kph / normalized station speed_kph
```

and clips the result to `[1, 5]`. Missing multipliers are omitted. NumPy's
linear quantile method is applied to the pooled array. One accepted normalized
station/time row receives one vote; edges, dates, weekdays, and stations are
not equally weighted.

The legacy report's route-reliability sampler does not retain all pooled rows.
It retains up to 10,000 evenly spaced order statistics, rounded to six decimal
places, and resamples those values with replacement. One sampled multiplier is
then applied to the entire selected route.

## Modern comparison contract

The modern comparison retains one row per:

```text
app edge × weekday × 15-minute bucket
```

inside the exact old PM window. Its comparison value is the already-computed
within-profile `slowdown_p95`; cross-profile quantiles give every profile one
vote. Results are shown for all candidates, direct plus supplemental evidence,
and direct evidence only. Zero-speed diagnostics are carried through but do
not modify policy-v1 or input values.

This is intentionally not a like-for-like replacement multiplier: the modern
value is a distribution of profile P95s and is not clipped to the old 1–5
range.

## Determinism and atomicity

Inputs are hash-verified against their typed manifests. Edge/weekday/bucket
identities must be unique and Monday–Friday only. Output is deterministically
ordered, written into a sibling staging directory, and promoted with one
atomic directory rename after every file and manifest validates. Interruption
cannot create an authoritative output directory.

Operational filtering uses both the source `local_month` value and the Hive
`month` partition when the latter exists. Graph maximum speed is attached by a
precomputed edge lookup, avoiding a repeated 213,954-row graph join for every
Arrow batch.

## Production command

```bash
.venv/bin/python scripts/analyze_historical_calibration_v2_p95_tail.py \
  --legacy-campaign data/traffic/processed/portland-vancouver-core-corridor-v2-full-day \
  --candidate data/traffic/processed/calibration-v2/historical-edge-profiles-v1 \
  --characterization data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-characterization \
  --policy data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1 \
  --graph-edges data/graphs/edges.parquet \
  --output data/traffic/processed/calibration-v2/historical-edge-profiles-v1-p95-tail-analysis
```

The output contains:

```text
p95-tail-profiles.parquet
p95-tail-summary.json
P95_TAIL_ANALYSIS.md
p95-tail-analysis-manifest.json
```

## Production result

The old pool reproduces 634,247 PM values and P95
`2.6509764097915816`. The modern output contains 29,321 PM profile identities,
29,124 of which have slowdown P95 evidence. The detailed report explains the
edge, corridor, weekday, bucket, support, zero-speed, and mapping concentration.

The generated analysis is the acceptance artifact. It is not a traffic model,
SUMO demand input, quality-policy revision, or calibration promotion.
