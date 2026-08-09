# Campaign 1 — OSM network and edge lengths

## Goal

Build a new Portland–Vancouver drive graph inside an isolated campaign
directory using the repository's committed region and existing graph builder.
The build calculates directed edge lengths and validates representative routes
without replacing the graph currently used by the application.

The source is OpenStreetMap obtained through OSMnx/Overpass. The retrieval
cache, graph artifacts, manifest, validation report, timestamps, and checksums
remain together under `data/osm/campaigns/`.

## Safety and evidence boundary

- This does not overwrite `data/graphs/`.
- This does not automatically promote the new graph into the application.
- OSM is the primary routable topology, not the legal authority for every
  speed limit or control.
- Edge length is calculated from geometry. It is not an agency survey value.
- The Overpass response represents the data available at retrieval time; it is
  not a guaranteed historical OSM snapshot.

## Step 1: create the runner

In a text editor, create this ignored local file:

```text
data/osm/network-campaign.mjs
```

Paste the complete code below. Replace only `REPLACE_WITH_CAMPAIGN_ID` with a
new value such as `osm-network-20260808T131500`. Do not reuse a campaign ID for
a new source retrieval.

```js
import { createHash } from "node:crypto";
import { appendFileSync, createReadStream, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

const CAMPAIGN_ID = "REPLACE_WITH_CAMPAIGN_ID";
if (CAMPAIGN_ID.startsWith("REPLACE_")) {
  throw new Error("Set CAMPAIGN_ID before running this campaign.");
}

const ROOT = process.cwd();
const CAMPAIGN_DIR = resolve(ROOT, "data/osm/campaigns", CAMPAIGN_ID);
const ARTIFACT_DIR = resolve(CAMPAIGN_DIR, "artifacts");
const CACHE_DIR = resolve(CAMPAIGN_DIR, "osmnx-cache");
const STATE_PATH = resolve(CAMPAIGN_DIR, "campaign-state.json");
const LOG_PATH = resolve(CAMPAIGN_DIR, "campaign.log");
mkdirSync(CAMPAIGN_DIR, { recursive: true });

const state = existsSync(STATE_PATH)
  ? JSON.parse(readFileSync(STATE_PATH, "utf8"))
  : { campaignId: CAMPAIGN_ID, startedAt: new Date().toISOString(), status: "running" };
const startMs = Date.parse(state.startedAt);

function stopwatch() {
  const total = Math.max(0, Date.now() - startMs);
  const hours = String(Math.floor(total / 3_600_000)).padStart(2, "0");
  const minutes = String(Math.floor(total / 60_000) % 60).padStart(2, "0");
  const seconds = String(Math.floor(total / 1_000) % 60).padStart(2, "0");
  const millis = String(total % 1_000).padStart(3, "0");
  return `${hours}:${minutes}:${seconds}.${millis}`;
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

function hashFile(path) {
  return new Promise((resolvePromise, rejectPromise) => {
    const hash = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("data", chunk => hash.update(chunk));
    stream.on("error", rejectPromise);
    stream.on("end", () => resolvePromise(hash.digest("hex")));
  });
}

async function run(label, command, args) {
  log("INFO", `START ${label}: ${command} ${args.join(" ")}`);
  await new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, { cwd: ROOT, env: process.env });
    for (const [stream, level] of [[child.stdout, "INFO"], [child.stderr, "WARN"]]) {
      let pending = "";
      stream.setEncoding("utf8");
      stream.on("data", chunk => {
        pending += chunk;
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? "";
        for (const line of lines) if (line) log(level, `${label}: ${line}`);
      });
      stream.on("end", () => { if (pending) log(level, `${label}: ${pending}`); });
    }
    child.on("error", rejectPromise);
    child.on("exit", code => code === 0
      ? resolvePromise()
      : rejectPromise(new Error(`${label} exited with code ${code}`)));
  });
  log("INFO", `FINISH ${label}`);
}

async function main() {
  log("INFO", `START campaign=${CAMPAIGN_ID} startedAt=${state.startedAt}`);
  saveState({ status: "running" });
  await run("prerequisites", process.execPath, ["--version"]);
  await run("python", resolve(ROOT, ".venv/bin/python"), ["--version"]);

  const graphPath = resolve(ARTIFACT_DIR, "portland-vancouver.graphml");
  const requiredBuildArtifacts = [
    graphPath,
    resolve(ARTIFACT_DIR, "graph-manifest.json"),
    resolve(ARTIFACT_DIR, "nodes.parquet"),
    resolve(ARTIFACT_DIR, "edges.parquet"),
  ];
  const buildComplete = requiredBuildArtifacts.every(path => existsSync(path) && statSync(path).size > 0);
  if (!buildComplete) {
    await run("graph-build", resolve(ROOT, ".venv/bin/python"), [
      "-m", "scripts.build_graph",
      "--region", "data/regions/portland-vancouver-v1.geojson",
      "--output", ARTIFACT_DIR,
      "--cache", CACHE_DIR,
      "--graph-version", CAMPAIGN_ID,
    ]);
  } else {
    log("INFO", "SKIP graph-build; all required build artifacts are present and non-empty");
  }

  await run("graph-validation", resolve(ROOT, ".venv/bin/python"), [
    "-m", "scripts.validate_graph",
    "--graph", graphPath,
    "--region", "data/regions/portland-vancouver-v1.geojson",
    "--report", resolve(ARTIFACT_DIR, "validation-report.json"),
  ]);

  await run("length-audit", resolve(ROOT, ".venv/bin/python"), ["-c", [
    "import json",
    "import pandas as pd",
    `p = r'${resolve(ARTIFACT_DIR, "edges.parquet")}'`,
    "df = pd.read_parquet(p, columns=['length'])",
    "bad = int(df['length'].isna().sum() + (df['length'] <= 0).sum())",
    "print(json.dumps({'directed_edges': len(df), 'missing_or_nonpositive_length': bad}))",
    "raise SystemExit(0 if bad == 0 else 1)",
  ].join(";")]);

  const files = ["graph-manifest.json", "validation-report.json", "portland-vancouver.graphml", "nodes.parquet", "edges.parquet"];
  const checksums = {};
  for (const name of files) {
    const path = resolve(ARTIFACT_DIR, name);
    checksums[name] = { bytes: statSync(path).size, sha256: await hashFile(path) };
    log("INFO", `HASH ${name} bytes=${checksums[name].bytes}`);
  }
  saveState({ status: "complete", completedAt: new Date().toISOString(), artifacts: checksums });
  log("INFO", `COMPLETE campaign=${CAMPAIGN_ID} artifacts=${files.length}`);
}

main().catch(error => {
  saveState({ status: "failed", error: error.message });
  log("ERROR", error.stack ?? error.message);
  process.exitCode = 1;
});
```

### What the code does

- Persists the original start time in `campaign-state.json`, so the stopwatch
  continues across restarts.
- Sends every child-process output line through the timestamped logger.
- Builds into a campaign-specific directory and refuses to overwrite the live
  application graph.
- Resumes by skipping the build only when GraphML, the manifest, and both
  Parquet tables are all present and non-empty, then reruns validation.
- Opens `edges.parquet` and fails if any directed edge lacks a positive length.
- Records byte lengths and SHA-256 hashes for all final artifacts.

## Step 2: run it

From the repository root:

```bash
caffeinate -i node data/osm/network-campaign.mjs
```

The command begins with a timestamped `START` line. Follow progress in another
terminal without altering the campaign:

```bash
tail -f data/osm/campaigns/YOUR_CAMPAIGN_ID/campaign.log
```

`tail` displays existing timestamped lines; it does not generate campaign
events itself.

Control-C stops the process. Run the same command to resume. Do not add
`--force`; this campaign intentionally uses a new output directory.

## Completion check

```bash
jq '{campaignId,status,startedAt,completedAt,artifacts}' \
  data/osm/campaigns/YOUR_CAMPAIGN_ID/campaign-state.json
```

Confirm:

- `status` is `complete`;
- the log contains one `COMPLETE` line;
- validation says `PASS`;
- `missing_or_nonpositive_length` was zero;
- no `.part` files remain;
- the manifest bounds match the committed Portland–Vancouver region.

Do not copy these artifacts into `data/graphs/` yet. Promotion requires a
separate comparison against the currently loaded graph and closure rematching.
