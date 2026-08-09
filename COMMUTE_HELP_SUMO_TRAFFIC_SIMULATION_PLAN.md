# Commute Help - Local Traffic Simulation and SUMO Integration Plan

## Implementation status — 2026-08-08

- [x] SUMO Phase 0 — pinned local runtime, doctor command, and readiness API.
- [x] SUMO Phase 1 — frozen OSM source, same-source NetworkX/SUMO rebuild,
  conservative directed edge bridge, graph-state migration audit, rebuilt traffic
  artifacts, topology gate, recoverable promotion, and active SUMO network.
- [x] SUMO Phase 2 — isolated deterministic worker, SQLite run records, progress,
  create/read/cancel API, TripInfo parsing, closure reroute proof, reproducibility,
  cancellation, and orphan-process check using the tiny committed fixture.
- [ ] SUMO Phase 3 — build and validate the default 24/7 traffic schedule.

The active physical network is still **uncalibrated**. Completing Phases 0–2
proves the runtime and orchestration, not Portland traffic accuracy.

> Repository: `Atlessc/commute-help`  
> Reviewed branch: `master`  
> Plan date: 2026-08-08  
> Purpose: Expand Commute Help from route-level traffic estimation and diversion modeling into a calibrated, local-only physical traffic simulation system without discarding the working routing, PORTAL, scenario, or playback architecture.

---

## 0. Executive decision

Commute Help should **not** replace its existing OSMnx + NetworkX routing engine with SUMO.

Instead, the application should become a **dual-engine system**:

1. **OSMnx + NetworkX remains the fast planning engine**
   - map-click snapping
   - origin/destination routing
   - closure selection
   - baseline and alternative route generation
   - immediate UI feedback
   - candidate corridor discovery
   - affected-area discovery
   - Google Maps handoff

2. **SUMO becomes the physical traffic simulation engine**
   - regional traffic assignment
   - vehicle queues
   - lane-level effects
   - merges
   - signalized intersections
   - capacity loss
   - spillback
   - dynamic rerouting
   - closure propagation
   - calibration against historical observations
   - validation of route and travel-time effects

3. **FastAPI remains the orchestration boundary**
   - validates simulation requests
   - owns model/version metadata
   - starts and monitors simulation workers
   - persists run state
   - exposes results to the frontend
   - never runs a long SUMO loop directly inside a request handler

4. **SQLite remains the authoritative metadata database**
   - scenarios
   - model bundle versions
   - simulation runs
   - calibration campaigns
   - benchmark trips
   - experiment results
   - cache records

5. **Generated SUMO artifacts remain local files**
   - networks
   - demand files
   - state checkpoints
   - trip output
   - queue output
   - edge aggregates
   - playback frames
   - calibration reports
   - model manifests

The result is still fully local and cannot create a cloud bill.

Six additional rules are non-negotiable:

1. Both the app graph and SUMO network must be rebuilt from one frozen, hashed
   local OSM extract. A newer SUMO network must not be paired with the current
   Overpass-built app graph merely because spatial matching appears possible.
2. Every closure run must first model the closure at the regional level. The
   microscopic affected area receives scenario boundary demand, not normal-day
   boundary demand.
3. An established closure and a closure that activates during the simulated
   window are different experiments and must use different warm-up behavior.
4. Microscopic vehicle scaling may not be used unless queue, density,
   throughput, and travel-time equivalence have been validated. The default is
   one SUMO vehicle per modeled vehicle.
5. Final route recommendations must use the composed regional and microscopic
   scenario costs. NetworkX remains the fast planner, not an excuse to return a
   free-flow route after physical simulation.
6. One deterministic run is one reproducible realization, not a reliability
   distribution. Percentiles and on-time probability require a bounded
   ensemble.

---

# 1. What already exists in `commute-help`

This plan is written for the repository that exists now, not for a blank project.

The repository already has:

```text
commute-help/
├── AGENTS.md
├── GAMEPLAN.md
├── README.md
├── package.json
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── schemas/
│   │   ├── services/
│   │   └── main.py
│   ├── tests/
│   └── requirements.txt
├── data/
│   ├── graphs/
│   ├── presets/
│   └── regions/
├── docs/
├── frontend/
│   └── src/
│       ├── api/
│       ├── features/
│       │   ├── closures/
│       │   ├── diversion/
│       │   ├── locations/
│       │   ├── map/
│       │   ├── results/
│       │   ├── scenarios/
│       │   ├── simulation/
│       │   └── traffic/
│       └── stores/
└── scripts/
```

Important existing systems that should be preserved:

- `GraphService`
- `SpatialService`
- `RoutingService`
- `ScenarioService`
- `TrafficService`
- `DiversionService`
- PORTAL station matching
- PORTAL 15-minute background profiles
- edge-prior generation
- background-demand seed experiments
- regional OD intake validation
- existing SQLite persistence
- frontend diversion map
- frontend simulation playback controls

## Current limitation that matters

The current frontend "simulation" is a **visual playback model**, not a physical traffic simulator.

`frontend/src/features/simulation/simulationPlayback.ts` currently:

- derives visible agents from diversion edge flows;
- uses deterministic pseudo-random offsets;
- moves points along edge geometry;
- interpolates between baseline and scenario flow;
- caps visible agents at 900;
- does not represent actual SUMO vehicles, queues, signal interactions, or lane changes.

That code is useful and should not simply be deleted.

The long-term change is:

> Keep the playback UI and MapLibre integration, but replace the synthetic playback data source with sampled SUMO output.

---

# 2. Revised architecture

```text
                         COMMUTE HELP
                              |
          +-------------------+-------------------+
          |                                       |
          v                                       v
 Fast planning path                       Physical simulation path
          |                                       |
 OSMnx + NetworkX                         SUMO / libsumo
          |                                       |
 - snapping                               - mesoscopic regional model
 - route candidates                       - microscopic closure area
 - closure edge IDs                       - queues / lanes / signals
 - affected corridors                     - rerouting / spillback
 - fast route comparison                  - calibration / validation
          |                                       |
          +-------------------+-------------------+
                              |
                              v
                        FastAPI services
                              |
             +----------------+----------------+
             |                                 |
             v                                 v
          SQLite                         Local artifacts
             |                                 |
 - run metadata                    - .net.xml
 - scenarios                       - .rou.xml
 - campaigns                       - .xml.gz outputs
 - benchmarks                      - Parquet summaries
 - model versions                  - state checkpoints
 - cache keys                      - manifests
             |                                 |
             +----------------+----------------+
                              |
                              v
                     React + MapLibre UI
```

## Hard architecture rule

**SUMO must run outside the FastAPI process.**

Do not do this:

```python
@router.post("/simulation")
def simulation(...):
    while libsumo.simulation.getMinExpectedNumber() > 0:
        libsumo.simulationStep()
```

That would:

- block server work;
- make cancellation ugly;
- make crashes more damaging;
- complicate cleanup;
- create process-global libsumo state issues;
- make test isolation harder.

Instead:

```text
FastAPI
  |
  | create run
  v
SQLite simulation_runs
  |
  | spawn
  v
separate Python worker process
  |
  v
SUMO / libsumo
  |
  +--> incremental run files
  +--> progress/status
  +--> final summary
```

One worker process at a time should be the default.

---

# 3. The two-level simulation model

A single laptop should not repeatedly run the entire Portland-Vancouver road network in full lane-level microscopic mode.

Use two connected levels.

## 3.1 Regional layer

Purpose:

- represent the entire useful Portland-Vancouver regional network;
- establish normal origin-destination demand;
- estimate bridge and freeway flows;
- model larger rerouting patterns;
- generate boundary conditions for detailed simulations;
- provide time-dependent edge travel costs.

Recommended mode:

```text
SUMO mesoscopic simulation
```

The regional layer should normally use:

- the full simulation network;
- real or accepted regional OD demand;
- PORTAL-calibrated time-of-day demand;
- broader vehicle-flow sampling;
- aggregated edge output;
- no browser-level vehicle playback unless debugging.

For every physical closure request, distinguish two regional results:

```text
normal regional state
closure regional state
regional delta
```

The closure regional run must include the selected closure schedule and the
currently approved rerouting behavior. It determines strategic changes such as
bridge choice, freeway approach, and long-distance corridor diversion before
the detailed subnetwork is created.

### Regional outputs

At minimum:

```text
edge_id
time_bucket
entered
left
flow_vph
mean_speed
travel_time
occupancy_or_density_if_available
queue_proxy
```

Also derive:

```text
boundary edge inflows
boundary edge outflows
regional route shares
time-dependent travel weights
```

Boundary output must preserve trip intent. The minimum coupling key is:

```text
entry SUMO edge
exit SUMO edge or destination zone
departure bucket
vehicle class
route-choice group
flow
```

Also preserve initial queues and vehicles already present in the affected area.
An entry-edge total without an exit or destination is not a sufficient
microscopic boundary condition.

---

## 3.2 Detailed closure layer

Purpose:

- represent the actual physical consequences near a closure;
- model queues and spillback;
- model lane reductions;
- model intersection and signal effects;
- model merges;
- model detailed rerouting near the disrupted area;
- simulate the selected user trip.

Recommended mode:

```text
SUMO microscopic simulation
```

The detailed region must be **generated per scenario**.

It must not contain hardcoded assumptions such as:

```text
Rose Quarter
I-5 Interstate Bridge
one fixed commute
one fixed closure polygon
```

### Inputs to affected-area discovery

Start with:

1. closed edges;
2. the baseline route;
3. closure-aware route candidates;
4. major alternative corridors;
5. origin/destination if nearby;
6. likely queue-backup corridors;
7. regional paths whose normal routes cross the closure.

Then create an initial geographic/network buffer.

### Expansion rule

After the first detailed run, inspect the boundary.

Expand and rerun when any of these occur near the boundary:

- material queue reaches boundary edges;
- congested speed reaches the boundary;
- a meaningful number of rerouted vehicles exit and immediately re-enter;
- diverted paths are cut off by the detailed network edge;
- boundary inflow exceeds expected regional capacity;
- selected-trip route behavior depends on omitted roads.

Example bounded policy:

```text
initial corridor buffer:       2.0 km
expansion increment:           1.5 km
maximum expansions:            3
maximum detailed area:         configurable
maximum detailed edges:        configurable
```

These values are starting configuration, not truths.

### Important implementation clarification

Do **not** begin by trying to run one magical network in mixed mesoscopic and microscopic modes simultaneously.

The first robust implementation should use:

```text
normal and closure regional mesoscopic runs
        |
        v
scenario-specific entry-to-exit boundary demand and initial state
        |
        v
separate microscopic subnetwork run
```

After the microscopic run, compose its detailed costs back into the regional
cost surface and re-evaluate route candidates. Repeat only while the selected
route, material corridor flows, or affected-area boundary conditions continue
to change beyond configured tolerances. Bound the iterations and report a
non-converged result honestly.

That is easier to validate and easier to reason about than pretending the two
layers are automatically coupled.

---

# 4. Network strategy

## 4.1 Do not convert the existing GraphML into SUMO

The current GraphML is ideal for NetworkX routing, but SUMO needs information that a routing graph may simplify away:

- lane structure;
- junction geometry;
- turn connections;
- traffic light logic;
- internal junction lanes;
- lane permissions;
- lane-to-lane connectivity.

Build both networks from the **same underlying OSM source**, not one from the other.

Target:

```text
same local OSM extract
      |
      +--> OSMnx / NetworkX graph
      |
      +--> SUMO netconvert network
```

This gives both engines a common source lineage.

### Current repository migration requirement

The current authoritative graph manifest identifies its source as an OSMnx
Overpass download. The repository does not currently retain one frozen OSM XML
or PBF artifact from which both engines were built.

Before SUMO edge mapping begins:

1. obtain and freeze one bounded local OSM extract;
2. record source date, bounds, license, size, and SHA-256;
3. rebuild the app graph from that extract;
4. build SUMO from that exact extract;
5. assign new graph and SUMO network versions;
6. rematch saved closures using the existing graph-version review rules;
7. rebuild or revalidate PORTAL station matches, edge priors, and other
   graph-derived artifacts; and
8. retain a migration report showing accepted, review, and failed mappings.

Do not treat OSMnx cache files as an adequate long-term source contract unless
the builder first proves that they contain the complete, reproducible input
needed by both pipelines.

---

## 4.2 SUMO network build

Add:

```text
scripts/build_sumo_network.py
```

Its responsibilities:

1. locate the approved frozen local OSM source and verify its checksum;
2. reject URL inputs;
3. call `netconvert`;
4. preserve original OSM identity where possible;
5. write a versioned SUMO network;
6. generate an app-edge to SUMO-edge mapping;
7. validate topology;
8. write a manifest;
9. never modify the NetworkX graph; and
10. refuse to combine artifacts whose source-extract identities differ.

Recommended `netconvert` starting options should be reviewed against the current SUMO release, but the build should include equivalents of:

```text
--geometry.remove
--ramps.guess
--junctions.join
--tls.guess-signals
--tls.discard-simple
--tls.join
--tls.default-type actuated
--output.original-names
```

Do not blindly assume generated signals are correct. Signal accuracy is a later validation layer.

---

# 5. The critical cross-engine edge map

Commute Help scenarios currently identify roads using app graph edges.

SUMO uses its own edge and lane IDs.

You need a versioned bridge between them.

Create:

```text
data/sumo/networks/<sumo-network-version>/edge-map.parquet
```

Suggested schema:

```text
graph_version
sumo_network_version

app_edge_id
app_u
app_v
app_key
osm_way_id
direction_signature

sumo_edge_id
sumo_lane_ids

road_name
road_class

app_geometry_wkb
sumo_geometry_wkb

match_method
match_score
match_distance_m
direction_error_degrees

status
review_reason
```

`status`:

```text
accepted
review
unmatched
```

## Matching priority

Use strongest evidence first:

1. preserved original OSM ID;
2. direction;
3. overlapping geometry;
4. road reference/name;
5. endpoint proximity;
6. topology.

Never map by nearest line alone.

## Closure rule

A user-selected closure may be sent to SUMO **only when every required affected app edge has an accepted mapping**.

If mapping is ambiguous:

```text
simulation blocked
```

Do not silently close the neighboring road because it "looked close enough."

---

# 6. Proposed local artifact layout

Add this generated-data convention:

```text
data/
├── osm/
│   └── sources/
│       └── <osm-source-version>/
│           ├── region.osm.xml-or-pbf
│           └── source-manifest.json
│
├── sumo/
│   ├── networks/
│   │   └── <sumo-network-version>/
│   │       ├── metro.net.xml
│   │       ├── netconvert.cfg
│   │       ├── edge-map.parquet
│   │       ├── edge-map-review.csv
│   │       ├── network-manifest.json
│   │       ├── validation-report.json
│   │       └── validation-report.md
│   │
│   ├── demand/
│   │   └── <demand-version>/
│   │       ├── od-demand.parquet
│   │       ├── zone-connectors.parquet
│   │       ├── regional.rou.xml.gz
│   │       ├── vehicle-types.add.xml
│   │       └── demand-manifest.json
│   │
│   ├── traffic-controls/
│   │   └── <traffic-control-version>/
│   │       ├── signal-programs.add.xml
│   │       ├── approach-controls.parquet
│   │       └── traffic-control-manifest.json
│   │
│   ├── baselines/
│   │   └── <model-component-version>/
│   │       ├── states/
│   │       │   ├── mon-thu-0715.xml.gz
│   │       │   └── ...
│   │       ├── edge-weights.parquet
│   │       ├── boundary-flows.parquet
│   │       └── baseline-manifest.json
│   │
│   ├── runs/
│   │   └── <run-id>/
│   │       ├── request.json
│   │       ├── manifest.json
│   │       ├── progress.json
│   │       ├── stdout.log
│   │       ├── stderr.log
│   │       ├── tripinfo.xml.gz
│   │       ├── queue.xml.gz
│   │       ├── edge-data.xml.gz
│   │       ├── summary.json
│   │       ├── validation.json
│   │       └── playback.parquet
│   │
│   ├── ensembles/
│   │   └── <ensemble-id>/
│   │       ├── ensemble-manifest.json
│   │       ├── member-runs.json
│   │       └── summary.json
│   │
│   └── models/
│       └── <model-version>/
│           ├── model-manifest.json
│           ├── parameter-set.json
│           ├── validation-report.json
│           ├── validation-report.md
│           └── checksums.json
│
└── benchmarks/
    └── private/
        └── ...
```

All generated SUMO networks, demand files, runs, private benchmarks, and large outputs should be ignored by Git.

Frozen real OSM extracts are also generated/local data and must remain ignored.
Their small source manifests may be committed only when they contain no private
paths and their license/redistribution terms permit it.

Tiny synthetic test fixtures may live under:

```text
backend/tests/fixtures/sumo/
```

and may be committed.

---

# 7. Repository file-by-file implementation map

This is the important "where does this code belong?" section.

---

## 7.1 Root files

### MODIFY: `GAMEPLAN.md`

Current status treats full SUMO microscopic simulation as deferred.

Change the plan so it says:

- NetworkX remains the fast routing engine.
- SUMO is now an active physical-simulation subsystem.
- regional simulation is mesoscopic;
- closure-detail simulation is microscopic;
- calibration must precede predictive claims;
- SUMO results do not automatically become production results until validation gates pass.

Do not delete the existing product requirements.

Add a new section such as:

```markdown
## Physical traffic simulation layer

Commute Help uses a dual-engine architecture...
```

---

### MODIFY: `AGENTS.md`

Add explicit coding-agent rules:

```text
- Never run SUMO inside a FastAPI request process.
- Long simulations must use the worker boundary.
- One local simulation process by default.
- Simulation inputs must be local files.
- Never accept a URL as a SUMO model input.
- Every run must have a deterministic seed.
- Every run must record the exact SUMO version.
- Never call a run historically calibrated unless its model bundle passed validation.
- Never apply a closure to SUMO without an accepted app-edge/SUMO-edge mapping.
- Preserve the existing NetworkX router.
- Never use production regional data in committed tests.
- Use tiny synthetic SUMO fixtures for automated tests.
- Calibration campaigns must be resumable from completed experiment records.
```

### Existing repo caveat

`AGENTS.md` is currently ignored by the repository `.gitignore`.

If the intent is for GitHub/Codex to receive future updates to this file, decide whether to remove `AGENTS.md` from `.gitignore`.

Do not change that behavior accidentally.

---

### MODIFY: `.gitignore`

Add explicit SUMO and benchmark rules:

```gitignore
data/sumo/
data/benchmarks/private/

*.net.xml
*.rou.xml
*.sumocfg
*.state.xml
*.state.xml.gz

tripinfo*.xml
tripinfo*.xml.gz
queue*.xml
queue*.xml.gz
edgedata*.xml
edgedata*.xml.gz
```

Keep synthetic fixtures under `backend/tests/fixtures/sumo/` committed by adding exceptions if necessary.

---

### MODIFY: `package.json`

Add orchestration commands.

Suggested names:

```json
{
  "sumo:doctor": ".venv/bin/python -m scripts.sumo_doctor",
  "sumo:build-network": ".venv/bin/python -m scripts.build_sumo_network",
  "sumo:validate-network": ".venv/bin/python -m scripts.validate_sumo_network",
  "sumo:build-demand": ".venv/bin/python -m scripts.build_sumo_demand",
  "sumo:build-baseline": ".venv/bin/python -m scripts.build_sumo_baseline",
  "sumo:calibrate": ".venv/bin/python -m scripts.calibrate_sumo",
  "sumo:validate-model": ".venv/bin/python -m scripts.validate_sumo_model",
  "sumo:bundle": ".venv/bin/python -m scripts.bundle_sumo_model"
}
```

Do not add all scripts as empty placeholders in one commit.

Add each command when its implementation slice exists.

---

## 7.2 Backend dependencies

### MODIFY: `backend/requirements.txt`

The backend currently contains the analysis stack but no SUMO dependency.

Preferred installation strategy:

1. Pin the tested SUMO release.
2. Use the official Python-distributed SUMO package where practical on macOS.
3. Verify `sumo`, `netconvert`, `sumolib`, and `libsumo` availability in the actual Python 3.12 environment.
4. Keep a subprocess fallback for environments where `libsumo` cannot be loaded cleanly.

At the time this plan was written, the current official SUMO release is in the 1.27.x line. Before pinning forever, verify the package available to the project environment and then lock the version that passes the project tests.

Example target after verification:

```text
eclipse-sumo==1.27.1
```

Optionally:

```text
libsumo==1.27.1
```

only if it installs and imports cleanly in the project's Python 3.12 virtual environment.

### Why not blindly add everything?

The application should own an abstraction such as:

```python
class SumoRuntime:
    ...
```

rather than making the rest of the application depend directly on whether the local machine uses:

```text
libsumo
```

or:

```text
sumo subprocess
```

---

# 8. New backend module structure

Create a dedicated SUMO package.

```text
backend/app/services/sumo/
├── __init__.py
├── environment.py
├── network_service.py
├── edge_mapping_service.py
├── demand_service.py
├── affected_area_service.py
├── run_service.py
├── output_service.py
├── calibration_service.py
├── scoring_service.py
├── validation_service.py
└── model_bundle_service.py
```

Do not create all of these as empty architecture theater.

Create them as their phases become real.

---

## 8.1 NEW: `backend/app/services/sumo/environment.py`

Own:

- SUMO binary discovery;
- SUMO version;
- `netconvert` discovery;
- libsumo import capability;
- runtime mode;
- local-only environment validation;
- approved data roots.

Example responsibility:

```python
@dataclass(frozen=True)
class SumoEnvironment:
    version: str
    sumo_binary: Path
    netconvert_binary: Path
    libsumo_available: bool
    runtime_mode: Literal["libsumo", "subprocess"]
```

No simulation business logic belongs here.

---

## 8.2 NEW: `backend/app/services/sumo/network_service.py`

Own:

- SUMO network manifest loading;
- network version compatibility;
- network path resolution;
- subnet extraction/build coordination;
- validation metadata.

It should **not** duplicate `GraphService`.

`GraphService` owns the app road graph.

`SumoNetworkService` owns the SUMO representation of the same source network.

---

## 8.3 NEW: `backend/app/services/sumo/edge_mapping_service.py`

Own:

- app-edge to SUMO-edge lookup;
- map version checks;
- closure mapping;
- ambiguity handling;
- coverage reports.

Primary method shape:

```python
def map_app_edges(
    self,
    graph_version: str,
    sumo_network_version: str,
    app_edge_ids: list[str],
) -> list[MappedSumoEdge]:
    ...
```

Raise a typed error if any requested closure edge is not safely mapped.

---

## 8.4 NEW: `backend/app/services/sumo/demand_service.py`

Own:

- accepted regional OD artifacts;
- zone-to-network connectors;
- conversion from OD demand into SUMO trips/flows;
- time-bucket departure generation;
- vehicle-class composition;
- demand scaling;
- manifest versioning.

Do not put OD quarantine logic here.

The existing `regional_od_intake_service.py` remains the intake/provenance gate.

The chain becomes:

```text
agency delivery
    |
regional_od_intake_service.py
    |
accepted canonical OD Parquet
    |
sumo/demand_service.py
    |
SUMO demand artifact
```

---

## 8.5 NEW: `backend/app/services/sumo/affected_area_service.py`

This is what makes the system generic for arbitrary closures.

Inputs:

```text
closure edge IDs
baseline route
alternative routes
regional route flows
selected user trip
previous boundary spillover report, if rerun
```

Outputs:

```text
selected app edge set
selected SUMO edge set
polygon/bounds
boundary entry edges
boundary exit edges
expansion generation
reason for every included corridor
```

Suggested result model:

```python
class AffectedArea(BaseModel):
    generation: int
    app_edge_ids: list[str]
    sumo_edge_ids: list[str]
    entry_edge_ids: list[str]
    exit_edge_ids: list[str]
    bounds: tuple[float, float, float, float]
    selection_reasons: dict[str, list[str]]
```

This service should use the existing graph/spatial/routing services instead of reimplementing graph traversal.

---

## 8.6 NEW: `backend/app/services/sumo/run_service.py`

This is the FastAPI-side simulation orchestrator.

Own:

- request validation;
- model bundle selection;
- run-key generation;
- cache lookup;
- run row creation;
- worker launch;
- worker cancellation;
- progress reading;
- status transitions;
- final artifact registration.

It should **not** call `simulationStep()` itself.

---

## 8.7 NEW: `backend/app/services/sumo/output_service.py`

Own:

- SUMO XML parsing;
- conversion to Parquet/JSON;
- TripInfo parsing;
- queue parsing;
- edge aggregation parsing;
- selected-trip extraction;
- playback sample extraction;
- summary generation.

This keeps SUMO XML-specific logic away from HTTP routes and scoring logic.

---

## 8.8 NEW: `backend/app/services/sumo/scoring_service.py`

Own the calibration loss function.

Do not bury calibration metrics inside `calibration_service.py`.

It should accept normalized simulated and observed values and return something like:

```python
class SimulationScore(BaseModel):
    total_loss: float
    counts_loss: float
    speeds_loss: float
    trip_times_loss: float
    queues_loss: float
    route_shares_loss: float
    invalid_penalty: float
    hard_failures: list[str]
```

---

## 8.9 NEW: `backend/app/services/sumo/calibration_service.py`

Own:

- candidate parameter generation;
- experiment creation;
- experiment scheduling;
- cache reuse;
- campaign resume;
- best-candidate selection;
- train/validation comparison.

Do not let it own:

- SUMO process management;
- raw output parsing;
- score definitions;
- SQLite connection internals.

---

## 8.10 NEW: `backend/app/services/sumo/validation_service.py`

Own hard physical and statistical gates.

Examples:

```text
closed-edge crossing
teleport count
unfinished trip rate
vehicle conservation
demand insertion failure
travel-time floor
queue boundary spillover
mapping completeness
held-out error
```

Validation should produce both:

```text
machine-readable JSON
human-readable Markdown
```

---

## 8.11 NEW: `backend/app/services/sumo/model_bundle_service.py`

Own the frozen model contract.

A model bundle identifies:

```text
SUMO version
SUMO network version
app graph version
OD demand version
traffic-profile version
traffic-control version
parameter-set version
driver-distribution version
validation report
random-seed policy
source provenance
checksums
```

No run should simply use "whatever files happen to be newest."

---

# 9. Worker boundary

Create:

```text
backend/app/workers/
├── __init__.py
└── sumo_worker.py
```

## Worker input

The worker receives one local request manifest path:

```bash
.venv/bin/python -m backend.app.workers.sumo_worker \
  --request data/sumo/runs/<run-id>/request.json
```

Do not pass a huge simulation contract as command-line arguments.

## Worker responsibilities

1. validate the request file;
2. validate checksums/version compatibility;
3. open only local files;
4. initialize SUMO;
5. apply deterministic seed;
6. load baseline state if requested;
7. load future demand routes;
8. run warmup;
9. activate closure at correct simulation time;
10. collect progress;
11. emit selected playback samples;
12. finish or cancel cleanly;
13. write output files;
14. write final worker result atomically;
15. return an exit code.

## Cancellation

Recommended model:

```text
FastAPI sets cancel_requested in SQLite
        |
run service notices / worker polls small local flag
        |
worker exits at safe simulation step
        |
if hung, parent terminates process after grace period
```

A calibration campaign is resumable.

An individual run does not need complicated mid-run resume in phase one. It may restart from the nearest approved baseline checkpoint.

---

# 10. Settings changes

### MODIFY: `backend/app/core/settings.py`

Add paths and hard resource limits.

Suggested fields:

```python
sumo_path: Path = Path("data/sumo")
sumo_networks_path: Path = Path("data/sumo/networks")
sumo_demand_path: Path = Path("data/sumo/demand")
sumo_baselines_path: Path = Path("data/sumo/baselines")
sumo_runs_path: Path = Path("data/sumo/runs")
sumo_models_path: Path = Path("data/sumo/models")

sumo_runtime_mode: Literal["auto", "libsumo", "subprocess"] = "auto"
sumo_offline_only: bool = True

sumo_max_parallel_runs: int = 1
sumo_max_run_seconds: int = 3600
sumo_max_run_disk_mb: int = 2048
sumo_max_calibration_experiments: int = 100

sumo_max_area_expansions: int = 3
sumo_max_detailed_edges: int = 50_000
sumo_max_coupling_iterations: int = 3

sumo_default_warmup_minutes: int = 45
sumo_default_analysis_minutes: int = 90
sumo_default_ensemble_runs: int = 12
sumo_max_ensemble_runs: int = 50

sumo_micro_real_vehicles_per_sim_vehicle: float = 1.0

sumo_playback_max_visible_vehicles: int = 900
```

The numeric defaults should be measured and adjusted on the target Mac.

Never use an environment variable containing an API key for this subsystem.

---

# 11. SQLite changes

### MODIFY: `backend/app/db/database.py`

Do not suddenly rewrite the project around an ORM migration framework.

The existing database manager already owns explicit SQLite initialization.

Continue that pattern until the project deliberately adopts migrations.

Increment the application schema version and add tables in a backwards-compatible way.

---

## 11.1 `simulation_runs`

Suggested schema:

```sql
CREATE TABLE IF NOT EXISTS simulation_runs (
    id TEXT PRIMARY KEY,

    scenario_id TEXT,
    scenario_revision INTEGER,

    run_kind TEXT NOT NULL
        CHECK (run_kind IN (
            'regional_baseline',
            'regional_closure',
            'closure_detailed',
            'calibration',
            'validation'
        )),

    status TEXT NOT NULL
        CHECK (status IN (
            'queued',
            'running',
            'completed',
            'invalid',
            'failed',
            'cancel_requested',
            'cancelled'
        )),

    run_key TEXT NOT NULL,
    seed INTEGER NOT NULL,

    parent_run_id TEXT,
    ensemble_id TEXT,
    ensemble_member_index INTEGER,
    representative_member INTEGER NOT NULL DEFAULT 0,

    graph_version TEXT NOT NULL,
    sumo_version TEXT NOT NULL,
    sumo_network_version TEXT NOT NULL,
    demand_version TEXT,
    traffic_control_version TEXT,
    model_version TEXT,

    request_json TEXT NOT NULL,
    summary_json TEXT,
    validation_json TEXT,

    artifact_dir TEXT NOT NULL,

    progress REAL NOT NULL DEFAULT 0.0,
    current_sim_second REAL,

    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,

    error_code TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_simulation_runs_status
ON simulation_runs(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_simulation_runs_key
ON simulation_runs(run_key);
```

Add an ensemble metadata table when ensemble execution becomes real. It should
bind the normalized scenario request, member seeds, uncertainty policy,
representative-run rule, aggregate summary, and member run IDs. Do not duplicate
large member outputs in SQLite.

The exact foreign-key strategy for scenario revisions can be added after checking current scenario deletion behavior.

---

## 11.2 `calibration_campaigns`

```sql
CREATE TABLE IF NOT EXISTS calibration_campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,

    status TEXT NOT NULL,

    graph_version TEXT NOT NULL,
    sumo_network_version TEXT NOT NULL,
    demand_version TEXT NOT NULL,

    split_manifest_json TEXT NOT NULL,
    parameter_space_json TEXT NOT NULL,
    scoring_config_json TEXT NOT NULL,

    maximum_experiments INTEGER NOT NULL,

    best_experiment_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## 11.3 `calibration_experiments`

```sql
CREATE TABLE IF NOT EXISTS calibration_experiments (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    run_id TEXT,

    cache_key TEXT NOT NULL,
    seed INTEGER NOT NULL,

    parameters_json TEXT NOT NULL,
    score_json TEXT,

    total_loss REAL,
    valid INTEGER,

    started_at TEXT,
    finished_at TEXT,

    FOREIGN KEY (campaign_id)
        REFERENCES calibration_campaigns(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_calibration_experiments_campaign
ON calibration_experiments(campaign_id);
```

---

## 11.4 `benchmark_trips`

```sql
CREATE TABLE IF NOT EXISTS benchmark_trips (
    id TEXT PRIMARY KEY,

    departure_time TEXT NOT NULL,
    day_type TEXT NOT NULL,

    origin_node TEXT,
    destination_node TEXT,
    origin_zone TEXT,
    destination_zone TEXT,

    corridor_label TEXT,

    actual_travel_seconds REAL NOT NULL,

    incident_flag INTEGER,
    weather_category TEXT,

    checkpoints_json TEXT,
    notes TEXT,

    created_at TEXT NOT NULL
);
```

Do not store full private addresses.

Use graph nodes, zones, or intentionally coarse labels.

---

## 11.5 `model_bundles`

```sql
CREATE TABLE IF NOT EXISTS model_bundles (
    version TEXT PRIMARY KEY,

    graph_version TEXT NOT NULL,
    sumo_version TEXT NOT NULL,
    sumo_network_version TEXT NOT NULL,
    demand_version TEXT NOT NULL,
    traffic_control_version TEXT,
    parameter_version TEXT NOT NULL,
    traffic_profile_version TEXT NOT NULL,

    evidence_level TEXT NOT NULL,

    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,

    redistribution_class TEXT NOT NULL,

    validation_summary_json TEXT NOT NULL,

    frozen_at TEXT NOT NULL
);
```

---

# 12. FastAPI integration

## 12.1 Do not overload existing `/api/simulations`

The current endpoint:

```text
POST /api/simulations
```

already means route reliability sampling.

Changing that endpoint to launch SUMO would break its current API meaning.

Create a new namespace.

Recommended:

```text
/api/simulation/...
```

---

## 12.2 NEW: `backend/app/schemas/simulation.py`

Add typed models such as:

```text
SimulationCapabilities
SimulationRunRequest
SimulationRunCreated
SimulationRunStatus
SimulationRunSummary
SimulationPlaybackChunk
SimulationValidation
SimulationModelBundleSummary
```

Example run request concept:

```json
{
  "scenarioId": "uuid-or-null",
  "scenarioRevision": 4,
  "runMode": "closure_detailed",
  "planningMode": "depart_at",
  "departureTime": "2026-09-14T07:22:00-07:00",
  "arrivalDeadline": null,
  "arrivalBufferMinutes": 8,
  "confidenceTarget": 0.9,
  "modelVersion": "pv-2026-08-v1",
  "seed": 842901,
  "selectedTrip": {
    "originNode": "123",
    "destinationNode": "456"
  },
  "closures": [],
  "options": {
    "closureStateMode": "established",
    "warmupMinutes": 45,
    "analysisMinutes": 90,
    "playback": true,
    "ensembleRuns": 12
  }
}
```

No file URL fields.

`planningMode` must support:

```text
depart_at
arrive_by
```

For `arrive_by`, the service searches candidate departure times against the
time-dependent model and returns the latest departure that meets the requested
confidence target. It must not subtract one average duration from the deadline.

`closureStateMode` must support:

```text
established
activates_at
scheduled
```

The selected scenario schedule remains authoritative. The mode controls how
the worker constructs warm-up and initial state; it does not override the
saved closure definition.

---

## 12.3 NEW: `backend/app/api/simulation.py`

Suggested endpoints:

```text
GET  /api/simulation/status
POST /api/simulation/runs
GET  /api/simulation/runs/{run_id}
POST /api/simulation/runs/{run_id}/cancel
GET  /api/simulation/runs/{run_id}/summary
GET  /api/simulation/runs/{run_id}/playback
GET  /api/simulation/models
```

### `GET /api/simulation/status`

Return:

```json
{
  "available": true,
  "sumoVersion": "1.27.1",
  "runtimeMode": "libsumo",
  "networkReady": true,
  "modelReady": false,
  "activeRunId": null,
  "maxParallelRuns": 1
}
```

This is the first useful API slice to implement.

---

## 12.4 MODIFY: `backend/app/main.py`

Instantiate:

```text
SumoEnvironmentService
SumoNetworkService
SumoEdgeMappingService
SimulationRunService
```

only after those components exist.

Add them to `application.state`.

Include:

```python
application.include_router(simulation_router, prefix="/api")
```

Shutdown must terminate any owned worker cleanly.

Do not initialize a heavy SUMO network into FastAPI memory at startup.

---

# 13. Setup and doctor commands

## 13.1 MODIFY: `scripts/setup.mjs`

Keep the existing setup behavior.

After Python dependencies install, add a bounded SUMO capability check.

Setup may install approved dependencies.

Normal `npm run dev` must not download them.

Suggested messages:

```text
SUMO runtime installed: 1.27.1
netconvert available
libsumo available
```

If only subprocess SUMO works:

```text
SUMO runtime installed: 1.27.1
libsumo unavailable
Using subprocess runtime
```

Do not make the app unusable merely because libsumo is unavailable if normal SUMO works.

---

## 13.2 NEW: `scripts/sumo_doctor.py`

Check:

```text
SUMO binary
SUMO version
netconvert
sumolib
libsumo capability
approved local SUMO data directories
SUMO network manifest
app/SUMO edge map
active model bundle
disk free space
stale running-job records
```

No writes except perhaps harmless log output.

---

## 13.3 MODIFY: `scripts/doctor.py`

Add a high-level SUMO status without duplicating every detail.

Example:

```text
[OK]   Road graph: ...
[OK]   SUMO runtime: 1.27.1
[WARN] SUMO model: no frozen calibrated model bundle yet
```

A missing calibrated model should be a warning while the system is under development, not necessarily a startup failure.

---

# 14. Building the 24/7 baseline

The current PORTAL profile layer is narrower than the intended final model.

At present, the application primarily exposes:

```text
weekday_morning
weekday_afternoon
```

for selected September/October weekday windows.

Keep those profiles for compatibility.

Build a new generalized baseline representation rather than mutating the old artifacts into a different meaning.

---

## 14.1 Day-type model

Start with:

```text
mon_thu
friday
saturday
sunday
```

Add only when evidence supports it:

```text
holiday
pre_holiday
seasonal group
school term
summer
```

Do not create a holiday profile from three bad observations and call it science.

The completed PORTAL campaign currently contains weekdays only. Therefore:

- weekday buckets may become historically calibrated after all other gates;
- Saturday and Sunday must initially use an explicit inferred/fallback source;
- weekend output must remain `modeled_unobserved` until supporting observations
  are imported and validated; and
- the scheduler may always return a usable state, but it must never imply that
  every minute of the week has equal evidence.

---

## 14.2 15-minute baseline bucket

Canonical profile row:

```text
profile_version
day_type
bucket_start_minute

detector_id
app_edge_id

volume_mean
volume_median
volume_std
volume_p10
volume_p50
volume_p85
volume_p90
volume_p95

speed_mean
speed_median
speed_std
speed_p10
speed_p50
speed_p85
speed_p90
speed_p95

occupancy_mean
occupancy_median

sample_count
valid_day_count
missing_percent
quality_flags
evidence_level
fallback_source
```

`bucket_start_minute`:

```text
00:00 -> 0
07:15 -> 435
23:45 -> 1425
```

That is easier to interpolate than string labels.

---

## 14.3 Interpolation

For a simulation starting at 07:22:

```text
07:15 bucket = A
07:30 bucket = B

fraction = 7 / 15
value = A + (B - A) * fraction
```

As simulation time advances, insertion rates and expected boundary flows should change smoothly.

Do not abruptly switch every driver at a 15-minute boundary.

Interpolation must preserve nonnegative flow and physically valid speed bounds.
Interpolated quantiles must remain ordered. If those constraints cannot be
maintained, interpolate a fitted distribution or its parameters instead of
independently interpolating every percentile.

---

## 14.4 File placement

Create a new compiler instead of overloading the current PORTAL profile compiler beyond recognition.

Suggested:

```text
backend/app/services/traffic_schedule_service.py
scripts/build_traffic_schedule.py
```

Generated artifact:

```text
data/traffic/processed/schedules/<version>/
├── schedule.parquet
├── schedule-manifest.json
├── validation-report.json
└── validation-report.md
```

`TrafficService` may expose this schedule, but should not become responsible for SUMO orchestration.

---

# 15. Regional OD demand

The repository already has the correct first gate:

```text
regional_od_intake_service.py
```

That intake process should remain authoritative for:

- provenance;
- units;
- agency model version;
- raw checksums;
- zone geometry;
- OD schema;
- vehicle-trip confirmation;
- source licensing;
- Metro/RTC overlap status.

Once a delivery passes intake:

```text
data/traffic/processed/regional-od/<campaign-id>/
├── od-demand.parquet
└── zones.parquet
```

the SUMO demand pipeline starts.

---

## 15.1 Zone connectors

Create:

```text
scripts/build_sumo_zone_connectors.py
```

or fold this into `build_sumo_demand.py` once stable.

For each TAZ:

- identify valid candidate entry/exit edges;
- avoid private/local-only roads for regional connector placement;
- preserve direction;
- support external gateways;
- record connector weight;
- record why it was selected.

Artifact:

```text
zone_id
connector_type
sumo_edge_id
app_edge_id
weight
distance_m
road_class
gateway
status
review_reason
```

Human review should be possible before assignment.

---

## 15.2 Demand equation

The baseline demand is:

\[
D_{o,d,t,c}
\]

where:

- `o` = origin zone;
- `d` = destination zone;
- `t` = departure bucket;
- `c` = vehicle class.

The regional OD table is the prior, not the final calibrated truth.

---

## 15.3 Convert to SUMO demand

The demand builder should produce:

```text
regional.rou.xml.gz
vehicle-types.add.xml
demand-manifest.json
```

Use deterministic stochastic departure generation.

For each run, store:

```text
source OD demand
time scaling
simulation sampling factor
seed
vehicle class mix
connector version
```

---

# 16. Count calibration

Before comparing counts, normalize detector semantics explicitly.

PORTAL observations may represent individual lane detectors while SUMO output
may be lane, edge, or station aggregate output. The calibration adapter must
record:

```text
detector ID
station ID
lane number
direction
aggregation level
source interval
source volume unit
normalized vehicles/hour
SUMO comparison edge/lane set
```

Never sum lane rates and station totals together, never treat a 15-minute count
as vehicles/hour without the documented conversion, and never compare one lane
against an all-lane SUMO edge. Quality reports must expose missing or duplicate
lane coverage.

For each period or bucket:

1. load OD prior;
2. generate regional demand;
3. route/simulate;
4. collect simulated counts at matched detector edges;
5. compare with held-in training observations;
6. modify a bounded demand parameterization;
7. rerun;
8. retain candidates that improve validation;
9. stop when improvement flattens or budget is exhausted.

SUMO `routeSampler` can be used as one demand-estimation tool where count, turn, and OD constraints are appropriate.

Do not use detector calibrators as the first shortcut to force every count to match.

A calibrator can make a broken OD model look artificially good by inserting/removing traffic at detectors.

---

# 17. Physical network calibration

Calibration order matters.

Do not ask an optimizer to compensate for bad road geometry by inventing psychotic driver behavior.

## Group A: physical network

Review/calibrate first:

```text
lane counts
speed limits
turn permissions
ramp geometry
merge length
road capacity
intersection geometry
turn-lane storage
signal location
signal program
stop / yield priority
```

If these are materially wrong, stop.

---

## Group B: demand

Then calibrate:

```text
zone-to-zone demand
departure distribution
bridge selection
freeway entrance shares
through traffic
local traffic
vehicle classes
freight share
```

---

## Group C: driver behavior

Only after A and B are credible:

```text
desired time headway
acceleration
comfortable deceleration
minimum gap
speed factor distribution
lane-changing aggressiveness
reaction interval
rerouting behavior
```

Tune existing SUMO models before writing custom vehicle physics.

---

# 18. Calibration parameter files

Do not hardcode calibration bounds throughout Python.

Commit reviewed default parameter-space files.

Example:

```text
config/
└── simulation/
    ├── calibration-space.v1.yaml
    ├── scoring.v1.yaml
    ├── driver-types.v1.yaml
    └── resource-limits.v1.yaml
```

Example:

```yaml
car_following:
  tau:
    min: 0.8
    max: 1.8
    default: 1.1

  accel:
    min: 1.5
    max: 3.5
    default: 2.6

  decel:
    min: 3.0
    max: 5.5
    default: 4.5
```

Every model bundle records the exact config checksums.

---

# 19. The local optimizer

Start simple.

Recommended progression:

1. coordinate/grid search for obvious physical parameters;
2. bounded random or Latin-hypercube-like exploration;
3. SciPy differential evolution for selected non-differentiable groups;
4. optional Bayesian optimization only if the experiment count becomes expensive enough to justify another dependency.

Do not begin with a giant black-box optimizer over 50 parameters.

## Experiment cache key

Hash:

```text
SUMO version
SUMO network version
demand version
traffic-control version
traffic schedule version
parameter set
scenario/closure schedule
simulation sampling factor
warmup period
analysis period
seed
```

Exact repeat:

```text
reuse completed result
```

Do not pay CPU twice for identical work.

---

# 20. Calibration loss

A run does not win because one commute happens to look correct.

Use:

\[
L =
w_c L_{counts}
+
w_s L_{speeds}
+
w_t L_{trip-times}
+
w_q L_{queues}
+
w_r L_{route-shares}
+
P_{invalid}
\]

Store every component separately.

---

## 20.1 Counts

Useful metrics:

```text
WAPE
GEH
corridor directional bias
time-bucket bias
```

Do not use only one metric.

GEH is useful for traffic-count comparisons, while WAPE makes aggregate error legible.

---

## 20.2 Speeds

Use:

```text
MAE in km/h
normalized MAE
time-of-day bias
corridor directional bias
```

---

## 20.3 Benchmark trip times

Use:

```text
absolute error
percentage error
p50/p85/p95 consistency where repeated trips exist
```

---

## 20.4 Queues

When observed queue data exists:

```text
queue length error
queue start-time error
queue dissipation-time error
```

When no queue truth exists, queue output should still be checked for physical plausibility but should not pretend to be historically calibrated.

---

## 20.5 Route shares

Evaluate strategic choices such as:

```text
I-5 vs I-205
bridge selection
major ramp usage
freeway vs arterial diversion
```

Use bounded share error rather than requiring exact individual routes.

---

# 21. Hard invalidation rules

A parameter set should not receive a merely "bad score" for physically impossible behavior.

It should be invalid.

Hard failures should include:

```text
vehicle crosses fully closed edge after activation
vehicle conservation failure beyond tolerance
large unexplained demand disappearance
excessive teleports
excessive insertion failure
gridlock caused by malformed network topology
selected trip travels below physical free-flow floor beyond tolerance
simulation abort
NaN / corrupt output
unmapped closure edge
wrong network/model version
```

A hard failure sets:

```text
valid = false
```

and a large invalid penalty.

---

# 22. Free-flow floor

Implement this before calibration.

For every benchmark route and selected trip:

```text
simulated travel time >= physically plausible free-flow travel time - tolerance
```

The tolerance only covers:

- model discretization;
- small network-matching differences;
- allowed speed-factor behavior.

It must not permit a calibrated model to "solve" a 22-minute free-flow trip in 13 minutes.

Store both:

```text
network free-flow floor
observed/simulated value
```

in validation output.

---

# 23. Train, validation, and frozen test data

Never calibrate and report success on the same samples.

Create a versioned split manifest.

Example:

```json
{
  "version": "pv-split-v1",
  "trainingDays": [],
  "validationDays": [],
  "testDays": [],
  "heldOutDetectorGroups": [],
  "heldOutBenchmarkTrips": [],
  "heldOutEvents": []
}
```

Baseline observations and disruption events require separate split groups:

```text
baseline training / validation / test
closure-event training / validation / final test
```

The final closure-event test must remain unopened until regional closure
routing, microscopic coupling, reactive routing, and the bounded driver
distributions are frozen. If an event influences a parameter or implementation
choice, that event is no longer a final test and must be reclassified.

## Training

Used to change:

```text
demand
network parameters
driver behavior
```

## Validation

Used to:

```text
choose candidate
stop tuning
detect overfit
```

## Test

Not examined while selecting parameters.

Used only after model freeze.

## Spatial holdout

Hold out entire detector groups or corridor/direction groups, not only random rows.

That tests whether calibration propagates beyond the exact detector locations used for fitting.

---

# 24. Local benchmark trips

Your benchmark-trip system should be a first-class local dataset.

Record:

```text
anonymous benchmark ID
departure time
day type
origin graph node or zone
destination graph node or zone
corridor label
actual travel seconds
incident flag
weather category if known
optional checkpoint times
notes
```

Avoid full private addresses.

## Target coverage

Eventually include:

```text
Vancouver -> Portland
Portland -> Vancouver
east-west Portland
I-5
I-205
I-84
US-26
OR-217
bridge crossings
suburb -> core
arterial-heavy trips
short neighborhood trips
AM
midday
PM
evening
weekend
```

Thirty to fifty carefully recorded trips are more valuable than thousands of fake route observations.

---

# 25. Warm-up and analyzed window

A run should distinguish:

```text
warm-up period
analysis period
```

Recommended starting bounds:

```text
warm-up:      30-60 minutes
analysis:     60-90 minutes
```

The exact values need measurement.

Warm-up output should not pollute the scored period.

Warm-up topology depends on closure state:

```text
established closure:
  closure active for the entire warm-up and analysis window

activates_at closure:
  normal baseline warm-up, activation at the specified simulation time

scheduled closure:
  apply every start/end transition that overlaps warm-up or analysis
```

An always-active closure must not load a normal checkpoint, warm for only a few
minutes, and then be presented as established traffic. Either build a compatible
closure state or run enough closure-active warm-up for queues and route choices
to stabilize.

---

# 26. Baseline checkpoints

Precompute normal traffic states.

Use SUMO state save/load where compatible.

Example baseline states:

```text
mon-thu 05:00
mon-thu 06:00
mon-thu 07:00
mon-thu 08:00
...
friday ...
saturday ...
sunday ...
```

A closure at 07:22 can:

1. load a nearby approved state;
2. load the matching future demand file;
3. warm briefly;
4. activate closure;
5. simulate the disruption.

That sequence applies to a closure that actually activates at 07:22. It does
not apply to an established or always-active closure. Established closures need
a closure-active state or closure-active warm-up, and scheduled reopening must
continue long enough to measure queue dissipation when it falls inside the
requested analysis window.

## Critical checkpoint caveat

SUMO state files do not eliminate the need for compatible route/demand inputs for vehicles that have not departed yet.

Therefore a baseline checkpoint manifest must bind:

```text
SUMO version
network version
demand version
traffic-control version
parameter version
random state policy
platform metadata if RNG state compatibility matters
```

Do not load an old state into a newer network because the filenames happened to match.

---

# 27. Applying closures in SUMO

The existing scenario model remains the user-facing closure definition.

At run preparation:

```text
Scenario closure
      |
      v
app graph edge IDs
      |
      v
accepted edge mapping
      |
      v
SUMO edges / lanes
```

Every translated restriction must include:

```text
state mode: established | activates_at | scheduled
effective start and end
direction
affected SUMO edges and lanes
grandfathering rule for vehicles already committed to the edge
reopening behavior
```

## Full road closure

Options include:

- rerouter-based temporary closure;
- lane/edge permissions;
- runtime closure mechanics supported by the chosen SUMO integration.

Acceptance test:

> No vehicle may traverse the closed edge after the closure becomes active unless explicitly grandfathered by a documented modeling rule.

---

## Lane reduction

Represent:

```text
which lanes close
remaining capacity
merge behavior
closure start/end
```

Do not approximate a one-lane closure only by multiplying edge travel time if microscopic simulation is active.

---

## Speed restriction

Apply the scheduled speed restriction to the affected SUMO edges/lanes.

---

## Directional closure

App directional identity must map to the correct directed SUMO edge.

Closing northbound must not silently close southbound.

---

# 28. Reactive drivers

Add this only after the baseline is validated.

Reactive routing is nevertheless required before the closure-capable model can
be called complete. A forced reroute for vehicles that reach a closed edge is
not equivalent to the desired behavior of traffic-aware drivers changing
corridors earlier in the trip.

Possible driver attributes:

```text
traffic-aware guidance adoption
reaction delay
reroute interval
minimum saving threshold
residential-detour willingness
familiar-route preference
route simplicity preference
```

Example conceptual distribution:

```text
traffic-aware guidance:       70%
reaction delay:               30 sec to 5 min
minimum useful saving:        2-8 min
residential-detour refusal:   heterogeneous
```

Those are assumptions until calibrated.

They must appear in the run/model manifest.

## SUMO implementation

Prefer built-in rerouting mechanisms first:

```text
rerouting device
periodic rerouting
time-dependent edge weights
rerouter objects
TraCI/libsumo route changes where needed
```

Do not build a fake Google Maps server.

Calibrate rerouting behavior only with closure-event training and validation
sets. Do not select parameters by watching the final disruption test improve.

---

# 29. Vehicle classes

Start with a small number of meaningful classes:

```text
passenger car
light commercial
heavy truck
bus, if relevant
```

Each may have:

```text
length
acceleration
deceleration
speed factor
lane behavior
routing constraints
```

Do not create twenty pseudo-realistic classes before the demand model is trustworthy.

---

# 30. Simulation sampling and scale

For regional mesoscopic calibration, it can be reasonable to simulate a sampled
population when the demand and capacity interpretation is validated.

Example:

```text
1 simulated vehicle = N real vehicles
```

But a constant recorded scale is not sufficient proof of physical validity.

For the microscopic affected area, default to:

```text
1 SUMO vehicle = 1 modeled vehicle
```

A reduced microscopic population is allowed only after equivalence tests show
that it preserves, within documented tolerances:

```text
queue length and storage
density
throughput
merge and lane-change pressure
travel time
spillback timing
```

Otherwise the smaller vehicle population will make queues physically too short
even if reported counts are multiplied afterward.

Suggested manifest fields:

```json
{
  "realVehicleScale": 1.0,
  "simulatedVehicleScale": 1.0,
  "displayVehiclePolicy": "stable_hash_sample",
  "maximumDisplayedVehicles": 900
}
```

## Important playback distinction

There are three different concepts:

1. real-world vehicle represented by demand;
2. SUMO simulated vehicle;
3. dot rendered on the map.

Do not collapse all three into one misleading legend.

Map sampling is independent of simulation scaling. If the UI samples only 900
vehicles for performance, say so.

Example:

```text
Simulation demand scale: 1 SUMO vehicle = 1 modeled vehicle
Map: deterministic sample of up to 900 SUMO vehicles
```

The selected user's simulated trip is always rendered, even if it would not pass the display sample.

---

# 31. Frontend integration

The frontend already has:

```text
frontend/src/features/simulation/
├── PlaybackControls.tsx
└── simulationPlayback.ts
```

Preserve the controls.

Change the underlying source.

---

## 31.1 NEW: `frontend/src/api/simulation.ts`

Own:

```text
getSimulationStatus()
createSimulationRun()
getSimulationRun()
cancelSimulationRun()
getSimulationSummary()
getSimulationPlayback()
listSimulationModels()
```

Use relative `/api/...` URLs.

---

## 31.2 MODIFY: `frontend/src/features/simulation/simulationPlayback.ts`

Current role:

```text
generate fake deterministic moving agents from diversion edge totals
```

Future role:

```text
normalize server-provided SUMO playback frames for MapLibre rendering
```

During migration, support both:

```typescript
type PlaybackSource =
  | { kind: 'diversion_preview'; ... }
  | { kind: 'sumo'; ... }
```

This provides a clean UX distinction:

```text
Preview
Physical simulation
```

Do not present the preview as if SUMO has run.

---

## 31.3 NEW: `frontend/src/features/simulation/SimulationRunPanel.tsx`

Show:

```text
selected model version
evidence level
queued/running/completed state
simulation clock
progress
cancel button
cache hit indicator
warnings
invalid-run reason
```

---

## 31.4 NEW: `frontend/src/features/simulation/SimulationStatus.tsx`

Show capability/readiness:

```text
SUMO available
network version
model bundle
calibration status
```

Example:

```text
Physical model: available
Model: pv-2026-09-v1
Evidence: historically calibrated
Validated through: 2026-08
```

or:

```text
Physical model: experimental
Evidence: modeled, not historically validated
```

---

# 32. Playback data contract

Do not stream every vehicle every simulation step to the browser.

Playback represents one recorded realization, normally the deterministic run
closest to the ensemble median selected-trip travel time. Reliability metrics
come from the full bounded ensemble, not from the playback run alone. The UI
must identify the playback run seed and state that it is representative rather
than the only possible outcome.

Generate a compact artifact.

Suggested rows:

```text
sim_second
vehicle_id
lon
lat
speed_mps
heading
vehicle_class
selected_trip
congestion_state
```

Use:

```text
stable vehicle hash sampling
```

so the same vehicles remain visible across frames.

Sample interval starting point:

```text
1-2 simulated seconds for selected trip
2-5 simulated seconds for background display vehicles
```

Tune from browser performance.

Generated artifact:

```text
playback.parquet
```

The API can return requested time ranges as compact JSON.

Do not send a 2 GB XML file to React.

---

# 33. Regional output should be aggregate by default

For regional mesoscopic runs, default outputs should be:

```text
edge aggregates
detector counts
travel time
trip summaries
route shares
boundary flows
```

Avoid:

```text
every vehicle position
every simulation step
```

unless explicitly in diagnostic mode.

---

# 34. Precomputation

Most normal traffic work should happen before a user presses "Run closure."

Precompute and version:

```text
24/7 day-type traffic schedule
regional OD demand
regional normal route alternatives
time-dependent regional edge costs
baseline state checkpoints
signal programs
boundary inflow templates
validated model parameters
```

Then a closure simulation becomes:

```text
select model bundle
select baseline state
resolve established/activation/scheduled closure state
run regional closure mesoscopic scenario
compare against normal regional state
generate affected area from the regional delta
load scenario entry-to-exit boundary demand and initial queues
run microscopic disruption simulation
expand and rerun if boundary checks fail
compose regional and microscopic time-dependent costs
re-evaluate route candidates until stable or bounded non-convergence
run the requested uncertainty ensemble
score selected trip and alternatives
render one representative deterministic realization
```

---

# 35. Zero-billing and offline guardrails

The simulation subsystem must not depend on:

```text
Google Maps API
Mapbox API
HERE
TomTom
AWS
Azure
Google Cloud
hosted model APIs
hosted experiment trackers
remote databases
```

## Input validator

All simulation source paths must:

1. be local filesystem paths;
2. resolve under approved data roots;
3. contain no URL scheme;
4. pass existence checks;
5. pass expected checksum/version checks.

Reject:

```text
http://
https://
s3://
gs://
azure://
ftp://
```

## Worker environment

Pass a minimal approved environment to the worker.

Do not hand arbitrary provider credentials to the simulation process.

## No network during calibration

Calibration should only read:

```text
local OSM
local PORTAL
local OD
local signal files
local benchmark records
local model configs
```

There is no reason for a calibration run to contact the internet.

---

# 36. Source provenance manifest

Every data package needs local provenance.

Example:

```json
{
  "sourceId": "portal-2026-campaign",
  "title": "PORTAL detector observations",
  "localPath": "data/traffic/...",
  "sha256": "...",
  "license": "...",
  "retrievedAt": "...",
  "redistribution": "restricted-or-allowed",
  "usedFor": [
    "count calibration",
    "speed validation"
  ]
}
```

The model bundle references these source manifests.

Use an explicit redistribution classification:

```text
public
public_with_attribution
local_only
restricted_derived
private
unknown
```

Do not copy restricted source data into Git because the code is open source.
Do not assume that a model bundle, edge-weight table, OD matrix, signal plan, or
calibrated parameter derived from restricted data is automatically publishable.

The public bundle/export builder must fail closed when any transitive input is
`restricted_derived`, `private`, or `unknown`. Open-source releases may contain
code, schemas, documentation, and synthetic fixtures while real data and
restricted derived artifacts remain local.

---

# 37. SUMO runtime versioning

Every run manifest records:

```text
sumo_version
runtime_mode
Python version
OS/platform
network version
```

Example:

```json
{
  "sumoVersion": "1.27.1",
  "runtimeMode": "libsumo",
  "pythonVersion": "3.12.x",
  "platform": "macOS-arm64"
}
```

If results change after a SUMO upgrade, you need to know why.

---

# 38. Determinism

Each run must have an explicit seed.

If a user does not supply one:

```text
derive one from normalized run request
```

or generate and immediately persist it before running.

Never use:

```python
random.random()
```

without a recorded seed for calibration.

A result should be reproducible given:

```text
model bundle
run request
seed
SUMO version
platform-dependent caveats
```

---

# 39. Run manifest

Every simulation directory should contain a manifest similar to:

```json
{
  "schemaVersion": 1,
  "runId": "uuid",
  "runKind": "closure_detailed",

  "createdAt": "...",

  "sumoVersion": "1.27.1",
  "runtimeMode": "libsumo",

  "osmSourceVersion": "...",
  "osmSourceSha256": "...",
  "graphVersion": "...",
  "sumoNetworkVersion": "...",
  "demandVersion": "...",
  "trafficControlVersion": "...",
  "modelVersion": "...",

  "trafficScheduleVersion": "...",

  "seed": 123456,

  "warmupSeconds": 2700,
  "analysisSeconds": 5400,

  "simulationScale": {
    "realVehiclesPerSimVehicle": 1,
    "scaleValidationId": null
  },

  "display": {
    "policy": "stable_hash_sample",
    "maximumVehicles": 900
  },

  "closureIds": [],
  "closureStateMode": "established",
  "affectedAreaGeneration": 1,

  "coupling": {
    "normalRegionalRunId": "...",
    "closureRegionalRunId": "...",
    "microRunIds": [],
    "iteration": 1,
    "converged": true,
    "remainingDelta": 0.0
  },

  "planning": {
    "mode": "depart_at",
    "departureTime": "...",
    "arrivalDeadline": null,
    "confidenceTarget": 0.9
  },

  "ensemble": {
    "memberCount": 12,
    "memberSeeds": [],
    "representativeSeed": 123456
  },

  "inputs": {},
  "outputs": {},

  "checksums": {}
}
```

---

# 40. Model bundle

The final product is not a neural-network checkpoint.

It is a versioned bundle.

Example:

```text
data/sumo/models/pv-2026-09-v1/
├── model-manifest.json
├── parameter-set.json
├── validation-report.json
├── validation-report.md
└── checksums.json
```

Manifest:

```json
{
  "modelVersion": "pv-2026-09-v1",

  "sumoVersion": "1.27.1",
  "osmSourceVersion": "...",
  "osmSourceSha256": "...",
  "graphVersion": "...",
  "sumoNetworkVersion": "...",
  "demandVersion": "...",
  "trafficControlVersion": "...",
  "trafficScheduleVersion": "...",
  "parameterVersion": "...",
  "driverDistributionVersion": "...",

  "evidenceLevel": "historically_calibrated",
  "redistributionClass": "local_only",

  "validation": {
    "passed": true,
    "testDataFrozen": true,
    "detectorCoverage": 0.0,
    "benchmarkTripCount": 0
  },

  "sources": [],
  "componentRedistribution": [],
  "checksums": {}
}
```

The UI runs a named bundle.

Never assemble an invisible model from whichever artifacts have newest timestamps.

---

# 41. Signal plans

Phase one may use OSM-derived plausible signals.

But signal programs strongly affect:

```text
arterial queues
turn delay
spillback
closure diversion
```

Therefore:

1. generate/import initial signal plans;
2. identify critical intersections in affected areas;
3. compare against available public signal timing data;
4. version real plans separately;
5. allow weekday/weekend/time-period plans;
6. do not globally optimize signal timing just to fit travel times unless that is a real-world timing plan.

Signals are only one part of intersection control. Create a versioned traffic-
control inventory containing at least:

```text
intersection ID
incoming approach and direction
control type: signal | stop | yield | uncontrolled | inferred
stop/yield enforcement direction
protected and permitted turn behavior
signal program and time-of-day plan
pedestrian-actuation assumption
source and source date
confidence
review status
```

An all-way stop, two-way stop, and uncontrolled side street must not collapse
into the same default. Guessed OSM controls remain inferred until independently
validated. A no-pedestrian signal scenario is a stated experiment, not a claim
that normal real-world operation has no pedestrian calls.

---

# 42. Queue propagation and affected-area expansion

After detailed simulation, create a boundary report.

Suggested metrics:

```text
maximum queue within 250 m of boundary
minimum speed within 250 m of boundary
vehicles delayed at boundary
rerouted vehicles touching boundary
selected-trip path boundary dependence
```

Expansion trigger example:

```text
queue > threshold near boundary
OR
speed < threshold for sustained interval
OR
rerouted flow near boundary > threshold
```

Store the reason:

```json
{
  "expanded": true,
  "generation": 2,
  "reasons": [
    "queue_reached_north_boundary",
    "rerouted_flow_exceeded_threshold"
  ]
}
```

This makes arbitrary closure support explainable.

---

# 43. Performance budgets

Start with strict limits.

Suggested initial defaults:

```text
parallel SUMO runs:             1
maximum run wall time:          60 min
maximum calibration campaign:   100 experiments
warm-up:                        45 min simulated
analysis:                       90 min simulated
max affected-area expansions:   3
max visible map vehicles:       900
```

Add disk budgeting:

```text
maximum run artifact size
maximum total runs cache
retention count
```

After scoring:

- retain manifest;
- retain summary;
- retain validation;
- retain compact outputs;
- compress or delete verbose raw artifacts according to policy.

Never delete the current best calibration experiment until the campaign is finalized.

---

# 44. Storage cleanup

Add a local cleanup command later:

```text
npm run sumo:clean-runs
```

Options:

```text
--older-than
--keep-best
--keep-model-referenced
--dry-run
```

Default must be dry-run or otherwise require an explicit destructive flag.

Never delete a run referenced by a frozen model bundle.

---

# 45. Testing strategy

Do not use the Portland metro network for every unit test.

Create a tiny synthetic SUMO fixture.

Suggested shape:

```text
A ---- B ---- C
      / \
     D---E
```

Include:

- one alternate path;
- one signal;
- one directional edge;
- one closable lane;
- one detector-equivalent observation.

Commit fixture files under:

```text
backend/tests/fixtures/sumo/
```

---

## 45.1 NEW tests

Add incrementally:

```text
backend/tests/test_sumo_environment.py
backend/tests/test_sumo_network_mapping.py
backend/tests/test_sumo_run_service.py
backend/tests/test_sumo_worker.py
backend/tests/test_sumo_output_service.py
backend/tests/test_sumo_validation.py
backend/tests/test_sumo_scoring.py
backend/tests/test_sumo_calibration.py
backend/tests/test_simulation_api.py
```

Not all at once.

---

## 45.2 Required behavior tests

### Closure correctness

```text
full closure blocks target edge
directional closure preserves opposite direction
lane reduction does not remove full road
scheduled closure applies at correct time
```

### Reproducibility

Same:

```text
model + request + seed
```

must produce equivalent scored output within documented platform tolerance.

### Cancellation

```text
run enters running
cancel requested
worker exits
status becomes cancelled
no orphan process
```

### Cache

Exact run repeats should reuse completed result.

Changed seed must not accidentally hit the same cache key unless the result is intentionally seed-independent.

### Mapping

Ambiguous closure mapping must fail safely.

### Physical validation

No vehicle on a fully closed edge after closure activation.

---

# 46. Current PORTAL data migration path

Do not throw away the existing PORTAL work.

Existing work already gives:

- detector observations;
- accepted station-to-app-edge mappings;
- directional freeway/ramp speed and flow;
- 15-minute aggregation;
- historical profile metadata;
- diagnostic OD fitting machinery.

Use it in stages.

## Stage 1

Map accepted PORTAL app edges through:

```text
app edge -> SUMO edge map
```

Then PORTAL can score SUMO output.

## Stage 2

Generalize from only AM/PM selectable profiles into 24/7 day-type schedules.

## Stage 3

Use held-out detector groups for validation.

## Stage 4

Use regional OD demand to make freeway observations constrain the broader network.

The existing diagnostic background seed remains valuable as:

```text
regression test
bootstrap
comparison model
```

but should not be promoted to production neighborhood traffic.

---

# 47. Existing `DiversionService` after SUMO

Do not delete `DiversionService`.

Its future purpose becomes:

```text
fast closure-impact preview
candidate corridor generation
initial affected-area estimation
fallback when SUMO model is unavailable
```

UI labels should distinguish:

```text
Fast model preview
Physical traffic simulation
```

This makes the app responsive without making the user run a microscopic model for every click.

---

# 48. Existing `TrafficService` after SUMO

Keep `TrafficService` focused on:

```text
historical observations
profiles
traffic evidence
route reliability sampling
background snapshots
```

Do not move worker/process management into it.

It can become a provider to SUMO services:

```text
TrafficScheduleService -> SumoDemandService
TrafficService -> ScoringService
```

---

# 49. Existing NetworkX routing after SUMO

NetworkX should remain the canonical fast route planning interface for the application.

SUMO can produce route changes and physical travel outcomes, but frontend workflow should not depend on launching SUMO just to:

```text
click Point A
click Point B
select closure
see route options
```

This is a major usability and architecture win.

For a completed physical run, however, the final recommendation must use the
composed scenario cost surface:

```text
regional closure time-dependent costs
+ microscopic affected-area travel times and queues
= final scenario costs
```

NetworkX may search that cost surface and generate new candidate paths. SUMO
then evaluates any materially new corridors. Continue until the selected route
and material corridor flows stabilize or the configured iteration budget is
exhausted. If convergence is not reached, return the best bounded result with a
visible non-convergence warning.

The final route must never silently fall back to free-flow or preview costs
after the UI says a physical simulation completed.

---

# 50. API/UI evidence levels

Extend the honesty rule.

Suggested simulation-specific statuses:

```text
preview_structural
simulation_uncalibrated
simulation_calibrated
simulation_validated_holdout
```

Keep compatibility with existing app evidence labels where practical.

A run can say:

```text
Physical simulation completed
Model evidence: uncalibrated
```

That is far better than implying "SUMO" automatically means "accurate."

---

# 51. SUMO subsystem phase plan

The safest implementation is staged so each phase has an acceptance gate.

---

## SUMO Phase 0 - Contract and environment

### Goal

Introduce SUMO without changing current route behavior.

### Add

```text
SUMO dependency
sumo_doctor.py
SUMO settings
simulation status schema/API
tiny synthetic fixture
```

### Modify

```text
GAMEPLAN.md
AGENTS.md
backend/requirements.txt
scripts/setup.mjs
scripts/doctor.py
backend/app/core/settings.py
backend/app/main.py
package.json
.gitignore
```

### Acceptance gate

```text
npm run setup
npm run sumo:doctor
npm test
npm run dev
GET /api/simulation/status
```

all work.

No Portland simulation yet.

---

## SUMO Phase 1 - Frozen OSM source, dual-network rebuild, and edge mapping

### Goal

Freeze one local OSM source, rebuild both engines from it, migrate graph-derived
state safely, and reliably map app edges to SUMO edges.

### Add

```text
frozen OSM source manifest
app graph rebuild from frozen source
build_sumo_network.py
validate_sumo_network.py
network_service.py
edge_mapping_service.py
saved-scenario and closure rematch report
PORTAL/edge-prior rematch validation
network mapping tests
```

### Acceptance gate

- frozen OSM source checksum and license recorded;
- app graph and SUMO manifests reference the identical source checksum;
- rebuilt app graph passes its existing graph gate;
- saved closure/scenario migration produces accepted/review/failed results and
  never silently changes roads;
- graph-derived PORTAL matches and edge priors are rebuilt or explicitly
  revalidated;
- network manifest valid;
- SUMO opens the network;
- representative freeway/arterial/local roads map correctly;
- directional mapping passes;
- selected closure test corpus contains no silent ambiguous matches;
- road crossings/interchanges remain topologically valid.

Do not continue to regional calibration until this passes.

---

## SUMO Phase 2 - Deterministic worker and one toy physical run

### Goal

Prove orchestration before adding Portland-scale demand.

### Add

```text
sumo_worker.py
run_service.py
output_service.py
simulation_runs table
POST /api/simulation/runs
GET /api/simulation/runs/{id}
cancel endpoint
TripInfo parser
```

### Acceptance gate

Tiny synthetic run:

- starts;
- progresses;
- closes an edge;
- selected trip reroutes;
- TripInfo parses;
- cancellation works;
- repeated seed is reproducible;
- no orphan processes remain.

---

## SUMO Phase 3 - 24/7 traffic schedule

### Goal

Turn PORTAL into a general time scheduler.

### Add

```text
traffic_schedule_service.py
build_traffic_schedule.py
day-type and 15-minute artifacts
interpolation tests
```

### Acceptance gate

For arbitrary local time:

```text
00:00 through 23:59
```

the service returns a versioned interpolated background state with quality metadata.

Old AM/PM profiles still work.

Weekday and weekend evidence levels are reported independently. Weekends remain
modeled fallback states until weekend observations pass their own data gate.

---

## SUMO Phase 4 - Benchmark trip system and free-flow floor

### Goal

Create independent trip-level truth before optimization.

### Add

```text
benchmark_trips table
benchmark service/script
free-flow validation
benchmark import/export
```

### Acceptance gate

At least a seed benchmark set can be scored without storing private addresses.

No simulated trip beats the configured physical free-flow floor.

Both `depart_at` and `arrive_by` contracts validate, including an arrive-by
departure-time search on a deterministic fixture.

---

## SUMO Phase 5 - Real regional OD demand to SUMO

### Goal

Use accepted Metro/RTC OD delivery.

### Dependency

The regional OD intake gate must pass.

### Add

```text
zone connectors
SUMO demand generation
external gateway handling
vehicle class mix
demand manifest
```

### Acceptance gate

- all OD zone IDs resolve;
- boundary/gateway behavior is documented;
- generated vehicles conserve accepted OD demand after sampling scale;
- every sampled/scaled regional run records its scale and passes its approved
  demand/capacity equivalence gate;
- demand does not silently originate in invalid streets;
- bi-state overlap is resolved.

---

## SUMO Phase 6 - Regional mesoscopic baseline

### Goal

Run the full region efficiently.

### Add

```text
regional run mode
edge aggregate output
boundary flow output
entry-to-exit boundary OD output
regional route-share output
baseline checkpoint generation
```

### Acceptance gate

- stable regional run;
- bounded runtime;
- no major teleport/insertion failure;
- regional output covers required corridors;
- conservation checks pass.
- boundary output preserves entry edge, exit edge or destination zone, time
  bucket, vehicle class, and route-choice group.

---

## SUMO Phase 7 - Calibration scoring and optimizer

### Goal

Fit the baseline without overfitting.

### Add

```text
scoring_service.py
calibration_service.py
calibration tables
split manifest
parameter-space config
```

### Acceptance gate

- training loss improves;
- validation loss improves or remains credible;
- held-out detector groups remain untouched by optimization;
- closure-event training, validation, and final-test partitions are distinct;
- campaign can stop/restart without losing completed experiments;
- identical candidates hit cache;
- invalid candidates are rejected.

---

## SUMO Phase 8 - Freeze first no-closure model bundle

### Goal

Create a reproducible normal-traffic model.

### Add

```text
model_bundle_service.py
bundle_sumo_model.py
validate_sumo_model.py
```

### Acceptance gate

Bundle contains all versions/checksums and passes frozen no-closure validation.

Only now should the UI be allowed to call it a calibrated model.

---

## SUMO Phase 9 - Generic regional closure simulation

### Goal

Run arbitrary closures across the regional mesoscopic network before choosing a
microscopic affected area.

### Add

```text
regional closure translation
regional dynamic rerouting
normal-versus-closure delta output
strategic corridor and bridge-share output
closure-state modes: established | activates_at | scheduled
```

### Acceptance gate

Test at least:

```text
freeway full closure
one-direction closure
lane reduction
arterial closure
multiple simultaneous closures
established closure
mid-run activation and reopening
```

- displaced demand remains conserved within disclosed tolerances;
- regional route and corridor changes are material inputs to affected-area
  discovery;
- no hardcoded I-5 assumptions exist; and
- always-active closures are active throughout their warm-up.

---

## SUMO Phase 10 - Generic microscopic affected area and coupling

### Goal

Model lane, queue, signal, merge, and spillback behavior near arbitrary closures
using scenario-specific regional boundary demand.

### Add

```text
affected_area_service.py
micro subnetwork generation
entry-to-exit boundary coupling
initial queue/vehicle state transfer
closure translation
area expansion logic
regional/micro cost composition
bounded route/cost convergence loop
```

### Acceptance gate

- microscopic demand defaults to one SUMO vehicle per modeled vehicle;
- no vehicle traverses a fully closed edge after the documented grandfathering
  boundary;
- lane reductions create physical merge and storage effects;
- traffic controls remain directional and versioned;
- boundary expansion triggers when queues or rerouted demand reach the edge;
- route and material corridor-flow convergence is measured;
- a bounded non-converged result is labeled visibly rather than hidden; and
- freeway, arterial, ramp, directional, lane-reduction, and simultaneous
  closure fixtures pass.

---

## SUMO Phase 11 - Reactive routing and bounded heterogeneity

### Goal

Add the traffic-aware response required for real spillover while keeping
behavior assumptions bounded and testable.

### Add

```text
rerouting adoption
reaction delay
minimum saving threshold
residential avoidance
familiar-route preference
small validated vehicle-class distributions
```

### Acceptance gate

- forced rerouting and traffic-aware anticipatory rerouting are distinguishable;
- parameters are fitted only with closure training data and selected with
  closure validation data;
- closure validation improves without degrading frozen normal-day gates; and
- the final closure-event test remains unopened.

---

## SUMO Phase 12 - Closure-event calibration and ensemble reliability

### Goal

Calibrate closure propagation on development events and produce statistical
trip results rather than treating one seed as truth.

### Add

```text
closure training/validation scoring
bounded seed and demand ensembles
depart-at and arrive-by evaluation
representative-run selection
uncertainty and convergence reporting
```

### Acceptance gate

Compare:

```text
affected corridors
speed degradation
queue timing
bridge/route shares
trip-time effects
```

- training and validation event metrics are stored separately;
- p50/p85/p90/p95 and on-time probability come from an ensemble;
- the representative playback seed is identified;
- arrive-by searches return the latest departure meeting the confidence target
  or state that none was found; and
- the final closure-event test remains unopened.

---

## SUMO Phase 13 - Freeze closure-capable model and open final disruption test

### Goal

Freeze the complete closure-capable bundle, then evaluate it once against
untouched historical disruption events.

### Add

```text
closure-capable model bundle
final disruption test report
redistribution classification report
```

### Acceptance gate

- no parameter or implementation choice changes after opening the final test;
- affected corridors, speed degradation, queue timing, strategic route shares,
  and trip-time effects are reported;
- all baseline gates still pass;
- every transitive model artifact has a redistribution classification; and
- failure remains a valid outcome and starts a new model version with a new
  untouched test set rather than retroactively tuning this result.

---

## SUMO Phase 14 - SUMO playback integration

### Goal

Replace synthetic dots for physical runs while keeping preview and physical
simulation visibly distinct.

### Add/modify

```text
frontend/src/api/simulation.ts
SimulationRunPanel.tsx
SimulationStatus.tsx
simulationPlayback.ts
MapLibre playback source
```

### Acceptance gate

- selected trip visible;
- deterministic sampled vehicles visible;
- traveled route remains blue and projected route may change after rerouting;
- pause/play/restart/scrub/follow/speed controls work;
- browser never receives unbounded vehicle output;
- legend separately states real-demand scale, SUMO scale, and display sampling;
- the playback seed and representative-run policy are visible; and
- percentile/reliability results are never inferred from the one playback run.

---

## SUMO Phase 15 - Incident generation, optional

Only after incident rates can be expressed using defensible local evidence such as:

```text
vehicle miles
road class
time of day
conditions
```

Do not add random crashes for visual drama.

Adding incidents changes the model contract. It requires new training,
validation, freeze, and untouched-test evidence before incident-enabled results
can inherit a validated evidence label.

---

# 52. Recommended first coding slice for Codex

Do **not** tell Codex:

> "Implement the whole SUMO plan."

That invites architecture confetti.

Use this first task instead:

```text
Implement SUMO Phase 0 and the smallest part of SUMO Phase 2 needed to prove a local SUMO runtime.

Requirements:

1. Read AGENTS.md and GAMEPLAN.md.
2. Preserve all existing NetworkX, TrafficService, DiversionService, and frontend behavior.
3. Add a pinned/tested SUMO runtime dependency compatible with Python 3.12 on this Mac.
4. Add scripts/sumo_doctor.py.
5. Extend scripts/setup.mjs and scripts/doctor.py with SUMO capability checks.
6. Add typed SUMO path/runtime settings to backend/app/core/settings.py.
7. Add backend/app/schemas/simulation.py.
8. Add GET /api/simulation/status only.
9. Add a tiny committed synthetic SUMO fixture under backend/tests/fixtures/sumo/.
10. Add tests that verify:
   - runtime discovery,
   - status endpoint,
   - SUMO version reporting,
   - the tiny fixture can be loaded.
11. Do not add Portland-scale simulation yet.
12. Do not change POST /api/simulations.
13. Do not put SUMO loops in FastAPI.
14. Do not add cloud services, API keys, Docker, or external runtime downloads.
15. Run the relevant backend tests, then the full repository test command.

Stop after this slice and report:
- changed files;
- tests run;
- detected SUMO version;
- detected runtime mode;
- any macOS/Python compatibility issue.
```

That creates a clean foundation.

---

# 53. Second coding slice

After SUMO Phase 0 passes:

```text
Implement the SUMO network build and app-edge/SUMO-edge mapping pipeline.

Do not implement demand calibration yet.

Requirements:
- freeze one approved local OSM extract and record its checksum/license;
- rebuild the app graph and SUMO network from that exact extract;
- migrate/rematch saved scenarios and graph-derived traffic artifacts through
  explicit accepted/review/failed reports;
- use netconvert;
- preserve original OSM identifiers;
- generate versioned network manifest;
- generate edge-map.parquet;
- use identity + direction + geometry + topology;
- emit accepted/review/unmatched;
- never silently map ambiguous edges;
- add a validation report;
- add mapping tests;
- update sumo:doctor to report mapping readiness.
```

---

# 54. Third coding slice

After edge mapping passes:

```text
Implement a deterministic local worker-run lifecycle using the synthetic fixture.

Requirements:
- simulation_runs SQLite table;
- separate sumo_worker.py process;
- POST /api/simulation/runs;
- GET status by run ID;
- cancellation;
- run manifests;
- deterministic seed;
- TripInfo output parsing;
- exact-run cache key;
- no Portland regional demand yet.
```

This keeps debugging dimensions small.

---

# 55. What must not happen

Do not:

- replace NetworkX with SUMO;
- pair the current Overpass-built graph with a differently dated SUMO network;
- run SUMO inside a FastAPI request;
- make the UI wait synchronously for a long simulation HTTP request;
- hardcode the Interstate Bridge;
- hardcode a home/work pair;
- call current visual dots "vehicles";
- make current background-demand seeds production truth;
- train against validation/test days;
- use all PORTAL detectors for calibration;
- force counts with calibrators and declare victory;
- optimize physical network, demand, and driver behavior simultaneously;
- save only a "best score" without the component losses;
- allow closure simulations with uncertain app/SUMO edge mapping;
- use URLs as simulation source inputs;
- add cloud databases or hosted experiment trackers;
- require Docker;
- download datasets when `npm run dev` starts;
- create an unbounded number of parallel SUMO processes;
- generate detailed per-vehicle output for the entire region by default;
- feed normal-day boundary totals into a microscopic closure run without
  scenario entry-to-exit intent;
- treat an established closure as a mid-run activation;
- use reduced microscopic vehicle demand without validated physical
  equivalence;
- calculate percentiles or on-time probability from one deterministic seed;
- tune reactive-routing behavior against the final disruption test;
- return a free-flow/preview route as the final result after physical
  simulation completed;
- expose private benchmark addresses;
- publish restricted agency raw data;
- silently load a model built for a different road graph;
- silently load old SUMO state into a new network;
- label an unvalidated SUMO result "historically calibrated."

---

# 56. Definition of done

The physical simulation subsystem is mature when all of the following are true.

## Environment

- `npm run setup` installs/verifies the approved local runtime.
- `npm run doctor` reports SUMO status.
- normal use requires no API key or cloud account.
- simulation can run with the network disconnected.

## Network

- app graph and SUMO network reference the same frozen OSM source checksum.
- saved scenarios and graph-derived traffic artifacts pass migration/rematch
  review after the dual-network rebuild.
- closure edge mapping is versioned and reviewed.
- directional closure mapping is correct.
- critical intersections/ramps are validated.
- traffic-control inventory distinguishes directional stop, yield, signal, and
  inferred controls.

## Demand

- real accepted regional OD demand is used.
- external gateways are represented.
- 24/7 day-type demand is available.
- demand conservation passes.

## Calibration

- parameters are bounded.
- train/validation/test are separated.
- detector holdouts exist.
- benchmark trip holdouts exist.
- no-closure model bundle passes validation.
- invalid physics are rejected.

## Closure simulation

- arbitrary closures can generate affected areas.
- the regional closure run precedes affected-area generation.
- boundary demand preserves entry-to-exit trip intent and time.
- microscopic simulation is not hardcoded to one corridor.
- boundary spillover can trigger bounded expansion.
- regional/micro coupling convergence is measured and non-convergence is
  disclosed.
- closed edges are never traversed after activation.
- lane reductions and directional closures behave correctly.
- established, activating, scheduled, and reopening closure behavior is tested.
- reactive traffic-aware rerouting is validated before closure-capable freeze.

## Performance

- one-run default prevents CPU pileups.
- runs are cancellable.
- campaigns are resumable.
- exact runs are cached.
- disk retention is bounded.
- browser output remains compact.

## UI

- preview and physical simulation are clearly distinguished.
- playback can use real SUMO sampled vehicles.
- selected trip is visible.
- scale/weight is disclosed.
- simulation scale and display sampling are disclosed separately.
- evidence level and model version are visible.
- depart-at and arrive-by are both supported by the physical model.
- reliability metrics come from an ensemble, while playback identifies its one
  representative seed.

## Reproducibility

Every result can identify:

```text
scenario revision
model bundle
SUMO version
network version
demand version
traffic-control version
traffic schedule
parameter set
seed
input checksums
```

## Licensing and privacy

- code and synthetic fixtures may be open source;
- raw real-world data, private benchmarks, and restricted derived artifacts
  remain local;
- every model component has a redistribution classification;
- public export fails closed for private, restricted, or unknown dependencies;
- no full private address is written to normal logs, manifests, or public
  artifacts.

---

# 57. Resulting target repository structure

The mature repository should evolve toward:

```text
commute-help/
├── AGENTS.md
├── GAMEPLAN.md
├── README.md
├── package.json
├── config/
│   └── simulation/
│       ├── calibration-space.v1.yaml
│       ├── driver-types.v1.yaml
│       ├── resource-limits.v1.yaml
│       └── scoring.v1.yaml
│
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── simulation.py
│   │   │   └── ...
│   │   ├── core/
│   │   │   └── settings.py
│   │   ├── db/
│   │   │   └── database.py
│   │   ├── schemas/
│   │   │   ├── simulation.py
│   │   │   └── ...
│   │   ├── services/
│   │   │   ├── traffic_service.py
│   │   │   ├── traffic_schedule_service.py
│   │   │   ├── diversion_service.py
│   │   │   └── sumo/
│   │   │       ├── environment.py
│   │   │       ├── network_service.py
│   │   │       ├── edge_mapping_service.py
│   │   │       ├── demand_service.py
│   │   │       ├── affected_area_service.py
│   │   │       ├── run_service.py
│   │   │       ├── output_service.py
│   │   │       ├── scoring_service.py
│   │   │       ├── calibration_service.py
│   │   │       ├── validation_service.py
│   │   │       └── model_bundle_service.py
│   │   ├── workers/
│   │   │   └── sumo_worker.py
│   │   └── main.py
│   │
│   ├── tests/
│   │   ├── fixtures/
│   │   │   └── sumo/
│   │   ├── test_sumo_environment.py
│   │   ├── test_sumo_network_mapping.py
│   │   ├── test_sumo_run_service.py
│   │   ├── test_sumo_worker.py
│   │   ├── test_sumo_output_service.py
│   │   ├── test_sumo_validation.py
│   │   ├── test_sumo_scoring.py
│   │   └── test_simulation_api.py
│   │
│   └── requirements.txt
│
├── frontend/
│   └── src/
│       ├── api/
│       │   ├── simulation.ts
│       │   └── ...
│       └── features/
│           └── simulation/
│               ├── PlaybackControls.tsx
│               ├── SimulationRunPanel.tsx
│               ├── SimulationStatus.tsx
│               └── simulationPlayback.ts
│
├── scripts/
│   ├── sumo_doctor.py
│   ├── build_sumo_network.py
│   ├── validate_sumo_network.py
│   ├── build_traffic_schedule.py
│   ├── build_sumo_demand.py
│   ├── build_sumo_baseline.py
│   ├── calibrate_sumo.py
│   ├── validate_sumo_model.py
│   └── bundle_sumo_model.py
│
├── docs/
│   ├── SUMO_SIMULATION.md
│   ├── SUMO_NETWORK_MAPPING.md
│   ├── SUMO_CALIBRATION.md
│   ├── SUMO_MODEL_BUNDLES.md
│   └── ...
│
└── data/
    ├── app.db
    ├── graphs/
    ├── traffic/
    ├── benchmarks/
    └── sumo/
```

That is an end-state map, not permission to generate twenty empty files in the first PR.

---

# 58. Official SUMO references

Use the official SUMO documentation as the technical authority for implementation details.

- SUMO downloads and Python packages:  
  https://sumo.dlr.de/docs/Downloads.php

- libsumo:  
  https://sumo.dlr.de/docs/Libsumo.html

- Mesoscopic simulation:  
  https://sumo.dlr.de/docs/Simulation/Meso.html

- OpenStreetMap network import:  
  https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html

- `netconvert`:  
  https://sumo.dlr.de/docs/netconvert.html

- Dynamic user assignment:  
  https://sumo.dlr.de/docs/Demand/Dynamic_User_Assignment.html

- Automatic routing / routing behavior:  
  https://sumo.dlr.de/docs/Simulation/Routing.html

- Routes from observation points / `routeSampler`:  
  https://sumo.dlr.de/docs/Demand/Routes_from_Observation_Points.html

- Calibrators:  
  https://sumo.dlr.de/docs/Simulation/Calibrator.html

- Save and load simulation state:  
  https://sumo.dlr.de/docs/Simulation/SaveAndLoad.html

- Traffic lights:  
  https://sumo.dlr.de/docs/Simulation/Traffic_Lights.html

- Vehicle types:  
  https://sumo.dlr.de/docs/Definition_of_Vehicles%2C_Vehicle_Types%2C_and_Routes.html

- Car-following models:  
  https://sumo.dlr.de/userdoc/Car-Following-Models/

- TripInfo output:  
  https://sumo.dlr.de/docs/Simulation/Output/TripInfo.html

- Queue output:  
  https://sumo.dlr.de/docs/Simulation/Output/QueueOutput.html

- TraCI:  
  https://sumo.dlr.de/userdoc/TraCI/

---

# 59. Final implementation principle

The architecture should preserve a clear evidence ladder:

```text
NetworkX structural route
        |
        v
fast diversion preview
        |
        v
normal and closure regional mesoscopic simulation
        |
        v
scenario-coupled microscopic closure simulation
        |
        v
reactive routing and uncertainty ensemble
        |
        v
calibrated model
        |
        v
untouched holdout validated model
```

Each step may add realism.

None may silently claim more evidence than it has.

The authoritative physical run lifecycle is:

```text
user scenario
  -> fast NetworkX preview
  -> resolve time, closure state, and model bundle
  -> normal regional state
  -> closure regional mesoscopic run
  -> regional delta and affected-area discovery
  -> scenario entry-to-exit boundary demand
  -> one-to-one microscopic run
  -> bounded boundary expansion
  -> composed scenario costs
  -> route/corridor convergence loop
  -> bounded uncertainty ensemble
  -> percentiles and on-time probability
  -> one representative seeded playback run
```

The goal is not "make SUMO produce traffic-looking dots."

The goal is:

> Build a versioned, reproducible, locally calibrated physical traffic model that can accept arbitrary user-defined road closures and estimate how disruption propagates through the Portland-Vancouver network, while retaining Commute Help's fast planning workflow and remaining fully local, bounded, testable, and impossible to turn into a cloud bill.
