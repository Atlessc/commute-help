# Campaign 4 — Operational signal timing requests

## Goal

Obtain existing operational timing records for the limited set of signals that
can materially affect closure diversion. This is a tracked records-acquisition
campaign, not a website scrape.

Requesting every controller record in the region would be expensive, slow, and
unnecessary for the first useful domino model. First prioritize signals on:

- likely diversion corridors;
- freeway ramp terminals;
- bridge approaches;
- bottlenecks identified by the historical data;
- intersections used by candidate fastest/resilient routes.

## Records to request

For each identified intersection and for the date range relevant to the traffic
campaign, ask for existing records containing:

- agency intersection/controller ID and effective date;
- time-of-day/day-of-week plan schedule;
- cycle length, offset, and coordination group;
- ring-barrier phase sequence and movement assignment;
- green splits plus minimum and maximum green;
- yellow and all-red clearance;
- protected/permissive turn operation and right-on-red restrictions;
- recall, detector configuration, gap/passage settings, and phase skips;
- pedestrian recall/call behavior and pedestrian timing;
- transit priority and emergency/railroad preemption;
- controller export, Synchro/UTDF file, timing sheet, or equivalent;
- available ATSPM/high-resolution event exports for a representative period.

Ask for existing electronic records, not for the agency to perform an analysis
or create a new pedestrian-free plan.

## “No pedestrians” simulation

Do not ask the agency to invent this counterfactual. Preserve the real vehicle
plan and later simulate zero pedestrian calls. Pedestrian recall must remain if
the controller runs it automatically. Yellow, all-red, vehicle minimum green,
coordination, detection, and preemption remain in force.

## Official submission channels

| Owner | First channel | Formal channel |
| --- | --- | --- |
| PBOT | Check whether the requested timing records are already published | [City of Portland Public Records Request Portal](https://www.portland.gov/public-records/request); select PBOT/Transportation |
| ODOT | [ODOT Signals](https://www.oregon.gov/odot/Engineering/Pages/Signals.aspx) and Traffic Plan Search | ODOT public-records coordinator: `ODOTPRR@odot.state.or.us` or the linked state request form |
| City of Vancouver | `trafficengineering@cityofvancouver.us` for available signal plans/data | [Vancouver Public Records Request](https://www.cityofvancouver.us/government/public-records-request/) or `citypdr@cityofvancouver.us` |
| Clark County | Check the downloaded asset inventory and Public Works ownership first | [Clark County Public Records](https://clark.wa.gov/councilors/public-records-overview) GovQA portal |

Requests and contact details can change. Open the official page before
submission and retain a copy of the acknowledgement.

## Step 1: define the critical intersections

After Campaign 2, create this CSV in a text editor:

```text
data/traffic/raw/signal-timing/critical-signals.csv
```

Use exactly this header:

```csv
agency,asset_id,intersection,longitude,latitude,reason,date_from,date_to
```

One row represents one known signal. Do not put home/work addresses or private
trip labels in this file. `reason` should use a technical description such as
`I-205 closure diversion corridor`.

If ownership or identity is uncertain, resolve it before requesting records.
Do not make the agency reverse-engineer which signal you meant.

## Step 2: create the tracker

Create this ignored file:

```text
data/traffic/raw/signal-timing-tracker.mjs
```

Paste the code and replace `REPLACE_WITH_CAMPAIGN_ID`.

```js
import { createHash } from "node:crypto";
import { appendFileSync, copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { basename, resolve } from "node:path";

const CAMPAIGN_ID = "REPLACE_WITH_CAMPAIGN_ID";
if (CAMPAIGN_ID.startsWith("REPLACE_")) throw new Error("Set CAMPAIGN_ID before running.");
const ROOT = process.cwd();
const DIR = resolve(ROOT, "data/traffic/raw/signal-timing", CAMPAIGN_ID);
const STATE_PATH = resolve(DIR, "campaign-state.json");
const EVENTS_PATH = resolve(DIR, "events.jsonl");
const LOG_PATH = resolve(DIR, "campaign.log");
mkdirSync(DIR, { recursive: true });
mkdirSync(resolve(DIR, "requests"), { recursive: true });
mkdirSync(resolve(DIR, "responses"), { recursive: true });

const state = existsSync(STATE_PATH) ? JSON.parse(readFileSync(STATE_PATH, "utf8")) : {
  schemaVersion: 1, campaignId: CAMPAIGN_ID, startedAt: new Date().toISOString(), status: "preparing", responses: {},
};
const startMs = Date.parse(state.startedAt);

function stopwatch() {
  const n = Math.max(0, Date.now() - startMs);
  return `${String(Math.floor(n/3600000)).padStart(2,"0")}:${String(Math.floor(n/60000)%60).padStart(2,"0")}:${String(Math.floor(n/1000)%60).padStart(2,"0")}.${String(n%1000).padStart(3,"0")}`;
}
function log(level, message) {
  const line = `[${new Date().toISOString()}] [+${stopwatch()}] ${level} ${message}`;
  console.log(line);
  appendFileSync(LOG_PATH, `${line}\n`);
}
function saveState(patch = {}) {
  Object.assign(state, patch, { updatedAt: new Date().toISOString() });
  writeFileSync(`${STATE_PATH}.part`, `${JSON.stringify(state, null, 2)}\n`);
  renameSync(`${STATE_PATH}.part`, STATE_PATH);
}
function event(kind, agency, detail) {
  const entry = { timestamp: new Date().toISOString(), elapsed: stopwatch(), kind, agency, detail };
  appendFileSync(EVENTS_PATH, `${JSON.stringify(entry)}\n`);
  log("INFO", `EVENT kind=${kind} agency=${agency} detail=${detail}`);
}
function sha256(path) { return createHash("sha256").update(readFileSync(path)).digest("hex"); }

const requestedRecords = `Please provide existing electronic traffic-signal timing records for the intersections listed in the attached critical-signals.csv, covering each row's requested date range where retained. Requested records include controller/intersection ID; effective date; day/time plan schedule; cycle, offset, coordination group, ring-barrier phasing, movement assignments, splits, minimum/maximum green, yellow and all-red, recall, detector and passage/gap settings, pedestrian timing and recall behavior, protected/permissive turns, transit priority, preemption, controller exports, Synchro/UTDF files, timing sheets, and available ATSPM or high-resolution controller-event exports. Please provide native electronic files where available. This request seeks existing records and does not ask the agency to create an analysis or a pedestrian-free timing plan. If the request is too broad or costly, please provide an estimate and help narrow it to the listed highest-priority intersections before performing billable work.`;

function init() {
  const csv = resolve(ROOT, "data/traffic/raw/signal-timing/critical-signals.csv");
  if (!existsSync(csv)) throw new Error(`Create ${csv} before init`);
  const header = readFileSync(csv, "utf8").split(/\r?\n/, 1)[0];
  const expected = "agency,asset_id,intersection,longitude,latitude,reason,date_from,date_to";
  if (header !== expected) throw new Error(`CSV header must be exactly: ${expected}`);
  const destinations = {
    pbot: "City of Portland Public Records Request Portal; select PBOT/Transportation",
    odot: "ODOT public-records process / ODOTPRR@odot.state.or.us",
    vancouver: "trafficengineering@cityofvancouver.us, then Vancouver Public Records if needed",
    clark_county: "Clark County GovQA Public Records Portal; select Public Works",
  };
  for (const [agency, destination] of Object.entries(destinations)) {
    const text = `# ${agency} signal-timing request\n\nCampaign: ${CAMPAIGN_ID}\nDestination: ${destination}\n\n## Records requested\n\n${requestedRecords}\n\n## Attachment\n\nAttach the reviewed critical-signals.csv rows owned by this agency only.\n`;
    writeFileSync(resolve(DIR, "requests", `${agency}.md`), text);
  }
  saveState({ status: "ready_to_submit", criticalSignals: { path: csv, bytes: statSync(csv).size, sha256: sha256(csv) } });
  event("initialized", "all", "request templates created and critical-signals.csv hashed");
}

function recordEvent(args) {
  const [agency, kind, ...detailParts] = args;
  if (!agency || !kind || detailParts.length === 0) throw new Error("event requires: agency kind detail");
  event(kind, agency, detailParts.join(" "));
  saveState();
}

function ingest(args) {
  const [agency, sourcePath] = args;
  if (!agency || !sourcePath) throw new Error("ingest requires: agency /absolute/path/to/response");
  const source = resolve(sourcePath);
  if (!existsSync(source)) throw new Error(`Response does not exist: ${source}`);
  const safeName = `${agency}-${new Date().toISOString().replace(/[:.]/g,"-")}-${basename(source).replace(/[^A-Za-z0-9_.-]/g,"_")}`;
  const target = resolve(DIR, "responses", safeName);
  copyFileSync(source, `${target}.part`);
  renameSync(`${target}.part`, target);
  state.responses[safeName] = { agency, path: `responses/${safeName}`, bytes: statSync(target).size, sha256: sha256(target), receivedAt: new Date().toISOString() };
  saveState({ status: "responses_received" });
  event("response_ingested", agency, `${safeName} bytes=${statSync(target).size}`);
}

function block(args) {
  const detail = args.join(" ");
  if (!detail) throw new Error("block requires a reason and next prerequisite");
  event("campaign_blocked", "all", detail);
  saveState({ status: "blocked", blockedAt: new Date().toISOString(), blocker: detail });
}

function complete(args) {
  const detail = args.join(" ");
  if (!detail) throw new Error("complete requires a summary of agency outcomes");
  event("campaign_complete", "all", detail);
  saveState({ status: "complete", completedAt: new Date().toISOString() });
  log("INFO", `COMPLETE campaign=${CAMPAIGN_ID}`);
}

const [command, ...args] = process.argv.slice(2);
log("INFO", `START campaign=${CAMPAIGN_ID} startedAt=${state.startedAt} command=${command ?? "none"}`);
try {
  if (command === "init") init();
  else if (command === "event") recordEvent(args);
  else if (command === "ingest") ingest(args);
  else if (command === "block") block(args);
  else if (command === "complete") complete(args);
  else throw new Error("Use: init | event AGENCY KIND DETAIL | ingest AGENCY FILE | block REASON | complete SUMMARY");
  log("INFO", `FINISH command=${command}`);
} catch (error) {
  saveState({ status: "failed", error: error.message });
  log("ERROR", error.stack ?? error.message);
  process.exitCode = 1;
}
```

### What the tracker does

- Persists one campaign start time, so events logged days later show total
  elapsed time.
- Validates and hashes the critical-signal list.
- Generates agency-specific request templates without sending anything.
- Appends submission, acknowledgement, clarification, estimate, payment, and
  completion events to machine-readable JSONL.
- Copies received files into immutable campaign storage and records checksums.
- Records a timestamped blocked state when critical-signal matching, ownership,
  permission, or another prerequisite is genuinely unresolved.

If the critical-signal list cannot yet be created without guessing, record the
blocker instead of initializing an empty campaign:

```bash
node data/traffic/raw/signal-timing-tracker.mjs block "Critical signals require confidence-scored matching to directed diversion edges and verified agency ownership"
```

After resolving the prerequisite, create the reviewed CSV and run `init`; the
tracker preserves the original start time and blocker event.

## Step 3: initialize and review

```bash
node data/traffic/raw/signal-timing-tracker.mjs init
```

Review every generated request and attach only the CSV rows owned by that
agency. Never submit all agencies' intersections to each recipient.

## Step 4: submit manually and log every milestone

The tracker does not send email, accept fees, or submit public-records forms.
Those are external actions requiring your review.

Immediately after a submission, record its reference number:

```bash
node data/traffic/raw/signal-timing-tracker.mjs event pbot submitted "portal reference PRR-EXAMPLE"
```

Other examples:

```bash
node data/traffic/raw/signal-timing-tracker.mjs event odot acknowledged "acknowledged; response estimate pending"
node data/traffic/raw/signal-timing-tracker.mjs event vancouver clarification "narrowed to listed critical intersections"
node data/traffic/raw/signal-timing-tracker.mjs event clark_county estimate "fee estimate received; not yet approved"
```

Do not approve a material fee without deciding whether the specific records
justify it. Log the decision either way.

## Step 5: ingest responses

Download an agency response to a known local path, then let the tracker copy and
hash it:

```bash
node data/traffic/raw/signal-timing-tracker.mjs ingest pbot /absolute/path/to/agency-response.zip
```

Never modify the ingested copy. Any extraction or normalization belongs in a
separate processed directory with a link back to the raw checksum.

## Completion gate

For every requested agency, the event log must show one of:

- fulfilled;
- no responsive records;
- denied with reason;
- withdrawn after a documented estimate;
- superseded by a narrower request.

Before modeling, classify each received record by effective date, ownership,
intersection identity, and whether it is a design plan, timing sheet,
controller export, or observed ATSPM event data. Current operational status must
be proven, not inferred from the mere existence of a file.

After every agency has a terminal outcome, close the tracked campaign:

```bash
node data/traffic/raw/signal-timing-tracker.mjs complete "PBOT fulfilled; ODOT fulfilled; Vancouver no responsive ATSPM; Clark County narrowed request fulfilled"
```

The command writes the required timestamped `COMPLETE` line and records the
summary in `events.jsonl`. Use the actual outcomes, not the example text.
