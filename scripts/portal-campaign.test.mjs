import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildChunks,
  downloadWithRetry,
  normalizeChunk,
  parseCsvLine,
  runCampaign,
  validateConfig,
  webMercatorToLonLat,
} from "./portal-campaign.mjs";

const baseConfig = {
  name: "fixture-campaign",
  start_date: "2025-09-08",
  end_date: "2025-09-09",
  highway_ids: [3, 4],
  resolution: "00:15:00",
  days_of_week: [2, 3, 4, 5, 6],
  chunk_days: 1,
  delay_ms: 1000,
  request_timeout_ms: 10000,
  max_retries: 0,
  max_response_bytes: 1048576,
  max_groups_per_chunk: 1000,
};

const highwayMetadata = [{ highwayid: 3, highwayname: "I-205", direction: "NORTH" }];
const detectorMetadata = [
  { detectorid: 100, stationid: 10, highwayid: 3 },
  { detectorid: 101, stationid: 10, highwayid: 3 },
];
const stationMetadata = {
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    properties: { stationid: 10, highwayid: 3 },
    geometry: { type: "Point", coordinates: [-13661137.0, 5700582.7] },
  }],
};
const freewayCsv = [
  "starttime,resolution,detector_id,speed,volume,occupancy,countreadings,delay,traveltime,vht,vmt",
  "2025-09-08T08:00:00-07:00,00:15:00,100,40,10,8,45,0,0,0,0",
  "2025-09-08T08:00:00-07:00,00:15:00,101,45,20,12,45,0,0,0,0",
  "",
].join("\n");

test("configuration produces one sequential request per highway and date chunk", () => {
  const config = validateConfig(baseConfig);
  const chunks = buildChunks(config);
  assert.equal(chunks.length, 4);
  assert.deepEqual(chunks.map((chunk) => chunk.highway_id), [3, 4, 3, 4]);
  assert.throws(() => validateConfig({ ...baseConfig, delay_ms: 999 }), /delay_ms/);
  assert.throws(() => validateConfig({ ...baseConfig, chunk_days: 8 }), /chunk_days/);
  assert.doesNotThrow(() => validateConfig({
    ...baseConfig,
    start_date: "2024-01-01",
    end_date: "2026-07-31",
  }));
});

test("single-day campaigns do not send requests for excluded weekend dates", () => {
  const config = validateConfig({
    ...baseConfig,
    start_date: "2025-09-05",
    end_date: "2025-09-08",
  });
  const chunks = buildChunks(config);
  assert.deepEqual(
    [...new Set(chunks.map((chunk) => chunk.start_date))],
    ["2025-09-05", "2025-09-08"],
  );
  assert.equal(chunks.length, 4);
});

test("CSV parsing handles quoted values and Web Mercator converts to lon-lat", () => {
  assert.deepEqual(parseCsvLine('one,"two, too","quote ""inside"""'), ["one", "two, too", 'quote "inside"']);
  const [longitude, latitude] = webMercatorToLonLat(-13661137.0, 5700582.7);
  assert.ok(longitude < -122 && longitude > -123);
  assert.ok(latitude > 45 && latitude < 46);
});

test("normalization aggregates detector lanes without loading the whole campaign", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "commute-help-normalize-"));
  const rawPath = path.join(directory, "raw.csv");
  const normalizedPath = path.join(directory, "normalized.csv");
  await writeFile(rawPath, freewayCsv);
  const [longitude, latitude] = webMercatorToLonLat(-13661137.0, 5700582.7);
  const metadata = {
    highways: new Map([[3, highwayMetadata[0]]]),
    detectors: new Map(detectorMetadata.map((item) => [item.detectorid, item])),
    stations: new Map([[10, { stationid: 10, highwayid: 3, longitude, latitude }]]),
  };
  const result = await normalizeChunk(rawPath, normalizedPath, metadata, validateConfig(baseConfig));
  assert.deepEqual(result, { raw_rows: 2, normalized_rows: 1, rejected_rows: 0 });
  const rows = (await readFile(normalizedPath, "utf8")).trim().split("\n");
  const values = parseCsvLine(rows[1]);
  assert.equal(values[0], "portal-station-10");
  assert.equal(values[3], "NORTH");
  assert.equal(Number(values[7]), 120);
  assert.ok(Math.abs(Number(values[6]) - 69.73824) < 0.0001);
  assert.equal(Number(values[8]), 10);
  assert.equal(values[9], "good");
});

test("streaming download enforces the response byte limit", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "commute-help-download-"));
  await assert.rejects(
    downloadWithRetry(
      new URL("https://example.test/data"),
      path.join(directory, "too-big.csv"),
      { max_retries: 0, request_timeout_ms: 10000, max_response_bytes: 4 },
      async () => new Response("12345", { status: 200 }),
    ),
    /exceeded 4 bytes/,
  );
});

test("a verified campaign rerun makes no additional HTTP requests", async () => {
  const outputRoot = await mkdtemp(path.join(os.tmpdir(), "commute-help-campaign-"));
  const config = validateConfig({ ...baseConfig, end_date: baseConfig.start_date, highway_ids: [3] });
  let requestCount = 0;
  const fetchImpl = async (url) => {
    requestCount += 1;
    const pathname = new URL(url).pathname;
    if (pathname.endsWith("highwaymetadata/")) return jsonResponse(highwayMetadata);
    if (pathname.endsWith("detectormetadata/")) return jsonResponse(detectorMetadata);
    if (pathname.endsWith("stationmetadata/")) return jsonResponse(stationMetadata);
    if (pathname.endsWith("freewaydata/")) return new Response(freewayCsv, { status: 200, headers: { "content-type": "text/csv" } });
    return new Response("not found", { status: 404 });
  };
  const limiter = async () => {};
  const first = await runCampaign(config, { outputRoot, fetchImpl, limiter });
  assert.equal(first.processed, 1);
  assert.equal(requestCount, 4);
  const second = await runCampaign(config, { outputRoot, fetchImpl, limiter });
  assert.equal(second.processed, 0);
  assert.equal(requestCount, 4);
  const manifest = JSON.parse(await readFile(path.join(outputRoot, "campaign-manifest.json"), "utf8"));
  const complete = Object.values(manifest.chunks)[0];
  assert.equal(complete.status, "complete");
  assert.equal(complete.normalized_rows, 1);
  assert.equal(complete.raw_rows, 2);
  assert.match(complete.raw.sha256, /^[a-f0-9]{64}$/);
});

function jsonResponse(value) {
  return new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
}
