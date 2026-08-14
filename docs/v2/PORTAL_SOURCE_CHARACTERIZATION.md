# PORTAL historical source characterization and Phase 1.2b normalization policy

**V2 chunk:** 1.2a — characterize source data; no importer is implemented here.

**Characterization date:** 2026-08-11

**Campaign inspected:** portland-vancouver-core-corridor-v2-full-day

**Evidence boundary:** This document characterizes the checked-in local PORTAL campaign, its local metadata snapshots, the existing finalization/matching code, and representative raw files from January 2024, June 2025, and July 2026. It does not make a calibration claim, generate a calibration-v2 artifact, derive a profile, or change a runtime traffic schedule.

## Decision summary

Phase 1.2b SHALL read manifest-verified **raw** PORTAL CSV rows and construct one calibration-v2 observation per source detector/time record. It SHALL use the detector, station, lane, highway, and direction metadata snapshots that belong to that same campaign. It SHALL NOT use the existing normalized CSV's field named volume as a volume count: that field is a legacy station-level **VPH** rollup.

The raw source has a usable intrinsic observation key, but no provider-issued row ID:

~~~
PORTAL freeway-data endpoint
+ detector_id
+ starttime including its UTC offset
+ resolution
+ metadata highway_id
+ metadata station_id
~~~

That key is independent of file order and partitioning. Manifest/file hashes belong in provenance, not in the primary observation identity, because the provider's inclusive end-date behavior creates an overlapping midnight source row between adjacent weekly exports.

## Confidence labels

- **[Verified]** Directly observed in local files, manifests, generated quality reports, or code tests.
- **[Inference]** Strongly supported by several local artifacts but not accompanied by upstream field-definition text in this checkout.
- **[Unresolved]** Not established by the available local evidence. Phase 1.2b must preserve it or defer it rather than guess.

## 1. Source inventory and provenance

### Primary source set

| Item | Observed identity |
|---|---|
| Provider/API | PORTAL Highways API, https://new.portal.its.pdx.edu/highways/api/freewaydata/ |
| Local acquisition manifest | data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json |
| Acquisition schema | 2 |
| Manifest SHA-256 | 114012559515f2da0a715d83d11b5d11ffade932529e4937cc5d40f1602735f2 |
| Logical date window | 2024-01-01 through 2026-07-31 |
| Requested resolution | 00:15:00 |
| Requested PORTAL weekdays | 2, 3, 4, 5, 6 = Monday–Friday |
| Directional highway IDs | 24 |
| Verified complete chunks | 3,240 / 3,240 |
| Raw source rows | 71,626,056 |
| Local raw/normalized storage | 5.40 GiB raw / 4.59 GiB normalized, 3,240 files each |

The manifest records each chunk's request URL, logical highway/date range, raw and normalized relative paths, byte count, SHA-256, row counts, rejected metadata joins, and completion timestamp. It also pins the metadata snapshots:

| Snapshot | Endpoint | SHA-256 |
|---|---|---|
| Highways | /highways/api/highwaymetadata/ | 722855db6d4a2b3bdd59a94d263d3bd4a143787cd336b7710206757a21727a98 |
| Detectors | /highways/api/detectormetadata/ | 60a4181623322ce9ca8a812a6a34a8097fe173e13603cc491e4edbe0947537b8 |
| Stations | /highways/api/stationmetadata/ | 83aacf08f8b865738ca45160d7a47db49b938f7a12ef5eb43a1456ad18c43583 |

**[Verified]** The raw and normalized CSV headers are invariant across all 3,240 files of each kind. The campaign is a source collection with immutable per-file hashes, not a provider release with a separately published source-data version. Phase 1.2b SHALL use the manifest SHA plus endpoint and acquisition-schema version as local source-collection provenance. It SHALL NOT fabricate an upstream PORTAL dataset-release version.

### Metadata hierarchy

~~~
raw detector_id
    -> detector metadata: stationid, highwayid, lanenumber, agency_lane
        -> station metadata: point, milepost, location text, agency
        -> highway metadata: directional highway name and cardinal direction
~~~

**[Verified]** The 24 selected highway IDs have 1,336 detector definitions and 511 station IDs in metadata; the finalized campaign observed 502 station IDs. The full detector snapshot has 1,555 detectors, 620 station IDs, and one to six detector definitions per station. lanenumber and agency_lane each range from 1 through 6.

**[Inference]** A detector record is lane-associated: the existing acquisition normalizer groups detector records by station, timestamp, and directional highway, then calls that operation lane aggregation. The source metadata proves that a detector has lane-number attributes; it does not prove every detector is a single physical lane loop. V2 preserves detector and lane metadata so later validation can answer that physical question without losing the raw evidence.

## 2. Observed schemas and field dictionary

### Raw provider CSV — authoritative input for Phase 1.2b

Every inspected raw file uses this exact header:

~~~
starttime,resolution,detector_id,speed,volume,occupancy,countreadings,
delay,traveltime,vht,vmt
~~~

| Field | Observed meaning / evidence | Unit or representation | Phase 1.2b disposition |
|---|---|---|---|
| starttime | **[Verified]** ISO 8601 timestamp with -08:00 or -07:00 offset. Provider documentation stored in this repo calls it starttime; it is arranged on 15-minute boundaries. | Offset timestamp | Preserve as source timestamp and model as interval start; see time policy. |
| resolution | **[Verified]** 00:15:00 in all campaign rows and manifest configuration. | 900 seconds | Preserve and require equality with campaign resolution. |
| detector_id | **[Verified]** Identifier joining detector metadata. | Integer-like ID | Preserve as detector identity. |
| speed | **[Inference]** Existing acquisition code names the local variable speedMph, multiplies by 1.609344, and tests the result. The raw traveltime × speed / 60 relation approximately equals vmt / volume in a representative row. | mph in existing pipeline; convert to km/h only with recorded conversion | Preserve raw value/provenance; populate calibration-v2 speed_kph from mph × 1.609344. |
| volume | **[Inference]** Per-detector count over the stated resolution, not VPH. In a representative 15-minute row, vmt / volume = 0.57, matching the apparent detector influence length. | vehicles per 15-minute interval | Preserve as volume_count; derive flow_vph = volume_count × 4 only because resolution is 900 seconds. |
| occupancy | **[Inference]** Numeric percentage-scale measure. Existing finalizer checks 0–100; raw samples include values such as 1.22, zero, and occasional values above 100. No authoritative local provider definition was found. | likely percent | Preserve raw numeric value; do not silently cap, rescale, or discard it. |
| countreadings | **[Verified]** Nonblank integer-like source field; observed values include 1–51. Existing normalizer compares it with 45 expected readings for a 15-minute interval. | number of contributing readings; cadence is **[Unresolved]** | Preserve as calibration-v2 sample_count. Do not create a new low-sample threshold here. |
| delay, traveltime, vht, vmt | **[Verified]** Present in all raw CSVs. **[Inference]** the representative row is internally consistent with vmt = volume × influence length and traveltime measured in minutes. | undocumented in local source definition | Do not map them into current calibration-v2 measurement fields or use them for calibration. |

### Existing normalized CSV — evidence aid, not canonical input

Every normalized file uses this header:

~~~
station_or_segment_id,timestamp_local,timezone,direction,latitude,longitude,
speed_kph,volume,occupancy,quality_flag,source
~~~

This is generated by scripts/portal-campaign.mjs, not emitted by PORTAL. It collapses raw detector rows sharing:

~~~
station ID + starttime + directional highway direction
~~~

using:

- sum of available raw volume counts, then multiplying by 3600 / resolution;
- volume-weighted mean speed where positive volume is available, otherwise median available speed;
- arithmetic mean occupancy;
- low_sample when any member detector has fewer than half of the script's expected reading count.

Therefore its field named volume is **VPH**, despite the ambiguous name. It is neither a raw detector count nor a source-provided direct flow field. The V2 importer SHALL NOT treat it as volume_count and SHALL NOT reconstruct detector-level observations from it.

## 3. Observation grain and calendar semantics

### Exact observed grain

**[Verified]** Each raw provider row is one detector_id + starttime + resolution record. The accompanying detector metadata supplies its station and lane attributes; highway metadata supplies its direction. The campaign normalizes those detector rows into station/timestamp/direction rows, but that station rollup is a local transformation—not the source grain.

The Phase 1.2b canonical raw-observation grain is therefore:

~~~
PORTAL freeway-data endpoint
+ detector_id
+ stationid (metadata assertion)
+ highwayid + direction (metadata assertion)
+ starttime
+ resolution
~~~

It is **not** a pre-aggregated station observation, an app-edge observation, or a SUMO-edge observation.

### Time and timezone

**[Verified]** Raw timestamps carry the Pacific UTC offset: January samples are -08:00; June/July samples are -07:00. The normalized output pins timezone=America/Los_Angeles. The complete finalized corpus has 96 observed minute-of-day buckets (00:00 through 23:45) and 675 configured/observed dates.

The local PORTAL documentation records that the freeway endpoint treats end_date as an **inclusive midnight boundary**. Schema-2 acquisition requests the day after a chunk's logical end, then removes rows whose source date falls outside the chunk's inclusive logical date range. The raw weekly files visibly end at the following Monday 00:00; the next weekly chunk also starts at that same Monday 00:00.

**[Verified]** This behavior produced 141,978 excluded source-boundary rows. It is an intentional cross-file overlap, not a detector duplicate.

**Phase 1.2b time policy:**

- SHALL parse starttime as an offset-aware timestamp and preserve original offset-bearing source text in provenance.
- SHALL treat it as the 900-second interval start for calibration-v2 calendar identity. This follows the provider field name, complete 15-minute grid, and documented inclusive end boundary.
- SHALL calculate exact date and weekday in America/Los_Angeles, then verify that the source offset matches that zone at that instant.
- SHALL set interval end to interval start + 900 seconds; it SHALL NOT infer a different measurement window.
- SHALL use the manifest logical date range to reject each chunk's extra next-midnight boundary before record-ID collision handling.
- SHALL NOT use a raw source file position, row number, or filename to decide a date/weekday.

### DST and weekend evidence

**[Verified]** The campaign deliberately requested PORTAL weekdays 2–6, which are Monday–Friday under provider Sunday=1 numbering. All 675 expected weekday dates are present. Raw weekends do **not** exist in this campaign; DST transition Sundays are consequently absent as well.

**[Unresolved]** This corpus cannot demonstrate fall-back duplicate-clock behavior or spring-forward missing-clock behavior. It shows the expected -08:00 to -07:00 seasonal offset change, but no transition-day records. Phase 1.2b SHALL keep the offset in source provenance and SHALL NOT infer weekend historical profiles or a DST repair rule from this campaign.

## 4. Measurements, zeroes, and missing values

### Existing finalizer evidence

The finalized station-level corpus reports 28,140,246 rows:

| Condition in existing finalizer | Rows | Existing interpretation |
|---|---:|---|
| Missing speed_kph | 1,076,714 | not profile eligible |
| Nonpositive or >200 speed_kph | 20,547 | not profile eligible |
| Missing normalized volume | 0 | none observed |
| Negative or >20,000 normalized volume | 4,583 | not profile eligible |
| Missing normalized occupancy | 0 | none observed |
| Occupancy outside 0–100 | 16,167 | recorded as invalid but does not itself determine profile eligibility |
| quality_flag=good | 27,709,380 | locally generated |
| quality_flag=low_sample | 430,866 | locally generated |

Those thresholds and quality_flag values are local policies, not provider-native source statuses. Phase 1.2b SHALL preserve raw values and distinguish its own validation flags from provider fields.

### Zero versus missing

**[Verified]** In all three representative raw files:

- speed can be blank and can also be explicit numeric 0.0;
- volume is nonblank and has explicit zeroes;
- occupancy is nonblank and has explicit zeroes;
- countreadings is nonblank and nonzero.

| Source condition | Phase 1.2b candidate status | Required representation |
|---|---|---|
| Numeric volume 0 | accepted | volume_count=0; never mark missing solely because it is zero. |
| Numeric occupancy 0 | accepted | occupancy_percent=0; never mark missing solely because it is zero. |
| Numeric speed 0 | accepted with unresolved source-quality flag | Preserve numeric zero; do not convert it to a blank. A later quality gate decides whether it is physically usable. |
| Blank/non-numeric speed | accepted-with-missing-measurement when identity/other fields are valid | speed_kph=null and missing_fields includes speed_kph. |
| Out-of-range numeric value under a later explicit validation rule | rejected candidate, not silently repaired | Preserve original value/reason in import audit or rejected observation when identity is constructible. |
| Missing/invalid identity or failed detector/station/highway metadata join | rejected | Do not invent station, lane, direction, coordinates, or mapping. |

The calibration-v2 schema already distinguishes null from 0 and permits accepted observations with some missing measurement fields. It also permits speed_kph=0 so raw evidence does not prematurely decide zero-speed semantics.

### Quality/status taxonomy

The raw provider CSV has no quality, status, or error column. Source-quality evidence is limited to missing/non-numeric values and countreadings. Local processing adds:

- source-row rejection when detector/station/highway/location metadata cannot be joined (954 raw rows across the campaign);
- logical-chunk boundary exclusion (141,978 rows);
- normalized good or low_sample flags;
- finalizer profile_eligible based on local thresholds.

**[Verified]** There are no provider-native status codes in retained raw CSVs. Therefore Phase 1.2b SHALL NOT map a local good label to provider valid, and SHALL NOT treat low_sample as a source-documented rejection.

| Evidence condition | disposition candidate | source_status candidate | Notes |
|---|---|---|---|
| Valid identity and numeric/raw-present measurements | accepted | unknown | No source-native field proves valid. |
| Valid identity with blank speed only | accepted | missing | Measurement absence remains explicit. |
| Valid identity with explicit numeric zero speed | accepted | unknown | Add deterministic zero_speed_semantics_unresolved flag. |
| Manifest boundary row outside this source chunk's logical dates | rejected in import audit | invalid | Do not emit canonical observation from this chunk; it is expected in adjacent chunk. |
| Missing detector/station/highway/location metadata | rejected | invalid | Do not fabricate association. |
| Invalid parse, malformed timestamp, or resolution mismatch | rejected | invalid | Preserve enough audit provenance to explain rejection. |
| Legacy low_sample | no direct raw mapping | n/a | Future quality gate may set a documented threshold. |

## 5. Duplicate and overlap taxonomy

| Case | Evidence | Phase 1.2b rule |
|---|---|---|
| Multiple detectors at one station/time/direction | **[Verified]** Metadata has 1–6 detectors per station; current normalizer intentionally collapses them. | Legitimate separate detector observations. Retain separately. |
| Exact raw detector/timestamp duplicates in three widely separated samples | **[Verified]** none found. | No assumption about full-corpus absence until compiler validation scans all raw files. |
| Duplicate station/timestamp/direction records in representative normalized files | **[Verified]** none found; finalized aggregate report has zero duplicate keys. | Result of current collapse, not proof raw detector records are station-grain. |
| Overlap at next chunk's Monday midnight | **[Verified]** raw weekly files include it; 141,978 rows were removed. | Filter by manifest logical date before canonical ID construction. |
| Conflicting values with same intrinsic identity | **[Unresolved]** not globally adjudicated. | Never choose a winner silently; emit deterministic conflict/rejection report. |
| Same provider record acquired by another campaign | **[Unresolved]** possible. | Intrinsic ID stays stable; provenance lists every source reference. |

### Candidate deterministic record identity

Before hashing, Phase 1.2b SHALL form this canonical tuple:

~~~
provider = portal-highways-freewaydata
endpoint = /highways/api/freewaydata/
detector_id = decimal source detector ID
station_id = decimal metadata station ID
highway_id = decimal metadata highway ID
direction = exact metadata cardinal value
interval_start = source starttime normalized to canonical RFC 3339 UTC instant
resolution_seconds = 900
~~~

It SHALL derive a stable record ID from a length-delimited UTF-8 serialization of that tuple, for example obs.portal.<sha256>. It SHALL retain original timestamp plus manifest/file hashes separately as provenance.

The identity SHALL NOT include CSV row number, source filename/path, import order, app-edge/SUMO-edge ID, matcher score/status, or derived metrics/profiles. Same identity with different canonical raw payload hashes is a reportable collision, never a silent merge.

## 6. Network association semantics

### PORTAL station -> app graph

The station matcher is a review gate:

~~~
matcher version: portal-station-edge-matcher-v1
graph version:   2026-08-08-portland-vancouver-frozen-v2
~~~

It uses station point, direction, route reference, road form, road class, candidate distance, bearing, and ambiguity evidence.

| Status | Stations | Meaning |
|---|---:|---|
| accepted | 426 | high-confidence station -> directed app-edge association |
| review | 55 | candidate/unreviewed; not eligible for automatic use |
| unmatched | 21 | absent association; 20 outside graph and one in-region unmatched |

Phase 1.2b SHALL attach RoadNetworkAssociation only for status exactly accepted, present graph version, and source-metadata station/direction agreement. It SHALL classify review/unmatched as candidate/unreviewed or absent in audit evidence and SHALL NOT populate accepted app-edge fields from them. It SHALL preserve matcher version, report SHA, graph version, status, confidence, and review reasons in typed importer provenance/audit output.

### App graph -> SUMO

The active app-to-SUMO map identifies:

~~~
graph version:        2026-08-08-portland-vancouver-frozen-v2
SUMO network version: pv-sumo-2026-08-08-v1
map artifact SHA-256: 2db8fce3b5101a357193c58039ff5311463eb858059b164c0e31a4bcd714ad97
~~~

Of the 426 accepted station-to-app-edge associations, 425 target app edges whose SUMO relations are all accepted; one targets a review app-to-SUMO relation.

This is not yet an accepted calibration-v2 station-to-SUMO mapping. The map artifact exposes network version and checksum, but no explicit mapping_version required by AcceptedSumoEdgeAssociation; station matching and SUMO mapping remain separate review gates.

Phase 1.2b SHALL emit accepted station-to-app-edge association fields where eligible, but SHALL NOT emit accepted_sumo_association until a versioned join contract supplies every required accepted field, including explicit mapping version. A numerically accepted-looking SUMO edge must not be promoted by inference.

## 7. Corpus coverage

**[Verified]** The finalized campaign covers every configured weekday date from 2024-01-01 through 2026-07-31: 675 / 675 dates, all months/seasons in that interval, 96 15-minute buckets, 502 observed stations, and EAST/NORTH/SOUTH/WEST directions.

There are no logical weekday-date gaps in the campaign report. There are observation-quality gaps: missing/invalid speed is 3.8993% of finalized station rows, and station/detector availability varies by road. Raw weekend observations are absent because the campaign did not request them. Their absence does not authorize a modeled or inferred historical weekend product.

## 8. Locked Phase 1.2b normalization policy

### SHALL rules

1. SHALL require complete acquisition-schema-2 manifest and checksum-verify every selected raw file before parsing.
2. SHALL read raw PORTAL records, not legacy normalized station rollups or PM profiles.
3. SHALL validate header equality, required columns, resolution=00:15:00, source date within logical inclusive chunk window, and offset-aware starttime.
4. SHALL join detector -> station/highway and highway -> direction only against manifest-pinned metadata snapshots.
5. SHALL retain one observation per raw detector/time identity, preserving detector/station/lane/highway/direction/location/calendar/source references.
6. SHALL represent raw volume as volume_count in vehicles per 15-minute interval, and derive VPH as volume_count × 4 with the denominator recorded.
7. SHALL convert speed from mph to km/h with 1.609344 and preserve blank speed as missing rather than zero.
8. SHALL preserve numeric zero separately from missing for speed, volume, and occupancy.
9. SHALL preserve countreadings as source sample count; SHALL NOT manufacture sample-day count or low-sample threshold.
10. SHALL use the intrinsic record ID above and detect same-ID/different-payload collisions deterministically.
11. SHALL record artifact and record provenance: manifest/raw-file/metadata hashes, endpoint, acquisition schema, code version, and network/matcher artifacts actually used.
12. SHALL keep Monday–Friday independent; SHALL NOT create a profile or weekend historical artifact.
13. SHALL set calibration status to not_calibrated. Historical input is not historically calibrated traffic.

### SHALL NOT rules

1. SHALL NOT aggregate lanes/detectors to stations in the raw observation importer.
2. SHALL NOT interpret normalized volume as source volume count.
3. SHALL NOT average speed, occupancy, or repeated observations in the importer.
4. SHALL NOT drop an observation merely because a measurement is zero, or coerce blank to zero.
5. SHALL NOT cap invalid occupancy/speed, fill missing speeds, or pick an outlier threshold in this phase.
6. SHALL NOT use good, low_sample, or profile_eligible as provider-native statuses.
7. SHALL NOT attach app-edge/SUMO-edge mapping unless existing gate marks it accepted; SHALL NOT synthesize a SUMO mapping version.
8. SHALL NOT derive profiles, demand input, SUMO input, calibration result, baseline/scenario world, or trip probe.

## 9. Phase 1.1 handoff decisions classified

| Question | Class | Phase 1.2a conclusion |
|---|---|---|
| Lane aggregation | A | Raw rows are detector-grain; defer aggregation. Existing station rollup choices are not adopted. |
| Station aggregation | B | Later product can group raw detector records by station/time/direction after validation. |
| Speed aggregation | C | Existing weighted/median fallback is implementation choice, not source semantics. |
| Occupancy aggregation | C | Existing arithmetic mean is implementation choice; source scale/meaning remains incomplete. |
| Duplicate handling | B | Filter documented boundary overlap; retain legitimate multi-detector observations; surface conflicts. |
| Flow derivation | A | flow_vph = raw interval volume × 4 for this 900-second campaign. |
| Empty buckets | C | Campaign has all buckets overall, but station/detector availability varies. |
| Minimum sample-day threshold | D | Source has countreadings, not a profile sample-day acceptance rule. |
| Outlier handling | D | Existing range checks are local policy, not a source-established V2 rule. |
| Mapping joins | B | Attach accepted station -> app edge only; defer SUMO association pending versioned join. |
| Record-ID generation | A | Use provider/detector/station/highway/direction/offset-timestamp/resolution tuple. |

## 10. Explicit unresolved questions

1. Does PORTAL define countreadings as 20-second detector samples, and what minimum count makes an observation usable?
2. Is raw occupancy definitively percent for every agency/detector, and what do values above 100 mean?
3. Does speed=0 mean an observed stopped condition, a source sentinel, or both?
4. What are authoritative provider definitions/units for delay, traveltime, vht, and vmt?
5. Are detector metadata lane numbers physical lanes for all agencies and equipment types?
6. What later validation rule collapses multiple detector/station observations mapped to one app edge without double counting flow?
7. What explicit mapping_version certifies the joined PORTAL station -> app edge -> SUMO edge relationship?
8. What sample-day, outlier, and held-out validation policy promotes date-level observations into a candidate profile?

## 11. Evidence consulted

- data/traffic/campaigns/portland-vancouver-core-corridor-v2-full-day/campaign-manifest.json
- representative raw and normalized CSVs from 2024-01, 2025-06, and 2026-07
- campaign metadata snapshots under metadata/
- docs/PORTAL_DATA_CAMPAIGN.md
- scripts/portal-campaign.mjs and scripts/portal-campaign.test.mjs
- scripts/finalize_portal_campaign.py and existing quality report
- portal station matcher/service and station-match artifacts
- existing profile compiler/artifacts
- SUMO edge mapping service plus active network/map manifests

No generated artifact was created or modified while assembling this characterization.
