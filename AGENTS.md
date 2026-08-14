# AGENTS.md — Commute Help

## Purpose

These instructions apply to every coding agent working in this repository.

Read this file and `frontend/COMMUTE_HELP_V2_OCEAN_GAMEPLAN.md` completely before making changes. Treat that game plan as the product plan and this file as the implementation contract.

In this project, **MVP means Max Viable Product**, never Minimum Viable Product.

## Product mission

Build a polished local-first web application that lets Tyler and teammates model planned road closures, compare normal and closure-aware trips, estimate arrival reliability, save reusable scenarios, and open a chosen route in Google Maps.

The product must remain general enough for future Portland-area road closures. Do not hardcode one closure, commute, home, workplace, date range, or route.

## Non-negotiable constraints

- macOS is the primary platform.
- No Docker or Docker Compose.
- No Supabase or cloud database.
- No public hosting requirement.
- SQLite is the authoritative application database.
- OpenStreetMap is the primary routable road network.
- React + TypeScript + Vite is the frontend.
- FastAPI + Python 3.12 is the backend.
- OSMnx + NetworkX is the initial routing engine.
- MapLibre owns map rendering.
- Use plain, maintainable component-level CSS. Do not introduce Tailwind, MUI, or another design system without approval.
- Normal daily startup from the repository root is exactly:

```bash
npm run dev
```

- A one-time `npm run setup` is allowed for dependency checks and graph generation.
- Never require the user to start frontend and backend manually.
- Never hardcode private addresses or coordinates.
- Never commit `.env`, SQLite databases, raw traffic data, OSM extracts, generated graphs, or other large artifacts.

## Expected daily behavior

The root `npm run dev` command must:

1. Start FastAPI using `.venv/bin/python`.
2. Start Vite.
3. Bind the development services for host-Mac and trusted-LAN use.
4. Shut down both processes when the user presses Control-C.
5. Print clear local and LAN instructions.

Frontend API calls must use relative `/api/...` URLs. Configure Vite to proxy `/api` to FastAPI. Do not hardcode `localhost:8787` in application code because that breaks LAN clients.

## Working method

Before editing:

1. Inspect the repository and Git status.
2. Read `AGENTS.md`, `frontend/COMMUTE_HELP_V2_OCEAN_GAMEPLAN.md`, and relevant existing code.
3. Identify the current phase and its acceptance gate.
4. Preserve unrelated user changes.
5. State a short plan for nontrivial work.

While editing:

1. Work in small integrated slices.
2. Keep the application runnable after each slice.
3. Prefer finishing one real user workflow over creating many unused abstractions.
4. Add or update tests with behavior changes.
5. Use typed API schemas and frontend models.
6. Surface errors to users; do not fail silently.
7. Avoid speculative dependencies.
8. Do not replace the locked stack without explicit approval.

After editing:

1. Run the smallest relevant tests first.
2. Run the broader affected suite.
3. Verify `npm run dev` when startup behavior could be affected.
4. Verify the actual browser workflow when UI behavior changes.
5. Summarize what changed, what was tested, and any remaining limitation.

Do not commit, push, publish, expose a LAN service, delete user data, or download multi-hundred-megabyte datasets unless the user’s current request authorizes it.

## Architecture boundaries

### Frontend owns

- map interaction and presentation
- origin/destination selection workflow
- closure-editing workflow
- scenario forms and result presentation
- unsaved draft recovery
- user preferences
- Google Maps launch/copy actions using backend-produced URLs

### Backend owns

- graph loading and graph-version reporting
- map-click snapping and directed edge selection
- closure application
- baseline and alternative routing
- traffic profiles and simulation
- scenario persistence and revisions
- result caching
- Google Maps waypoint selection and URL generation
- import/export validation

Do not move authoritative routing logic into the browser.

### SQLite owns

- saved scenarios
- scenario revisions
- calculated result metadata
- traffic profile metadata
- route/simulation cache records
- app-wide settings needed by multiple browsers

IndexedDB may store drafts and browser-specific preferences only. It is not the shared scenario database.

## Source organization

Use feature-based frontend folders and service-based backend modules. Keep files focused; do not let `App.tsx` or `main.py` become the whole application.

Preferred frontend areas:

```text
frontend/src/api
frontend/src/components
frontend/src/features/map
frontend/src/features/locations
frontend/src/features/closures
frontend/src/features/scenarios
frontend/src/features/routing
frontend/src/features/results
frontend/src/stores
frontend/src/types
frontend/src/utils
frontend/src/styles
```

Preferred backend areas:

```text
backend/app/api
backend/app/core
backend/app/db
backend/app/models
backend/app/schemas
backend/app/services
backend/app/workers
backend/tests
```

## Code conventions

### Python

- Target Python 3.12.
- Add type hints to application code.
- Use Pydantic models at API boundaries.
- Keep FastAPI route handlers thin.
- Put business logic in services.
- Use `pathlib.Path` for file paths.
- Store configuration in a typed settings object.
- Use timezone-aware datetimes in `America/Los_Angeles` where local time matters.
- Prefer explicit, structured exceptions translated into safe API errors.
- Do not mutate the shared routing graph per request.

### TypeScript and React

- Keep TypeScript strict.
- Avoid `any`; document unavoidable exceptions.
- Use functional components and hooks.
- Keep server state in TanStack Query.
- Keep workflow/UI state in Zustand only where shared state is actually needed.
- Validate imported scenario JSON with Zod.
- Keep MapLibre objects behind a focused map integration layer.
- Do not represent every road as a React component.
- Use component-level CSS and shared design tokens.

### Geographic data

- GeoJSON coordinates are `[longitude, latitude]`.
- Use EPSG:4326 for API interchange unless an endpoint explicitly documents another CRS.
- Use a suitable projected CRS for distance/spatial operations.
- Use meters, seconds, km/h, and vehicles/hour internally.
- Convert to miles, minutes, and mph only for display.
- Preserve road direction and multi-edge keys.
- A visual line crossing is not automatically an intersection.

## Routing correctness rules

- Treat the graph as directed.
- A northbound closure must not close the southbound edge unless the user chose both directions.
- Full closures make affected directed edges unavailable.
- Lane restrictions and speed restrictions change costs but do not remove an edge.
- Scheduled closures apply only during their active interval.
- Never deep-copy the full graph for each route.
- A* is the normal router; compare representative results against Dijkstra in tests.
- Alternatives must be meaningfully different, not cosmetic one-block variants.
- Road access rules, one-way restrictions, bridge/tunnel topology, and private roads must be respected as far as the chosen data and engine support.
- If the engine cannot enforce a particular restriction, disclose and track the limitation instead of pretending it is supported.

## Closure identity rules

Persist enough information to rematch a closure after a graph update:

- processed edge ID
- OSM way IDs
- directed `u`, `v`, and `key`
- graph version
- direction
- road name
- geometry snapshot
- geometry fingerprint

Never silently remap a closure with low confidence. Warn the user and ask for review.

## Traffic-model honesty rules

Every travel-time result must identify its evidence level:

- `free_flow`
- `modeled_uncalibrated`
- `historically_calibrated`
- `live_external`

Do not call a synthetic result “historical.” Do not report fake precision. Include sample count, source window, profile version, and important assumptions.

For commute decisions, prioritize median, p85/p90/p95, on-time probability, and latest safe departure—not only the mean.

## Scenario rules

- Scenarios are user data, not hardcoded configuration.
- Give every scenario a schema version, graph version, revision, timestamps, and stable UUID.
- Preserve scenario revisions for shared edits.
- Use optimistic revision checks to prevent silent overwrites.
- Validate imports before writing anything.
- Keep raw imports immutable and normalized data separate.
- Never discard an unsaved browser draft without confirmation.

## Google Maps rules

- Use standard Google Maps URLs that do not require an API key.
- Generate a small set of strategic waypoints rather than every route coordinate.
- Respect browser URL and waypoint constraints.
- Include origin, destination, and driving mode.
- Warn users that Google Maps recalculates independently and may change the selected path.
- Never claim that Google Maps received or will honor the app’s private closure definitions.

## Performance rules

- Load the graph once per backend process.
- Build the spatial index once.
- Keep dynamic browser GeoJSON small.
- Query nearby roads through the backend; do not send the whole graph to the client.
- Cache using graph version, normalized scenario input, and model version.
- Keep CPU-heavy work outside the async server event loop.
- Use SQLite WAL mode and short transactions.
- Provide progress and cancellation for simulations that may take more than a few seconds.
- Profile before making large architectural changes.

## SUMO physical-simulation rules

- Preserve NetworkX as the fast planning engine; SUMO is a separate physical-simulation subsystem.
- Never run SUMO or libsumo inside the FastAPI process. Long runs use a separate worker process.
- Use one local SUMO worker by default and enforce configured time, disk, area, and experiment limits.
- Simulation inputs must be local files under approved data roots; never accept a URL as model input.
- Every run records a deterministic seed and exact SUMO, OSM source, network, demand, traffic-control, and model versions.
- Never call a SUMO result historically calibrated until its frozen model bundle passes the required validation gates.
- Never apply a closure without accepted app-edge-to-SUMO-edge mappings for every affected direction.
- Regional closure simulation precedes microscopic affected-area simulation.
- Microscopic runs default to one SUMO vehicle per modeled vehicle unless physical scaling equivalence is validated.
- Never use production regional data in committed tests; use tiny synthetic SUMO fixtures.
- Calibration campaigns must resume from completed experiment records and must not tune against frozen final-test data.

### V2 world domains and computational-reuse invariants

The V2 physical-simulation hierarchy is:

```text
historical calibration artifacts
    -> regional_baseline
        -> trip_probe
        -> regional_scenario
            -> trip_probe
```

Cost must be amortized down this hierarchy. Building and calibrating a baseline may be expensive; creating another probe against existing worlds must not repeat that regional work.

The run domains are authoritative:

- `regional_baseline` builds regional traffic without an origin, destination, selected trip, or closure.
- `regional_scenario` references one immutable parent baseline and applies directed closures or restrictions from a valid parent checkpoint. It never contains a selected-trip origin or destination.
- `trip_probe` references an already-created baseline checkpoint directly or an already-created scenario checkpoint. It injects the selected vehicle without rebuilding or mutating the referenced parent world.

Required reuse behavior:

- A new trip must not rerun a regional baseline.
- A new trip must not rerun an existing compatible scenario world.
- A new origin or destination must not rebuild regional demand.
- A new closure must reuse the latest causally valid compatible checkpoint from its parent baseline. Replaying farther back is permitted only when causal correctness requires it or no compatible later checkpoint exists.
- Multiple scenarios may share one baseline, and multiple probes may share one scenario.
- Parent baseline and scenario artifacts are immutable. Continuation, scenario, and probe workers write new child artifacts.
- `regional_baseline` identity contains regional-world inputs only, including the applicable source/model/network/demand versions, regional calendar and simulation window, scale, regional seed/RNG policy, and regional driver/traffic-control configuration.
- `regional_scenario` identity contains its parent baseline identity plus the complete scenario definition, including directed closures/restrictions, awareness and physical restriction times, scenario behavior model, and scenario seed/RNG policy.
- Checkpoint identity contains the parent world identity plus simulation time.
- `trip_probe` identity contains its parent checkpoint/world references plus origin, destination, departure time, probe configuration, routing policy, and probe RNG state.
- Trip origin, trip destination, and trip departure time must never participate in `regional_baseline` or `regional_scenario` identity.
- Reuse must be visible in manifests and result metadata through parent IDs, checkpoint/fork time, cache disposition, and avoided simulation work. A hidden fallback that recomputes a parent is not a cache hit.
- Keep temporary 100-second crash-recovery checkpoints distinct from canonical 15-minute world checkpoints. Recovery artifacts do not become reusable worlds until they pass the world promotion and resume/fork-equivalence gates.
- Baseline and scenario regional states are not required to be identical when probed. Comparisons require equivalent probe conditions: origin, destination, departure conditions, vehicle/driver configuration, and controlled probe RNG policy.

Traffic evidence and calendar identity must remain explicit:

- Historical input is not the same as historical calibration. Do not label a schedule, demand artifact, SUMO result, percentile, or world `historically_calibrated` merely because historical observations were imported.
- Weekday, exact-date, and season-matched profiles are distinct identities. Never silently pool or substitute one for another.
- Keep Monday, Tuesday, Wednesday, Thursday, and Friday distinguishable in V2 historical artifacts and baseline worlds.
- Do not create, infer, or expose historical weekend profiles until observed weekend data passes its own quality and validation gates.
- Promotion requires reproducible provenance and explicit calibration, held-out validation, and evidence gates; a successful simulation run proves execution, not traffic accuracy.

## Security and privacy

- Treat home/work locations and commute patterns as sensitive local data.
- Never log full private addresses at normal log levels.
- Never commit scenario databases or exported personal scenarios.
- Default to trusted local-network use only.
- Do not add public port forwarding, telemetry, analytics, cloud sync, or accounts without approval.
- Do not expose stack traces or filesystem paths in browser-facing errors.
- Do not add API keys when an unkeyed local approach is sufficient.

## UI expectations

The interface is for nontechnical teammates. It must:

- guide users through Trip, Closures, Conditions, Run, Compare, and Save/Navigate
- use plain language
- support keyboard and touch interaction
- show loading and simulation progress
- make directionality obvious without relying on color alone
- allow undo and recovery
- clearly distinguish baseline and closure results
- disclose whether results are free-flow, modeled, historical, or live
- remain usable on a laptop and tablet-sized screen

Do not prioritize visual flourishes over correctness, responsiveness, or a complete workflow.

## Testing gates

At minimum, maintain tests for:

- API health and startup status
- directed closure behavior
- closure scheduling
- lane/speed restrictions
- route availability and no-route handling
- scenario CRUD and revision conflicts
- scenario JSON round trips
- graph-version mismatch warnings
- Google Maps URL creation
- frontend core workflow

Add regression fixtures for every important routing bug.

Before declaring a phase complete, satisfy its gate in `frontend/COMMUTE_HELP_V2_OCEAN_GAMEPLAN.md`.

## Dependency rules

- Use the existing package manager and lockfiles.
- Ask before adding a large framework or external service.
- Keep Python dependencies in `backend/requirements.txt`.
- Keep frontend dependencies in `frontend/package.json`.
- Keep orchestration-only Node dependencies in the root `package.json`.
- Do not edit `.venv` or `node_modules` contents directly.
- Do not upgrade unrelated dependencies during feature work.

## Data and Git rules

Do not commit:

```text
.venv/
node_modules/
.env*
data/app.db
data/**/*.osm.pbf
data/**/*.graphml
data/**/*.parquet
data/traffic/raw/
data/exports/
```

Do commit small manifests, schemas, scripts, fixtures, and documentation needed to reproduce generated artifacts.

Preserve the user’s NTAD CSV as a reference dataset. Do not modify it in place and do not make routing depend on it.

## Definition of done

Work is complete only when:

1. The requested behavior works through the real UI/API path.
2. Relevant automated tests pass.
3. Startup still works through root `npm run dev` when affected.
4. Errors and limitations are visible and honest.
5. Documentation is updated.
6. No prohibited architecture or private data was introduced.
7. The app remains runnable and understandable for the next agent.

If a request conflicts with these instructions, stop and explain the conflict before changing the locked architecture.
