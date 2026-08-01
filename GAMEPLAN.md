# Commute Help — Max Viable Product Game Plan

## 1. Product definition

Commute Help is a local-first web application for evaluating how planned road closures may affect commutes and other trips in the Portland–Vancouver region.

In this project, **MVP always means Max Viable Product**: the most complete, reliable, and easy-to-use product that can be finished within the available two-week window. The product must become usable early, but the weekend checkpoint is not the final scope.

The app must allow a nontechnical teammate to:

1. Open the web app from a browser.
2. Choose an origin and destination.
3. Choose a departure time or an arrive-by deadline.
4. Select one or more road segments directly on a map.
5. Define direction, schedule, severity, lane reductions, and speed restrictions for each closure.
6. Compare the normal route against closure-aware alternatives.
7. See expected travel time, reliability, and a recommended departure time.
8. Save, reopen, duplicate, rename, export, and import scenarios.
9. Share scenarios and results with teammates using the same Mac-hosted app.
10. Open a selected route in Google Maps for live navigation.

The finished application is launched from the repository root with:

```bash
npm run dev
```

One-time installation and road-network preparation may use:

```bash
npm run setup
```

Daily use must not require Docker, a cloud database, manually starting two servers, or remembering Python commands.

## 2. Locked requirements

### Required

- macOS-native development and operation.
- React, TypeScript, and Vite frontend.
- FastAPI Python backend.
- OSMnx, NetworkX, GeoPandas, and Shapely for the first routing engine.
- OpenStreetMap as the routable road-network source.
- SQLite as the authoritative local application database.
- Complete driveable Portland–Vancouver roads, including neighborhood streets and legal service roads.
- Directed edges so northbound and southbound closures can be treated independently.
- Interactive map-based road selection.
- Baseline, closure-aware, and alternative routes.
- Local scenario persistence and automatic restoration.
- JSON scenario import/export.
- Shared scenarios for browsers using the Mac-hosted app.
- Historical traffic profile import and calibration.
- Average, median, percentile, and on-time probability results.
- Google Maps route handoff.
- Generalized support for future closures without hardcoded I-5 logic.
- Root-level `npm run dev` launcher.

### Explicitly excluded

- Supabase or another hosted database.
- Docker or Docker Compose.
- Requiring a public deployment.
- Hardcoded home, work, closure, or commute values.
- Treating the uploaded NTAD CSV as the routing network.
- Loading the whole road network into the browser as one GeoJSON document.
- Rendering each road as a React component.
- Claiming synthetic traffic is an exact real-world prediction.

### Deferred unless the core product is stable early

- Full SUMO microscopic traffic simulation.
- Public internet hosting.
- User accounts.
- Google Docs-style simultaneous scenario editing.
- Paid historical traffic providers.
- Native iOS or Android applications.

## 3. Success criteria

The Max Viable Product is complete when all of the following are true:

- `npm run dev` starts the frontend and backend together.
- The app opens without requiring Terminal commands beyond the launcher.
- The routing graph loads once and is reused across requests.
- A user can select Point A and Point B by searching or clicking the map.
- A user can select a road segment and specify its affected direction.
- Baseline routes may use the road, while closure routes never use an active fully closed directed edge.
- A one-direction closure does not incorrectly close the opposite direction.
- The app returns at least one alternative when a legal alternative exists.
- The result compares time, distance, reliability, and arrival risk.
- A saved scenario survives server and browser restarts.
- Different browsers connected to the same Mac see the same saved scenarios.
- Scenario export/import works without losing closure geometry or conditions.
- Old scenarios are checked against the current road-network version.
- Historical results disclose their data source, period, sample size, and calibration state.
- Uncalibrated results are clearly labeled as modeled estimates.
- A selected route opens in Google Maps with origin, destination, and strategic waypoints.
- Core routing, persistence, and API tests pass.

## 4. Architecture

```text
Browser on host Mac or trusted LAN
        |
        | HTTP
        v
React + TypeScript + MapLibre
        |
        | /api
        v
FastAPI application
        |
        +-- Routing service: OSMnx + NetworkX
        +-- Spatial service: GeoPandas + Shapely spatial index
        +-- Traffic model: historical profiles + stochastic sampling
        +-- Scenario service: SQLite
        +-- Export service: JSON + Google Maps URLs
        |
        v
Local files under data/
```

### Development processes

The root launcher starts:

- Vite on port `5173`.
- FastAPI/Uvicorn on port `8787`.

Vite proxies `/api` requests to FastAPI. Frontend code must call relative URLs such as `/api/routes`, not hardcode `localhost:8787`, so LAN browsers work correctly.

### Local persistence

SQLite is the shared source of truth:

```text
data/app.db
```

Use SQLite WAL mode and short transactions so multiple teammates can read scenarios concurrently. IndexedDB may preserve unsaved drafts and frontend preferences, but it must not replace SQLite for shared scenarios.

### Road-network artifacts

```text
data/
├── app.db
├── osm/
│   ├── source files
│   └── regional extracts
├── graphs/
│   ├── portland-vancouver.graphml
│   ├── nodes.parquet
│   ├── edges.parquet
│   ├── edge-spatial-index metadata
│   └── graph-manifest.json
├── traffic/
│   ├── raw/
│   ├── normalized/
│   └── traffic-profile-manifest.json
├── reference/
│   └── ntad_major_roads.csv
└── exports/
```

Large datasets and generated graphs must not be committed to Git.

## 5. Recommended repository structure

```text
commute-help/
├── AGENTS.md
├── GAMEPLAN.md
├── README.md
├── package.json
├── package-lock.json
├── .gitignore
├── .env.example
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── features/
│   │   │   ├── map/
│   │   │   ├── locations/
│   │   │   ├── closures/
│   │   │   ├── scenarios/
│   │   │   ├── routing/
│   │   │   └── results/
│   │   ├── stores/
│   │   ├── types/
│   │   ├── utils/
│   │   └── styles/
│   └── vite.config.ts
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── graph_service.py
│   │   │   ├── routing_service.py
│   │   │   ├── closure_service.py
│   │   │   ├── scenario_service.py
│   │   │   ├── traffic_service.py
│   │   │   └── google_maps_service.py
│   │   └── workers/
│   ├── tests/
│   └── requirements.txt
├── scripts/
│   ├── setup.py
│   ├── build_graph.py
│   ├── validate_graph.py
│   ├── import_traffic.py
│   └── backup.py
└── data/
```

## 6. Data strategy

### Primary routing network: OpenStreetMap

The routable graph should include legally driveable instances of:

- motorway and motorway links
- trunk and trunk links
- primary and primary links
- secondary and secondary links
- tertiary and tertiary links
- unclassified public roads
- residential streets
- living streets
- public service roads when appropriate
- legal, ordinary-vehicle tracks only when explicitly allowed

Exclude or prevent through-routing on:

- private driveways
- emergency-only roads
- parking aisles
- closed or gated roads
- pedestrian-only paths
- bicycle-only paths
- destination-only roads unless the trip starts or ends there

Small roads should exist in the graph, but routing costs must discourage unrealistic shortcuts through alleys, parking areas, and local access roads.

### Initial graph-building approach

For the fastest reliable implementation:

1. Define a versioned Portland–Vancouver polygon or buffered bounding region.
2. Use OSMnx once during setup to obtain the driveable network.
3. Preserve unsimplified identifiers while preprocessing.
4. Add speeds and free-flow travel times.
5. Normalize lane counts, road classes, surface, access, and direction.
6. Save the graph as GraphML.
7. Save nodes and edges as Parquet for spatial and analytical operations.
8. Create a graph manifest containing source date, region, build code version, and checksum.
9. Load the saved graph for daily use; do not redownload it during `npm run dev`.

Oregon and Washington PBF files may be retained as offline master sources. PBF ingestion is a later setup-path improvement and must not block the first working graph.

### Supplemental validation layers

Use, but do not directly merge into the routing graph:

- Oregon All Public Roads.
- WSDOT local public roads.
- PBOT and Metro road layers where useful.
- Census TIGER/Line All Roads.
- The uploaded NTAD major-roads CSV.

Compare these sources spatially against OpenStreetMap and flag possible missing roads. Directly merging overlapping centerlines would create duplicates and false connections.

### Graph integrity checks

The setup process must report:

- total nodes and directed edges
- strongly and weakly connected components
- percentage contained in the largest connected component
- isolated components and dead ends
- edges missing speed, lane, or name data
- invalid geometries
- suspicious zero-length or extreme-length edges
- one-way edge counts
- bridge and tunnel crossings
- sampled route success between major regional points

Crossing lines must not be connected unless they share a legitimate intersection node. Bridges, tunnels, and interchanges must remain topologically correct.

## 7. Routing design

### Internal graph model

Every routeable edge should expose at least:

```text
u
v
key
edge_id
osm_way_ids
geometry
length_m
road_name
road_class
oneway
lanes
maxspeed_kph
free_flow_seconds
estimated_capacity_vph
service_penalty
residential_penalty
surface_penalty
```

Use meters and seconds internally. Convert only for display.

### Stable closure references

Do not save a closure using only a transient NetworkX key. Store:

- processed edge ID
- OSM way ID or IDs
- directed `u`, `v`, and `key`
- road name
- direction label
- geometry snapshot
- geometry fingerprint
- graph version

When reopening an old scenario:

1. Match the saved processed edge ID.
2. Fall back to OSM way and direction.
3. Fall back to a spatial geometry match.
4. Warn the user if confidence is insufficient.

Never silently move a saved closure to a different road.

### Baseline routing

Use A* with admissible geographic-distance heuristics and free-flow or time-dependent edge weights. Dijkstra may be used as a correctness reference in tests.

### Applying closures

Do not copy the entire regional graph for every request. Pass a request-specific closure set to the weight function or operate on a lightweight graph view.

Closure effects:

- Full closure: directed edge is unavailable.
- Directional closure: only matching directed edges are unavailable.
- Lane reduction: reduce estimated capacity and increase congestion cost.
- Speed restriction: cap edge speed during the active period.
- Partial severity: combine capacity and speed adjustments.
- Scheduled closure: apply only when the route traversal time overlaps its active window.

### Alternatives

Generate meaningful alternatives rather than trivial variants that differ by one block. Candidate generation may use Yen-style K-shortest paths or repeated A* with overlap penalties.

Filter and rank candidates by:

- travel time
- distance
- percentage overlap with the best route
- residential-road exposure
- number of difficult transitions
- modeled reliability
- closure proximity

The UI should normally show three useful choices:

- Fastest expected.
- Most reliable.
- Best balanced route.

### Time-dependent weights

An edge cost should ultimately be a function of:

```text
edge
date
day of week
time bucket
traffic scenario
weather/event modifiers
active closures
modeled volume/capacity
```

The first working version may use free-flow time plus configurable road-class multipliers. Historical profiles are added without changing the route API contract.

## 8. Traffic modeling and historical calibration

### Honesty rule

Before calibration, label results as modeled estimates. After calibration, show the source period and number of observations. Never present synthetic precision as measured truth.

### Historical-data targets

Prioritize September and October weekday data for multiple recent years, with separate profiles for morning and afternoon periods.

Preferred free sources to investigate and import:

- Portland State University PORTAL freeway data.
- ODOT traffic counts and detector data.
- PBOT vehicle volume and speed counts.
- Metro regional counts.
- WSDOT freeway and count-station data for Vancouver approaches.
- NOAA weather observations.
- Public holiday, school-calendar, incident, and major-event flags.

Data adapters must accept local CSV or Parquet files so a provider website or API change does not break the product.

### Normalized observation format

```text
source
station_or_segment_id
timestamp_local
timezone
direction
volume
speed_kph
occupancy
travel_time_seconds
quality_flag
latitude
longitude
```

Map observation stations or travel-time segments to graph edges with a recorded confidence score. Keep raw data immutable and write normalized output separately.

### Profiles

Aggregate by:

- road segment or calibrated corridor
- direction
- month or seasonal window
- weekday
- five- or fifteen-minute time bucket

Store:

- observation count
- mean
- median
- standard deviation
- p10, p50, p85, p90, and p95
- missing-data rate
- quality flags

Use medians and percentiles for commute decisions; do not rely only on means.

### Initial congestion model

Estimate capacity from road class and lanes, then use a configurable volume-to-capacity function. A standard starting point is the BPR form:

```text
congested_time = free_flow_time * (1 + alpha * (volume / capacity) ^ beta)
```

Keep `alpha` and `beta` configurable and documented. They are calibration parameters, not universal truths.

### Network assignment progression

Implement in this order:

1. Single-trip closure-aware routing.
2. Batch origin-destination assignment.
3. Incremental assignment across several demand portions.
4. Method of successive averages for approximate equilibrium.
5. Stochastic demand and route-choice sampling.
6. Optional SUMO integration if needed after the product is stable.

### Commute reliability

For a chosen route and departure window, simulate or sample travel times repeatedly. Return:

- mean and median travel time
- likely range
- p85, p90, and p95 travel time
- probability of arriving by the deadline
- latest recommended departure for the selected confidence target
- value of leaving 5, 10, or 15 minutes earlier
- sample count and model/calibration label

Include user-configurable parking, walking, security, elevator, and desk-arrival buffers. “Arrive at work” should mean the user’s actual deadline, not merely reaching the destination parking lot.

## 9. Scenario model

A scenario must be data, never hardcoded application behavior.

```json
{
  "schemaVersion": 1,
  "id": "uuid",
  "name": "Example weekday closure",
  "revision": 1,
  "graphVersion": "2026-07-31-portland-vancouver-v1",
  "origin": {
    "label": "Home",
    "lat": 45.0,
    "lng": -122.0
  },
  "destination": {
    "label": "Work",
    "lat": 45.0,
    "lng": -122.0
  },
  "trip": {
    "departureTime": "07:15",
    "arrivalDeadline": "08:00",
    "confidenceTarget": 0.95,
    "parkingAndWalkingMinutes": 8
  },
  "closures": [],
  "conditions": {
    "trafficProfile": "weekday_morning",
    "trafficLevel": "historical_typical",
    "weather": "normal",
    "allowResidentialDetours": true,
    "allowServiceRoads": false,
    "simulationRuns": 100
  },
  "mapState": {},
  "createdAt": "ISO-8601",
  "updatedAt": "ISO-8601"
}
```

The database should retain scenario revisions instead of overwriting important shared work without traceability.

## 10. API contract

Initial endpoints:

```text
GET    /api/health
GET    /api/status
GET    /api/graph/manifest
GET    /api/roads/nearby
POST   /api/roads/select
POST   /api/routes/compare
POST   /api/routes/alternatives
POST   /api/routes/google-maps-url
GET    /api/scenarios
POST   /api/scenarios
GET    /api/scenarios/{id}
PUT    /api/scenarios/{id}
POST   /api/scenarios/{id}/duplicate
DELETE /api/scenarios/{id}
GET    /api/scenarios/{id}/export
POST   /api/scenarios/import
GET    /api/traffic/profiles
POST   /api/simulations
GET    /api/simulations/{id}
```

Use Pydantic request and response schemas. API errors must be structured and user-safe. A failed route must distinguish invalid input, no nearby road, no legal route, graph unavailable, and internal failure.

Long simulation requests should become jobs with progress reporting rather than holding one HTTP request open indefinitely.

## 11. User interface

### Main workflow

Use a clear step-based layout:

1. **Trip** — choose Point A, Point B, depart-at/arrive-by, and arrival buffer.
2. **Closures** — select roads and define direction, dates, times, and severity.
3. **Conditions** — choose traffic profile, weather/event modifiers, and detour preferences.
4. **Run** — calculate baseline and scenario results.
5. **Compare** — inspect routes, metrics, reliability, and spillover.
6. **Save or navigate** — save the scenario or open the selected route in Google Maps.

### Map behavior

- MapLibre owns the map.
- React owns controls and state, not individual road features.
- Use GeoJSON only for small dynamic layers such as selected closures and returned routes.
- Query the backend spatial index when the user clicks near a road.
- Show a road-selection confirmation card with name, direction, and endpoints.
- Allow undo, redo, deselect, and clear-all.
- Make opposing directions visually distinguishable.
- Preserve the map viewport in saved scenarios.

### Results

Display a direct comparison table:

```text
Metric                 Normal       Closure      Difference
Travel time            ...          ...          ...
Distance               ...          ...          ...
P95 travel time        ...          ...          ...
On-time probability    ...          ...          ...
Latest safe departure  ...          ...          ...
Residential exposure   ...          ...          ...
```

Every result must state whether it uses free-flow, synthetic, historical, or live information.

### Accessibility and usability

- Keyboard-accessible controls.
- Visible focus states.
- Color must not be the only indicator of normal versus closed routes.
- Touch-friendly controls for tablets.
- Plain language for nontechnical teammates.
- Loading progress for graph startup and simulations.
- Helpful recovery messages when no route exists.
- Never discard an unsaved scenario without confirmation.

## 12. Google Maps handoff

Generate standard Google Maps URLs without requiring an API key.

Include:

- origin
- destination
- driving mode
- a limited number of strategic waypoints

Waypoints should preserve the selected corridor rather than reproduce every vertex. Prefer:

- major interchange choices
- bridge selection
- the point where the alternative leaves the baseline
- the point where it rejoins
- important turns around the closure

Simplify and rank candidate waypoints so the URL remains within browser limits. Provide both **Open in Google Maps** and **Copy link**.

The UI must warn that Google Maps recalculates with its own road and live-traffic data and may change the locally modeled path. The app cannot transmit a private closure model directly to Google Maps.

## 13. Performance plan

- Build the regional graph once during setup.
- Load and preprocess it once per backend process.
- Maintain an in-memory spatial index for edge selection.
- Avoid per-request deep copies of the graph.
- Hash graph version, scenario inputs, and model version for caching.
- Cache deterministic route results in SQLite.
- Store analytical tables as Parquet.
- Return simplified route geometry for initial rendering, with optional full-detail retrieval.
- Cancel stale frontend requests when inputs change.
- Debounce road-click and address-search requests.
- Run CPU-heavy simulation work outside the main async event loop.
- Paginate scenarios and simulation history.
- Use SQLite indexes for scenario IDs, update time, and cache hashes.

Performance targets on the host Mac after graph startup:

- nearby-road selection: under 250 ms typical
- baseline route: under 1 second typical
- baseline plus three alternatives: under 3 seconds typical
- scenario save/load: under 300 ms typical
- cached comparison: under 500 ms typical
- long simulation: visible progress and cancellation

## 14. Team access

The host Mac is authoritative. The app is intended for the host or a trusted local network.

- `npm run dev` starts services on addresses reachable from the Mac.
- The terminal prints both the local and LAN URLs.
- Vite proxies `/api` to FastAPI.
- SQLite stores all shared scenarios.
- A lightweight editor name may be stored with scenario revisions.
- Use optimistic revision checks to prevent silent overwrites.
- Do not add public port forwarding as part of this product.
- Document the macOS firewall prompt and trusted-network requirement.

Provide backup and restore commands for `data/app.db`, scenario exports, graph manifests, and imported traffic profiles.

## 15. Testing strategy

### Backend unit tests

- full closure removes the selected directed edge
- one-direction closure preserves the opposite direction
- scheduled closures apply only during their active period
- lane and speed restrictions change costs without removing the edge
- A* agrees with Dijkstra on representative cases
- selected alternatives obey overlap thresholds
- no-route results are handled explicitly
- scenario revisions and conflict checks work
- exported and reimported scenarios remain equivalent
- Google Maps URLs are valid and remain below limits

### Graph tests

- one-way roads are respected
- freeway ramps connect correctly
- bridge and tunnel crossings are not false intersections
- representative Portland/Vancouver origin-destination pairs are routable
- selected neighborhood streets exist
- private and parking roads are not used as inappropriate shortcuts

### Frontend tests

- origin/destination workflow
- road selection and direction confirmation
- closure editing and deletion
- baseline/closure comparison rendering
- scenario save, load, duplicate, import, and export
- unsaved-change protection
- Google Maps handoff
- keyboard and mobile interaction

### End-to-end acceptance cases

Create versioned fixtures for:

- no closures
- a directional freeway closure
- a full bridge or ramp closure
- a lane-reduction scenario
- a closure that makes a destination unreachable
- multiple simultaneous closures
- an older scenario opened after a graph update

## 16. Build sequence

### Phase 0 — Foundation and launcher

Deliverables:

- repository structure
- Python virtual environment and requirements
- root `package.json` scripts
- FastAPI health endpoint
- Vite `/api` proxy
- SQLite initialization
- `.gitignore`, `.env.example`, README
- `npm run dev` starts everything

Gate: both processes start and the browser can call `/api/health` through Vite.

### Phase 1 — Local road graph

Deliverables:

- versioned region definition
- setup/build script
- complete driveable OSM graph
- travel-time and capacity defaults
- GraphML and Parquet output
- graph manifest
- validation report
- backend graph service with startup status

Gate: representative routes work and graph integrity checks pass.

### Phase 2 — Map, trip selection, and basic routing

Deliverables:

- MapLibre map
- click-to-set origin/destination
- optional address search with caching
- map snapping
- baseline route
- route geometry and summary
- clear loading/error states

Gate: a nontechnical user can calculate a normal route without editing code.

### Phase 3 — Interactive closures

Deliverables:

- nearby-road query
- directional edge selection
- selection confirmation and undo
- full, directional, lane, and speed restrictions
- closure dates and times
- closure-aware route comparison
- verification that active closures are not traversed

Gate: baseline and closure routes differ correctly, including directionality.

### Phase 4 — Alternatives and Google Maps

Deliverables:

- multiple meaningfully distinct routes
- fastest, reliable, and balanced rankings
- overlap and residential-road metrics
- strategic waypoint selection
- Open/Copy Google Maps actions
- handoff warning

Gate: chosen routes open successfully and Google is guided toward the selected corridor.

### Phase 5 — Scenario persistence and shared use

Deliverables:

- SQLite scenario tables
- scenario revisions
- save, load, duplicate, rename, archive/delete
- automatic restoration of the last scenario
- browser draft recovery
- JSON import/export
- graph-version rematching
- LAN access instructions
- backup script

Gate: two browsers can use the Mac-hosted app and see the same saved scenario.

### Phase 6 — Historical profiles and reliability

Deliverables:

- local data import workflow
- station/segment-to-edge matching
- normalized Parquet observations
- September/October weekday profiles
- morning and afternoon profiles
- data-quality report
- stochastic travel-time sampling
- p50/p85/p90/p95
- on-time probability
- latest safe departure
- arrival-buffer controls

Gate: results disclose calibration status and reproduce from versioned inputs.

### Phase 7 — Network diversion model

Deliverables:

- configurable synthetic origin-destination demand
- road capacity estimates
- incremental assignment
- method of successive averages
- modeled edge-volume changes
- spillover heatmap
- cached batch runs
- progress and cancellation

Gate: closure scenarios show plausible relative diversion and remain clearly labeled as modeled.

### Phase 8 — Product hardening

Deliverables:

- responsive and accessible interface
- performance profiling
- graceful graph/database recovery
- clean setup and troubleshooting documentation
- tests and acceptance fixtures
- backup/restore validation
- optional one-click `.command` launcher

Gate: a teammate unfamiliar with the code can start and use the app from written instructions.

## 17. Weekend target

Aim to finish Phases 0–5 during the first weekend:

- one-command startup
- local graph
- interactive map
- origin/destination
- directional closures
- baseline and closure comparison
- several alternatives
- Google Maps export
- local shared scenarios
- JSON import/export

If schedule pressure occurs, reduce visual polish before removing closure correctness, persistence, or graph validation.

## 18. Two-week target

### Days 1–2

- Foundation, root launcher, database, graph build, validation.
- Baseline map and Point A-to-B routing.

### Days 3–4

- Interactive directional closure selection.
- Baseline/closure comparisons and alternatives.
- Google Maps handoff.

### Days 5–6

- SQLite scenarios, revisions, browser draft recovery, import/export.
- LAN testing with a second browser/device.

### Days 7–9

- Historical-data adapters, normalization, quality checks, edge matching.
- September/October weekday profiles.

### Days 10–11

- Reliability sampling, percentiles, on-time probability, safe departure.
- Parking/walking buffers and morning/afternoon commute presets.

### Days 12–13

- Batch demand, capacity, diversion assignment, spillover visualization.
- Caching and performance work.

### Day 14

- Full acceptance run, accessibility, documentation, backups, recovery test.
- Freeze a working release before optional experiments.

## 19. Risk controls and fallbacks

### Road download or PBF ingestion takes too long

Build the bounded graph through OSMnx once, save it, and continue. Improve offline PBF rebuilds after the working vertical slice.

### Historical data cannot be downloaded automatically

Provide a documented local CSV import screen and adapters. Do not block routing and scenario features.

### Neighborhood speed history is unavailable

Use freeway/corridor observations to calibrate regional conditions and conservative road-class distributions for local streets. Label the limitation.

### Alternatives are nearly identical

Apply overlap penalties and corridor-level diversity filters instead of merely requesting more shortest paths.

### Simulation is slow

Reduce the regional demand sample, preprocess graph arrays, cache scenario hashes, and run jobs in worker processes. Do not freeze the API event loop.

### SQLite contention appears

Use WAL mode, short writes, revision checks, and one application-owned database layer. Do not introduce a cloud database.

### Google Maps changes the route

Improve strategic waypoint placement and display the limitation. Do not claim that Google can ingest local closures.

### Two-week scope pressure

Preserve, in order:

1. Correct graph and closure directionality.
2. Easy map workflow.
3. Scenario persistence.
4. Reliable comparisons and honest labels.
5. Google Maps handoff.
6. Historical profiles.
7. Network-wide diversion sophistication.
8. Cosmetic extras.

## 20. Definition of done for every change

A change is not done until:

- it follows `AGENTS.md`
- it is integrated into the real workflow, not left as disconnected scaffolding
- relevant tests pass
- the frontend and backend still launch through `npm run dev`
- errors are visible and understandable
- no private location is hardcoded or committed
- no large generated data is accidentally committed
- documentation is updated when behavior or setup changes
- the change has been tested from a user’s perspective

## 21. Immediate next action

After these planning files are placed in the repository, complete Phase 0 only:

1. Finish backend dependency capture.
2. Create the FastAPI application and health endpoint.
3. Add the Vite `/api` proxy.
4. Add SQLite startup with WAL mode.
5. Add root scripts so `npm run dev` starts both processes.
6. Add `.gitignore`, `.env.example`, and README startup instructions.
7. Verify `/api/health` through `http://localhost:5173/api/health`.
8. Commit the stable foundation before starting road-network work.

