# Calibration-v2 artifact contract

**Phase:** 1.1 — schema and synthetic fixture only

**Schema version:** `2`

**Product calibration state allowed in this phase:** `not_calibrated`

## Purpose

Calibration-v2 is the versioned boundary between accepted historical traffic input and later regional traffic calibration. The contract preserves enough identity to explain where a value came from, when and where it was observed, which network mapping it used, and whether it is an exact observation or a derived profile.

Phase 1.1 does not compile production PORTAL data or promote a traffic model.

Canonical types live in:

```text
backend/app/schemas/calibration_v2.py
```

Deterministic parsing, serialization, and hashing live in:

```text
backend/app/services/calibration_v2_artifact_service.py
```

## Artifact envelope

Each artifact has:

```text
schema_version = 2
artifact_type = commute_help_calibration
artifact_id
artifact_status = historical_input | synthetic_fixture
calibration_status = not_calibrated
generated_at
generator identity
source dataset identity
one or more checksum-bound source references
typed records
```

`generated_at` must be timezone-aware. Source references identify a file or manifest through a normalized relative logical path and include a SHA-256 checksum. Absolute paths and parent traversal are rejected so artifacts preserve reproducible identities without leaking host filesystem layout. Synthetic artifacts and historical-input artifacts cannot silently exchange source-dataset status.

## Record grains

The `record_kind` discriminator makes the two grains structurally different.

### Exact-date observation

```text
record_kind = observation
station + detector + optional lane
direction
source location
exact local date
Monday | Tuesday | Wednesday | Thursday | Friday
timezone
timezone-aware interval start/end
900-second interval
optional accepted road/network association
measurements and explicit units
sample count
quality, missing fields, and exclusion reasons
```

The weekday must agree with the exact date in the declared IANA timezone. Saturday and Sunday exact dates fail validation rather than being folded into another day type.

### Derived weekday/profile value

```text
record_kind = derived_profile
direction
road/network association
Monday | Tuesday | Wednesday | Thursday | Friday
timezone
15-minute bucket start/end
season/profile identity and version
source artifact/record/station identities
source date window
observation count and sample days
explicit aggregation policy
typed metric distributions
quality status
```

Derived profiles do not have an `exact_date`. Exact observations do not have a seasonal profile identity. This prevents exact-date evidence from being silently treated as a canonical weekday profile.

## Measurements and missingness

Observation units are explicit:

| Field | Unit |
|---|---|
| `volume_count` | vehicles per observation interval |
| `flow_vph` | vehicles per hour |
| `speed_kph` | kilometers per hour |
| `occupancy_percent` | percent |
| `interval_seconds` | seconds |

Missing numeric values are JSON `null` and must also be listed in `quality.missing_fields`. A present value, including zero, cannot be listed as missing. This preserves the difference between a valid zero vehicle count and an absent count.

Rejected observations require an exclusion reason. Accepted observations require a positive sample count and at least one measurement.

Profile distributions carry their own unit and sample count. Quantiles must be nondecreasing. Fields are optional so a source does not need to invent unavailable occupancy or percentile values.

## Evidence boundary

Historical origin and calibration status are separate axes:

```text
input_status:
  historical_observation_input
  derived_historical_profile_input
  synthetic_fixture

calibration_status:
  not_calibrated
```

The Phase 1.1 schema has no `historically_calibrated` value. Importing historical observations, creating a profile, or successfully parsing an artifact cannot promote it. Later promotion requires the explicit Phase 2 calibration and held-out validation gates.

## Network associations

An observation may omit `road_association` when no accepted association exists. The compiler must not invent one.

When an app-edge association is known, it records:

```text
graph_version
app_edge_id
OSM way IDs, if retained
```

SUMO associations are optional and can appear only as an explicitly accepted association with:

```text
mapping_version
SUMO network version
one or more SUMO edge IDs
```

This contract intentionally has no generic guessed/pending SUMO mapping object. Unaccepted mappings remain outside the accepted association field and can be carried by source quality/exclusion evidence until a later mapping gate accepts them.

## Aggregation provenance

A derived profile identifies its source artifacts and source stations. It may additionally list exact source record IDs. It also preserves:

- source date window;
- observation count;
- distinct sample-day count;
- detector-collapse rule;
- lane-collapse rule;
- missing-bucket rule;
- outlier rule;
- aggregation method;
- minimum sample days.

Phase 1.1 defines the allowed policy vocabulary but does not choose a production policy. The Phase 1.2 diagnostic compiler must select and report one explicit policy instead of relying on an implicit dataframe operation.

## Deterministic serialization

Canonical JSON:

- includes explicit `null` values;
- sorts JSON object keys;
- sorts records by `record_id`;
- sorts source references and set-like identifier/quality fields;
- uses compact UTF-8 JSON with one trailing newline.

Equivalent validated artifacts therefore produce the same canonical SHA-256 even if their input record ordering differs.

## Synthetic fixture

The committed fixture is:

```text
backend/tests/fixtures/calibration_v2/tiny-calibration-v2.json
```

It uses invented IDs and values and includes:

- Monday and Tuesday observations;
- 07:00 and 07:15 intervals;
- northbound and eastbound directions;
- volume, flow, and speed;
- explicit sample counts and units;
- one accepted app-edge/SUMO-edge association;
- one valid zero-volume observation;
- one rejected all-measurements-missing observation;
- one candidate derived Monday profile with source-record provenance.

The fixture is structural test evidence only. It is not PORTAL data, a regional traffic profile, or a calibrated model artifact.

## Phase 1.2 handoff decisions

The diagnostic compiler must decide and make visible:

1. Whether one source row is already station-aggregated or still detector/lane-grained.
2. Whether lane counts are summed and how speed/occupancy are weighted.
3. Whether detector duplicates are retained, summed, or collapsed by median.
4. How count and flow are derived when only one is supplied.
5. Which source statuses are accepted, rejected, or retained as missing evidence.
6. Which missing buckets remain explicit versus being excluded from derived profiles.
7. The minimum number of sample days for a candidate profile.
8. The outlier policy and whether it changes any source measurement.
9. How accepted station-to-app-edge and app-edge-to-SUMO-edge mappings are joined without upgrading unaccepted mappings.
10. How source record IDs remain reproducible when production output is partitioned or streamed.

Those are compiler-policy decisions. They must not be smuggled into Phase 1.1 as fabricated data or untested calibration assumptions.
