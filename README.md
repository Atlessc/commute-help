# Commute Help

Commute Help is a local-first web application for comparing normal trips with
routes affected by planned road closures in the Portland–Vancouver region. It
is designed for the host Mac and trusted devices on the same local network.

The application has completed **Phase 0: Foundation and launcher**,
**Phase 1: Local road graph**, **Phase 2: Map, trip selection and basic
routing**, **Phase 3: Interactive closures**, **Phase 4: Alternatives and
Google Maps**, **Phase 5: Scenario persistence and shared use**, and **Phase 6:
Historical profiles and reliability**, and **Phase 7: Network diversion**. A
user can model road impacts, compare corridors, estimate arrival risk, inspect
modeled network spillover, hand a route to Google Maps, and save, revise,
reopen, duplicate, archive, export, or import shared scenarios.

## Requirements

- macOS
- Node.js 20 or newer
- Python 3.12

No Docker, cloud database, or API key is required.

## First-time setup

From the repository root:

```bash
npm run setup
```

This creates or validates `.venv`, installs the root and frontend Node
dependencies, installs `backend/requirements.txt`, and verifies the pinned local
SUMO runtime. It does not download a road network. If this Mac does not have the generated graph yet, run
`npm run graph:build` once after setup.

Verify the installation and local data without changing anything:

```bash
npm run doctor
```

Inspect only the physical-simulation runtime:

```bash
npm run sumo:doctor
```

SUMO 1.27.1, `sumolib`, and `libsumo` are pinned locally. The frozen-v2 app graph
and active SUMO network share one checksum-recorded OSM source. The physical
network is ready, but the model bundle remains intentionally unavailable until
the 24/7 schedule, demand, calibration, and validation phases pass.

The physical-simulation API now supports both the committed toy fixture and a
regional baseline-versus-closure comparison:

```text
GET  /api/simulation/status
POST /api/simulation/runs
GET  /api/simulation/runs/{run_id}
POST /api/simulation/runs/{run_id}/cancel
GET  /api/simulation/runs/{run_id}/playback
```

Every run remains in an isolated worker process. Regional requests use the
active Portland–Vancouver network, compile the local 24/7 proxy demand for the
selected time, run an identical-seed no-closure baseline and closure scenario,
and return bounded physical playback frames. Results remain uncalibrated.

The active general traffic schedule is
`pv-portal-24x7-2026-08-08-v4`. It covers every 15-minute bucket and interpolates
smoothly for arbitrary local times. Monday–Friday values are compiled from the
completed PORTAL campaign. Saturday and Sunday currently reuse an unscaled
Monday–Thursday shape and are explicitly labeled `modeled_unobserved`; they are
not disguised as weekend observations.

## Record and validate local benchmark trips

Benchmark trips provide independent trip-level truth for later calibration.
They are stored in the local SQLite database using graph nodes or coarse zones,
not addresses or coordinates. Their exports and reports are written under the
Git-ignored `data/benchmarks/private/` directory.

Seed a benchmark from an existing saved scenario without copying its address,
label, or coordinates:

```bash
npm run benchmarks -- seed-from-scenario \
  --scenario-name "YOUR SAVED SCENARIO" \
  --benchmark-id "anonymous-pm-trip-v1" \
  --corridor-label "anonymous-pm-corridor" \
  --actual-seconds 2400 \
  --reported-no-traffic-seconds 1320
```

The `actual-seconds` value is the measured end-to-end trip. The optional
`reported-no-traffic-seconds` value is a separate real-world comparison, not a
synthetic model result. Use an anonymous ID and coarse corridor label because
both are stored verbatim.

Inspect, score, export, or restore the private set with:

```bash
npm run benchmarks -- list
npm run benchmarks -- score
npm run benchmarks -- export
npm run benchmarks -- import data/benchmarks/private/benchmarks.json
```

Every command logs an ISO timestamp plus elapsed stopwatch time. `score`
compares each value with the active directed graph's shortest physical
free-flow path. It allows only a small explicit tolerance—3%, or five seconds
when larger—and exits unsuccessfully if a value is materially faster than that
floor. This catches physically impossible simulation output; passing it does
not mean Portland traffic has been calibrated.

Phase 4 also defines deterministic `depart_at` and `arrive_by` backend timing
contracts. Arrive-by searches the time-dependent travel-time function for the
latest feasible departure instead of subtracting a single average duration.
These are validation primitives for the future scenario-run service, not yet a
new physical-simulation control in the browser.

## Build local 24/7 SUMO demand now

Commute Help can build regional physical demand without waiting for Metro or RTC.
It uses the existing 80-zone, 5,706-pair detector-fitted proxy matrix for spatial
trip patterns, smoothly blends its AM and PM shapes, and uses the active PORTAL
schedule to scale demand for any local date and minute.

Build a local snapshot for the simulation time you want:

```bash
npm run sumo:build-proxy-demand -- \
  --demand-version local-proxy-2026-09-15t0700-v1 \
  --departure 2026-09-15T07:00:00-07:00 \
  --duration-minutes 60 \
  --scale 1 \
  --seed 20260808
```

This works 24/7. Weekday schedule values combine observed historical input with
explicit gap filling. Weekend values remain `modeled_unobserved`. The generated
demand always remains `modeled_uncalibrated`; it is useful simulation input, not
an agency OD table or a claim of exact neighborhood trip-making behavior.

Scale 1 is the default because one SUMO vehicle must normally represent one
physical vehicle for congestion and queue formation. Larger scales are useful
for bounded pipeline checks, but they reduce physical density and cannot be used
for traffic conclusions until a separate capacity-equivalence gate passes.

Balanced stochastic rounding keeps both every OD-row error and total represented
demand within one sampling unit. For the checked 07:00 run, 81,121.820 modeled
trips became 81,100 represented trips and 1,622/1,622 SUMO vehicles routed.

## Build agency-backed SUMO demand later

The Phase 5 builder converts an `assignment_ready` Metro, RTC, or reconciled
regional OD intake into class-aware, time-distributed SUMO routes. It refuses to
use a merely normalized package when the bi-state overlap gate is still unknown.

After the real delivery passes the documented intake process, build one immutable
demand version for each requested period:

```bash
npm run sumo:build-demand -- \
  --intake data/traffic/processed/regional-od/YOUR-CAMPAIGN-ID \
  --demand-version YOUR-DEMAND-VERSION \
  --period weekday_morning \
  --start-time 07:00 \
  --scale 10 \
  --seed 20260808
```

`--scale 10` means one physical SUMO vehicle represents ten accepted OD vehicle
trips. This reduces local compute cost but also changes physical density, so the
scale is never hidden and must pass later capacity-equivalence validation. Use
`--scale 1` for one simulated vehicle per accepted vehicle when the Mac and run
window can handle it.

The command logs an ISO timestamp and stopwatch on every stage, including
30-second progress updates while `duarouter` is working. It writes ignored local
artifacts under `data/sumo/demand/<demand-version>/`:

```text
demand-manifest.json
regional.rou.xml.gz
vehicle-types.add.xml
zone-connectors.parquet
zone-connectors-review.csv
validation-report.md
build.log
duarouter.stdout.log
duarouter.stderr.log
```

The manifest binds the output to intake, zone, edge-map, and SUMO-network
checksums. It records connector policy, external gateway movement, vehicle-class
mapping, random seed, sampling scale, represented demand, conservation error,
release status, and route validation. SUMO's generated timestamp and absolute
local paths are removed from the packaged route file.

The committed synthetic fixture can verify the machinery without using agency
or private data:

```bash
npm run traffic:intake-regional-od -- \
  --campaign backend/tests/fixtures/regional_od

npm run sumo:build-demand -- \
  --intake data/traffic/processed/regional-od/synthetic-regional-od-v1 \
  --demand-version synthetic-phase5-am-v1 \
  --period weekday_morning \
  --start-time 07:00 \
  --scale 50 \
  --seed 20260808
```

The agency path is an accuracy upgrade. It does not block local proxy simulation.
Agency-backed results still remain uncalibrated until their count, speed,
capacity, gateway, and held-out validation gates pass.

## Daily startup

From the repository root:

```bash
npm run dev
```

The launcher starts FastAPI and Vite together and prints the available local
and LAN URLs. Open [http://localhost:5173](http://localhost:5173). Press Control-C once to stop both
processes.

On macOS, you can instead double-click **Commute Help.command** in Finder. It
runs first-time setup when required and then starts the same root `npm run dev`
workflow. Control-C still stops both services.

LAN access is intended only on a trusted network. If macOS asks whether Node or
Python may accept incoming connections, allow it only when LAN access is
needed. Do not configure public port forwarding.

## Configuration

Defaults work without configuration. To override them, copy `.env.example` to
`.env` and edit the local copy. `.env` and the SQLite database are ignored by
Git.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMMUTE_HELP_ENVIRONMENT` | `development` | Runtime label shown by `/api/status` |
| `COMMUTE_HELP_DATABASE_PATH` | `data/app.db` | Authoritative SQLite database path |
| `COMMUTE_HELP_GRAPH_PATH` | `data/graphs/portland-vancouver.graphml` | Generated routing graph |
| `COMMUTE_HELP_GRAPH_MANIFEST_PATH` | `data/graphs/graph-manifest.json` | Version and checksum manifest |
| `COMMUTE_HELP_TRAFFIC_PATH` | `data/traffic` | Ignored raw, normalized, and profile artifacts |
| `COMMUTE_HELP_TRAFFIC_SCHEDULE_PATH` | `data/traffic/processed/schedules/active/schedule.parquet` | Active 24/7 schedule |
| `COMMUTE_HELP_TRAFFIC_SCHEDULE_MANIFEST_PATH` | `data/traffic/processed/schedules/active/schedule-manifest.json` | Schedule provenance and checksum |
| `COMMUTE_HELP_PROXY_OD_SEED_PATH` | active graph's `od-demand-seeds.parquet` | Local detector-fitted proxy OD paths |
| `COMMUTE_HELP_PROXY_OD_REPORT_PATH` | active graph's background-seed report | Proxy model version and evidence gate |
| `COMMUTE_HELP_SUMO_RUNTIME_MODE` | `auto` | Prefer `libsumo`, with local subprocess fallback |
| `COMMUTE_HELP_SUMO_OFFLINE_ONLY` | `true` | Keep physical simulation on approved local inputs |

The frontend always calls relative `/api/...` URLs. Vite proxies those requests
to FastAPI, which keeps the application usable from LAN browsers.

## Build the regional road graph

The graph build downloads the driveable OpenStreetMap network inside the
committed `data/regions/portland-vancouver-v1.geojson` boundary. It can take
several minutes and uses substantial disk space. Generated GraphML, GeoParquet,
and Overpass cache files remain local and are ignored by Git.

```bash
npm run graph:build
```

The builder will not replace an existing graph unless the command is rerun as
`npm run graph:build -- --force`.

The builder adds explicit travel-time, lane, capacity, and penalty defaults,
preserves direction and OSM identities, then writes:

- `data/graphs/portland-vancouver.graphml`
- `data/graphs/nodes.parquet`
- `data/graphs/edges.parquet`
- `data/graphs/graph-manifest.json`
- `data/graphs/validation-report.json`

The manifest records checksums, graph version, source, region, artifact sizes,
and integrity metrics. Re-run the integrity report without downloading data:

```bash
npm run graph:validate
```

Restart `npm run dev` after a successful build. `/api/status` will report the
loaded graph version and counts, while `/api/graph/manifest` returns the
browser-safe build manifest.

## Select a trip and calculate a baseline

With `npm run dev` running, open [http://localhost:5173](http://localhost:5173):

1. Choose Point A and click the map, or use the accessible coordinate entry.
2. Choose Point B the same way.
3. Select **Calculate normal route**.

The backend snaps clicks using projected EPSG:32610 spatial indexes and routes
over the directed graph with A*. The browser receives only selected points and
the small returned route geometry—not the regional graph.

Phase 2 results are explicitly labeled `free_flow` and uncalibrated. They use
OSM direction/access data, normalized speeds, and penalties that discourage
service, residential, and unpaved shortcuts. They are not live or historical
traffic predictions.

The initial MapLibre background uses OpenStreetMap raster tiles and therefore
needs internet access to display cartography. Snapping and routing use the local
graph and remain backend-authoritative. Offline basemap packaging is not yet
implemented.

## Compare road closures and restrictions

After calculating the normal route:

1. Select **Choose closed road on map**.
2. Click every road section involved in the closure; picking stays active so
   you can add several sections without reopening the tool.
3. Select **Done selecting roads**. Both directions are selected by default
   when available; uncheck a direction for a one-way impact.
4. Choose a full closure, lanes remaining, or temporary speed in mph.
5. Set the trip departure and optionally give each road impact a start and end.
6. Give the plan a name and use **Save closure plan** to store the trip, road
   sections, directions, impacts, and schedules directly in shared SQLite.
7. Select **Compare road impacts** when you are ready to calculate the route.

Selected sections are highlighted on the map and each direction is labeled in
plain language. The backend removes fully closed directed edges, applies a
lanes-before/lanes-after multiplier for lane restrictions, and derives edge
travel time from temporary speeds. It never mutates the shared graph. Results
show the normal and impact-aware routes, their time and distance difference,
or an explicit no-route state.

Lane and speed results are explicitly labeled `modeled_uncalibrated`; they are
not live or historical traffic observations. Schedule inputs use the browser's
local time and are sent to the API as timezone-aware timestamps. A restriction
is active at its start time and inactive at its end time. When no restriction
is active at departure, the comparison says so explicitly and leaves the
baseline in effect.

Scheduling is currently evaluated at the trip's departure instant. The router
does not yet activate or expire a restriction while a trip is already in
progress.

## Compare alternatives and navigate

After calculating or comparing a route, select **Find alternative routes**.
The backend generates up to three corridors and rejects candidates sharing 85%
or more of either route's distance. Each available choice reports modeled time,
distance, overlap with the fastest route, and residential-road share:

- **Fastest** has the lowest modeled travel time.
- **Reliable proxy** favors fewer class-penalized local-road segments. This is
  an uncalibrated structural proxy, not a reliability forecast.
- **Balanced** combines modeled time, residential-road share, and separation
  from the fastest corridor.

Select a card to preview that corridor on the map. The backend then chooses up
to three spaced waypoints and produces a standard, unkeyed Google Maps URL.
Use **Open in Google Maps** or **Copy link** to hand it off.

Google Maps recalculates every trip independently. It does not receive the
app's private road restrictions and may choose another path despite the
waypoints. Alternative rankings use free-flow and uncalibrated structural
costs. The separate reliability panel calibrates travel-time variation when a
historical profile is selected; it does not change route selection.

## Estimate arrival reliability

After calculating a route, the trip-timing panel supports two questions:

- **I need to arrive by** accepts an arrival deadline, buffer, confidence
  target, and traffic evidence. It reports the latest safe departure, on-time
  probability, travel-time percentiles, and the benefit of leaving 5, 10, or
  15 minutes earlier.
- **I want to leave at** accepts a departure/start time and reports the median,
  p85, p90, and p95 arrival clock times plus the arrival time for the selected
  confidence target.

Changing the leave-at time also updates the trip departure used to evaluate
scheduled road impacts. Recompare the route after changing it when a closure
has an active-time window.

Until real observations are imported, the only choice is **Modeled estimate ·
no history**. This is explicitly labeled `modeled_uncalibrated` and uses a
deterministic structural variation model around the selected route's free-flow
time. It is not historical or live traffic. Selecting an imported profile
changes the label to `historically_calibrated`; every result identifies the
source, observation window, sample count, and profile version. Repeating the
same route and settings reproduces the same samples.

Open **Get historical traffic data** after calculating a route. The recommended
path is **Download directly from PORTAL**:

1. Choose a completed date range of at most 62 days. September 1 through
   October 31 of the latest completed fall is prefilled for the current profile
   model.
2. Choose 15-minute resolution and up to eight directional highways.
3. Select **Get traffic from PORTAL**.

No API key or manual download is required. The backend calls PORTAL's public
highway, detector, station, and highway-metadata endpoints. It restricts the
highway list to stations within the loaded road-graph region, requests weekdays
by default, combines lane detectors into one directional station observation,
converts mph to km/h, converts interval vehicle counts to vehicles/hour, and
passes the result through the normal graph-matching and profile pipeline.

The exact PORTAL request and original JSON responses are preserved under the
ignored `data/traffic/raw/<import-id>/` directory alongside the generated CSV;
normalized matched observations remain separate. Requests are rejected when
they exceed 62 days, eight highways, a future date, or a 60 MB response.

For a larger historical corpus, use the resumable streaming campaign instead
of asking the application server to hold a multi-month response:

```bash
cp scripts/portal-campaign.example.json data/traffic/portal-campaign.json
npm run traffic:campaign -- --config data/traffic/portal-campaign.json --dry-run
npm run traffic:campaign -- --config data/traffic/portal-campaign.json --max-chunks 1
npm run traffic:campaign -- --config data/traffic/portal-campaign.json
```

It downloads one date/highway partition at a time, waits three seconds between
request starts by default, streams raw CSV directly to ignored local storage,
normalizes one bounded chunk at a time, writes SHA-256 checksums and row counts,
and resumes only after verifying completed files. PORTAL publishes no numeric
API rate limit; the script therefore uses conservative sequential access and
backs off on HTTP 429/5xx responses. See
[the PORTAL campaign research and operating guide](docs/PORTAL_DATA_CAMPAIGN.md)
before changing the request policy or starting a large recurring collection.

The manual fallback accepts a local CSV or Parquet file of at most 50 MB.
Required columns are:

| Column | Requirement |
| --- | --- |
| `station_or_segment_id` | Required; an exact graph edge ID is a high-confidence match |
| `timestamp_local` | Required; ISO-8601 timestamp, preferably with UTC offset |
| `speed_kph` | Required unless `travel_time_seconds` is present |
| `travel_time_seconds` | Required unless `speed_kph` is present |
| `timezone` | Optional; defaults to `America/Los_Angeles` for naive timestamps |
| `latitude`, `longitude` | Required when the station/segment ID is not a graph edge ID |
| `direction` | Optional cardinal direction (`NB`, `southbound`, etc.) used to choose a directed edge and retained |
| `volume`, `occupancy`, `quality_flag`, `source` | Optional quality and provenance fields |

The original upload is preserved byte-for-byte under an ignored
`data/traffic/raw/<import-id>/` directory. Accepted rows are written separately
to ignored Parquet storage under `data/traffic/normalized/`; the source file is
never modified. Coordinate matches farther than 500 meters from a routable road
are rejected, and the UI reports accepted/rejected rows, matched/unmatched
station IDs, missing speed and volume percentages, and source quality flags.

Profiles use September and October weekdays only, with morning observations
from 05:00–09:59 and afternoon observations from 14:00–18:59 Pacific time.
Bad, invalid, and rejected quality flags are excluded from calibration. A
profile is bound to the current graph version; after a graph update it must be
reimported and reviewed rather than silently reused.

The committed `NTAD_North_American_Roads_...csv` is a road-reference dataset,
not timestamped traffic observations. Commute Help does not use it to claim
historical calibration.

## Run the physical regional simulation

After comparing one or more active road impacts, open **Simulation** and select
**Run physical simulation**. Step 4 sends the selected trip, every directed road
impact, its schedule and severity, and the selected departure time to the local
regional SUMO worker.

The worker builds one schedule-scaled proxy OD population, routes it through
the active SUMO network, and reuses the exact demand and seed for two mesoscopic
runs. Full closures disallow the selected directed SUMO lanes, lane restrictions
reduce usable lanes, speed restrictions cap lane speed, and scheduled impacts
activate and reopen at their declared times. Active vehicles receive dynamic
travel-time rerouting after a state change.

Playback begins paused. The selected trip follows the physical SUMO trajectory;
its traveled history stays blue and its current projected route stays teal.
Background dots are deterministic sampled SUMO vehicles, not browser-invented
traffic. Pause, restart, scrub, and 1x through 50x change playback only—the
worker always computes as quickly as the Mac allows. Road overlays compare mean
active vehicle occupancy and speed between the two runs.

Use the default 1:1 physical scale for congestion conclusions. Faster 5:1,
10:1, and 25:1 modes are visibly marked as previews because reducing physical
density changes queue formation. The worker enforces the active graph's hard
free-flow floor and rejects physically impossible selected-trip results.

Time-specific routed demand is checksum-verified and cached locally under
`data/sumo/demand/runtime-cache/`. Runs report progress and can be cancelled.
The browser receives at most 900 displayed background vehicles per frame and
the 300 strongest changed roads; it never receives the full regional vehicle
population.

The result is `modeled_uncalibrated`. The local 80-zone proxy captures a
detector-fitted regional pattern but is not an observed regional OD table.
Mesoscopic playback does not yet provide affected-area microscopic signal,
merge, or lane-changing fidelity, and one run does not provide p85/p90/p95 or
on-time probability. Those claims remain blocked until the documented
calibration, microscopic coupling, and ensemble gates pass.

## Save and share scenarios

The **Save and share this plan** panel is available before choosing a new trip,
so another browser can immediately open scenarios stored by teammates. SQLite
is authoritative for shared scenarios:

- Saving an existing scenario creates a new immutable revision. A stale browser
  receives a revision-conflict error instead of silently overwriting changes.
- Rename by editing the scenario name and saving the next revision.
- **Duplicate** creates a separate scenario at revision 1.
- **Archive** hides a scenario from the active list after confirmation without
  deleting its revision history.
- **Export** downloads versioned JSON. **Import** validates that JSON with Zod in
  the browser and Pydantic in the API before writing anything; the original
  imported payload is retained separately from the normalized scenario row.
- The last opened scenario is restored automatically in that browser.
- Unsaved planning state is stored only in browser IndexedDB. After a reload,
  the app asks whether to recover or discard it and never silently discards it.
- The saved map viewport, trip endpoints, restrictions, schedules, selected
  route, timing mode, arrival deadline or departure start, buffer, confidence
  target, and profile choice are restored with the scenario.
- Synthetic demand, demand-pair count, assignment iterations, and endpoint
  dispersion settings are also restored. Calculated diversion results are
  recomputed or retrieved from the shared SQLite cache.

Closure snapshots retain the processed edge ID, OSM way IDs, directed
`u`/`v`/`key`, graph version, road name, geometry, and geometry fingerprint.
After a graph update, the backend rematches only when OSM identity, geometry,
and direction provide a unique high-confidence match. Otherwise it marks the
scenario **review required** and does not apply the old closures.

All browsers must connect to the same running Mac to share scenarios. Use only
the LAN URL printed by `npm run dev`, only on a trusted network, and do not add
public port forwarding. The host Mac's SQLite database remains authoritative.

## Backup and recovery

Create a consistent SQLite backup plus the graph manifest, traffic-profile
manifest, and local exports:

```bash
npm run backup
```

Backups are written under ignored `data/backups/<timestamp>/` directories. To
verify a backup before depending on it:

```bash
npm run backup:verify -- data/backups/<timestamp>
```

Every new backup records SHA-256 checksums and file sizes, and verification also
runs SQLite `quick_check` on the copied database. To recover, stop `npm run dev`,
verify the selected backup, make one more copy of the current `data/app.db`, then
copy the verified backup's `app.db` into the configured database path and
restart the app. Restore a matching graph manifest or rebuild the graph if the
scenario screen reports a graph-version review warning. Never overwrite a live
database while Commute Help is running.

## Troubleshooting

Start with the read-only diagnostic command:

```bash
npm run doctor
```

It checks the Python environment, root and frontend Node dependencies, SQLite
integrity, graph manifest, graph size, and graph checksum. Its recovery messages
do not expose private addresses or scenario contents.

| Symptom | Recovery |
| --- | --- |
| Setup says Python is wrong | Install Python 3.12, remove only the project `.venv`, then rerun `npm run setup`. |
| Browser says the road graph is unavailable | Run `npm run graph:build`, then restart. If artifacts already exist, run `npm run graph:validate` before rebuilding with `--force`. |
| Doctor reports a SQLite integrity failure | Stop the app and restore only from a backup that passes `npm run backup:verify -- <backup-directory>`. |
| Port 5173 or 8787 is already in use | Return to the other Commute Help terminal and press Control-C, then run `npm run dev` once. |
| Map streets are blank but routes still calculate | The internet basemap tiles are unavailable; snapping and routing still use the local graph. |
| A saved closure needs review after a graph update | Open the scenario warning and reselect uncertain roads; low-confidence rematches are intentionally never applied. |

### Graph startup performance

The road graph is verified, loaded, projected, and spatially indexed once when
FastAPI starts. Until that finishes, the readiness banner may remain visible;
routing requests do not repeat this work. A local profile on August 1, 2026,
using the current 85,210-node and 213,927-edge regional graph took about 19
seconds and briefly peaked near 4.2 GB of memory. Graph size and Mac hardware
will change those numbers, so allow extra startup time and memory after a graph
rebuild rather than repeatedly restarting the launcher.

## Checks

Run all current checks:

```bash
npm test
```

With the application running, the Phase 0 health gate is:

```bash
curl http://localhost:5173/api/health
```

The response reports both API and database readiness. `/api/status` reports
whether the routing graph is absent, ready, or failed validation/loading.

## Data and privacy

SQLite data, private environment settings, generated graphs, OSM extracts,
traffic data, and exports are local artifacts and must not be committed. Home
and work locations are treated as sensitive data.

See [GAMEPLAN.md](GAMEPLAN.md) for the complete Max Viable Product plan and
[AGENTS.md](AGENTS.md) for the implementation contract.
