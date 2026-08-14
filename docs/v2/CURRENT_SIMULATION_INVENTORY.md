# Commute Help V2 current simulation inventory

**V2 chunk:** 0.1 — Current-system inventory

**Inventory date:** 2026-08-11

**Branch inspected:** `Commute-Help-v2`

**Runtime behavior changed by this chunk:** No

## Purpose and evidence boundary

This document records the simulation system that exists before the V2 run-domain split. It is an implementation inventory, not a claim that the current traffic model is calibrated or that any V2 world contract already exists.

The current regional SUMO path is integrated and runnable, but its product evidence remains `modeled_uncalibrated`. The live checkout, active manifests, schemas, services, workers, generated-artifact layout, and tests were inspected. Generated data was read only; no raw PORTAL data or simulation artifacts were changed.

## Executive finding

Today's `regional_comparison` is one coupled job:

```text
selected departure + selected trip + selected closures
                        |
                        v
             build/reuse demand package
                        |
                        v
        run or load trip-specific baseline result
                        |
                        v
            run closure scenario from time zero
                        |
                        v
          compare the selected trip and edge stats
```

The implementation has two useful reuse mechanisms already:

1. A generated demand package can be reused for matching time-window, scale, seed, network, schedule, and background-seed inputs.
2. A completed closure-independent baseline result can be reused for an identical selected trip and physical configuration.

Neither mechanism is a V2 baseline world:

- the baseline cache key includes the selected origin and destination;
- the cached baseline contains the selected trip result rather than a forkable regional world state;
- a scenario always runs from simulation second zero;
- scenario identity and scenario reuse do not exist;
- a second trip changes the baseline cache key and reruns the scenario;
- the 100-second checkpoints are run-local recovery artifacts, not canonical cross-run 15-minute world checkpoints;
- app shutdown requests cancellation of processes it owns, and startup does not reconcile or resume database rows left in active states.

The V2 seam is therefore real and specific: preserve the proven network, demand, checkpoint, mapping, rerouting, telemetry, and playback pieces while separating reusable regional state from closure state and selected-trip state.

## 1. Current run domains

### API and database contracts

The public request schema and SQLite constraint currently recognize only:

| Run kind | Purpose | Required inputs |
|---|---|---|
| `validation` | Tiny deterministic `tiny_closure` fixture | Fixture name and seed |
| `regional_comparison` | Physical regional baseline plus closure comparison | Timezone-aware departure, origin/destination app edges, origin/destination graph nodes, and at least one closure |

Evidence:

- `backend/app/schemas/simulation.py` defines both literals and validates all selected-trip and closure fields together.
- `backend/app/db/database.py` restricts `simulation_runs.run_kind` to those two values.
- `backend/app/workers/sumo_worker.py` dispatches `regional_comparison` to the regional worker and treats everything else as the toy validation run.
- `backend/tests/test_regional_simulation_schema.py` tests the combined regional contract; there are no tests for `regional_baseline`, `regional_scenario`, or `trip_probe` because those contracts do not exist yet.

### Frontend request path

`frontend/src/api/simulation.ts::startPhysicalSimulation()` always sends one `regional_comparison` request containing:

```text
seed
departure_time
origin_app_edge_id
destination_app_edge_id
origin_node_id
destination_node_id
warmup_minutes
analysis_minutes
physical scale
all directed closure sections and restrictions
```

`PhysicalSimulationPanel` cannot start a physical run without `origin`, `destination`, and `closure` props. Its progress copy describes a combined baseline-and-closure calculation. The result UI receives one summary with `baseline`, `scenario`, and `comparison` sections.

The panel currently initializes `runId` to a previously generated UUID rather than `null`. That behavior is outside this documentation-only chunk, but it is part of the current UI inventory and should be deliberately resolved in a later behavior-changing chunk.

### Request lifecycle

1. `POST /api/simulation/runs` validates the combined Pydantic request.
2. `SimulationRunService.create()` allows only one process tracked by the current app instance.
3. The service builds a worker request, creates `data/sumo/runs/<run-id>/`, writes `request.json`, and inserts a `simulation_runs` row.
4. A SHA-256 `run_key` is stored, but it is not unique and is not queried to deduplicate or reuse a run.
5. A `sumo_worker` subprocess dispatches to `regional_sumo_worker.run_regional()`.
6. The API polls the SQLite row plus `progress.json`.
7. The monitor reads `result.json` and writes the final summary/status to SQLite.
8. Completed scenario playback is returned from `playback.json`.

## 2. Current historical and traffic artifacts

### Live active versions

The following active local artifacts were observed on 2026-08-11:

| Artifact | Active version/status | Current role |
|---|---|---|
| App graph | `2026-08-08-portland-vancouver-frozen-v2` | OSM/NetworkX topology, trip free-flow floor, app edge identity |
| SUMO network | `pv-sumo-2026-08-08-v1`, SUMO `1.27.1` | Regional mesoscopic physical network |
| Traffic schedule | `pv-portal-24x7-2026-08-08-v4`, 15-minute resolution | Detector-derived time-of-day scaling input |
| Background seed | `regional-proxy-od-ipf-v2` | Gateway-aware proxy OD spatial pattern |
| Background-seed evidence | `modeled_uncalibrated`, `diagnostic_only` | Simulation input, not calibrated traffic truth |
| Frozen active SUMO model | Missing | `model_ready` cannot be true from an active model manifest |

The active schedule contains `mon_thu`, `friday`, `saturday`, and `sunday` day types. Weekday rows are labeled `observed_historical_input`; weekend rows are deliberately modeled as `modeled_unobserved`. This is current V1-era behavior. V2's Monday-through-Friday-only historical rule will require a later artifact/schema change; this inventory does not relabel or remove data.

### Historical profile products

There are two related but distinct historical paths today:

1. **Route reliability profiles**
   - PORTAL observations are pooled into September/October weekday morning or afternoon multiplier samples.
   - A multiplier is observed/free-flow slowdown, clipped to a bounded range.
   - Stored profile statistics include median, P85, P90, P95, sample count, source window, and matched-edge count.
   - The historical service samples these pooled route-wide multipliers and applies them to a selected route's free-flow time.

2. **The active 24/7 schedule**
   - Matched detector observations are compiled into 15-minute rows containing volume, speed, occupancy, quantiles, evidence, and quality flags.
   - The runtime interpolates between adjacent 15-minute buckets.
   - Monday through Thursday are currently collapsed to `mon_thu`; Friday is separate; modeled weekends are present.
   - The regional proxy OD compiler uses the schedule's detector-volume total to scale an AM/PM spatial pattern.

Historical input is therefore present, but it is not yet the V2 calibration artifact of `edge + direction + exact date + weekday + 15-minute bucket`, nor has current SUMO output passed a PORTAL-versus-SUMO promotion gate.

## 3. Current demand construction

### Background seed

The active background seed supplies gateway-aware proxy origin/destination pairs with AM and PM demand weights. Its own validation report calls it `diagnostic_only` and `modeled_uncalibrated`.

The seed retains:

- graph/model version;
- gateway and internal movement classes;
- AM and PM demand values;
- deterministic path/flow evidence;
- seed validation summaries and limitations.

It does not represent an observed regional household/job trip table.

### Runtime snapshot

For each `regional_comparison`, `compile_proxy_od_snapshot()`:

1. Starts at `departure_time - warmup_minutes`.
2. Interpolates one AM/PM spatial mix at that start time.
3. Reads the traffic schedule at that start time to calculate one regional scale factor.
4. Converts the resulting rates into trips across the entire warmup-plus-analysis duration.

The generated SUMO demand package then uses deterministic stochastic sampling and `duarouter` to assign routes. The request seed, network, schedule, background-seed inputs, time window, duration, and scale form a demand-cache key.

This means the schedule is 15-minute addressable, but the regional run currently uses a **single start-time snapshot spread over the full run**. Demand does not change to a new OD target every 15 minutes during one run.

### Current demand reuse

Demand packages under `data/sumo/demand/runtime-cache/` are reusable when their physical inputs match. The demand key intentionally does not contain closure edges or the selected trip, so this reuse is broader than the baseline-result cache.

The demand artifact is still a routed departure file, not a durable contract for exact future demand after a world checkpoint. V2 must preserve deterministic future departures when resuming or forking a world.

## 4. Current baseline and closure coupling

### Baseline pass

The regional worker runs a variant named `baseline` with `closures=[]`. It still:

- maps the selected origin and destination;
- finds the selected trip's SUMO route;
- inserts `selected-trip` after warmup;
- runs the full requested regional time window;
- collects selected-trip, aggregate edge, vehicle-count, teleport, and route-change results.

Baseline playback frames are intentionally not retained.

### Baseline-result cache

The current compressed baseline cache is useful but narrow. Its key includes:

```text
worker source hash
network and graph manifest hashes
SUMO binary identity
demand version and route hash
departure time
selected origin and destination app/SUMO edges
warmup and analysis duration
scale and seed
reroute, aggregate, and frame intervals
```

The cached value is a completed baseline result containing selected-trip and aggregate output. It is not a SUMO checkpoint that can be forked into a new closure or a new trip.

Consequences:

- Same closure, different trip: baseline cache miss and full scenario rerun.
- Different closure, identical trip and all other inputs: baseline result may be reused, but the scenario runs again from zero.
- Same trip and closure repeated: baseline may be reused, but there is no scenario cache or completed-run deduplication.
- Same weekday/time with a different origin or destination: demand may be reused, baseline result cannot.

### Closure pass

The `scenario` variant receives the same demand, scale, seed, and selected trip plus the closures. It always starts its own timeline at zero. Full/lane restrictions use a native SUMO rerouter; speed restrictions are synchronized in the worker and cause active vehicles to reroute when the state changes. Physical rerouting is configured at 60 seconds by default.

The closure contract supports physical `starts_at` and `ends_at`, but it has no separate `awareness_start`. There is no explicit planned-closure period in which route awareness can precede physical restriction.

Accepted app-edge-to-SUMO-edge mappings are required for origin, destination, and every closure edge. A rejected or mixed-status mapping blocks the run.

## 5. Current checkpoint and restart behavior

### 100-second compute checkpoints

The configured compute chunk is 100 simulation seconds. Each baseline and scenario variant runs as a sequence of child processes. At every committed chunk boundary the worker writes:

```text
sumo-state.xml.gz
python-state.json.gz
chunk-result.json.gz
checkpoint.json
```

SUMO state is saved with RNG state enabled. Python state retains selected-trip state, traveled path, edge aggregates, counts, restriction state, original lane/speed values, projected route, and remaining vehicle count. A partially written attempt is kept in a temporary directory and only atomically promoted after validation. One failed child is retried once from the preceding committed checkpoint.

`scripts/probe_sumo_100s_checkpoint.py` provides a focused SUMO-state equivalence probe, and the worker checks checkpoint time and required files while resuming chunks.

### What these checkpoints are not

Current checkpoints are nested under one run ID and one variant. They are optimized for child-process failure recovery. They are not:

- content-addressed world artifacts;
- promoted every 15 minutes for scenario forks;
- independently registered in SQLite;
- keyed to a baseline-world identity;
- guaranteed to include a versioned future-demand contract;
- reused by another closure or another trip request.

### App and worker durability

`SimulationRunService` owns subprocess handles only in memory. During FastAPI shutdown it writes `cancel.requested`, waits briefly, and terminates if necessary. On startup it does not scan active rows, reconnect to workers, or reconcile stale `running`/`cancel_requested` states. The local database currently contains such stale active-looking rows, which is consistent with the absence of startup reconciliation.

Therefore the chunk checkpoint mechanism is recovery **inside the current orchestrated run**, not yet the V2 guarantee that a multi-hour world survives an app/dev-server interruption.

## 6. Current telemetry and playback

### Result summary

The completed regional result contains:

- evidence level, seed, demand/model version, and physical scale;
- whether the trip-specific baseline cache hit;
- baseline and scenario selected-trip metrics;
- departed, arrived, remaining, and teleported vehicle counts;
- selected-route hashes and reroute flag;
- bounded edge changes comparing mean active vehicles and mean speed;
- selected-trip duration delta;
- NetworkX diagnostic floor and SUMO-native free-flow validation;
- assumptions and playback availability.

This is sufficient for an integrated comparison and basic diagnostics. Phase 2.1 now adds a separate **optional** native SUMO `edgeData` artifact at complete 900-second intervals, without replacing this summary. It preserves native edge IDs and reports entered/departed/left/arrived counts, entered-derived VPH, sampled vehicle-seconds, speed, SUMO's speed-based travel-time estimate, density, SUMO edge occupancy, waiting time, and time loss. The implementation composes checkpoint-local native fragments because the worker intentionally exits libsumo every 100 seconds. See `docs/v2/SUMO_NATIVE_EDGE_TELEMETRY.md`.

The Phase 2.1 artifact remains `modeled_uncalibrated`. It is not yet joined to app edges or compared with PORTAL; those remain Phase 2.2 work.

### Playback

Only scenario frames are recorded. They are buffered into bounded gzip chunks and streamed into the legacy `playback.json` response. Frames include sampled background agents plus the selected trip's current coordinate, traveled route, projected route, and selected-route edge IDs.

The baseline has result metrics but no equivalent playable frame series. There is no independently playable baseline world and no baseline-versus-scenario world scrubber.

## 7. Current reliability and P95 path

The reliability API is separate from the SUMO physical comparison API.

### Without a selected profile

- A deterministic seed is derived from the route and request.
- Route-wide lognormal multipliers are generated around an uncalibrated structural assumption.
- Samples are floored at free flow.
- Evidence is `modeled_uncalibrated`.

### With a selected historical profile

- Stored morning/afternoon multiplier samples are resampled with replacement.
- The chosen multiplier applies to the selected route's complete free-flow duration.
- Median/P85/P90/P95, on-time probability, safe departure, and early-departure benefits are calculated from that array.
- Current code labels this response `historically_calibrated` when a stored profile is selected.

The current pooled PM distribution produced the useful approximate P95 slowdown clue called out by the V2 plan. The current path does not preserve which edge, direction, exact date, weekday, or 15-minute bucket produced an individual tail sample. It also does not backtest whether reported P85/P90/P95 achieve their claimed empirical coverage.

Under the V2 evidence contract, importing or selecting historical input must not by itself promote a model to `historically_calibrated`. That behavior is recorded here for Phase 1/2 correction; it is not changed in Phase 0.1.

## 8. Current evidence labels

| Surface/artifact | Current label | Meaning in current implementation |
|---|---|---|
| NetworkX free-flow route | `free_flow` | Static graph travel time |
| Lane/speed/diversion structural result | `modeled_uncalibrated` | Modeled cost or flow without validation proof |
| SUMO regional comparison | `modeled_uncalibrated` | Integrated physical simulation, not traffic-accuracy proof |
| Background proxy OD | `modeled_uncalibrated` / `diagnostic_only` | Detector-constrained proxy, not observed OD |
| Schedule weekday rows | `observed_historical_input` | Historical input row, not a calibrated SUMO result |
| Schedule weekend rows | `modeled_unobserved` | Fabricated fallback, prohibited as historical V2 evidence |
| Reliability with imported profile | `historically_calibrated` | Current label; stronger than V2 promotion rules permit without held-out validation |

The missing active SUMO model manifest is another explicit signal that the integrated SUMO path is not a promoted calibrated model bundle.

## 9. Present computational reuse map

```text
PORTAL-derived schedule + proxy seed + network
                     |
                     v
        demand cache (trip/closure independent)
                     |
       +-------------+----------------+
       |                              |
       v                              v
Trip A + physical config       Trip B + physical config
       |                              |
       v                              v
trip-specific baseline A       trip-specific baseline B
       |                              |
       v                              v
closure scenario from 0        closure scenario from 0
```

The existing cache reduces some duplicated work, but reuse stops at the selected-trip boundary.

## 10. Required V2 computational hierarchy

The target seam identified by this inventory is:

```text
historical calibration artifacts
              |
              v
reusable baseline world (no trip, no closure)
              |
     +--------+---------+
     |                  |
     v                  v
scenario world A   scenario world B
     |                  |
  +--+--+             +-+-+
  v  v  v             v   v
probes                probes
```

Cost should be amortized down the hierarchy:

```text
calibration and baseline build  -> expensive, infrequent
scenario fork and stabilization -> smaller, reusable per scenario
trip probe                       -> comparatively cheap, repeatable
```

The following would violate V2 reuse even if they produced correct-looking output:

- a new trip reruns a regional baseline;
- a new trip reruns an existing scenario world;
- a new destination rebuilds regional demand;
- a new closure replays the parent weekday from midnight when a valid earlier checkpoint exists;
- a probe mutates its canonical parent world;
- a cache key omits any physical input that changes the resulting world.

These are inventory conclusions for the Phase 0.2 contract. They do not alter runtime behavior in Phase 0.1.

## 11. Reusable pieces to preserve

V2 should preserve or evolve, rather than casually replace:

- checksum-bound graph, network, schedule, seed, demand, and edge-map artifacts;
- the accepted directed app-edge-to-SUMO-edge mapping gate;
- deterministic seed lineage and approximate 1:1 physical semantics;
- separate SUMO child processes rather than libsumo inside FastAPI;
- 60-second physical rerouting;
- the 100-second atomic compute/retry checkpoint layer;
- SUMO RNG-state persistence;
- bounded playback chunking;
- explicit cancellation and safe local paths;
- free-flow floor diagnostics;
- visible `modeled_uncalibrated` limitations;
- demand conservation and deterministic sampling checks.

## 12. Gaps mapped to later V2 phases

| Current gap | First owning V2 phase |
|---|---|
| No complete current-system developer inventory | 0.1 — this document |
| Reuse invariants are not in the implementation contract | 0.2 |
| No date-level and weekday-specific calibration-v2 artifact | 1.1–1.3 |
| P95 tail loses edge/time provenance | 1.4 |
| No native 15-minute SUMO calibration telemetry/comparator | 2.1–2.2 |
| No promotion lifecycle for model worlds | 2.3 |
| One start-time demand snapshot spans the run | 3.1–3.3 |
| One routed path per major proxy OD movement | 3.4 |
| No `regional_baseline`/`regional_scenario`/`trip_probe` contracts | 4.1 |
| Baseline contains the selected trip | 4.2 |
| No canonical 15-minute fork checkpoints | 4.3–4.4 |
| Closure scenario replays from zero and has no reusable identity | 5.1–5.2 |
| No separate awareness time | 5.3 |
| No bounded scenario stabilization/convergence report | 5.5 |
| No immutable selected-trip probe against saved worlds | 6.1 |
| No arrival-time-aware predictive edge evaluation | 6.2 |
| P95 samples do not preserve whole-day correlation or coverage proof | 7.1–7.2 |
| No baseline/scenario/probe world UI hierarchy | 9.1–9.3 |
| App restart cancels or strands active work | 10.1 |
| No content-addressed baseline -> scenario -> probe DAG | 10.2 |
| Run/checkpoint artifact retention is not classified | 10.3 |

## 13. Phase 0.1 acceptance checklist

- [x] Current run kinds are identified.
- [x] Frontend-to-worker request flow is traced.
- [x] Traffic profile and active schedule artifacts are distinguished.
- [x] Active graph, SUMO network, schedule, and background-seed versions are recorded.
- [x] Background seed and runtime demand construction are traced.
- [x] Selected-trip and closure coupling are identified.
- [x] Existing demand and baseline-result reuse are documented without overstating them.
- [x] 100-second checkpoint and playback formats are documented.
- [x] Reliability/P95 calculation and evidence-label paths are documented.
- [x] V2 reuse boundaries and later owning phases are identified.
- [x] No runtime code or generated data was changed.

Tyler's Phase 0.1 gate remains:

```bash
git diff --check
npm run test:backend
```
