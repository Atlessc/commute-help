# Commute Help regional demand request v2

## Purpose

Commute Help needs an independent absolute-demand input for historical MOSS replay. Its frozen 287-station PORTAL operator identifies an observable traffic effect, but it does not identify total route demand or traffic on routes outside detector support. The requested data will provide magnitude and origin/destination structure only. It will not be represented as observed route choice or copied into PORTAL prediction targets.

## Preferred source path

1. Request one current, validated existing-conditions Portland–Vancouver motor-vehicle OD export from Metro Modeling Services.
2. Ask Metro to state explicitly whether the export includes Clark County and all external/gateway zones used by the model.
3. Request RTC data only as the authoritative Clark County supplement or fallback if Metro's delivered matrix omits current Clark County detail, uses an older RTC input, or lacks sufficient Washington gateway metadata.
4. Do not merge Metro and RTC matrices unless both agencies document their shared boundary, model-year relationship, and overlap treatment.

Official request paths:

- Metro Modeling Services: <https://www.oregonmetro.gov/what-metro-does/tools-and-data/modeling-services>
- RTC demand model and data-request form: <https://rtc.wa.gov/data/travel-modeling/demand-model/>
- ODOT model and traffic-data resources: <https://www.oregon.gov/odot/planning/pages/technical-tools.aspx> and <https://www.oregon.gov/ODOT/Data/Pages/Traffic-Counting.aspx>

## Concise request text

```text
Project: Commute Help, local research on historical freeway traffic and planned-road-closure diversion.

Please provide the most recent validated existing-conditions motor-vehicle demand export that covers the Portland–Vancouver modeled region, including Clark County and external gateway zones.

Minimum requested demand fields:
- origin zone ID;
- destination zone ID;
- model period or time bucket;
- absolute vehicle trips for the period (not person trips unless documented vehicle-occupancy factors are also supplied);
- vehicle class, at least passenger/light vehicle and truck/commercial where available.

Please also provide:
- matching zone polygons or centroids and stable zone IDs;
- explicit internal versus external/gateway-zone definitions;
- model name/version, base year, network year, calendar-day class, period names, period start/end, timezone, and whether values are period totals or rates;
- treatment of intrazonal and external-to-external trips;
- whether Clark County demand is native to this model, imported from RTC, or reconciled through another process;
- assignment-network or zone-connector correspondence sufficient to map zones/gateways to a separate canonical route set;
- if readily available, assigned link volumes or path/select-link outputs for lineage review, not as a requirement for per-route demand;
- license, attribution, internal-use, redistribution, and derived-output terms;
- a data dictionary.

Preferred machine-readable formats: CSV, Parquet, OMX, MAT, or documented database export. Proprietary model packages may accompany these files but are not a substitute for a documented trip table.

Historical temporal anchor request, if maintained separately:
- finalized directional 15-minute or hourly vehicle counts for external gateways or permanent count stations for the 2024 calibration dates;
- stable count-location IDs, direction, lane scope, vehicle class, timestamp/timezone semantics, quality flags, and missing-data flags.

The project will independently audit whether any count location overlaps its frozen PORTAL operator before using it as a magnitude anchor. Counts already consumed by that operator will not be reused as independent scale evidence.
```

## Minimum accepted source package

The source package may use native agency zones. Commute Help will later derive a smaller route-compatible demand grouping; it does not request canonical-route quantities.

Required demand grain:

```text
source_id
model_version
model_year
network_year
calendar_class
period_id
period_start_local
period_end_local
timezone
origin_zone_id
destination_zone_id
vehicle_class
trip_value
trip_measure
value_basis
quality_status
```

Required zone/gateway grain:

```text
source_id
model_version
zone_id
zone_type
geometry_or_centroid
gateway_direction
network_connector_reference
```

`zone_type` must distinguish at least `internal` and `external_gateway`. `trip_measure` must resolve to vehicle trips. `value_basis` must state `period_total` or `vehicles_per_hour`, with complete period duration metadata.

## Why this is the minimum

- One regional total cannot locate loading and can move congestion to the wrong corridors.
- Gateway totals alone omit internal-to-internal traffic.
- Origin totals without destination relationships leave materially different network loading patterns equivalent.
- A zone-pair table supplies absolute magnitude and spatial distribution while leaving path choice to the frozen canonical route universe and KL materialization.
- Individual canonical-route demand is neither requested nor scientifically identified.

## Historical-date specificity

An existing-conditions OD table supplies baseline magnitude. It does not automatically supply the exact demand for a historical date and 15-minute bucket. A genuine historical replay additionally needs one preregistered temporal-scaling source that is independent of the frozen PORTAL equations. Candidate inputs are finalized external-gateway or permanent-recorder counts after an identity/non-overlap audit.

If only typical-day model periods are supplied, the artifact remains a baseline magnitude prior and must not be labeled date-specific historical demand.

## Intake gate

Before any value becomes usable:

- preserve original files and correspondence immutably;
- hash every file;
- verify units and period durations;
- reject negative trips and duplicate canonical keys;
- require complete zone foreign-key coverage;
- audit internal, external, and external-to-external coverage;
- document Oregon/Washington overlap and model-year consistency;
- verify route-universe endpoint mappability;
- verify coverage of OD groups containing zero-observable routes;
- retain missing values as unknown;
- record license and publication restrictions;
- keep the source distinct from PORTAL observation-constrained `y`.

No request has been submitted by Codex. Tyler remains the external requestor.
