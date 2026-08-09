# Campaign 2 — Authority speeds, signs, and signal inventories

## Goal

Create immutable, partitioned snapshots of the official GIS layers that can
enrich the OSM graph:

- Portland legal speed segments
- ODOT posted-speed segments
- WSDOT legal-speed segments
- Clark County road-log segments
- Portland regulatory signs
- Portland traffic signals
- Clark County signs and traffic signals

The runner restricts spatial queries to the committed Portland–Vancouver
bounding envelope. Each ArcGIS layer is enumerated by object ID, downloaded in
bounded source-native batches, hashed, checkpointed, and safely resumable.
Seven layers return GeoJSON. ODOT posted speed is retained as native Esri JSON
because its measured polylines fail the server's advertised GeoJSON encoder;
the native response includes geometry in EPSG:4326.

## Evidence boundary

- The datasets have different owners and update schedules.
- Portland's speed layer excludes freeways and associated ramps; ODOT fills
  many of those Oregon gaps.
- WSDOT's layer covers state routes, not every Vancouver city street.
- Clark County's road log covers county-maintained roads and should not be
  treated as a complete Vancouver municipal inventory.
- Sign/signal points are not yet matched to directed road approaches.
- A signal point does not contain the active timing plan.

### Known Vancouver municipal-speed gap

No authoritative Vancouver-wide municipal speed-limit polyline endpoint was
identified. After the public layers finish, ask Vancouver Traffic Engineering
or use the [City public-records process](https://www.cityofvancouver.us/government/public-records-request/)
for this existing record:

> Current GIS road-centerline or road-segment inventory containing posted speed
> limits, segment identifiers, effective dates, direction where applicable, and
> street classification for City-maintained streets. GeoPackage, File
> Geodatabase, Shapefile, or GeoJSON preferred.

Record this as an unresolved coverage gap until a response arrives. Washington's
statutory 25 mph city-street baseline is not a substitute for posted arterial
limits.

## Source endpoints

| Local name | Official layer |
| --- | --- |
| `pbot_speed_limits` | `https://www.portlandmaps.com/od/rest/services/COP_OpenData_Transportation/MapServer/225` |
| `odot_posted_speed` | `https://gis.odot.state.or.us/arcgis1006/rest/services/transgis/catalog/MapServer/158` |
| `wsdot_legal_speed` | `https://data.wsdot.wa.gov/arcgis/rest/services/Shared/RoadwayCharacteristicData/FeatureServer/4` |
| `clark_county_road_log` | `https://gis.clark.wa.gov/arcgisfed/rest/services/ClarkView_Public/CountyRoadLogCRAB/MapServer/0` |
| `pbot_regulatory_signs` | `https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/100` |
| `pbot_traffic_signals` | `https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/199` |
| `clark_county_signs` | `https://gis.clark.wa.gov/arcgisfed2/rest/services/MapsOnline/TrafficCollisions/MapServer/12` |
| `clark_county_signals` | `https://gis.clark.wa.gov/arcgisfed2/rest/services/MapsOnline/TrafficCollisions/MapServer/14` |

## Step 1: create the runner

Create this ignored local file in a text editor:

```text
data/traffic/raw/authority-road-controls.mjs
```

Paste the complete code. Replace `REPLACE_WITH_CAMPAIGN_ID`. Leave
`MAX_RETAINED_BATCHES_PER_SOURCE` at `1` for the smoke run. This is a cap on
the total retained batches, so restarting the smoke run cannot quietly add a
second batch.

```js
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const CAMPAIGN_ID = "REPLACE_WITH_CAMPAIGN_ID";
const MAX_RETAINED_BATCHES_PER_SOURCE = 1; // Change to null only after the smoke-run gate passes.
const REQUEST_DELAY_MS = 3000;
const REQUEST_TIMEOUT_MS = 120000;
const MAX_RETRIES = 5;
const MAX_RESPONSE_BYTES = 50 * 1024 * 1024;
const BATCH_SIZE = 500;
const REGION = "-123.05,45.25,-122.25,45.85";

const SOURCES = [
  ["pbot_speed_limits", "https://www.portlandmaps.com/od/rest/services/COP_OpenData_Transportation/MapServer/225"],
  ["odot_posted_speed", "https://gis.odot.state.or.us/arcgis1006/rest/services/transgis/catalog/MapServer/158", "json"],
  ["wsdot_legal_speed", "https://data.wsdot.wa.gov/arcgis/rest/services/Shared/RoadwayCharacteristicData/FeatureServer/4"],
  ["clark_county_road_log", "https://gis.clark.wa.gov/arcgisfed/rest/services/ClarkView_Public/CountyRoadLogCRAB/MapServer/0"],
  ["pbot_regulatory_signs", "https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/100"],
  ["pbot_traffic_signals", "https://www.portlandmaps.com/arcgis/rest/services/Public/PBOT_Assets/MapServer/199"],
  ["clark_county_signs", "https://gis.clark.wa.gov/arcgisfed2/rest/services/MapsOnline/TrafficCollisions/MapServer/12"],
  ["clark_county_signals", "https://gis.clark.wa.gov/arcgisfed2/rest/services/MapsOnline/TrafficCollisions/MapServer/14"],
].map(([name, layerUrl, format = "geojson"]) => ({ name, layerUrl, format }));

if (CAMPAIGN_ID.startsWith("REPLACE_")) throw new Error("Set CAMPAIGN_ID before running.");
const ROOT = process.cwd();
const DIR = resolve(ROOT, "data/traffic/raw/road-controls", CAMPAIGN_ID);
const STATE_PATH = resolve(DIR, "campaign-manifest.json");
const LOG_PATH = resolve(DIR, "campaign.log");
mkdirSync(DIR, { recursive: true });
const state = existsSync(STATE_PATH) ? JSON.parse(readFileSync(STATE_PATH, "utf8")) : {
  schemaVersion: 1,
  campaignId: CAMPAIGN_ID,
  startedAt: new Date().toISOString(),
  status: "running",
  region: REGION,
  sources: {},
};
const startMs = Date.parse(state.startedAt);
let lastRequestAt = 0;

function stopwatch() {
  const n = Math.max(0, Date.now() - startMs);
  return `${String(Math.floor(n / 3600000)).padStart(2,"0")}:${String(Math.floor(n / 60000)%60).padStart(2,"0")}:${String(Math.floor(n / 1000)%60).padStart(2,"0")}.${String(n%1000).padStart(3,"0")}`;
}
function log(level, message) {
  const line = `[${new Date().toISOString()}] [+${stopwatch()}] ${level} ${message}`;
  console.log(line);
  appendFileSync(LOG_PATH, `${line}\n`);
}
function atomicJson(path, value) {
  const part = `${path}.part`;
  writeFileSync(part, `${JSON.stringify(value, null, 2)}\n`);
  renameSync(part, path);
}
function saveState(patch = {}) {
  Object.assign(state, patch, { updatedAt: new Date().toISOString() });
  atomicJson(STATE_PATH, state);
}
function sha256(buffer) { return createHash("sha256").update(buffer).digest("hex"); }
function sleep(ms) { return new Promise(resolvePromise => setTimeout(resolvePromise, ms)); }

async function fetchBounded(url, label, options = {}) {
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const delay = Math.max(0, REQUEST_DELAY_MS - (Date.now() - lastRequestAt));
    if (delay) await sleep(delay);
    lastRequestAt = Date.now();
    const requestTarget = new URL(url);
    log("INFO", `REQUEST ${label} method=${options.method ?? "GET"} attempt=${attempt + 1} ${requestTarget.origin}${requestTarget.pathname}`);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: { "User-Agent": "Commute-Help/road-controls local research", ...options.headers },
      });
      const retryAfter = Number(response.headers.get("retry-after") ?? 0) * 1000;
      if ((response.status === 429 || response.status >= 500) && attempt < MAX_RETRIES) {
        log("WARN", `RETRY ${label} http=${response.status}`);
        await sleep(retryAfter || 1000 * 2 ** attempt);
        continue;
      }
      if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      if (bytes.length > MAX_RESPONSE_BYTES) throw new Error(`${label} exceeded ${MAX_RESPONSE_BYTES} bytes`);
      const contentType = response.headers.get("content-type") ?? "";
      if (contentType.includes("text/html")) throw new Error(`${label} returned HTML instead of data`);
      log("INFO", `RESPONSE ${label} bytes=${bytes.length}`);
      return bytes;
    } catch (error) {
      if (attempt >= MAX_RETRIES || /HTTP 4\d\d/.test(error.message)) throw error;
      log("WARN", `RETRY ${label} error=${error.message}`);
      await sleep(1000 * 2 ** attempt);
    } finally { clearTimeout(timer); }
  }
  throw new Error(`${label} exhausted retries`);
}

function spatialParams(extra = {}) {
  return new URLSearchParams({
    where: "1=1",
    geometry: REGION,
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    ...extra,
  });
}

async function downloadSource(source) {
  const sourceDir = resolve(DIR, source.name);
  mkdirSync(sourceDir, { recursive: true });
  const sourceState = state.sources[source.name] ?? { status: "running", batches: {} };
  state.sources[source.name] = sourceState;

  const metadataPath = resolve(sourceDir, "metadata.json");
  let metadataBytes;
  if (existsSync(metadataPath) && sourceState.metadata) {
    metadataBytes = readFileSync(metadataPath);
    if (sha256(metadataBytes) !== sourceState.metadata.sha256) throw new Error(`${source.name} metadata checksum mismatch`);
    log("INFO", `SKIP ${source.name}/metadata verified`);
  } else {
    metadataBytes = await fetchBounded(`${source.layerUrl}?f=json`, `${source.name}/metadata`);
    writeFileSync(`${metadataPath}.part`, metadataBytes);
    renameSync(`${metadataPath}.part`, metadataPath);
    sourceState.metadata = { bytes: metadataBytes.length, sha256: sha256(metadataBytes) };
    saveState();
  }
  const metadata = JSON.parse(metadataBytes.toString("utf8"));
  if (metadata.error || metadata.type !== "Feature Layer") throw new Error(`${source.name} metadata is not a Feature Layer`);

  const idsPath = resolve(sourceDir, "object-ids.json");
  let idsBytes;
  let idsUrl = sourceState.idQuery;
  if (existsSync(idsPath) && sourceState.ids) {
    idsBytes = readFileSync(idsPath);
    if (sha256(idsBytes) !== sourceState.ids.sha256) throw new Error(`${source.name} object ID checksum mismatch`);
    log("INFO", `SKIP ${source.name}/ids verified`);
  } else {
    idsUrl = `${source.layerUrl}/query?${spatialParams({ returnIdsOnly: "true", f: "json" })}`;
    idsBytes = await fetchBounded(idsUrl, `${source.name}/ids`);
    writeFileSync(`${idsPath}.part`, idsBytes);
    renameSync(`${idsPath}.part`, idsPath);
    sourceState.ids = { bytes: idsBytes.length, sha256: sha256(idsBytes) };
    sourceState.idQuery = idsUrl;
    saveState();
  }
  const idsResult = JSON.parse(idsBytes.toString("utf8"));
  if (!Array.isArray(idsResult.objectIds)) throw new Error(`${source.name} did not return objectIds`);
  const ids = [...idsResult.objectIds].sort((a, b) => a - b);
  sourceState.expectedFeatures = ids.length;
  sourceState.objectIdField = idsResult.objectIdFieldName ?? metadata.objectIdField ?? "OBJECTID";
  sourceState.layerUrl = source.layerUrl;
  saveState();

  const batchSize = Math.min(BATCH_SIZE, Number(metadata.maxRecordCount) || BATCH_SIZE);
  for (let offset = 0; offset < ids.length; offset += batchSize) {
    const batchIds = ids.slice(offset, offset + batchSize);
    const batchId = `${String(offset).padStart(8,"0")}-${String(offset + batchIds.length - 1).padStart(8,"0")}`;
    const target = resolve(sourceDir, `${batchId}.${source.format}`);
    const prior = sourceState.batches[batchId];
    if (prior && existsSync(target) && statSync(target).size === prior.bytes && sha256(readFileSync(target)) === prior.sha256) {
      log("INFO", `SKIP ${source.name}/${batchId} verified`);
      continue;
    }
    if (MAX_RETAINED_BATCHES_PER_SOURCE !== null && Object.keys(sourceState.batches).length >= MAX_RETAINED_BATCHES_PER_SOURCE) break;
    const params = new URLSearchParams({
      objectIds: batchIds.join(","), outFields: "*", returnGeometry: "true", outSR: "4326", f: source.format,
    });
    const url = `${source.layerUrl}/query`;
    const bytes = await fetchBounded(url, `${source.name}/${batchId}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
      body: params,
    });
    const parsed = JSON.parse(bytes.toString("utf8"));
    if (parsed.error) throw new Error(`${source.name}/${batchId} ArcGIS error ${JSON.stringify(parsed.error)}`);
    if (!Array.isArray(parsed.features)) throw new Error(`${source.name}/${batchId} has no feature array`);
    if (source.format === "geojson" && parsed.type !== "FeatureCollection") throw new Error(`${source.name}/${batchId} is not GeoJSON`);
    writeFileSync(`${target}.part`, bytes);
    renameSync(`${target}.part`, target);
    if (parsed.features.length !== batchIds.length) throw new Error(`${source.name}/${batchId} expected ${batchIds.length} features but received ${parsed.features.length}`);
    sourceState.batches[batchId] = {
      features: parsed.features.length,
      bytes: bytes.length,
      sha256: sha256(bytes),
      request: { method: "POST", url, parameters: Object.fromEntries(params) },
      completedAt: new Date().toISOString(),
    };
    saveState();
    log("INFO", `CHECKPOINT ${source.name} retained=${Object.values(sourceState.batches).reduce((n,b)=>n+b.features,0)}/${ids.length}`);
  }
  const retained = Object.values(sourceState.batches).reduce((n, batch) => n + batch.features, 0);
  sourceState.status = retained === ids.length ? "complete" : "partial";
  saveState();
}

async function main() {
  log("INFO", `START campaign=${CAMPAIGN_ID} startedAt=${state.startedAt} maxRetainedBatches=${MAX_RETAINED_BATCHES_PER_SOURCE}`);
  saveState({ status: "running" });
  for (const source of SOURCES) await downloadSource(source);
  const complete = Object.values(state.sources).every(source => source.status === "complete");
  saveState({ status: complete ? "complete" : "partial", completedAt: complete ? new Date().toISOString() : null });
  log("INFO", `${complete ? "COMPLETE" : "SAFE-POINT"} campaign=${CAMPAIGN_ID}`);
}

main().catch(error => {
  saveState({ status: "failed", error: error.message });
  log("ERROR", error.stack ?? error.message);
  process.exitCode = 1;
});
```

### What the code does

- Queries layer metadata before downloading features and stops if the endpoint
  no longer describes a Feature Layer.
- Enumerates object IDs inside the committed region, avoiding unreliable
  assumptions about record ordering or server maximums.
- Requests at most 500 features at a time, or the layer's advertised maximum
  when lower, with one request in flight and at least three seconds between
  request starts. Feature queries use POST bodies to avoid URL-length limits.
- Retries only transient failures, rejects unexpected HTML, and caps response
  size at 50 MB.
- Stores each batch independently and verifies byte length plus SHA-256 before
  skipping it on resume.
- Leaves a `partial` safe point during the smoke run.

## Step 2: smoke run

```bash
caffeinate -i node data/traffic/raw/authority-road-controls.mjs
```

This retains at most one batch from each source, including across restarts.
Inspect:

```bash
jq '{status,region,sources}' \
  data/traffic/raw/road-controls/YOUR_CAMPAIGN_ID/campaign-manifest.json
```

Open at least one retained batch from every source and confirm:

- each GeoJSON source is a `FeatureCollection`;
- the ODOT Esri JSON has `features`, `geometry.paths`, and spatial reference
  4326;
- coordinates are plausible longitude/latitude values;
- speed layers contain their documented speed field;
- PBOT signs include `SignCode` and `Rotation`;
- signal layers contain point features;
- the manifest's expected feature count is not zero without an understood
  reason.

## Step 3: full run

Change this one line in the runner:

```js
const MAX_RETAINED_BATCHES_PER_SOURCE = null;
```

Run the identical command again. Verified smoke partitions are skipped:

```bash
caffeinate -i node data/traffic/raw/authority-road-controls.mjs
```

### Expected scale

The August 8, 2026 endpoint check found 114,885 regional features across the
eight layers. Seven layers can use the campaign's 500-feature cap; the PBOT
speed layer advertises a 200-record maximum. That is about 333 data requests,
plus 16 metadata and object-ID requests. The three-second spacing therefore
creates a theoretical floor of about 17.5 minutes for the full run; response
time and retries make the real duration longer. The runner discovers and logs
current counts and limits, so these figures are planning estimates, not a
completeness claim.

## Completion gate

Every source must say `complete`, the sum of `features` across its batches must
equal `expectedFeatures`, and the campaign must have no `.part` files.

Retain the output exactly as downloaded. Directional edge matching, legal-speed
precedence, deduplication, and conflict resolution belong to a later normalized
dataset, never to these raw files.
