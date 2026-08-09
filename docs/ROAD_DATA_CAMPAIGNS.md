# Road-data acquisition campaigns

This handbook turns the road-data research into five bounded, reviewable
campaigns for the Portland–Vancouver routing region. Run them in order. They
produce local source artifacts and evidence; they do not silently modify the
application graph or claim that the resulting simulation is calibrated.

Research and endpoint checks were last performed on August 8, 2026.

## Campaign order

| Order | Guide | Outcome | Network activity |
| --- | --- | --- | --- |
| 1 | [OSM network snapshot](campaigns/01_OSM_NETWORK.md) | A new, separately versioned directed road graph with calculated edge lengths | OSMnx/Overpass through the existing graph builder |
| 2 | [Authority road controls](campaigns/02_AUTHORITY_ROAD_CONTROLS.md) | Partitioned snapshots of legal speeds, regulatory signs, and signal inventories | Official ArcGIS REST queries |
| 3 | [ODOT signal plans](campaigns/03_ODOT_SIGNAL_PLANS.md) | A resumable index of ODOT signal drawings, followed by approved document downloads | Undocumented public search endpoint; permission gate before bulk downloads |
| 4 | [Operational signal timing](campaigns/04_SIGNAL_TIMING_REQUESTS.md) | Tracked records requests and immutable agency responses for critical intersections | Human submission through official agency channels |
| 5 | [Regional OD demand](campaigns/05_REGIONAL_OD_DEMAND.md) | Base-year auto trip tables, TAZ geometry, gateways, units, and use terms | Human data request to Metro and RTC |

## What these campaigns do and do not establish

After Campaigns 1 and 2, Commute Help can know the road geometry, calculated
length, legal speed where an authority publishes it, and many stop/signal
locations. Campaign 3 supplies design evidence for ODOT-owned signals.
Campaign 4 seeks operational timing records.

These are separate concepts:

- **Legal speed** is the maximum established by a posted or statutory rule.
- **Observed speed** is what the historical traffic source measured.
- **Modeled speed** is an estimate after congestion and controls are applied.
- **Road length** is calculated from the edge geometry in a projected CRS.
- **Signal inventory** proves a signal exists; it does not prove its active
  cycle, offset, phase split, or detector behavior.
- **A design plan** is not automatically the active controller program.

Do not label a route `historically_calibrated` merely because these files were
downloaded. That label requires directed edge matching and held-out validation.

## Required working assumptions

The instructions rely only on facts already established for this repository:

- The primary machine is macOS using zsh.
- Commands are run from the repository root.
- Node.js, npm, Python 3.12 in `.venv`, `curl`, `jq`, `shasum`, and
  `caffeinate` are the expected local tools.
- The committed region is
  `data/regions/portland-vancouver-v1.geojson`, whose bounds are
  `[-123.05, 45.25, -122.25, 45.85]`.
- Large/raw output belongs under ignored `data/` paths and must not be
  committed.
- All geographic API interchange is EPSG:4326: `[longitude, latitude]`.

If any prerequisite check fails, stop at that line. Do not replace a missing
tool with a different package or global install without reviewing what it is.

## Logging contract

Every supplied runner logs in this form:

```text
[2026-08-08T20:15:04.222Z] [+00:03:17.044] INFO downloaded 500/4,213 features
```

The first bracket is the wall-clock timestamp. The second is a stopwatch from
that campaign's persisted start time. Every runner writes the same lines to the
terminal and to `campaign.log`. Each campaign also has a machine-readable
manifest or event log.

The only command you type to start a runner is shaped like this:

```bash
caffeinate -i node /absolute/path/to/the-campaign-runner.mjs
```

The runner's first action is to create its directory and write a `START` log.
Network calls, retries, subprocess output, checkpoints, skips, failures, and
completion all pass through the timestamped logger. `caffeinate -i` prevents
idle system sleep while the command is running; it does not prevent you from
stopping safely with Control-C.

## Universal operating rules

1. Run only one acquisition campaign at a time.
2. Run the documented prerequisite or dry-run step first.
3. Start with the documented one-batch or index-only mode.
4. Keep concurrency at one unless an agency explicitly approves otherwise.
5. Honor `Retry-After`; back off on HTTP 429 and 5xx responses.
6. Stop on HTTP 401, 403, unexpected HTML, schema changes, or repeated errors.
7. Write downloads to `.part`, verify them, then rename atomically.
8. Never overwrite an immutable raw response. Start a new campaign ID when the
   query definition or source changes.
9. Record source URL, request parameters, retrieval time, byte count, and
   SHA-256 for every retained artifact.
10. Do not scrape Google Maps, Apple Maps, or street-view imagery.

## Before each campaign

Use a unique ID. The guides use this pattern:

```text
<source>-YYYYMMDDTHHMMSS
```

Do not use `latest` as the campaign identity. “Latest” eventually becomes an
archaeological layer, usually around the moment it is most inconvenient.

Each guide tells you where to save its standalone `.mjs` runner. Create the
file in a text editor and paste the complete code block exactly. The runner
itself creates the output directory, so no unlogged setup command is required.

## Completion gates

A campaign is complete only when:

- its log contains one `COMPLETE` line;
- its manifest says `complete`;
- no `.part` files remain;
- every retained file has a recorded SHA-256 and byte length;
- its expected and actual partition/feature counts agree;
- restart/resume produces only verified skips;
- its limitations and unresolved gaps are written down.

After the initial four road/control campaigns, the next implementation task is
confidence-scored matching of authority features and controls to directed OSM
edges, followed by profile construction and held-out validation. Campaign 5
then replaces diagnostic proxy demand with regional OD evidence.

The local implementation of that first post-campaign step is documented in
[Unmodeled-road edge priors](EDGE_MODEL_PRIORS.md). It produces an auditable
all-edge artifact and leaves background demand assignment as a separate gate.

## Primary source references

- [OpenStreetMap copyright and ODbL](https://www.openstreetmap.org/copyright)
- [PBOT Speed Limits metadata](https://www.portlandmaps.com/metadata/index.cfm?LayerID=54340&action=DisplayLayer)
- [PBOT Regulatory Signs](https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/100)
- [PBOT Traffic Signals](https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/199)
- [ODOT Posted Speed](https://gis.odot.state.or.us/arcgis1006/rest/services/transgis/catalog/MapServer/158)
- [WSDOT Legal Speed Limits](https://data.wsdot.wa.gov/arcgis/rest/services/Shared/RoadwayCharacteristicData/FeatureServer/4)
- [Clark County Road Log](https://gis.clark.wa.gov/arcgisfed/rest/services/ClarkView_Public/CountyRoadLogCRAB/MapServer)
- [ODOT Traffic Plan Search](https://ecmnet.odot.state.or.us/TrafficPlans/TrafficPlanSearch)
- [ODOT Signals](https://www.oregon.gov/odot/Engineering/Pages/Signals.aspx)
