#!/usr/bin/env node

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { Readable, Transform } from "node:stream";
import { finished, pipeline } from "node:stream/promises";
import { pathToFileURL } from "node:url";

export const PORTAL_BASE_URL = "https://new.portal.its.pdx.edu";
const MANIFEST_VERSION = 2;
const RUNTIME_CONFIG_KEYS = [
  "delay_ms",
  "request_timeout_ms",
  "max_retries",
  "max_response_bytes",
];
const NORMALIZED_COLUMNS = [
  "station_or_segment_id",
  "timestamp_local",
  "timezone",
  "direction",
  "latitude",
  "longitude",
  "speed_kph",
  "volume",
  "occupancy",
  "quality_flag",
  "source",
];

const METADATA_ENDPOINTS = {
  highways: "/highways/api/highwaymetadata/",
  detectors: "/highways/api/detectormetadata/",
  stations: "/highways/api/stationmetadata/",
};

export function validateConfig(input) {
  const config = {
    ...input,
    resolution: input.resolution ?? "00:15:00",
    days_of_week: input.days_of_week ?? [2, 3, 4, 5, 6],
    chunk_days: input.chunk_days ?? 1,
    delay_ms: input.delay_ms ?? 3000,
    request_timeout_ms: input.request_timeout_ms ?? 120000,
    max_retries: input.max_retries ?? 5,
    max_response_bytes: input.max_response_bytes ?? 25 * 1024 * 1024,
    max_groups_per_chunk: input.max_groups_per_chunk ?? 250000,
  };
  if (!/^[a-z0-9][a-z0-9._-]{0,79}$/i.test(config.name ?? "")) {
    throw new Error("name must be 1-80 safe filename characters");
  }
  const start = parseDate(config.start_date, "start_date");
  const end = parseDate(config.end_date, "end_date");
  if (end < start) throw new Error("end_date must be on or after start_date");
  const campaignDays = Math.round((end - start) / 86_400_000) + 1;
  if (campaignDays > 1096) {
    throw new Error("a campaign is limited to 1,096 days; use multiple named campaigns");
  }
  if (!Array.isArray(config.highway_ids) || config.highway_ids.length === 0) {
    throw new Error("highway_ids must contain at least one PORTAL highway ID");
  }
  if (config.highway_ids.length > 64) throw new Error("highway_ids is limited to 64 IDs");
  config.highway_ids = [...new Set(config.highway_ids.map((value) => positiveInteger(value, "highway_id")))];
  if (!["00:15:00", "01:00:00"].includes(config.resolution)) {
    throw new Error("resolution must be 00:15:00 or 01:00:00");
  }
  if (!Array.isArray(config.days_of_week) || config.days_of_week.length === 0) {
    throw new Error("days_of_week must contain PORTAL weekday values 1 through 7");
  }
  config.days_of_week = [...new Set(config.days_of_week.map((value) => positiveInteger(value, "day_of_week")))];
  if (config.days_of_week.some((day) => day > 7)) throw new Error("days_of_week values must be 1 through 7");
  integerRange(config.chunk_days, 1, 7, "chunk_days");
  integerRange(config.delay_ms, 100, 60_000, "delay_ms");
  integerRange(config.request_timeout_ms, 10_000, 600_000, "request_timeout_ms");
  integerRange(config.max_retries, 0, 10, "max_retries");
  integerRange(config.max_response_bytes, 1_048_576, 100 * 1024 * 1024, "max_response_bytes");
  integerRange(config.max_groups_per_chunk, 100, 1_000_000, "max_groups_per_chunk");
  return config;
}

export function buildChunks(config) {
  const chunks = [];
  const end = parseDate(config.end_date, "end_date");
  for (let cursor = parseDate(config.start_date, "start_date"); cursor <= end;) {
    const chunkEnd = new Date(Math.min(
      end.getTime(),
      cursor.getTime() + (config.chunk_days - 1) * 86_400_000,
    ));
    const portalWeekday = cursor.getUTCDay() + 1;
    const skipSingleExcludedDay = config.chunk_days === 1
      && !config.days_of_week.includes(portalWeekday);
    if (skipSingleExcludedDay) {
      cursor = new Date(chunkEnd.getTime() + 86_400_000);
      continue;
    }
    for (const highwayId of config.highway_ids) {
      const startDate = formatDate(cursor);
      const endDate = formatDate(chunkEnd);
      chunks.push({
        id: `highway-${highwayId}_${startDate}_${endDate}_${config.resolution.replaceAll(":", "-")}`,
        highway_id: highwayId,
        start_date: startDate,
        end_date: endDate,
      });
    }
    cursor = new Date(chunkEnd.getTime() + 86_400_000);
  }
  return chunks;
}

export function parseCsvLine(line) {
  const fields = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quoted) {
      if (character === '"' && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        value += character;
      }
    } else if (character === '"') {
      quoted = true;
    } else if (character === ",") {
      fields.push(value);
      value = "";
    } else {
      value += character;
    }
  }
  if (quoted) throw new Error("unterminated quoted CSV field");
  fields.push(value.replace(/\r$/, ""));
  return fields;
}

export function csvCell(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function webMercatorToLonLat(x, y) {
  const longitude = (Number(x) / 20_037_508.34) * 180;
  const latitudeRadians = 2 * Math.atan(Math.exp(Number(y) / 6_378_137)) - Math.PI / 2;
  return [longitude, latitudeRadians * 180 / Math.PI];
}

export async function loadMetadata(metadataDirectory) {
  const [highwaysPayload, detectorsPayload, stationsPayload] = await Promise.all([
    readJson(path.join(metadataDirectory, "highways.json")),
    readJson(path.join(metadataDirectory, "detectors.json")),
    readJson(path.join(metadataDirectory, "stations.json")),
  ]);
  if (!Array.isArray(highwaysPayload) || !Array.isArray(detectorsPayload)) {
    throw new Error("PORTAL highway or detector metadata has an unexpected format");
  }
  if (!Array.isArray(stationsPayload?.features)) {
    throw new Error("PORTAL station metadata is not a GeoJSON feature collection");
  }
  const highways = new Map(highwaysPayload.map((item) => [Number(item.highwayid), item]));
  const detectors = new Map(detectorsPayload.map((item) => [Number(item.detectorid), item]));
  const stations = new Map();
  for (const feature of stationsPayload.features) {
    const properties = { ...(feature.properties ?? {}) };
    const coordinates = feature.geometry?.coordinates;
    if (Array.isArray(coordinates) && coordinates.length >= 2) {
      const x = Number(coordinates[0]);
      const y = Number(coordinates[1]);
      const [longitude, latitude] = Math.abs(x) <= 180 && Math.abs(y) <= 90
        ? [x, y]
        : webMercatorToLonLat(x, y);
      properties.longitude = longitude;
      properties.latitude = latitude;
    }
    if (Number.isInteger(Number(properties.stationid))) {
      stations.set(Number(properties.stationid), properties);
    }
  }
  return { highways, detectors, stations };
}

export async function normalizeChunk(rawPath, normalizedPath, metadata, config, chunk) {
  const input = createReadStream(rawPath, { encoding: "utf8" });
  const lines = readline.createInterface({ input, crlfDelay: Infinity });
  let headers;
  let rawRows = 0;
  let rejectedRows = 0;
  let outOfWindowRows = 0;
  const groups = new Map();
  const resolutionSeconds = resolutionToSeconds(config.resolution);
  const hourlyFactor = 3600 / resolutionSeconds;
  const expectedReadings = Math.max(resolutionSeconds / 20, 1);

  for await (const line of lines) {
    if (!headers) {
      headers = parseCsvLine(line);
      for (const required of ["starttime", "detector_id", "speed", "volume", "occupancy", "countreadings"]) {
        if (!headers.includes(required)) throw new Error(`PORTAL CSV is missing ${required}`);
      }
      continue;
    }
    if (!line.trim()) continue;
    rawRows += 1;
    const values = parseCsvLine(line);
    const row = Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
    const observationDate = row.starttime.slice(0, 10);
    if (chunk && (observationDate < chunk.start_date || observationDate > chunk.end_date)) {
      outOfWindowRows += 1;
      continue;
    }
    const detector = metadata.detectors.get(Number(row.detector_id));
    const station = detector ? metadata.stations.get(Number(detector.stationid)) : undefined;
    const highway = detector ? metadata.highways.get(Number(detector.highwayid)) : undefined;
    if (!detector || !station || !highway || !Number.isFinite(station.latitude) || !Number.isFinite(station.longitude)) {
      rejectedRows += 1;
      continue;
    }
    const key = `${station.stationid}\u0000${row.starttime}\u0000${highway.direction ?? "unknown"}`;
    let group = groups.get(key);
    if (!group) {
      if (groups.size >= config.max_groups_per_chunk) {
        throw new Error(`chunk exceeded max_groups_per_chunk (${config.max_groups_per_chunk})`);
      }
      group = {
        stationId: Number(station.stationid),
        timestamp: row.starttime,
        direction: highway.direction ?? "unknown",
        latitude: Number(station.latitude),
        longitude: Number(station.longitude),
        volumes: [],
        speeds: [],
        occupancies: [],
        lowSample: false,
      };
      groups.set(key, group);
    }
    const volume = finiteNumber(row.volume);
    const speedMph = finiteNumber(row.speed);
    const occupancy = finiteNumber(row.occupancy);
    const countReadings = finiteNumber(row.countreadings) ?? 0;
    group.volumes.push(volume);
    group.speeds.push(speedMph === null ? null : speedMph * 1.609344);
    if (occupancy !== null) group.occupancies.push(occupancy);
    if (countReadings < expectedReadings * 0.5) group.lowSample = true;
  }
  if (!headers) throw new Error("PORTAL returned an empty CSV file");

  await mkdir(path.dirname(normalizedPath), { recursive: true });
  const temporary = `${normalizedPath}.part`;
  const output = createWriteStream(temporary, { encoding: "utf8" });
  output.write(`${NORMALIZED_COLUMNS.join(",")}\n`);
  let normalizedRows = 0;
  for (const group of groups.values()) {
    const validVolumes = group.volumes.filter((value) => value !== null);
    const intervalVolume = validVolumes.length ? validVolumes.reduce((sum, value) => sum + value, 0) : null;
    let speedKph = null;
    let weightedSpeed = 0;
    let totalWeight = 0;
    const availableSpeeds = [];
    for (let index = 0; index < group.speeds.length; index += 1) {
      const speed = group.speeds[index];
      const volume = group.volumes[index];
      if (speed !== null) availableSpeeds.push(speed);
      if (speed !== null && volume !== null && volume > 0) {
        weightedSpeed += speed * volume;
        totalWeight += volume;
      }
    }
    if (totalWeight > 0) speedKph = weightedSpeed / totalWeight;
    else if (availableSpeeds.length) speedKph = median(availableSpeeds);
    const occupancy = group.occupancies.length
      ? group.occupancies.reduce((sum, value) => sum + value, 0) / group.occupancies.length
      : null;
    const values = [
      `portal-station-${group.stationId}`,
      group.timestamp,
      "America/Los_Angeles",
      group.direction,
      group.latitude,
      group.longitude,
      speedKph,
      intervalVolume === null ? null : intervalVolume * hourlyFactor,
      occupancy,
      group.lowSample ? "low_sample" : "good",
      "PORTAL Highways API campaign",
    ];
    output.write(`${values.map(csvCell).join(",")}\n`);
    normalizedRows += 1;
  }
  output.end();
  await finished(output);
  await rename(temporary, normalizedPath);
  return {
    raw_rows: rawRows,
    normalized_rows: normalizedRows,
    rejected_rows: rejectedRows,
    out_of_window_rows: outOfWindowRows,
  };
}

export async function runCampaign(configInput, options = {}) {
  const config = validateConfig(configInput);
  const outputRoot = path.resolve(options.outputRoot ?? path.join("data", "traffic", "campaigns", config.name));
  const metadataDirectory = path.join(outputRoot, "metadata");
  const rawDirectory = path.join(outputRoot, "raw");
  const normalizedDirectory = path.join(outputRoot, "normalized");
  const manifestPath = path.join(outputRoot, "campaign-manifest.json");
  await Promise.all([mkdir(metadataDirectory, { recursive: true }), mkdir(rawDirectory, { recursive: true }), mkdir(normalizedDirectory, { recursive: true })]);

  const chunks = buildChunks(config);
  if (options.dryRun) {
    return { outputRoot, chunks, config };
  }
  const limiter = options.limiter ?? createRequestLimiter(config.delay_ms);
  const fetchImpl = options.fetchImpl ?? fetch;
  const manifest = await loadOrCreateManifest(manifestPath, config, chunks);
  await acquireMetadata({ config, metadataDirectory, manifest, manifestPath, fetchImpl, limiter });
  const metadata = await loadMetadata(metadataDirectory);
  let processed = 0;
  for (const chunk of chunks) {
    if (options.maxChunks !== undefined && processed >= options.maxChunks) break;
    const prior = manifest.chunks[chunk.id];
    if (!options.force && await completedChunkIsValid(prior, outputRoot)) {
      console.log(`skip ${chunk.id} (verified)`);
      continue;
    }
    const rawRelative = path.join("raw", `${chunk.id}.csv`);
    const normalizedRelative = path.join("normalized", `${chunk.id}.csv`);
    const rawPath = path.join(outputRoot, rawRelative);
    const normalizedPath = path.join(outputRoot, normalizedRelative);
    const url = buildFreewayUrl(chunk, config);
    console.log(`download ${chunk.id}`);
    manifest.chunks[chunk.id] = { ...chunk, status: "downloading", request_url: url.toString(), updated_at: now() };
    await writeJsonAtomic(manifestPath, manifest);
    try {
      const raw = await downloadWithRetry(url, rawPath, config, fetchImpl, limiter);
      const counts = await normalizeChunk(rawPath, normalizedPath, metadata, config, chunk);
      const normalized = { bytes: (await stat(normalizedPath)).size, sha256: await sha256File(normalizedPath) };
      manifest.chunks[chunk.id] = {
        ...chunk,
        status: "complete",
        request_url: url.toString(),
        raw: { path: rawRelative, ...raw },
        normalized: { path: normalizedRelative, ...normalized },
        ...counts,
        completed_at: now(),
      };
      manifest.updated_at = now();
      await writeJsonAtomic(manifestPath, manifest);
      processed += 1;
    } catch (error) {
      manifest.chunks[chunk.id] = { ...chunk, status: "failed", request_url: url.toString(), error: safeError(error), updated_at: now() };
      manifest.updated_at = now();
      await writeJsonAtomic(manifestPath, manifest);
      throw error;
    }
  }
  return { outputRoot, chunks, manifest, processed };
}

async function acquireMetadata({ config, metadataDirectory, manifest, manifestPath, fetchImpl, limiter }) {
  for (const [name, endpoint] of Object.entries(METADATA_ENDPOINTS)) {
    const target = path.join(metadataDirectory, `${name}.json`);
    const prior = manifest.metadata[name];
    if (prior && await fileMatches(target, prior)) continue;
    const url = new URL(endpoint, PORTAL_BASE_URL);
    url.searchParams.set("format", "json");
    console.log(`metadata ${name}`);
    const result = await downloadWithRetry(url, target, { ...config, max_response_bytes: Math.min(config.max_response_bytes, 10 * 1024 * 1024) }, fetchImpl, limiter);
    manifest.metadata[name] = { path: path.relative(path.dirname(manifestPath), target), endpoint, ...result, completed_at: now() };
    manifest.updated_at = now();
    await writeJsonAtomic(manifestPath, manifest);
  }
}

export async function downloadWithRetry(url, targetPath, config, fetchImpl, limiter = async () => {}) {
  let lastError;
  for (let attempt = 0; attempt <= config.max_retries; attempt += 1) {
    await limiter();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), config.request_timeout_ms);
    try {
      const response = await fetchImpl(url, {
        headers: {
          Accept: "text/csv, application/json;q=0.9",
          "User-Agent": "Commute-Help/1.0 controlled local PORTAL data campaign",
          Referer: `${PORTAL_BASE_URL}/downloads/`,
        },
        redirect: "follow",
        signal: controller.signal,
      });
      if (!response.ok) {
        const error = new Error(`PORTAL returned HTTP ${response.status}`);
        error.status = response.status;
        error.retryAfter = response.headers.get("retry-after");
        if (response.status === 403) throw error;
        if (response.status !== 429 && response.status < 500) throw error;
        throw error;
      }
      const declaredLength = Number(response.headers.get("content-length"));
      if (Number.isFinite(declaredLength) && declaredLength > config.max_response_bytes) {
        const error = new Error(`response declares ${declaredLength} bytes, over limit ${config.max_response_bytes}`);
        error.nonRetryable = true;
        throw error;
      }
      if (!response.body) throw new Error("PORTAL returned no response body");
      await mkdir(path.dirname(targetPath), { recursive: true });
      const temporary = `${targetPath}.part`;
      const hash = createHash("sha256");
      let bytes = 0;
      const meter = new Transform({
        transform(chunk, _encoding, callback) {
          bytes += chunk.length;
          if (bytes > config.max_response_bytes) {
            callback(new Error(`response exceeded ${config.max_response_bytes} bytes`));
            return;
          }
          hash.update(chunk);
          callback(null, chunk);
        },
      });
      try {
        await unlink(temporary).catch(() => {});
        await pipeline(Readable.fromWeb(response.body), meter, createWriteStream(temporary, { flags: "wx" }));
        await rename(temporary, targetPath);
      } catch (error) {
        await unlink(temporary).catch(() => {});
        throw error;
      }
      return { bytes, sha256: hash.digest("hex") };
    } catch (error) {
      lastError = error;
      const retryable = !error.nonRetryable
        && (error.name === "AbortError" || error.status === 429 || error.status >= 500 || error.status === undefined);
      if (!retryable || attempt >= config.max_retries) throw error;
      const waitMs = retryDelay(error.retryAfter, attempt);
      console.warn(`retry ${attempt + 1}/${config.max_retries} after ${waitMs}ms: ${safeError(error)}`);
      await sleep(waitMs);
    } finally {
      clearTimeout(timeout);
    }
  }
  throw lastError;
}

export function buildFreewayUrl(chunk, config) {
  const url = new URL("/highways/api/freewaydata/", PORTAL_BASE_URL);
  url.searchParams.set("start_date", chunk.start_date);
  url.searchParams.set("end_date", addDays(chunk.end_date, 1));
  for (const day of config.days_of_week) url.searchParams.append("days_of_week", String(day));
  url.searchParams.set("format", "csv");
  url.searchParams.append("highway_id", String(chunk.highway_id));
  url.searchParams.set("resolution", config.resolution);
  return url;
}

function createRequestLimiter(delayMs) {
  let lastStarted = 0;
  return async () => {
    const remaining = delayMs - (Date.now() - lastStarted);
    if (remaining > 0) await sleep(remaining);
    lastStarted = Date.now();
  };
}

function retryDelay(retryAfter, attempt) {
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds)) return Math.min(Math.max(seconds * 1000, 1000), 300_000);
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) return Math.min(Math.max(date - Date.now(), 1000), 300_000);
  }
  return Math.min(2000 * 2 ** attempt, 120_000);
}

async function loadOrCreateManifest(manifestPath, config, chunks) {
  try {
    const manifest = await readJson(manifestPath);
    if (manifest.schema_version !== MANIFEST_VERSION) {
      throw new Error(
        `campaign manifest uses acquisition schema ${manifest.schema_version}; `
        + `use a new campaign name for schema ${MANIFEST_VERSION}`,
      );
    }
    if (JSON.stringify(dataConfig(manifest.config)) !== JSON.stringify(dataConfig(config))) {
      throw new Error(
        "data-defining campaign config changed; use a new campaign name or restore "
        + "the dates, highways, resolution, weekdays, chunking, and normalization limits",
      );
    }
    const runtimeChanges = changedRuntimeConfig(manifest.config, config);
    if (Object.keys(runtimeChanges).length > 0) {
      manifest.runtime_config_history ??= [];
      manifest.runtime_config_history.push({ changed_at: now(), changes: runtimeChanges });
      manifest.config = { ...manifest.config };
      for (const key of RUNTIME_CONFIG_KEYS) manifest.config[key] = config[key];
      manifest.access_policy.configured_delay_ms = config.delay_ms;
      manifest.updated_at = now();
      await writeJsonAtomic(manifestPath, manifest);
    }
    return manifest;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const manifest = {
    schema_version: MANIFEST_VERSION,
    created_at: now(),
    updated_at: now(),
    source: PORTAL_BASE_URL,
    access_policy: {
      published_numeric_rate_limit: null,
      concurrency: 1,
      configured_delay_ms: config.delay_ms,
      chunk_count: chunks.length,
      note: "Public tokenless GET API. PORTAL requires date-constrained large-data requests; no numeric quota was published as of 2026-08-01.",
    },
    config,
    metadata: {},
    chunks: {},
  };
  await writeJsonAtomic(manifestPath, manifest);
  return manifest;
}

function dataConfig(config) {
  return Object.fromEntries(
    Object.entries(config)
      .filter(([key]) => !RUNTIME_CONFIG_KEYS.includes(key))
      .sort(([left], [right]) => left.localeCompare(right)),
  );
}

function changedRuntimeConfig(previous, current) {
  const changes = {};
  for (const key of RUNTIME_CONFIG_KEYS) {
    if (previous[key] !== current[key]) {
      changes[key] = { from: previous[key], to: current[key] };
    }
  }
  return changes;
}

async function completedChunkIsValid(chunk, outputRoot) {
  if (chunk?.status !== "complete" || !chunk.raw || !chunk.normalized) return false;
  return await fileMatches(path.join(outputRoot, chunk.raw.path), chunk.raw)
    && await fileMatches(path.join(outputRoot, chunk.normalized.path), chunk.normalized);
}

async function fileMatches(filePath, record) {
  try {
    const details = await stat(filePath);
    return details.size === record.bytes && await sha256File(filePath) === record.sha256;
  } catch {
    return false;
  }
}

async function sha256File(filePath) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk);
  return hash.digest("hex");
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function writeJsonAtomic(filePath, value) {
  await mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.part`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await rename(temporary, filePath);
}

function finiteNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function median(values) {
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function resolutionToSeconds(value) {
  const [hours, minutes, seconds] = value.split(":").map(Number);
  return hours * 3600 + minutes * 60 + seconds;
}

function positiveInteger(value, name) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) throw new Error(`${name} must be a positive integer`);
  return number;
}

function integerRange(value, minimum, maximum, name) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} through ${maximum}`);
  }
}

function parseDate(value, name) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? "")) throw new Error(`${name} must use YYYY-MM-DD`);
  const result = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(result.getTime()) || formatDate(result) !== value) throw new Error(`${name} is not a real date`);
  return result;
}

function formatDate(value) {
  return value.toISOString().slice(0, 10);
}

function addDays(value, days) {
  const date = parseDate(value, "campaign date");
  date.setUTCDate(date.getUTCDate() + days);
  return formatDate(date);
}

function now() {
  return new Date().toISOString();
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function safeError(error) {
  return error instanceof Error ? error.message : String(error);
}

function parseArguments(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--config") options.configPath = argv[++index];
    else if (argument === "--output-root") options.outputRoot = argv[++index];
    else if (argument === "--max-chunks") options.maxChunks = Number(argv[++index]);
    else if (argument === "--dry-run") options.dryRun = true;
    else if (argument === "--force") options.force = true;
    else if (argument === "--help" || argument === "-h") options.help = true;
    else throw new Error(`unknown argument: ${argument}`);
  }
  if (options.maxChunks !== undefined && (!Number.isInteger(options.maxChunks) || options.maxChunks < 1)) {
    throw new Error("--max-chunks must be a positive integer");
  }
  return options;
}

function printHelp() {
  console.log(`Usage: npm run traffic:campaign -- --config PATH [options]

Options:
  --output-root PATH  Override data/traffic/campaigns/<name>
  --max-chunks N      Process at most N observation chunks this run
  --dry-run           Validate and print the partition plan without network calls
  --force             Re-download chunks instead of resuming verified files
  --help              Show this help`);
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }
  if (!options.configPath) throw new Error("--config PATH is required");
  const config = JSON.parse(await readFile(path.resolve(options.configPath), "utf8"));
  const result = await runCampaign(config, options);
  if (options.dryRun) {
    console.log(`validated ${result.chunks.length} sequential chunks -> ${result.outputRoot}`);
  } else {
    console.log(`campaign safe point: processed ${result.processed} new chunks -> ${result.outputRoot}`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`PORTAL campaign stopped safely: ${safeError(error)}`);
    process.exitCode = 1;
  });
}
