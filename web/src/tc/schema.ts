/** The tropical cyclone product, schema v1, as the shell reads it —
 * `docs/tc.md` is normative and `xuebuild/tc/schema.py` writes what this
 * validates.
 *
 * The shapes are `docs/schemas/tc-{index,storm}.schema.json`, checked by
 * the generated validators; this module adds what a schema cannot say.
 * Structural admission, like the manifest's: an agency or a model key the
 * registry does not know passes, a shape that is wrong or a
 * `schemaVersion` above the one implemented does not. */

import { checkTcIndex } from "../schema/generated/tc-index";
import { checkTcStorm, type TcPoint as TcPointRecord } from "../schema/generated/tc-storm";
import { pointer, schemaVersion, type ProductPointer } from "../schema/validate";

const TC_SCHEMA_VERSION = 1;
export const TC_POINTER_FILENAME = "latest-tc.json";
const TC_MISSING = -32768;

type TcLevel = "A" | "B" | "C";

export type TcPointer = ProductPointer<"tc">;

interface TcPosition {
  time: string;
  lat: number;
  lon: number;
}

export interface TcIndexEntry {
  id: string;
  level: TcLevel;
  basin: string;
  name: string | null;
  path: string;
  byteLength: number;
  crc32: string;
  aliases: Record<string, { from: string; to: string }>;
  position: TcPosition | null;
  vmax: number | null;
  pmin: number | null;
  class: string | null;
  classAgency: string | null;
  agencies: string[];
  models: string[];
  best: string[];
}

interface TcSourceStatus {
  id: string;
  ok: boolean;
  fetched?: string;
  url?: string;
  cycle?: string;
  error?: string;
}

export interface TcIndex {
  schemaVersion: 1;
  issued: string;
  generated: string;
  storms: TcIndexEntry[];
  crosswalk: Record<string, string>;
  sources: TcSourceStatus[];
}

export type TcRadii = Partial<Record<"34" | "50" | "64", (number | null)[]>>;
/** The order a radii threshold's four quadrants come in. */
export const QUADRANT_CODES = ["NE", "SE", "SW", "NW"] as const;
const THRESHOLDS: readonly string[] = ["34", "50", "64"];

export interface TcPoint {
  time: string;
  /** Seconds from the forecast's `base`; absent on a best track. */
  lead?: number;
  lat: number;
  lon: number;
  vmax: number | null;
  pmin: number | null;
  radii: TcRadii | null;
  class: string | null;
  rmw: number | null;
  gust: number | null;
  cone: number | null;
}

export interface TcForecast {
  issued: string;
  base: string;
  run?: string;
  number?: string;
  points: TcPoint[];
}

export interface TcTrack {
  source: string | null;
  provisional: boolean;
  points: TcPoint[];
}

export interface TcEnsemble {
  issued: string;
  base: string;
  run?: string;
  leads: number[];
  members: number[];
  lat: number[];
  lon: number[];
  vmax: number[];
  pmin: number[];
  mean: TcForecast | null;
}

interface TcAlert {
  time: string;
  line: [[number, number], [number, number]] | null;
  halfWidth: number | null;
  center: [number, number] | null;
}

export interface TcStorm {
  schemaVersion: 1;
  id: string;
  level: TcLevel;
  basin: string;
  name: string | null;
  sid: string | null;
  intl: string | null;
  aliases: Record<string, { from: string; to: string }>;
  best: Record<string, TcTrack>;
  agencies: Record<string, TcForecast>;
  models: Record<string, TcForecast | TcEnsemble>;
  impact: Record<string, unknown>;
  alert: TcAlert | null;
  sources: TcSourceStatus[];
}

const ATCF_ID = /^[A-Z]{2}\d{6}$/;
const STORM_ID = /^(?:[A-Z]{2}\d{6}|x-[a-z]{2}-\d{10}-\d+)$/;
const KEY = /^[a-z][a-z0-9]*$/;
/** An alias as the centres spell it (`EP97`, `ecmwf:70E`). */
const ALIAS = /^[A-Za-z0-9:._-]+$/;
/** `tc/YYYY/MM/DD/tc.<hour>/index.json`, or the flat `tc.<hour>/index.json`
 * issues were published under before. */
const TC_PATH =
  /^(?:tc\/(\d{4})\/(\d{2})\/(\d{2})\/tc\.\1\2\3\d{2}|tc\.\d{10})\/index\.json$/;

type Window = { from: string; to: string };
type SourceRecord = { id: string; ok: boolean; error?: string; fetched?: string; url?: string; cycle?: string };

/** The keys of an agency, model or best-track section: ids on shape alone,
 * so a key the registry does not know passes. */
function keyed<T, R>(
  section: Record<string, T>,
  label: string,
  each: (value: T, label: string) => R,
): Record<string, R> {
  const result: Record<string, R> = {};
  for (const [key, item] of Object.entries(section)) {
    if (!KEY.test(key)) throw new Error(`${label} has a malformed key ${key}`);
    result[key] = each(item, `${label}.${key}`);
  }
  return result;
}

function aliases(value: Record<string, Window>, label: string): Record<string, Window> {
  const result: Record<string, Window> = {};
  for (const [alias, window] of Object.entries(value)) {
    if (!ALIAS.test(alias)) throw new Error(`${label} has a malformed alias ${alias}`);
    if (Date.parse(window.from) > Date.parse(window.to))
      throw new Error(`${label}.${alias} ends before it starts`);
    result[alias] = { from: window.from, to: window.to };
  }
  return result;
}

function point(value: TcPointRecord, label: string, forecast: boolean): TcPoint {
  const result: TcPoint = {
    time: value.time,
    lat: value.lat,
    lon: value.lon,
    vmax: value.vmax ?? null,
    pmin: value.pmin ?? null,
    radii: null,
    class: value.class ?? null,
    rmw: value.rmw ?? null,
    gust: value.gust ?? null,
    cone: value.cone ?? null,
  };
  if (forecast) {
    if (value.lead === undefined) throw new Error(`${label}.lead must be a non-negative integer`);
    result.lead = value.lead;
  }
  if (value.radii !== null && value.radii !== undefined) {
    const parsed: TcRadii = {};
    for (const [threshold, quadrants] of Object.entries(value.radii)) {
      if (!THRESHOLDS.includes(threshold))
        throw new Error(`${label}.radii has threshold ${threshold}`);
      parsed[threshold as "34" | "50" | "64"] = quadrants;
    }
    result.radii = parsed;
  }
  return result;
}

type ForecastRecord = { issued: string; base: string; run?: string; number?: string; points: TcPointRecord[] };

/** A forecast's leads strictly increase and each point is valid at
 * `base + lead`. */
function forecast(value: ForecastRecord, label: string): TcForecast {
  const base = Date.parse(value.base);
  let previous = -1;
  const points = value.points.map((item, index) => {
    const parsed = point(item, `${label}.points[${index}]`, true);
    const lead = parsed.lead!;
    if (lead <= previous)
      throw new Error(`${label}.points must have increasing leads`);
    if (Date.parse(parsed.time) !== base + lead * 1000)
      throw new Error(`${label}.points[${index}] time is not base + lead`);
    previous = lead;
    return parsed;
  });
  const result: TcForecast = { issued: value.issued, base: value.base, points };
  if (value.run !== undefined) result.run = value.run;
  if (value.number !== undefined) result.number = value.number;
  return result;
}

/** A best track's points strictly increase in time. */
function track(
  value: { source?: string | null; provisional: boolean; points: TcPointRecord[] },
  label: string,
): TcTrack {
  let previous = -Infinity;
  const points = value.points.map((item, index) => {
    const parsed = point(item, `${label}.points[${index}]`, false);
    const time = Date.parse(parsed.time);
    if (time <= previous)
      throw new Error(`${label}.points must be in increasing time`);
    previous = time;
    return parsed;
  });
  return { source: value.source ?? null, provisional: value.provisional, points };
}

type EnsembleRecord = {
  issued: string;
  base: string;
  run?: string;
  leads: number[];
  members: number[];
  lat: number[];
  lon: number[];
  vmax: number[];
  pmin: number[];
  mean?: ForecastRecord | null;
};

/** A member set: increasing leads, unique members, and every array one
 * value per member and lead. */
function ensemble(value: EnsembleRecord, label: string): TcEnsemble {
  const { leads, members } = value;
  if (leads.some((lead, i) => i > 0 && lead <= leads[i - 1]!))
    throw new Error(`${label}.leads must be increasing non-negative integers`);
  if (new Set(members).size !== members.length)
    throw new Error(`${label}.members must be unique integers`);
  const expected = leads.length * members.length;
  for (const key of ["lat", "lon", "vmax", "pmin"] as const)
    if (value[key].length !== expected)
      throw new Error(`${label}.${key} must hold ${expected} integers`);
  const result: TcEnsemble = {
    issued: value.issued,
    base: value.base,
    leads,
    members,
    lat: value.lat,
    lon: value.lon,
    vmax: value.vmax,
    pmin: value.pmin,
    mean: value.mean === null || value.mean === undefined ? null : forecast(value.mean, `${label}.mean`),
  };
  if (value.run !== undefined) result.run = value.run;
  return result;
}

export function isTcEnsemble(
  value: TcForecast | TcEnsemble,
): value is TcEnsemble {
  return "leads" in value;
}

function sources(input: SourceRecord[], label: string): TcSourceStatus[] {
  return input.map((value, index) => {
    if (!value.ok && value.error === undefined)
      throw new Error(`${label}[${index}] failed without an error`);
    const status: TcSourceStatus = { id: value.id, ok: value.ok };
    if (value.fetched !== undefined) status.fetched = value.fetched;
    if (value.url !== undefined) status.url = value.url;
    if (value.cycle !== undefined) status.cycle = value.cycle;
    if (value.error !== undefined) status.error = value.error;
    return status;
  });
}

/** `<storm id>.json`: the version gate, `docs/schemas/tc-storm.schema.json`,
 * then what the schema cannot say — an A-level storm's ATCF id, the alias
 * and section keys, the radii thresholds, the leads and times in order. */
export function validateTcStorm(input: unknown): TcStorm {
  schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "tc storm", TC_SCHEMA_VERSION);
  checkTcStorm(input, "storm");
  const value = input;
  if (value.level === "A" && !ATCF_ID.test(value.id))
    throw new Error("an A-level storm carries an ATCF id");
  let alert: TcAlert | null = null;
  if (value.alert !== null && value.alert !== undefined) {
    const { line, center } = value.alert;
    alert = {
      time: value.alert.time,
      line: line === null || line === undefined ? null : [[line[0]![0], line[0]![1]], [line[1]![0], line[1]![1]]],
      halfWidth: value.alert.halfWidth ?? null,
      center: center === null || center === undefined ? null : [center[0], center[1]],
    };
  }
  return {
    schemaVersion: 1,
    id: value.id,
    level: value.level,
    basin: value.basin,
    name: value.name ?? null,
    sid: value.sid ?? null,
    intl: value.intl ?? null,
    aliases: aliases(value.aliases, "storm.aliases"),
    best: keyed(value.best, "storm.best", track),
    agencies: keyed(value.agencies, "storm.agencies", forecast),
    models: keyed(value.models, "storm.models", (item, label) =>
      "leads" in item ? ensemble(item as EnsembleRecord, label) : forecast(item as ForecastRecord, label),
    ),
    impact: value.impact,
    alert,
    sources: sources(value.sources, "storm.sources"),
  };
}

/** `index.json`: the version gate, `docs/schemas/tc-index.schema.json`,
 * then unique storms and paths, the alias keys and windows, and a
 * crosswalk that maps a storm to another. */
export function validateTcIndex(input: unknown): TcIndex {
  schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "tc index", TC_SCHEMA_VERSION);
  checkTcIndex(input, "index");
  const ids = new Set<string>();
  const paths = new Set<string>();
  const storms = input.storms.map((entry, index): TcIndexEntry => {
    const label = `index.storms[${index}]`;
    if (ids.has(entry.id)) throw new Error(`index lists ${entry.id} twice`);
    ids.add(entry.id);
    if (paths.has(entry.path)) throw new Error(`${label}.path names another storm's file`);
    paths.add(entry.path);
    const position = entry.position ?? null;
    return {
      id: entry.id,
      level: entry.level,
      basin: entry.basin,
      name: entry.name ?? null,
      path: entry.path,
      byteLength: entry.byteLength,
      crc32: entry.crc32,
      aliases: aliases(entry.aliases, `${label}.aliases`),
      position: position === null ? null : { time: position.time, lat: position.lat, lon: position.lon },
      vmax: entry.vmax ?? null,
      pmin: entry.pmin ?? null,
      class: entry.class ?? null,
      classAgency: entry.classAgency ?? null,
      agencies: entry.agencies,
      models: entry.models,
      best: entry.best,
    };
  });
  for (const [from, to] of Object.entries(input.crosswalk)) {
    if (from === to || !STORM_ID.test(from))
      throw new Error(`index.crosswalk ${from} is malformed`);
  }
  return {
    schemaVersion: 1,
    issued: input.issued,
    generated: input.generated,
    storms,
    crosswalk: input.crosswalk,
    sources: sources(input.sources, "index.sources"),
  };
}

export function validateTcPointer(input: unknown): TcPointer {
  return pointer(input, "tc", TC_PATH, TC_SCHEMA_VERSION);
}

/** One ensemble member's track, unpacked from the fixed-point arrays: the
 * leads it has a position at, in seconds, and the positions. A member
 * that never found the system comes back empty. */
interface TcMemberTrack {
  member: number;
  leads: number[];
  lat: number[];
  lon: number[];
  vmax: (number | null)[];
  pmin: (number | null)[];
}

export function ensembleMembers(ensemble: TcEnsemble): TcMemberTrack[] {
  const count = ensemble.leads.length;
  return ensemble.members.map((member, row) => {
    const track: TcMemberTrack = {
      member,
      leads: [],
      lat: [],
      lon: [],
      vmax: [],
      pmin: [],
    };
    for (let column = 0; column < count; column += 1) {
      const slot = row * count + column;
      const lat = ensemble.lat[slot]!;
      const lon = ensemble.lon[slot]!;
      if (lat === TC_MISSING || lon === TC_MISSING) continue;
      track.leads.push(ensemble.leads[column]!);
      track.lat.push(lat / 100);
      track.lon.push(lon / 100);
      const vmax = ensemble.vmax[slot]!;
      const pmin = ensemble.pmin[slot]!;
      track.vmax.push(vmax === TC_MISSING ? null : vmax / 10);
      track.pmin.push(pmin === TC_MISSING ? null : pmin / 10);
    }
    return track;
  });
}
