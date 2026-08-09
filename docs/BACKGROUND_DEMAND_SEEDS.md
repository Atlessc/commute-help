# Background demand and flow seeds

This compiler is the normal-network calibration gate between all-edge road
priors and closure reassignment. It replaces the diversion engine's small
origin/destination demand cloud with a regional, path-based diagnostic model.

It is deliberately separate from the selectable traffic profiles and live
routing path. A successful build does not automatically make it available in
the UI.

## Run it

From the repository root:

```bash
npm run traffic:build-background-seed
```

The command reads:

- `data/traffic/processed/edge-priors/<graph-version>/edge-priors.parquet`
- `data/graphs/nodes.parquet`
- `data/graphs/graph-manifest.json`

It writes ignored generated artifacts to:

```text
data/traffic/processed/background-seeds/<graph-version>/
├── edge-flow-seeds.parquet
├── od-demand-seeds.parquet
├── validation-report.json
├── validation-report.md
└── build.log
```

Every log line includes an ISO timestamp and elapsed stopwatch. Parquet files
are written to `.part` files and promoted only after a successful write.

Optional bounded controls:

```bash
.venv/bin/python -m scripts.build_background_seed \
  --zone-count 80 \
  --maximum-od-pairs 6000
```

## Model mechanism

1. Collapse parallel directed edges to the lowest-cost routable representative
   for proxy path generation while retaining its stable edge ID.
2. Divide the projected region into five-kilometer cells.
3. Select one high-activity node from each chosen cell. Activity is a network
   proxy based on incident modeled capacity, not observed population or jobs.
4. Use spatially balanced selection to retain 80 cells across the region.
5. Generate directed OD candidates between cells 3–55 kilometers apart.
6. Route each pair deterministically using free-flow time and road-class
   access penalties.
7. Split historical detector edges into train and holdout sets by
   corridor-and-direction group.
8. Fit nonnegative OD path weights against training volumes only.
9. Evaluate held-out volume error and path coverage.
10. Assign every fitted OD weight along its whole path and verify node-flow and
    edge-total conservation.

The morning and afternoon periods are fitted separately. Holdout observations
are retained in the edge artifact but never used to change OD weights.

## Evidence and promotion gate

The result stays `modeled_uncalibrated`. A period passes the internal candidate
gate only when all of these are true:

- at least 20 held-out edges exist;
- at least 70% of held-out edges are reached by a proxy path;
- held-out volume WAPE is at most 50%;
- at least 15% of all directed graph edges carry positive proxy flow;
- flow-conservation error is no more than `0.000001` vehicles/hour.

The network-coverage rule prevents a good freeway-detector score from being
misrepresented as a usable neighborhood background model.

## Current regional result

The August 8, 2026 run produced 80 proxy zones and 5,706 OD paths in about 25
seconds.

| Period | Train WAPE | Holdout edges | Holdout coverage | Holdout WAPE | Positive-flow edges | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Weekday morning | 17.0% | 20 | 100.0% | 23.0% | 5.432% | Diagnostic only |
| Weekday afternoon | 15.8% | 128 | 99.2% | 39.1% | 5.432% | Diagnostic only |

Flow conservation passed to floating-point tolerance, but only 11,621 of
213,927 directed edges carry proxy flow. The other 202,306 edge rows are
not evidence of zero traffic; they are roads the coarse proxy path set did not
use. The machine-readable report contains the exact current counts.

Therefore this artifact is useful for:

- testing OD calibration and conservation machinery;
- checking freeway/corridor directional fit;
- evaluating import schemas for a real OD matrix;
- regression fixtures and performance measurements.

It is not yet suitable for:

- selectable production background traffic;
- neighborhood spillover claims;
- closure domino-effect playback;
- calling a result historically calibrated.

## Required remediation

The next data input must replace road-capacity proxy zones with real productions
and attractions:

1. Metro base-year auto OD trip tables by time period for Oregon-side model
   zones.
2. RTC base-year auto OD trip tables by time period for Clark County and the
   bi-state model boundary.
3. Matching TAZ polygons and stable zone identifiers.
4. External/study-area gateway zones so Columbia River and regional through
   trips are not created inside the network.
5. Ideally, modeled assignment link volumes for a second validation reference.

RTC publishes its 665-zone Clark County TAZ layer but directs model-data users
to a data request form. Metro documents that its travel models produce trip
origins, destinations, modes, and time-of-day travel; the official public page
does not expose a ready-to-download OD matrix. See
[Regional OD demand acquisition](campaigns/05_REGIONAL_OD_DEMAND.md) for the
request specification and intake gate.
