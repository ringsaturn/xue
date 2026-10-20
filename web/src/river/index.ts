/**
 * A showcase case's river directory (`docs/river.md`, version 1): the
 * index the catalog row's `river` block names, held to the same rules the
 * writer's validator holds it to (`xuebuild/river/schema.py`), and the
 * gauge lines it points into.
 *
 * Everything here validates before it trusts and throws `RiverIndexError`
 * on the first rule broken; `fetchRiverIndex` turns that into one console
 * warning and a null, so a case whose river data this shell cannot read
 * still plays its field. Nothing here touches the map.
 */

import { fetchImmutable } from "../fetchimmutable";
import { fetchSpanBytes } from "../stations/fetch";
import type { ShowcaseRiver } from "../showcase-catalog";

export const RIVER_INDEX_VERSION = 1;
export const GAUGES_FILENAME = "gauges.jsonl";

export type RiverKind = "network" | "grid";
export type RiverTrackKind = "analysis" | "forecast";

/** A file the index names, relative to it, requested under its `crc32`. */
export interface RiverFile {
  path: string;
  byteLength: number;
  crc32: string;
}

/** The reaches' GeoParquet (`zarr-profile.md`, "Network store"). */
export interface RiverGeometry extends RiverFile {
  featureCount: number;
  rowGroupRows: number;
}

/** One network store: an analysis window or one forecast cycle. */
export interface RiverTrack {
  id: string;
  kind: RiverTrackKind;
  runTime: string;
  /** `runTime` as epoch milliseconds. */
  runTimeMs: number;
  store: RiverFile;
}

/** The byte span of one line of `gauges.jsonl`. */
export interface RiverSpan {
  path: string;
  offset: number;
  length: number;
}

/** A reported peak: a time where the report gives one, the value, its
 * unit where it is not the gauge's (a stage), and who said so. */
export interface RiverPeak {
  time?: string;
  timeMs?: number;
  value: number;
  unit?: string;
  note?: string;
}

export interface RiverGauge {
  id: string;
  name: string;
  lon: number;
  lat: number;
  source: string;
  unit: string;
  /** Network only: the GeoParquet row, which is the store's feature. */
  feature?: number;
  /** Grid only: the model cell the gauge was snapped to. */
  sample?: [number, number];
  series: RiverSpan;
  outflow?: RiverSpan;
  peaks: RiverPeak[];
}

/** A mapped flood extent, drawn from `at` on with its credit. */
export interface RiverExtent extends RiverFile {
  at: string;
  atMs: number;
  credit: string;
}

export interface RiverIndex {
  version: 1;
  kind: RiverKind;
  /** Present exactly on a network index. */
  geometry?: RiverGeometry;
  /** Non-empty on a network index, empty on a grid one. */
  tracks: RiverTrack[];
  gauges: RiverGauge[];
  extent: RiverExtent[];
  credits: string[];
  disclaimer?: string;
}

export class RiverIndexError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RiverIndexError";
  }
}

const CRC32 = /^[0-9a-f]{8}$/;
const SLUG = /^[a-z0-9][a-z0-9_.-]*$/;
const GAUGE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const UNIT = /^[A-Za-z0-9/^.·°%-]+$/;
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z$/;

const INDEX_KEYS = new Set(["version", "kind", "geometry", "tracks", "gauges", "extent", "credits", "disclaimer"]);
const GAUGE_KEYS = new Set(["id", "name", "lon", "lat", "source", "unit", "feature", "sample", "series", "outflow", "peaks"]);
const PEAK_KEYS = new Set(["time", "value", "unit", "note"]);
const LINE_KEYS = new Set(["id", "time", "value", "stage", "stageUnit"]);

function fail(message: string): never {
  throw new RiverIndexError(message);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function keysWithin(value: Record<string, unknown>, allowed: ReadonlySet<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function keysExactly(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => key in value);
}

/** A path relative to the index: no leading `/`, no scheme, no backslash,
 * no `..`, `.` or empty component, no query or fragment. */
export function isRelativePath(value: unknown): value is string {
  if (typeof value !== "string" || value === "" || value.startsWith("/") || value.endsWith("/")) return false;
  if (/[\\:?#]/.test(value)) return false;
  return value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

/** An ISO 8601 UTC instant ending in `Z`, as epoch milliseconds. */
function time(value: unknown, label: string): number {
  if (typeof value !== "string" || !ISO_UTC.test(value)) fail(`${label} must be an ISO 8601 UTC timestamp ending in Z`);
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) fail(`${label} is not a valid timestamp`);
  return ms;
}

function finite(value: unknown, label: string, low = -Infinity, high = Infinity): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < low || value > high) {
    fail(`${label} must be a finite number in [${low}, ${high}]`);
  }
  return value;
}

function count(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    fail(`${label} must be an integer of at least ${minimum}`);
  }
  return value;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim() === "") fail(`${label} must be a non-empty string`);
  return value;
}

function file(value: Record<string, unknown>, label: string, suffix: string): RiverFile {
  if (!isRelativePath(value.path)) fail(`${label}.path must be a relative path inside the river directory`);
  if (!value.path.endsWith(suffix)) fail(`${label}.path must name a ${suffix} file`);
  const byteLength = count(value.byteLength, `${label}.byteLength`, 1);
  if (typeof value.crc32 !== "string" || !CRC32.test(value.crc32)) fail(`${label}.crc32 must be 8 lowercase hex characters`);
  return { path: value.path, byteLength, crc32: value.crc32 };
}

function span(value: unknown, label: string): RiverSpan {
  if (!isObject(value) || !keysExactly(value, ["path", "offset", "length"])) fail(`${label} must be {path, offset, length}`);
  if (value.path !== GAUGES_FILENAME) fail(`${label}.path must be ${GAUGES_FILENAME}`);
  return { path: GAUGES_FILENAME, offset: count(value.offset, `${label}.offset`), length: count(value.length, `${label}.length`, 2) };
}

function geometryBlock(value: unknown): RiverGeometry {
  const keys = ["path", "byteLength", "crc32", "featureCount", "rowGroupRows"];
  if (!isObject(value) || !keysExactly(value, keys)) fail(`geometry must be {${keys.join(", ")}}`);
  return {
    ...file(value, "geometry", ".parquet"),
    featureCount: count(value.featureCount, "geometry.featureCount", 1),
    rowGroupRows: count(value.rowGroupRows, "geometry.rowGroupRows", 1),
  };
}

function tracksBlock(value: unknown): RiverTrack[] {
  if (!Array.isArray(value) || value.length === 0) fail("tracks must be a non-empty list");
  const seen = new Set<string>();
  return value.map((track: unknown, position) => {
    const where = `tracks[${position}]`;
    if (!isObject(track) || !keysExactly(track, ["id", "kind", "runTime", "store"])) fail(`${where} must be {id, kind, runTime, store}`);
    if (typeof track.id !== "string" || !SLUG.test(track.id) || seen.has(track.id)) fail(`${where}.id must be a unique slug`);
    seen.add(track.id);
    if (track.kind !== "analysis" && track.kind !== "forecast") fail(`${where}.kind must be analysis or forecast`);
    const runTimeMs = time(track.runTime, `${where}.runTime`);
    if (!isObject(track.store) || !keysExactly(track.store, ["path", "byteLength", "crc32"])) {
      fail(`${where}.store must be {path, byteLength, crc32}`);
    }
    return {
      id: track.id,
      kind: track.kind,
      runTime: track.runTime as string,
      runTimeMs,
      store: file(track.store, `${where}.store`, ".zarr"),
    };
  });
}

function gaugesBlock(value: unknown, featureCount: number | null): RiverGauge[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) fail("gauges must be a list");
  const seen = new Set<string>();
  return value.map((gauge: unknown, position) => {
    let where = `gauges[${position}]`;
    if (!isObject(gauge) || !keysWithin(gauge, GAUGE_KEYS)) fail(`${where} has a key outside {${[...GAUGE_KEYS].join(", ")}}`);
    if (typeof gauge.id !== "string" || !GAUGE_ID.test(gauge.id) || seen.has(gauge.id)) fail(`${where}.id must be a unique identifier`);
    seen.add(gauge.id);
    where = `gauge ${gauge.id}`;
    const entry: RiverGauge = {
      id: gauge.id,
      name: text(gauge.name, `${where}.name`),
      source: text(gauge.source, `${where}.source`),
      unit: typeof gauge.unit === "string" && UNIT.test(gauge.unit) ? gauge.unit : fail(`${where}.unit must be a unit symbol`),
      lon: finite(gauge.lon, `${where}.lon`, -180, 180),
      lat: finite(gauge.lat, `${where}.lat`, -90, 90),
      series: span(gauge.series, `${where}.series`),
      peaks: [],
    };
    if (featureCount !== null) {
      const feature = count(gauge.feature, `${where}.feature`);
      if (feature >= featureCount) fail(`${where}.feature is past the geometry's last row`);
      entry.feature = feature;
    } else if ("feature" in gauge) {
      fail(`${where}: feature belongs to a network index only`);
    }
    if ("sample" in gauge) {
      const sample = gauge.sample;
      if (!Array.isArray(sample) || sample.length !== 2) fail(`${where}.sample must be [lon, lat]`);
      entry.sample = [finite(sample[0], `${where}.sample[0]`, -180, 180), finite(sample[1], `${where}.sample[1]`, -90, 90)];
    }
    if ("outflow" in gauge) entry.outflow = span(gauge.outflow, `${where}.outflow`);
    const peaks = gauge.peaks ?? [];
    if (!Array.isArray(peaks)) fail(`${where}.peaks must be a list`);
    entry.peaks = peaks.map((peak: unknown, index) => {
      const label = `${where}.peaks[${index}]`;
      if (!isObject(peak) || !keysWithin(peak, PEAK_KEYS) || !("value" in peak)) fail(`${label} must be {time?, value, unit?, note?}`);
      const out: RiverPeak = { value: finite(peak.value, `${label}.value`) };
      if ("time" in peak) {
        out.timeMs = time(peak.time, `${label}.time`);
        out.time = peak.time as string;
      }
      if ("unit" in peak) {
        if (typeof peak.unit !== "string" || !UNIT.test(peak.unit)) fail(`${label}.unit must be a unit symbol`);
        out.unit = peak.unit;
      }
      if ("note" in peak) out.note = text(peak.note, `${label}.note`);
      return out;
    });
    return entry;
  });
}

function extentBlock(value: unknown): RiverExtent[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) fail("extent must be a list");
  return value.map((item: unknown, position) => {
    const where = `extent[${position}]`;
    const keys = ["path", "byteLength", "crc32", "at", "credit"];
    if (!isObject(item) || !keysExactly(item, keys)) fail(`${where} must be {${keys.join(", ")}}`);
    return {
      ...file(item, where, ".geojson"),
      atMs: time(item.at, `${where}.at`),
      at: item.at as string,
      credit: text(item.credit, `${where}.credit`),
    };
  });
}

/** The index, validated (`docs/river.md` §2). Throws `RiverIndexError`. */
export function parseRiverIndex(input: unknown): RiverIndex {
  if (!isObject(input) || !keysWithin(input, INDEX_KEYS)) fail(`a river index is an object of {${[...INDEX_KEYS].join(", ")}}`);
  if (input.version !== RIVER_INDEX_VERSION) fail(`river index version must be ${RIVER_INDEX_VERSION}`);
  const kind = input.kind;
  if (kind !== "network" && kind !== "grid") fail("kind must be network or grid");
  const network = kind === "network";
  if (network !== "geometry" in input || network !== "tracks" in input) {
    fail("geometry and tracks are present on a network index and absent on a grid one");
  }
  const geometry = network ? geometryBlock(input.geometry) : undefined;
  const tracks = network ? tracksBlock(input.tracks) : [];
  const gauges = gaugesBlock(input.gauges, geometry ? geometry.featureCount : null);
  const extent = extentBlock(input.extent);
  const credits = input.credits ?? [];
  if (!Array.isArray(credits) || credits.some((item) => typeof item !== "string" || item.trim() === "")) {
    fail("credits must be a list of non-empty strings");
  }
  const index: RiverIndex = { version: 1, kind, tracks, gauges, extent, credits: credits as string[] };
  if (geometry) index.geometry = geometry;
  if ("disclaimer" in input) index.disclaimer = text(input.disclaimer, "disclaimer");
  if (!network && gauges.length === 0 && extent.length === 0) fail("a grid index carries gauges or extents, or it says nothing");
  return index;
}

/** One gauge line (`docs/river.md` §3): times as epoch milliseconds, the
 * values with `null` for no reading. The stage is read past, not kept. */
export interface GaugeSeries {
  id: string;
  times: number[];
  values: (number | null)[];
}

export function parseGaugeLine(bytes: Uint8Array, expectedId: string): GaugeSeries {
  let line: unknown;
  try {
    line = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    fail(`gauge ${expectedId}: its span is not one JSON object`);
  }
  if (!isObject(line) || !keysWithin(line, LINE_KEYS)) fail(`gauge ${expectedId}: line must be {id, time, value, stage?, stageUnit?}`);
  if (line.id !== expectedId) fail(`gauge ${expectedId}: the span holds the line of ${String(line.id)}`);
  const times = line.time;
  const values = line.value;
  if (!Array.isArray(times) || !Array.isArray(values) || times.length === 0 || times.length !== values.length) {
    fail(`gauge ${expectedId}: time and value must be non-empty lists of one length`);
  }
  const parsed: number[] = [];
  for (const [index, moment] of times.entries()) {
    const ms = time(moment, `gauge ${expectedId}.time[${index}]`);
    if (index > 0 && ms <= parsed[index - 1]!) fail(`gauge ${expectedId}: times must be strictly increasing`);
    parsed.push(ms);
  }
  const read = values.map((value: unknown) => (value === null ? null : finite(value, `gauge ${expectedId}.value[]`)));
  return { id: expectedId, times: parsed, values: read };
}

/** A path beside the index, as an absolute URL with no query: a store's
 * root, which `ZarrStore` versions object by object. */
export function riverPathUrl(indexUrl: string, path: string): string {
  const url = new URL(path, indexUrl);
  url.search = "";
  return url.href;
}

/** A file's URL beside the index, under its own `?v=`. */
export function riverFileUrl(indexUrl: string, path: string, crc32: string): string {
  const url = new URL(riverPathUrl(indexUrl, path));
  url.searchParams.set("v", crc32);
  return url.href;
}

export interface LoadedRiverIndex {
  index: RiverIndex;
  /** The index's absolute URL, every other path resolving against it. */
  indexUrl: string;
  /** The index's own CRC: the `?v=` of `gauges.jsonl`. */
  crc32: string;
}

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

/** Fetch the catalog row's index under its CRC and validate it. A refusal
 * or a failed request is one console warning and null: the case then
 * plays without its river data. */
export async function fetchRiverIndex(
  baseUrl: string,
  block: ShowcaseRiver,
  fetchImpl: FetchLike = (url, init) => fetchImmutable(url, init),
  base: string = typeof document === "undefined" ? "http://localhost/" : document.baseURI,
): Promise<LoadedRiverIndex | null> {
  const url = new URL(block.indexPath, new URL(baseUrl, base));
  url.searchParams.set("v", block.crc32);
  try {
    const response = await fetchImpl(url.href);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const index = parseRiverIndex(await response.json());
    return { index, indexUrl: url.href, crc32: block.crc32 };
  } catch (error) {
    console.warn(`river: index ${block.indexPath} not read; playing the case without it:`, error instanceof Error ? error.message : error);
    return null;
  }
}

/** One gauge line by its byte span: `gauges.jsonl` is immutable with the
 * directory, so it is requested under the index's CRC. */
export async function fetchGaugeSeries(loaded: LoadedRiverIndex, gaugeId: string, which: RiverSpan): Promise<GaugeSeries> {
  const url = riverFileUrl(loaded.indexUrl, which.path, loaded.crc32);
  const bytes = await fetchSpanBytes(url, which.offset, which.length);
  return parseGaugeLine(bytes, gaugeId);
}
