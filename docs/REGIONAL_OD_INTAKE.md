# Regional OD intake and validation

This is the quarantine gate for Metro, RTC, or another agency's regional
origin/destination delivery. It verifies provenance, units, geography, zone
relationships, bi-state overlap status, and release terms before any supplied
value can reach background assignment.

The intake does not assume an agency filename or column layout. A small JSON
manifest explicitly maps the delivered fields to Commute Help's canonical
schema. Raw files remain immutable and ignored by Git.

## When files arrive

Create one timestamped campaign directory:

```text
data/traffic/raw/regional-od/regional-od-YYYYMMDDTHHMMSSZ/
├── metro/
├── rtc/
├── correspondence/
└── campaign-manifest.json
```

Copy the agency's original files and relevant written release terms into that
directory without editing them. Record the byte length and SHA-256 of each OD
and zone file in `campaign-manifest.json`.

Do not combine Metro and RTC tables in the raw directory. Each supplied file
must retain its own agency and model identity.

## Manifest contract

Start from this structure and replace every example value with information
from the delivery and written correspondence:

```json
{
  "schema_version": 1,
  "campaign_id": "regional-od-YYYYMMDDTHHMMSSZ",
  "agency": "metro",
  "received_at": "2026-08-20T17:30:00Z",
  "model": {
    "name": "AGENCY-PROVIDED MODEL NAME",
    "version": "AGENCY-PROVIDED VERSION",
    "base_year": 2025,
    "network_year": 2025
  },
  "files": {
    "od_table": {
      "path": "metro/ORIGINAL_OD_FILENAME.csv",
      "format": "csv",
      "bytes": 123,
      "sha256": "64 LOWERCASE HEX CHARACTERS"
    },
    "zones": {
      "path": "metro/ORIGINAL_ZONE_FILENAME.gpkg",
      "format": "gpkg",
      "bytes": 123,
      "sha256": "64 LOWERCASE HEX CHARACTERS"
    }
  },
  "column_mapping": {
    "origin_zone_id": "AGENCY ORIGIN COLUMN",
    "destination_zone_id": "AGENCY DESTINATION COLUMN",
    "period": "AGENCY PERIOD COLUMN",
    "trip_value": "AGENCY TRIP VALUE COLUMN",
    "vehicle_class": "AGENCY VEHICLE CLASS COLUMN"
  },
  "zone_column_mapping": {
    "zone_id": "AGENCY ZONE ID COLUMN",
    "zone_type": "AGENCY ZONE TYPE COLUMN"
  },
  "period_aliases": {
    "AGENCY AM LABEL": "weekday_morning",
    "AGENCY PM LABEL": "weekday_afternoon"
  },
  "units": {
    "trip_measure": "vehicle_trips",
    "value_basis": "period_total",
    "period_duration_minutes": {
      "weekday_morning": 120,
      "weekday_afternoon": 180
    }
  },
  "boundary_overlap": {
    "status": "unknown",
    "reference": "Retained correspondence or model documentation"
  },
  "license": {
    "terms_reference": "correspondence/RETAINED-TERMS-FILE",
    "internal_model_use_authorized": true,
    "source_data_redistribution": "prohibited",
    "open_source_code_publication": "not_restricted_by_data_terms",
    "derived_output_publication": "unknown",
    "attribution": "AGENCY-PROVIDED ATTRIBUTION"
  }
}
```

Supported OD table formats are `csv` and `parquet`. Supported zone formats are
`geojson`, `gpkg`, `shapefile`, and GeoParquet. If an agency sends OMX, DBF, or
a proprietary model package, retain it unchanged and add a reviewed adapter;
do not convert it manually and then describe the conversion as source data.

`trip_measure` must be confirmed as `vehicle_trips`. Person trips are blocked
until documented occupancy factors exist. `value_basis` must be either
`period_total` or `vehicles_per_hour`, and every normalized period must have a
duration in minutes.

## Run the gate

From the repository root:

```bash
npm run traffic:intake-regional-od -- \
  --campaign data/traffic/raw/regional-od/regional-od-YYYYMMDDTHHMMSSZ
```

The command writes timestamped and stopwatch-prefixed progress to the terminal
and to:

```text
data/traffic/processed/regional-od/<campaign-id>/intake.log
```

It writes:

```text
data/traffic/processed/regional-od/<campaign-id>/
├── intake-report.json
├── intake-report.md
├── od-demand.parquet
└── zones.parquet
```

Use `--validate-only` to generate the reports without normalized Parquet:

```bash
npm run traffic:intake-regional-od -- \
  --campaign data/traffic/raw/regional-od/regional-od-YYYYMMDDTHHMMSSZ \
  --validate-only
```

Writes use `.part` files and atomic rename. A blocked intake withholds both
normalized artifacts and exits with status 2. A malformed manifest, unsafe
path, unsupported format, missing file, byte mismatch, or checksum mismatch
stops before parsing and exits with status 1.

## What the gate checks

- raw file length and SHA-256;
- campaign-relative paths that cannot escape the immutable directory;
- explicit source-to-canonical column mapping;
- model name, version, base year, and network year;
- trip measure, rate/total basis, and period duration;
- numeric and nonnegative vehicle trips;
- duplicate canonical OD keys;
- nonblank origin and destination IDs;
- unique, valid zone geometries in EPSG:4326 interchange form;
- 100% origin/destination foreign-key coverage;
- identified external or gateway zones;
- external-to-external movement presence;
- retained terms and explicit internal-use authorization;
- source-data redistribution status;
- open-source code and derived-output publication status; and
- whether Oregon/Washington demand overlap is documented.

The three readiness results are deliberately separate:

- `normalization_ready`: schema, units, geography, provenance, and internal-use
  terms are sufficient to create canonical Parquet.
- `assignment_ready`: normalization passes and bi-state overlap is documented
  or not applicable.
- `publication_ready`: assignment passes and written terms allow both the
  intended code release and derived-output publication.

A source may be usable internally while publication remains blocked. The
software must preserve that distinction.

## Synthetic verification fixture

The committed fixture under
`backend/tests/fixtures/regional_od/` contains seven invented OD rows and three
invented zones. It does not contain Metro, RTC, PORTAL, household, commute, or
private-location data.

Run its tests with:

```bash
.venv/bin/python -m pytest backend/tests/test_regional_od_intake_service.py -q
```

The tests cover a passing delivery, duplicate OD keys, undocumented units and
terms, orphan zones, unknown bi-state overlap, and checksum tampering.

## What still happens after a passing intake

Do not register this artifact as a selectable background profile. The Phase 5
connector and SUMO-demand machinery is now available, but the real delivery must
still pass these gates:

1. reconcile Metro and RTC overlap and external gateways in the intake record;
2. build and review zone-to-road connectors and the demand manifest;
3. route normal-network demand with `npm run sumo:build-demand`;
4. evaluate held-out PORTAL count and speed error;
5. verify demand and node-flow conservation; and
6. disclose coverage, error, evidence level, and publication restrictions.

Until those gates pass, the data is an accepted source package, not a
historically calibrated closure-domino model.
