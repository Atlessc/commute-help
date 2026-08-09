# PORTAL background traffic profiles

The completed local PORTAL campaign can now be converted into two selectable,
versioned background-traffic profiles without contacting PORTAL again:

- September/October weekday morning, 5:00–10:00 a.m.
- September/October weekday afternoon, 2:00–7:00 p.m.

The workflow has two gates. Matching writes a review report and does not touch
SQLite. Compilation uses only automatically accepted matches, creates a compact
edge/time artifact, and then registers the profiles in the local app database.

## 1. Rebuild the station match report

From the repository root:

```bash
npm run traffic:match-stations
```

This command:

1. scans the finalized Parquet corpus for its distinct observed station IDs;
2. reads PORTAL station and highway metadata already stored by the campaign;
3. compares each point with nearby directed OSM edges using distance, direction,
   route reference, mainline/ramp form, road class, and competing-edge evidence;
4. writes CSV, Parquet, JSON, and Markdown review artifacts; and
5. leaves SQLite unchanged.

Every console and file log entry includes an ISO timestamp and a stopwatch from
the start of that command. The persistent log is:

```text
data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-matching.log
```

Review the human-readable gate here:

```text
data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/station-matching/station-match-report.md
```

Status meanings:

- `accepted`: eligible for profile compilation;
- `review`: plausible candidate, but the evidence is ambiguous or conflicts;
- `unmatched`: no safe candidate; and
- `outside_graph_region`: observed by the source campaign but outside this
  Portland–Vancouver graph, so intentionally excluded.

The matcher never silently promotes `review` or `unmatched` rows.

## 2. Build and register the profiles

```bash
npm run traffic:build-profiles
```

This command streams only September/October weekday rows in bounded batches.
It does not load the 28-million-row campaign into memory. It keeps the 5–10 a.m.
and 2–7 p.m. windows, joins accepted station matches, aggregates typical speed
and hourly volume by directed edge and 15-minute bucket, writes a compact
Parquet artifact, and idempotently registers two records in `data/app.db`.

Its persistent log and report are:

```text
data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/profiles/profile-build.log
data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/profiles/profile-build-report.md
```

To validate the artifacts without registering them in SQLite:

```bash
npm run traffic:build-profiles -- --no-register
```

Re-running the normal command is safe. Profile and import identities are derived
from the campaign, graph, matcher, compiler, and artifact checksums, so the same
build updates its own records instead of creating duplicates.

## 3. Select a background profile

1. Start the app normally with `npm run dev` if it is not already running.
2. Build or compare a trip.
3. In **Conditions · trip timing**, open **Traffic evidence**.
4. Select **Sep/Oct weekday AM** or **Sep/Oct weekday PM**.
5. In the traffic-diversion panel, choose the corresponding **Background
   traffic** profile before running the closure simulation.

The frontend obtains these options from `GET /api/traffic/profiles`; no browser
upload is needed. A running development server sees the SQLite records
immediately.

## What the profile does and does not prove

The artifact contains historical typical speed and volume for accepted,
directed PORTAL-matched freeway/ramp edges. It is historically calibrated at
those matched edges. It is not live traffic, does not include incidents or
weather, and does not directly observe neighborhood streets.

The current diversion engine seeds the matched edges with these observations.
Unobserved edges still rely on the structural road graph and capacity model;
their background demand is not yet historically calibrated. The next modeling
gate is corridor propagation and held-out validation, so observed freeway flow
can influence connected unobserved edges without pretending PORTAL measured
them directly.
