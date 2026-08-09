# Unmodeled-road edge priors

This is the modeling gate between the completed road-data campaigns and the
network diversion simulation. It creates one evidence-ranked row for every
directed OSM graph edge without changing the live routing graph or SQLite.

The artifact answers four separate questions:

1. What road length, legal/explicit/inferred speed, lanes, and capacity prior
   are available for this direction?
2. Is the downstream node controlled by a signal, stop, or mini-roundabout?
3. Does the compact PORTAL profile contain historical observations for this
   edge in the weekday morning or afternoon period?
4. Which value is authoritative, explicit OSM data, a local corridor
   inference, a road-class default, or still unresolved?

## Build it

From the repository root:

```bash
npm run traffic:build-edge-priors
```

The command uses:

- `data/graphs/edges.parquet`
- `data/graphs/nodes.parquet`
- the newest complete campaign under `data/traffic/raw/road-controls/`
- the compact PORTAL edge-bucket profile, when present

It writes ignored generated data to:

```text
data/traffic/processed/edge-priors/<graph-version>/
├── edge-priors.parquet
├── authority-match-review.csv
├── quality-report.json
├── quality-report.md
└── build.log
```

Every log line contains both an ISO timestamp and a stopwatch value. The
Parquet and review CSV are written through `.part` files and renamed only after
the write succeeds.

To select inputs explicitly:

```bash
.venv/bin/python -m scripts.build_edge_priors \
  --campaign data/traffic/raw/road-controls/road-controls-20260808T214549Z \
  --traffic-profiles data/traffic/processed/portland-vancouver-core-corridor-v2-full-day/profiles/edge-bucket-profiles.parquet \
  --output data/traffic/processed/edge-priors/2026-07-31-portland-vancouver-v1
```

## Evidence hierarchy

Speed is resolved in this order:

1. a non-ambiguous authority line match;
2. an explicit OSM `maxspeed` value;
3. a consistent same-road, same-class value within a one-kilometer projected
   tile;
4. the versioned road-class default.

Authority matches require nearby, similarly oriented geometry plus road-name
or route evidence. A very strong geometric overlap can substitute for a name
match. Competing close matches with different posted speeds are withheld from
the resolved value and written to `authority-match-review.csv`.

Lane counts use explicit OSM lanes, then consistent local corridor evidence,
then the road-class default. Clark County `NumThruLanes` remains in
`authority_total_thru_lanes`; it is not divided or applied to a directed edge
without evidence about how the total is allocated by direction.

Capacity is a model prior calculated from resolved lanes and the existing
road-class per-lane capacity table. It is not a measured saturation flow.

## Control handling

- OSM control nodes are attached to incoming directed edges.
- PBOT signal points and Clark County records whose type is exactly
  `Traffic Signal` are matched to nearby graph nodes and attached to incoming
  directed edges.
- Clark County records whose sign type is exactly `Stop` are matched only as
  proximity evidence. Their approach direction is recorded as `unresolved`,
  so the model must not treat every nearby incoming edge as stopped.
- PBOT regulatory sign codes are not promoted because the local campaign
  metadata does not define what each code means.
- Signal inventory does not supply cycle length, phase split, offset,
  pedestrian timing, or detector actuation. `control_timing_available` remains
  false.

## Current verified build

The August 8, 2026 build for graph
`2026-07-31-portland-vancouver-v1` completed in about 74 seconds and produced:

- 213,927 unique directed-edge rows;
- 64,114 accepted authority speed matches;
- 15,004 authority match review rows;
- 45,555 explicit OSM speed rows;
- 4,709 same-corridor speed inferences;
- 99,549 road-class speed defaults;
- 8,510 directed edges ending at a traffic signal;
- 5,515 directed edges near official stop-sign evidence whose controlled
  approach remains unresolved;
- 281 edges with weekday AM or PM historical PORTAL evidence;
- zero nulls in resolved length, speed, lanes, or capacity;
- no remaining `.part` files.

The generated `quality-report.json` is the source of truth for a newer build;
the counts above are a dated verification record.

## What this does not do

It does not assign synthetic background traffic to the 213,646 unobserved
edges. Doing that now by copying nearby detector volumes would create or destroy
vehicles and produce an attractive but physically inconsistent heatmap.

The next gate is a demand-and-flow seed layer that conserves vehicles across
the network. It should combine the 281 historically observed edges with
origin/destination demand, road hierarchy, land-use access, and count-station
constraints, then validate normal-network assignment before any closure is
introduced. Only after that validation should dynamic reassignment and visual
spillover playback use these priors.
