# Phase 2.1 — native SUMO 15-minute edge telemetry

## Scope and integration point

Phase 2.1 adds an optional evidence artifact to the existing regional comparison
worker. It does not compare against PORTAL, tune demand, calibrate SUMO, replace
playback, or change the regional simulation model.

The regional worker still runs:

- SUMO 1.27.1 in mesoscopic mode;
- physical rerouting every 60 simulation seconds;
- one disposable libsumo child and atomic recovery checkpoint every 100
  simulation seconds;
- the existing scenario playback stream and baseline/scenario result summaries;
- local-only inputs and outputs.

The minimum safe integration point is the disposable variant child. When the
request has `edge_telemetry_interval_seconds: 900`, each child configures native
SUMO `edgeData` for its own committed 100-second window. The first committed
fragment includes SUMO's complete non-internal edge catalog; later fragments
omit unused edges to avoid writing the regional catalog every 100 seconds.

After both baseline and scenario variants finish, one bounded merge applies
SUMO's documented temporal aggregation rules and writes dense, complete
900-second edge intervals. This is necessary because the current runner
intentionally crosses process boundaries every 100 seconds; an in-process
900-second SUMO output device cannot survive those child exits.

Telemetry-enabled runs deliberately bypass the trip-specific baseline summary
cache in Phase 2.1. A cached JSON summary has no native edge fragments, so using
it would create a baseline/scenario evidence mismatch. Normal runs retain the
existing cache behavior.

## Interval contract

The artifact uses half-open SUMO-clock intervals `[start, end)` of exactly 900
seconds. `0–900` contains steps beginning at 0 through the step before 900;
`900–1800` begins a new, non-overlapping interval.

- A run beginning on a 900-second boundary emits that interval after all 900
  seconds are committed.
- A nonzero, non-aligned start omits the incomplete leading interval. It does
  not relabel a shorter window as 15 minutes.
- A run ending before the next boundary omits the incomplete tail. The manifest
  records omitted leading/trailing seconds.
- Resume uses only atomically promoted numeric checkpoint directories. It
  ignores `.tmp` output, reloads the committed SUMO/Python state, and discovers
  one native fragment per committed checkpoint. The merge rejects overlaps,
  gaps, repeated edge IDs, missing fragments, or inconsistent interval metadata.
- The existing 100-second recovery cadence is unchanged. These are telemetry
  intervals, not the durable cross-run 15-minute world checkpoints planned for
  Phase 4.

The current regional run begins at SUMO time zero. A 30-minute smoke with zero
warmup therefore emits exactly `0–900` and `900–1800` for both baseline and
scenario.

## Artifact contract

A successful telemetry run atomically promotes:

```text
data/sumo/runs/<application-run-id>/edge-telemetry-15m/
  edge-telemetry-15m.parquet
  edge-telemetry-manifest.json
```

One Parquet row is:

```text
deterministic run identity
× baseline/scenario variant
× native non-internal SUMO edge ID
× complete 900-second interval
```

The `variant` dimension is required because a regional comparison simulates two
different physical worlds. `sumo_edge_id` is never replaced by an app edge ID.
The Parquet row repeats the network version, SUMO version, mesoscopic mode,
seed, and demand version so an extracted record remains interpretable. The
manifest additionally records graph/network/demand hashes, physical scale,
producer and metric-semantics versions, row/file hashes, and content identity.

Evidence remains `modeled_uncalibrated`; calibration status remains
`not_calibrated`.

`content_digest` excludes `generated_at` and the random application run UUID.
It includes deterministic run input identity, network/demand provenance,
interval contract, and the canonical ordered row-content digest. Equivalent
inputs with the same deterministic run identity therefore reproduce the same
content and physical Parquet hash.

## Metric semantics

The source semantics below follow SUMO's edge/lane mean-data contract. They are
simulation-wide edge measurements, not PORTAL point-detector measurements.

| Artifact metric | Native SUMO source | Unit | 900-second interpretation | Mesoscopic behavior / nulls | Future Phase 2.2 role |
| --- | --- | --- | --- | --- | --- |
| `entered_count` | `edgeData edge@entered` | vehicles | Sum of vehicles moving onto the edge from upstream; excludes vehicles emitted directly on the edge | Nonnegative zero is retained | Candidate upstream count; semantics must be aligned before comparison |
| `departed_count` | `edgeData edge@departed` | vehicles | Vehicles emitted directly onto this edge | Nonnegative zero is retained | Context for origin-edge count differences |
| `left_count` | `edgeData edge@left` | vehicles | Vehicles moving from this edge to a downstream edge | Nonnegative zero is retained | Candidate downstream count |
| `arrived_count` | `edgeData edge@arrived` | vehicles | Vehicles ending their route on this edge | Nonnegative zero is retained | Context for destination-edge count differences |
| `flow_vph` | derived from `entered_count` | vehicles/hour | `entered_count × 3600 / 900`, exactly `entered_count × 4` | Zero remains zero; no scale-to-real-vehicles correction is hidden in the field | Candidate flow after mapping/grain policy is defined |
| `sampled_vehicle_seconds` | `edgeData edge@sampledSeconds` | vehicle-seconds | Sum of vehicle presence over the interval | Zero is explicit and makes state metrics null | Exposure/support denominator |
| `mean_speed_mps` | `edgeData edge@speed`, composed with `sampledSeconds` weights | m/s | SUMO space-mean speed across time and edge space | Null with no sampled traffic; zero is preserved | Primary speed target after mapping |
| `mean_speed_kph` | `mean_speed_mps × 3.6` | km/h | Unit conversion only | Same null/zero behavior as m/s | Convenience comparison unit |
| `mean_travel_time_seconds` | SUMO `traveltime` semantics, recomputed from aggregated speed and the native speed/travel-time edge-length relationship | seconds | Estimated time for a vehicle front to pass the edge; SUMO documents it as an estimate based on mean speed, not observed per-vehicle traversal time | Null if no positive-speed native edge-length evidence exists | Secondary travel-state target |
| `density_veh_per_km` | `edgeData edge@density` | vehicles/km | Duration-weighted mean; empty checkpoint subwindows contribute zero when the edge otherwise has samples | Null when the full interval has no edge samples | Traffic-state diagnostic / possible target |
| `occupancy_percent` | `edgeData edge@occupancy` | percent | Duration-weighted mean percentage of edge space occupied | Null with no samples | Semantically secondary: it is not PORTAL detector occupancy |
| `waiting_time_seconds` | `edgeData edge@waitingTime` | total vehicle-seconds | Sum of seconds vehicles were considered halting by SUMO | Null with no samples | Congestion diagnostic |
| `time_loss_seconds` | `edgeData edge@timeLoss` | total vehicle-seconds | Sum of time lost relative to desired speed | Null with no samples | Congestion diagnostic |

SUMO's native temporal aggregation rules are preserved: count attributes sum;
speed is weighted by `sampledSeconds`; density and occupancy are averaged over
time; waiting time and time loss sum; travel time is derived from the aggregate
mean speed. No PORTAL harmonization or detector-occupancy reinterpretation
occurs in Phase 2.1.

## Failure and atomicity

Each native fragment is written inside the checkpoint's `.tmp` directory,
parsed and structurally validated after libsumo closes, and promoted only with
the complete SUMO state, Python state, chunk result, and checkpoint metadata.
An interrupted child leaves no authoritative fragment.

Final Parquet and manifest are built under `.edge-telemetry-15m.pending`. The
completed directory is promoted only after both variant sequences, native edge
catalogs, interval coverage, row ordering, hashes, and typed manifest validate.
A cancelled or failed run may retain committed recovery fragments, but it has no
authoritative completed telemetry manifest and cannot masquerade as a completed
calibration run.

## Tyler acceptance smoke

The smoke reuses the newest usable completed regional request for its selected
trip and generic closures, forces a zero-minute warmup, 30-minute analysis,
one-to-one physical scale, and enables 900-second telemetry:

```bash
.venv/bin/python -m scripts.run_sumo_telemetry_smoke --analysis-minutes 30
```

This is intentionally not run by the Phase 2.1 implementation agent. It is the
real regional acceptance gate and may take substantial local compute.

After it completes, inspect the newest artifact without another SUMO run:

```bash
.venv/bin/python -m scripts.inspect_sumo_edge_telemetry --latest
```

The inspector reports row/interval/edge counts, exact boundaries,
min/median/max flow and mean speed, non-null metric counts, duplicate identities,
negative values, file size/hash, and run/network identity. These checks establish
structure and plausibility only; they do not compare against PORTAL.

## Known Phase 2.1 limits

- The current proxy demand remains a single start-time snapshot over the run;
  Phase 2.1 does not make demand dynamic.
- Telemetry-enabled baseline summaries are recomputed because the old cache has
  no telemetry payload.
- Native edge travel time is SUMO's speed-based estimate, not an exact mean of
  completed edge traversals.
- SUMO edge occupancy and PORTAL detector occupancy are not interchangeable.
- There is no app-edge join, PORTAL comparator, WAPE/MAE/bias scoring, quality
  threshold, or calibration promotion in this phase.
