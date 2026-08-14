# Phase 1.3 historical quality policy-v1

This is the first human-reviewed evidence-use policy for the frozen Phase 1.3
candidate profiles. It applies classifications to the separate read-only
quality characterization. It does not alter candidate traffic measurements,
establish SUMO accuracy, or promote anything to `historically_calibrated`.

Policy identity:

```text
historical_calibration_v2_quality_policy / phase-1.3-policy-v1
```

Output grain:

```text
app_edge_id × weekday × 15-minute bucket
```

## Date-support policy

The classifier evaluates the exact integer ratio
`observed_date_count / possible_date_count`; it does not hardcode 122 days.
With the current 135 possible dates per weekday, 90% begins at 122 dates.

| Coverage | Evidence-use class | Meaning |
|---|---|---|
| `>= 0.90` | `direct_calibration_evidence` | Sufficient date support to serve as a direct calibration target. |
| `>= 0.75` and `< 0.90` | `supplemental_evidence` | Retained for supplemental or diagnostic use, not a direct target. |
| `< 0.75` | `insufficient_direct_evidence` | Retained, but insufficient for direct calibration. |

The class describes historical support, not whether the observed value is
physically correct. Sparse profiles are never deleted.

## Measurement-specific evidence

Eligibility is not one giant all-measurements gate.

### Flow

Flow is direct evidence when date support is direct and at least one flow
measurement is present. Zero flow remains valid numeric evidence. Speed or
occupancy anomalies do not invalidate flow.

### Speed

Speed is direct evidence when date support is direct and at least one nonzero
numeric speed exists. Missing and zero speeds remain represented. Zero speed
is a diagnostic, carries no rejection-percentage threshold in v1, and cannot
produce a slowdown ratio. A zero-speed occurrence does not invalidate other
nonzero speeds or flow.

### Occupancy

Occupancy is direct evidence when date support is direct and at least one
occupancy value not above 100 exists. Values above 100 remain preserved in the
source artifacts but are excluded from occupancy calibration sample counts.
They are not clipped, repaired, or used to invalidate flow or speed.

### Aggregated sample count

`sample_count` is aggregated edge/date/bucket `countreadings` evidence. Policy
v1 preserves its distribution and per-profile support statistics but applies
no global minimum threshold because this aggregate is affected by the number
of contributing detectors and stations.

## Network fallback semantics

An insufficient historical evidence class does not mean zero traffic, a
closed road, or an unusable edge. Later world-building must use a separately
governed prior or model fallback when direct historical evidence is absent.
This policy does not create that fallback.

## Artifact and atomicity

The compiler reads only:

```text
historical-edge-profiles-v1/historical-calibration-manifest.json
historical-edge-profiles-v1-quality-characterization/
    quality-characterization-manifest.json
    profile-quality-characterization.parquet
    quality-characterization.json
```

It emits a separate immutable directory containing:

```text
quality-policy-profile-status.parquet
quality-policy-report.json
QUALITY_POLICY_REPORT.md
quality-policy-manifest.json
```

Output is constructed in a sibling staging directory and promoted with one
atomic rename only after Parquet, reports, hashes, and the typed manifest are
complete. Interrupted work cannot become authoritative. Equivalent source
content and policy parameters produce the same content digest; `generated_at`
does not participate in that identity.

## Command

```bash
.venv/bin/python scripts/apply_historical_calibration_v2_quality_policy.py \
  --candidate data/traffic/processed/calibration-v2/historical-edge-profiles-v1 \
  --characterization data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-characterization \
  --output data/traffic/processed/calibration-v2/historical-edge-profiles-v1-quality-policy-v1
```

No Phase 1.4, demand-generation, SUMO-calibration, or world-building behavior
is part of this policy application.
