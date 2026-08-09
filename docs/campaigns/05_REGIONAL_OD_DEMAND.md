# Campaign 5 — Regional origin/destination demand

## Objective

Obtain a base-year regional motor-vehicle OD matrix and matching zone geometry
for the Portland–Vancouver study area. This campaign replaces the diagnostic
road-capacity proxy demand in `regional-proxy-od-ipf-v1`.

This is a permission-and-data-request campaign. Do not scrape model portals or
guess proprietary model formats.

## Official source status

- [Metro Modeling Services](https://www.oregonmetro.gov/what-metro-does/tools-and-data/modeling-services)
  describes a four-step trip-based travel demand model with trip origins,
  destinations, modes, and time-of-day behavior. Contact information is
  provided, but an OD matrix download was not located on the public page.
- [RTC Demand Model](https://rtc.wa.gov/data/travel-modeling/demand-model/)
  documents the four-step Clark County model and provides a Data Request Form.
- [RTC Mapping Data](https://rtc.wa.gov/data/mapping-and-gis/) publishes the
  665-zone Clark County TAZ shapefile as a small public download.

The zone polygons alone are not demand. Do not start assignment with a TAZ
shapefile unless the matching productions, attractions, or OD table is also
available.

## Request specification

Send the same technical request to Metro and RTC, adjusted for agency coverage:

```text
Project: Commute Help, a local research tool for planned road-closure diversion

Requested base year: the most recent validated existing-conditions model
Geography: Portland-Vancouver modeled region, including external gateway zones
Mode: motor vehicle / auto person trips converted to vehicle trips, if available
Periods: weekday AM peak and PM peak; 15-minute or hourly detail if available

Requested files:
1. OD trip table with origin zone ID, destination zone ID, period, and vehicle trips
2. TAZ polygon or centroid geometry with the same stable zone IDs
3. external/gateway-zone definitions
4. field definitions, units, peak-period duration, and expansion factors
5. assignment network node/link crosswalk, if distributable
6. base-year modeled link volumes and speeds, if distributable
7. license, attribution, redistribution, and publication restrictions

Please state whether trip values are vehicles per hour, vehicles per period,
person trips, or daily totals, and whether truck/commercial trips are included.
```

Ask for a data dictionary before accepting a binary model package. A giant
opaque proprietary project file is technically data, in the same sense that a
sealed engine block is technically transportation.

## Local intake directory

Create a unique immutable campaign directory only after files are received:

```text
data/traffic/raw/regional-od/regional-od-YYYYMMDDTHHMMSSZ/
├── metro/
├── rtc/
├── correspondence/
├── campaign-manifest.json
└── campaign.log
```

Do not commit this directory.

## Intake manifest requirements

For every received file, record:

- agency and contact channel;
- original filename;
- received timestamp;
- byte length and SHA-256;
- stated model/base year;
- units and time-period definition;
- license or use restriction;
- whether the file contains sensitive or restricted fields;
- immutable raw path;
- normalized output path, after an adapter exists.

The intake logger must follow the repository logging contract:

```text
[2026-08-08T20:15:04.222Z] [+00:03:17.044] INFO verified metro/am_od.csv
```

## Do not normalize until these questions are answered

- Are rows vehicles, person trips, or tours?
- Are values hourly rates or totals over a multi-hour peak period?
- Are intrazonal trips present?
- Are external-to-external through trips present?
- Are commercial vehicles and trucks separate?
- Do zone IDs match the supplied geometry exactly?
- Are the Oregon and Washington models already reconciled at the bi-state
  boundary, or will overlapping trips need deduplication?
- What base-year network produced the supplied assignment volumes?

## Completion gate

This campaign is complete only when:

- at least one agency supplies a documented OD table and matching zone IDs;
- raw files have hashes and immutable provenance;
- units and peak-period durations are known;
- duplicate OD keys and negative trip values have been audited;
- zone foreign-key coverage is 100%, or every orphan is explained;
- Oregon/Washington boundary overlap is understood;
- use and redistribution terms are recorded;
- no `.part` files remain.

After that gate, implement an adapter to the canonical grain:

```text
source
model_version
base_year
period
origin_zone_id
destination_zone_id
vehicle_trips
period_duration_minutes
vehicle_class
quality_flag
```

The background-seed compiler can then replace its proxy `prior_weight` values
with imported vehicle trips while retaining the same conservation and held-out
validation machinery.

The local quarantine, normalization, and permission gate is implemented in
[Regional OD intake and validation](../REGIONAL_OD_INTAKE.md). It can be tested
with a fully synthetic fixture while agency responses are pending. Do not run
it against a received file until units, period duration, zone identity, and
release terms have been recorded in the immutable campaign manifest.
