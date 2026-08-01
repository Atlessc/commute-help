# PORTAL data campaign

Research checked on August 1, 2026.

## Access and usage findings

PORTAL describes its API as public, tokenless, and `GET`-only. Its official
examples automate CSV downloads to local files, and its project mission says
the archive exists to reduce barriers to publicly available regional
transportation data.

Primary references:

- [Getting Started with the PORTAL API](https://adus.github.io/portal-documentation/documents/gettingstarted/)
- [API Access Examples](https://adus.github.io/portal-documentation/documents/access_examples/)
- [Freeway Data endpoint](https://adus.github.io/portal-documentation/documents/freewaydata/)
- [PORTAL Downloads](https://new.portal.its.pdx.edu/downloads/)
- [PORTAL data definitions](https://adus.github.io/portal-documentation/documents/definitions/)

No PORTAL-specific terms-of-service, data-license, acceptable-use page, or
numeric API quota was found in the official site or documentation. The live
API response did not include `RateLimit`, `X-RateLimit-*`, or `Retry-After`
headers. `/robots.txt`, `/terms/`, and `/privacy/` returned HTTP 404. These
absences are **not** permission for unlimited traffic or unrestricted data
redistribution. The campaign is intended for controlled local research and
preserves source provenance. Confirm redistribution or a large recurring
download directly with `askportal@pdx.edu`.

## Endpoints used

| Purpose                  | Endpoint                            | Campaign format           |
| ------------------------ | ----------------------------------- | ------------------------- |
| Highway observations     | `/highways/api/freewaydata/`      | streamed CSV              |
| Highway ID and direction | `/highways/api/highwaymetadata/`  | JSON metadata snapshot    |
| Detector-to-station join | `/highways/api/detectormetadata/` | JSON metadata snapshot    |
| Station location         | `/highways/api/stationmetadata/`  | GeoJSON metadata snapshot |

The API overview currently spells the station route as `stationsmetadata`
in one table. The endpoint-specific documentation and live server use the
singular `/highways/api/stationmetadata/`, which is what Commute Help uses.

The freeway endpoint requires `start_date` and `end_date` for reliable access.
It accepts repeated `days_of_week` and `highway_id` parameters, plus `resolution`
and `format=csv`. The official weekday numbering is Sunday `1` through Saturday
`7`. The campaign deliberately makes one highway request at a time.

## Conservative request policy

Because no formal requests-per-minute limit is published, the script applies
these defaults:

- concurrency of exactly one
- one calendar day and one directional highway per observation request
- at least three seconds between request starts
- a 120-second request timeout
- a 25 MB response cap enforced while streaming
- five exponential-backoff retries for network failures, HTTP 429, and HTTP 5xx
- honor `Retry-After` if the server begins sending it
- do not retry HTTP 403 or other non-transient HTTP 4xx responses
- stop the campaign at the first unrecovered failure

The delay can be configured but cannot be set below one second. Before a large
or recurring campaign, ask PORTAL whether they prefer a different schedule or
can provide a formal quota.

A named campaign may span at most 1,096 calendar days. When `chunk_days` is `1`,
dates excluded by `days_of_week` are removed from the plan instead of generating
empty API requests. Longer windows should be separated into independently named
campaigns so their manifests and source assumptions remain reviewable.

## Storage and heap behavior

Every response is streamed into an atomic `.part` file and renamed only after
success. Each raw chunk has a separate normalized CSV. The script never builds
one full-campaign CSV in memory. Only one date/highway partition is aggregated
at a time, with a configured maximum number of station/time groups.

`campaign-manifest.json` records the exact request URL, status, byte length,
SHA-256 checksum, raw row count, normalized row count, rejected join count, and
completion time. Rerunning the same command hashes completed files and skips
verified chunks. An interrupted `.part` file is disposable and is replaced on
the next attempt.

All campaign output is under the ignored directory:

```text
data/traffic/campaigns/<campaign-name>/
  campaign-manifest.json
  metadata/
  raw/
  normalized/
```

## Run a campaign

Copy the committed example to an ignored local path, edit the dates and exact
highway IDs, and inspect the plan:

```bash
cp scripts/portal-campaign.example.json data/traffic/portal-campaign.json
npm run traffic:campaign -- --config data/traffic/portal-campaign.json --dry-run
```

Process one observation partition as a safe first run:

```bash
npm run traffic:campaign -- --config data/traffic/portal-campaign.json --max-chunks 1
```

Resume or finish the campaign with the same command without `--max-chunks`:

```bash
npm run traffic:campaign -- --config data/traffic/portal-campaign.json
```

Use `--force` only when intentionally replacing verified partitions. Changing
the configuration of an existing named campaign is rejected; use a new name so
the provenance remains unambiguous.

## Normalization note

PORTAL freeway CSV rows are detector/lane observations. Commute Help joins them
to stations and directions, aggregates lanes into station/time records, converts
mph to km/h, and converts the interval vehicle count to vehicles/hour. The last
conversion is based on the API data relationship documented for VMT and
confirmed in live 15-minute samples (`vmt = volume * detector influence area`).
The PORTAL documentation uses multiple volume measures, so this assumption is
recorded and must be revalidated if the upstream schema or calculations change.

The normalized partitions match Commute Help's traffic-import columns, but a
future bounded batch-import/indexing step is still needed to combine a large
campaign into one application traffic profile without loading the corpus at
once.
