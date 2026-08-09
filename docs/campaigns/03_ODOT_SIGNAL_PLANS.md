# Campaign 3 — ODOT signal-plan index and documents

## Goal

Index publicly searchable ODOT signal drawings for the Oregon side of the
Portland metropolitan region, then download the indexed documents only after
ODOT confirms acceptable bulk access.

The public search page submits form data to an undocumented JSON endpoint:

```text
POST https://ecmnet.odot.state.or.us/TrafficPlans/TrafficPlanSearch/Search
```

Returned records include a drawing ID, drawing number/year, discipline,
drawing type, project, work area, city, county, route, highway, milepoints, and
whether downloadable content exists. Individual content is returned from:

```text
GET https://ecmnet.odot.state.or.us/TrafficPlans/Home/Download/{drawing-id}
```

This behavior was verified against the public application on August 8, 2026.
It is not a published bulk API and has no published numeric rate limit.

## Mandatory permission gate

Indexing uses the same narrow searches performed by the public page. Before
bulk document downloads, contact the support address displayed by the site:

```text
Computer_Support.ODOT@odot.oregon.gov
```

Suggested request:

> I am building a noncommercial, local transportation model for planned road
> closures in the Portland–Vancouver region. I would like to index and download
> publicly available ODOT traffic-signal plan drawings from Traffic Plan
> Search. Is programmatic use of the public JSON search and individual document
> download routes permitted? Is there a preferred bulk export, request delay,
> maximum daily volume, or attribution requirement? I will use one request at a
> time, preserve provenance, and stop on throttling or access errors.

Save ODOT's answer with the campaign. If ODOT offers a bulk export, use that
instead and record its delivery details. Silence is not approval.

## Search boundary

The supplied runner explicitly searches:

- discipline: `Signal`
- counties: `MULTNOMAH`, `WASHINGTON`, and `CLACKAMAS`
- drawing years: 1900 through the current calendar year
- maximum results per query: 500

The 1900 lower bound is a declared campaign boundary, not a claim about the
archive's first record. Change it before the first run if ODOT gives a more
appropriate archive boundary. If any county/year query still returns
`MoreResults=true`, the runner stops rather than silently accepting an
incomplete index.

## Step 1: create the runner

Create this ignored file:

```text
data/traffic/raw/odot-signal-plans.mjs
```

Paste the complete code, replace `REPLACE_WITH_CAMPAIGN_ID`, and leave
`DOWNLOAD_DECISION` set to `"pending"` for indexing.

```js
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const CAMPAIGN_ID = "REPLACE_WITH_CAMPAIGN_ID";
const FIRST_YEAR = 1900;
const LAST_YEAR = new Date().getFullYear();
const COUNTIES = ["MULTNOMAH", "WASHINGTON", "CLACKAMAS"];
const DOWNLOAD_DECISION = "pending"; // pending | approved | declined
if (!["pending", "approved", "declined"].includes(DOWNLOAD_DECISION)) throw new Error("Invalid DOWNLOAD_DECISION");
const DOWNLOAD_APPROVED = DOWNLOAD_DECISION === "approved";
const REQUEST_DELAY_MS = 3000;
const DOWNLOAD_DELAY_MS = 5000;
const TIMEOUT_MS = 120000;
const MAX_RETRIES = 5;
const MAX_SEARCH_BYTES = 10 * 1024 * 1024;
const MAX_DOCUMENT_BYTES = 100 * 1024 * 1024;
const BASE = "https://ecmnet.odot.state.or.us";

if (CAMPAIGN_ID.startsWith("REPLACE_")) throw new Error("Set CAMPAIGN_ID before running.");
const ROOT = process.cwd();
const DIR = resolve(ROOT, "data/traffic/raw/odot-signal-plans", CAMPAIGN_ID);
const QUERY_DIR = resolve(DIR, "queries");
const DOCUMENT_DIR = resolve(DIR, "documents");
const STATE_PATH = resolve(DIR, "campaign-manifest.json");
const LOG_PATH = resolve(DIR, "campaign.log");
mkdirSync(QUERY_DIR, { recursive: true });
mkdirSync(DOCUMENT_DIR, { recursive: true });

const state = existsSync(STATE_PATH) ? JSON.parse(readFileSync(STATE_PATH, "utf8")) : {
  schemaVersion: 1,
  campaignId: CAMPAIGN_ID,
  startedAt: new Date().toISOString(),
  status: "indexing",
  search: { firstYear: FIRST_YEAR, lastYear: LAST_YEAR, counties: COUNTIES, queries: {} },
  documents: {},
};
const startMs = Date.parse(state.startedAt);
let cookie = "";
let lastRequestAt = 0;

function stopwatch() {
  const n = Math.max(0, Date.now() - startMs);
  return `${String(Math.floor(n/3600000)).padStart(2,"0")}:${String(Math.floor(n/60000)%60).padStart(2,"0")}:${String(Math.floor(n/1000)%60).padStart(2,"0")}.${String(n%1000).padStart(3,"0")}`;
}
function log(level, message) {
  const line = `[${new Date().toISOString()}] [+${stopwatch()}] ${level} ${message}`;
  console.log(line);
  appendFileSync(LOG_PATH, `${line}\n`);
}
function atomicJson(path, value) {
  writeFileSync(`${path}.part`, `${JSON.stringify(value, null, 2)}\n`);
  renameSync(`${path}.part`, path);
}
function saveState(patch = {}) {
  Object.assign(state, patch, { updatedAt: new Date().toISOString() });
  atomicJson(STATE_PATH, state);
}
function sha256(bytes) { return createHash("sha256").update(bytes).digest("hex"); }
function sleep(ms) { return new Promise(resolvePromise => setTimeout(resolvePromise, ms)); }

async function request(url, options, label, limit, minimumDelay = REQUEST_DELAY_MS) {
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const delay = Math.max(0, minimumDelay - (Date.now() - lastRequestAt));
    if (delay) await sleep(delay);
    lastRequestAt = Date.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    log("INFO", `REQUEST ${label} attempt=${attempt + 1} ${url}`);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: { "User-Agent": "Commute-Help/ODOT-signal-plan local research", ...(cookie ? { Cookie: cookie } : {}), ...(options.headers ?? {}) },
      });
      const setCookie = response.headers.get("set-cookie");
      if (setCookie) cookie = setCookie.split(";", 1)[0];
      if ((response.status === 429 || response.status >= 500) && attempt < MAX_RETRIES) {
        const retryAfter = Number(response.headers.get("retry-after") ?? 0) * 1000;
        log("WARN", `RETRY ${label} http=${response.status}`);
        await sleep(retryAfter || 1000 * 2 ** attempt);
        continue;
      }
      if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      if (bytes.length > limit) throw new Error(`${label} exceeded byte cap ${limit}`);
      log("INFO", `RESPONSE ${label} bytes=${bytes.length}`);
      return { bytes, contentType: response.headers.get("content-type") ?? "", disposition: response.headers.get("content-disposition") ?? "" };
    } catch (error) {
      if (attempt >= MAX_RETRIES || /HTTP 4\d\d/.test(error.message)) throw error;
      log("WARN", `RETRY ${label} error=${error.message}`);
      await sleep(1000 * 2 ** attempt);
    } finally { clearTimeout(timer); }
  }
  throw new Error(`${label} exhausted retries`);
}

async function establishSession() {
  await request(`${BASE}/TrafficPlans/TrafficPlanSearch`, {}, "session", MAX_SEARCH_BYTES);
}

async function indexPlans() {
  const indexed = new Map(Object.entries(state.documents));
  for (const county of COUNTIES) {
    for (let year = FIRST_YEAR; year <= LAST_YEAR; year++) {
      const queryId = `${county.toLowerCase()}-${year}`;
      const target = resolve(QUERY_DIR, `${queryId}.json`);
      let result;
      if (existsSync(target) && state.search.queries[queryId]) {
        const bytes = readFileSync(target);
        if (sha256(bytes) !== state.search.queries[queryId].sha256) throw new Error(`Checksum mismatch for ${queryId}`);
        result = JSON.parse(bytes.toString("utf8"));
        log("INFO", `SKIP query=${queryId} verified records=${result.Documents.length}`);
      } else {
        const body = new URLSearchParams({
          TrafficPlanDiscipline: "Signal", County: county,
          DrawingYearFrom: String(year), DrawingYearTo: String(year), MaxResults: "500",
        });
        const response = await request(`${BASE}/TrafficPlans/TrafficPlanSearch/Search`, {
          method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" }, body,
        }, `search/${queryId}`, MAX_SEARCH_BYTES);
        result = JSON.parse(response.bytes.toString("utf8"));
        if (!Array.isArray(result.Documents)) throw new Error(`${queryId} returned an unexpected schema`);
        if (result.MoreResults) throw new Error(`${queryId} exceeded 500 results; refine this query before continuing`);
        writeFileSync(`${target}.part`, response.bytes);
        renameSync(`${target}.part`, target);
        state.search.queries[queryId] = { records: result.Documents.length, bytes: response.bytes.length, sha256: sha256(response.bytes), completedAt: new Date().toISOString() };
      }
      for (const document of result.Documents) indexed.set(String(document.Id), { metadata: document, download: state.documents[String(document.Id)]?.download ?? null });
      state.documents = Object.fromEntries(indexed);
      saveState();
      log("INFO", `CHECKPOINT query=${queryId} uniqueDocuments=${indexed.size}`);
    }
  }
  atomicJson(resolve(DIR, "signal-plan-index.json"), [...indexed.values()].map(value => value.metadata));
  state.indexCompletedAt = new Date().toISOString();
  const status = DOWNLOAD_DECISION === "approved" ? "downloading" : DOWNLOAD_DECISION === "declined" ? "index_complete_download_declined" : "indexed_permission_required";
  saveState({ status });
}

function extension(contentType, disposition) {
  const match = /filename\*?=(?:UTF-8''|\")?([^\";]+)/i.exec(disposition);
  if (match && /\.[A-Za-z0-9]{1,8}$/.test(match[1])) return match[1].match(/\.[A-Za-z0-9]{1,8}$/)[0].toLowerCase();
  if (contentType.includes("pdf")) return ".pdf";
  if (contentType.includes("zip")) return ".zip";
  return ".bin";
}

async function downloadPlans() {
  if (!DOWNLOAD_APPROVED) {
    if (DOWNLOAD_DECISION === "declined") {
      saveState({ status: "complete", completionScope: "index_only", completedAt: new Date().toISOString() });
      log("INFO", `COMPLETE campaign=${CAMPAIGN_ID} index-only; ODOT did not approve bulk downloads`);
    } else {
      log("WARN", "SAFE-POINT index complete; bulk document download is disabled pending written ODOT approval");
    }
    return;
  }
  for (const [id, record] of Object.entries(state.documents)) {
    if (!record.metadata.HasContent) { log("INFO", `SKIP document=${id} no content`); continue; }
    if (record.download?.path) {
      const existing = resolve(DIR, record.download.path);
      if (existsSync(existing) && statSync(existing).size === record.download.bytes && sha256(readFileSync(existing)) === record.download.sha256) {
        log("INFO", `SKIP document=${id} verified`); continue;
      }
    }
    const response = await request(`${BASE}/TrafficPlans/Home/Download/${encodeURIComponent(id)}`, {}, `download/${id}`, MAX_DOCUMENT_BYTES, DOWNLOAD_DELAY_MS);
    if (response.contentType.includes("text/html")) throw new Error(`download/${id} returned HTML`);
    const name = `${id.replace(/[^A-Za-z0-9_.-]/g, "_")}${extension(response.contentType, response.disposition)}`;
    const target = resolve(DOCUMENT_DIR, name);
    writeFileSync(`${target}.part`, response.bytes);
    renameSync(`${target}.part`, target);
    record.download = { path: `documents/${name}`, bytes: response.bytes.length, sha256: sha256(response.bytes), contentType: response.contentType, requestUrl: `${BASE}/TrafficPlans/Home/Download/${id}`, completedAt: new Date().toISOString() };
    saveState();
    log("INFO", `CHECKPOINT document=${id} bytes=${response.bytes.length}`);
  }
  saveState({ status: "complete", completedAt: new Date().toISOString() });
  log("INFO", `COMPLETE campaign=${CAMPAIGN_ID} documents=${Object.keys(state.documents).length}`);
}

async function main() {
  log("INFO", `START campaign=${CAMPAIGN_ID} startedAt=${state.startedAt} downloadApproved=${DOWNLOAD_APPROVED}`);
  await establishSession();
  await indexPlans();
  await downloadPlans();
}

main().catch(error => {
  saveState({ status: "failed", error: error.message });
  log("ERROR", error.stack ?? error.message);
  process.exitCode = 1;
});
```

### What the code does

- Establishes a normal public-site session before searches.
- Searches one county and one drawing year at a time, three seconds apart.
- Stops on a truncated result rather than pretending the first 500 are all
  records.
- Stores every raw query response and a deduplicated index.
- Resumes by verifying query and document checksums.
- Keeps bulk downloads disabled until you explicitly record approval and
  change the documented permission decision.
- Downloads one document at a time with a five-second delay and a 100 MB cap.

## Step 2: build the index

```bash
caffeinate -i node data/traffic/raw/odot-signal-plans.mjs
```

The expected safe-point status is:

```text
indexed_permission_required
```

With `FIRST_YEAR = 1900`, the 2026 index contains 381 county-year searches:
127 years multiplied by three counties. Three-second spacing gives a minimum
of about 19 minutes, excluding response time and retries. Each completed query
is checkpointed, so an interruption does not restart the index.

Inspect the index:

```bash
jq 'length' \
  data/traffic/raw/odot-signal-plans/YOUR_CAMPAIGN_ID/signal-plan-index.json
```

Also inspect several records for route, work area, year, drawing type, and
`HasContent`. Index records outside the final graph can be filtered later; raw
search results should remain unchanged.

## Step 3: record permission

Save ODOT's written answer inside:

```text
data/traffic/raw/odot-signal-plans/YOUR_CAMPAIGN_ID/permission/
```

Record its receipt in the campaign permission directory. Set
`DOWNLOAD_DECISION` to `"approved"` only for approval, or `"declined"` when
ODOT denies bulk access. Then rerun so that decision becomes a timestamped
campaign event. If ODOT imposes a different delay or daily cap, update those
constants and write the change into a local
`permission/README.txt` before starting.

If the decision is `"declined"`, the runner records `status: "complete"` with
`completionScope: "index_only"`; it does not download any plan documents.

## Step 4: download approved documents

Change:

```js
const DOWNLOAD_DECISION = "approved";
```

Then run the same command. The index is checksum-verified and skipped; document
downloads resume at the first missing or invalid file.

## Completion and use

The campaign is complete when every `HasContent=true` record has a verified
download and no `.part` files remain.

Treat drawings as reference evidence. ODOT warns that archive standards change
over time. A drawing can establish geometry, heads, phases, detection, or a
historical design, but it must not be labeled as the current operational timing
unless its effective status is independently verified.
