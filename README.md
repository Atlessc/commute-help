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
dependencies, and installs `backend/requirements.txt`. It does not download a
road network; graph generation belongs to Phase 1.

## Daily startup

From the repository root:

```bash
npm run dev
```

The launcher starts FastAPI and Vite together and prints the available local
and LAN URLs. Open [http://localhost:5173](http://localhost:5173). Press Control-C once to stop both
processes.

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
2. Click the exact road segment to close.
3. Confirm the affected travel direction, or choose both directions.
4. Choose a full closure, lanes remaining, or temporary speed in mph.
5. Set the trip departure and optionally give each road impact a start and end.
6. Use **Add another road section** as many times as needed.
7. Select **Compare road impacts**.

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

After calculating a route, use **How early should you leave?** to choose an
arrive-by time, arrival buffer, confidence target, and traffic evidence. The
result reports median, p85, p90, p95, likely range, on-time probability, latest
safe departure, and how the probability changes when leaving 5, 10, or 15
minutes earlier.

Until real observations are imported, the only choice is **Modeled estimate ·
no history**. This is explicitly labeled `modeled_uncalibrated` and uses a
deterministic structural variation model around the selected route's free-flow
time. It is not historical or live traffic. Selecting an imported profile
changes the label to `historically_calibrated`; every result identifies the
source, observation window, sample count, and profile version. Repeating the
same route and settings reproduces the same samples.

Open **Import historical observations** to upload a local CSV or Parquet file
of at most 50 MB. Required columns are:

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

## Model network diversion

After comparing one or more active road impacts, use **Where might traffic
divert?** to estimate relative spillover across the surrounding network. Choose
the synthetic hourly demand, number of origin/destination pairs, assignment
iterations, and endpoint dispersion radius, then select **Model network
diversion**.

The backend disperses deterministic synthetic trips around the selected route,
assigns them to the normal and impact-aware directed graphs, and compares the
resulting edge volumes. It uses estimated road capacities, incremental
all-or-nothing assignment, the method of successive averages, and an
uncalibrated BPR congestion curve. Full closures remove only selected directed
edges; lane restrictions reduce estimated capacity; temporary speeds change
route cost. The map shows modeled increases as solid orange lines and decreases
as dashed blue lines, with direction and vehicles/hour changes listed below.

Every result is labeled `modeled_uncalibrated`. The displayed vehicles/hour are
changes in the configured synthetic demand, not observed road counts. The model
does not include background regional traffic, traffic signals, queues spilling
between intersections, live conditions, or historical calibration, so use it
to compare relative diversion patterns rather than predict actual volumes.

Runs report progress and can be cancelled. Completed results are cached in
SQLite by graph version, model version, and normalized inputs; repeating the
same run can reuse the local cache. Active jobs are process-local and do not
survive a backend restart. To keep browser GeoJSON small, only the strongest
changed edges are returned for display, while summary counts and extrema cover
the full changed set.

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
  route, arrival deadline, buffer, confidence target, and profile choice are
  restored with the scenario.
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
recover, stop `npm run dev`, make one more copy of the current `data/app.db`,
then copy the chosen backup's `app.db` into the configured database path and
restart the app. Restore a matching graph manifest or rebuild the graph if the
scenario screen reports a graph-version review warning.

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
