/** The tropical cyclone product, schema v1, as the shell reads it —
 * `docs/tc.md` is normative and `xuebuild/tc/schema.py` writes what this
 * validates. Structural admission, like the manifest's: an agency or a
 * model key the registry does not know passes, a shape that is wrong or
 * a `schemaVersion` above the one implemented does not. */

export const TC_SCHEMA_VERSION = 1;
export const TC_POINTER_FILENAME = "latest-tc.json";
export const TC_MISSING = -32768;

export type TcLevel = "A" | "B" | "C";

export interface TcPointer {
  schemaVersion: 1;
  product: "tc";
  issued: string;
  path: string;
  byteLength: number;
  crc32: string;
}

export interface TcPosition {
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

export interface TcSourceStatus {
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

export interface TcAlert {
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
const SYNTHETIC_ID = /^x-[a-z]{2}-\d{10}-\d+$/;
const KEY = /^[a-z][a-z0-9]*$/;
const CRC32 = /^[0-9a-f]{8}$/;

export function isTcStormId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    (ATCF_ID.test(value) || SYNTHETIC_ID.test(value))
  );
}

function object(input: unknown, label: string): Record<string, unknown> {
  if (typeof input !== "object" || input === null || Array.isArray(input))
    throw new Error(`${label} must be an object`);
  return input as Record<string, unknown>;
}

function timestamp(value: unknown, label: string): number {
  if (typeof value !== "string" || !value.endsWith("Z"))
    throw new Error(`${label} must be a UTC timestamp`);
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed))
    throw new Error(`${label} is not a valid timestamp`);
  return parsed;
}

function number(
  value: unknown,
  label: string,
  minimum?: number,
  maximum?: number,
): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error(`${label} must be a number`);
  if (minimum !== undefined && value < minimum)
    throw new Error(`${label} is below ${minimum}`);
  if (maximum !== undefined && value > maximum)
    throw new Error(`${label} is above ${maximum}`);
  return value;
}

function optionalNumber(
  value: unknown,
  label: string,
  minimum?: number,
  maximum?: number,
): number | null {
  return value === null || value === undefined
    ? null
    : number(value, label, minimum, maximum);
}

function optionalString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string")
    throw new Error(`${label} must be a string or null`);
  return value;
}

function keyed<T>(
  input: unknown,
  label: string,
  each: (value: unknown, label: string) => T,
): Record<string, T> {
  const value = object(input, label);
  const result: Record<string, T> = {};
  for (const [key, item] of Object.entries(value)) {
    if (!KEY.test(key)) throw new Error(`${label} has a malformed key ${key}`);
    result[key] = each(item, `${label}.${key}`);
  }
  return result;
}

function aliases(
  input: unknown,
  label: string,
): Record<string, { from: string; to: string }> {
  const value = object(input, label);
  const result: Record<string, { from: string; to: string }> = {};
  for (const [alias, window] of Object.entries(value)) {
    const item = object(window, `${label}.${alias}`);
    const from = item.from;
    const to = item.to;
    if (
      timestamp(from, `${label}.${alias}.from`) >
      timestamp(to, `${label}.${alias}.to`)
    ) {
      throw new Error(`${label}.${alias} ends before it starts`);
    }
    result[alias] = { from: from as string, to: to as string };
  }
  return result;
}

function point(input: unknown, label: string, forecast: boolean): TcPoint {
  const value = object(input, label);
  timestamp(value.time, `${label}.time`);
  const result: TcPoint = {
    time: value.time as string,
    lat: number(value.lat, `${label}.lat`, -90, 90),
    lon: number(value.lon, `${label}.lon`, -180, 180),
    vmax: optionalNumber(value.vmax, `${label}.vmax`, 0),
    pmin: optionalNumber(value.pmin, `${label}.pmin`, 800, 1100),
    radii: null,
    class: optionalString(value.class, `${label}.class`),
    rmw: optionalNumber(value.rmw, `${label}.rmw`, 0),
    gust: optionalNumber(value.gust, `${label}.gust`, 0),
    cone: optionalNumber(value.cone, `${label}.cone`, 0),
  };
  if (forecast) {
    const lead = value.lead;
    if (typeof lead !== "number" || !Number.isInteger(lead) || lead < 0)
      throw new Error(`${label}.lead must be a non-negative integer`);
    result.lead = lead;
  }
  if (value.radii !== null && value.radii !== undefined) {
    const radii = object(value.radii, `${label}.radii`);
    const parsed: TcRadii = {};
    for (const [threshold, quadrants] of Object.entries(radii)) {
      if (threshold !== "34" && threshold !== "50" && threshold !== "64")
        throw new Error(`${label}.radii has threshold ${threshold}`);
      if (!Array.isArray(quadrants) || quadrants.length !== 4)
        throw new Error(
          `${label}.radii[${threshold}] must list four quadrants`,
        );
      parsed[threshold] = quadrants.map((q, index) =>
        optionalNumber(q, `${label}.radii[${threshold}][${index}]`, 0),
      );
    }
    result.radii = parsed;
  }
  return result;
}

export function validateTcForecast(
  input: unknown,
  label = "forecast",
): TcForecast {
  const value = object(input, label);
  timestamp(value.issued, `${label}.issued`);
  const base = timestamp(value.base, `${label}.base`);
  if (
    value.run !== undefined &&
    (typeof value.run !== "string" || !/^\d{10}$/.test(value.run))
  )
    throw new Error(`${label}.run is malformed`);
  if (value.number !== undefined && typeof value.number !== "string")
    throw new Error(`${label}.number must be a string`);
  if (!Array.isArray(value.points))
    throw new Error(`${label}.points must be a list`);
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
  const result: TcForecast = {
    issued: value.issued as string,
    base: value.base as string,
    points,
  };
  if (typeof value.run === "string") result.run = value.run;
  if (typeof value.number === "string") result.number = value.number;
  return result;
}

export function validateTcTrack(input: unknown, label = "track"): TcTrack {
  const value = object(input, label);
  const source = optionalString(value.source, `${label}.source`);
  if (source !== null && !KEY.test(source))
    throw new Error(`${label}.source is malformed`);
  if (typeof value.provisional !== "boolean")
    throw new Error(`${label}.provisional must be a boolean`);
  if (!Array.isArray(value.points))
    throw new Error(`${label}.points must be a list`);
  let previous = -Infinity;
  const points = value.points.map((item, index) => {
    const parsed = point(item, `${label}.points[${index}]`, false);
    const time = Date.parse(parsed.time);
    if (time <= previous)
      throw new Error(`${label}.points must be in increasing time`);
    previous = time;
    return parsed;
  });
  return { source, provisional: value.provisional, points };
}

export function validateTcEnsemble(
  input: unknown,
  label = "ensemble",
): TcEnsemble {
  const value = object(input, label);
  timestamp(value.issued, `${label}.issued`);
  timestamp(value.base, `${label}.base`);
  if (
    value.run !== undefined &&
    (typeof value.run !== "string" || !/^\d{10}$/.test(value.run))
  )
    throw new Error(`${label}.run is malformed`);
  const leads = value.leads;
  if (
    !Array.isArray(leads) ||
    leads.some(
      (lead, i) =>
        !Number.isInteger(lead) || lead < 0 || (i > 0 && lead <= leads[i - 1]),
    )
  ) {
    throw new Error(`${label}.leads must be increasing non-negative integers`);
  }
  const members = value.members;
  if (
    !Array.isArray(members) ||
    members.some((m) => !Number.isInteger(m)) ||
    new Set(members).size !== members.length
  ) {
    throw new Error(`${label}.members must be unique integers`);
  }
  const expected = leads.length * members.length;
  const arrays: Record<"lat" | "lon" | "vmax" | "pmin", number[]> = {
    lat: [],
    lon: [],
    vmax: [],
    pmin: [],
  };
  const bounds = {
    lat: [-9000, 9000],
    lon: [-18000, 18000],
    vmax: [0, 1500],
    pmin: [8000, 11000],
  } as const;
  for (const key of ["lat", "lon", "vmax", "pmin"] as const) {
    const values = value[key];
    if (!Array.isArray(values) || values.length !== expected)
      throw new Error(`${label}.${key} must hold ${expected} integers`);
    for (const item of values) {
      if (!Number.isInteger(item))
        throw new Error(`${label}.${key} must be integers`);
      if (
        item !== TC_MISSING &&
        (item < bounds[key][0] || item > bounds[key][1])
      )
        throw new Error(`${label}.${key} out of range`);
    }
    arrays[key] = values as number[];
  }
  const result: TcEnsemble = {
    issued: value.issued as string,
    base: value.base as string,
    leads: leads as number[],
    members: members as number[],
    ...arrays,
    mean:
      value.mean === null || value.mean === undefined
        ? null
        : validateTcForecast(value.mean, `${label}.mean`),
  };
  if (typeof value.run === "string") result.run = value.run;
  return result;
}

export function isTcEnsemble(
  value: TcForecast | TcEnsemble,
): value is TcEnsemble {
  return "leads" in value;
}

function sources(input: unknown, label: string): TcSourceStatus[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input.map((item, index) => {
    const value = object(item, `${label}[${index}]`);
    if (typeof value.id !== "string" || !KEY.test(value.id))
      throw new Error(`${label}[${index}].id is malformed`);
    if (typeof value.ok !== "boolean")
      throw new Error(`${label}[${index}].ok must be a boolean`);
    if (!value.ok && typeof value.error !== "string")
      throw new Error(`${label}[${index}] failed without an error`);
    const status: TcSourceStatus = { id: value.id, ok: value.ok };
    if (typeof value.fetched === "string") status.fetched = value.fetched;
    if (typeof value.url === "string") status.url = value.url;
    if (typeof value.cycle === "string") status.cycle = value.cycle;
    if (typeof value.error === "string") status.error = value.error;
    return status;
  });
}

function level(value: unknown, label: string): TcLevel {
  if (value !== "A" && value !== "B" && value !== "C")
    throw new Error(`${label} must be A, B or C`);
  return value;
}

function basin(value: unknown, label: string): string {
  if (typeof value !== "string" || !/^[A-Z]{2}$/.test(value))
    throw new Error(`${label} must be a two-letter basin`);
  return value;
}

function crc(value: unknown, label: string): string {
  if (typeof value !== "string" || !CRC32.test(value))
    throw new Error(`${label} must be 8 hex characters`);
  return value;
}

function byteLength(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) <= 0)
    throw new Error(`${label} must be a positive integer`);
  return value as number;
}

export function validateTcStorm(input: unknown): TcStorm {
  const value = object(input, "storm");
  if (value.schemaVersion !== TC_SCHEMA_VERSION)
    throw new Error("unsupported tc storm schema version");
  if (!isTcStormId(value.id)) throw new Error("storm id is malformed");
  const stormLevel = level(value.level, "storm.level");
  if (stormLevel === "A" && !ATCF_ID.test(value.id))
    throw new Error("an A-level storm carries an ATCF id");
  const alertValue = value.alert;
  let alert: TcAlert | null = null;
  if (alertValue !== null && alertValue !== undefined) {
    const item = object(alertValue, "storm.alert");
    timestamp(item.time, "storm.alert.time");
    let line: TcAlert["line"] = null;
    if (item.line !== null && item.line !== undefined) {
      if (!Array.isArray(item.line) || item.line.length !== 2)
        throw new Error("storm.alert.line must be two ends");
      line = item.line.map((end, index) => {
        if (!Array.isArray(end) || end.length !== 2)
          throw new Error(`storm.alert.line[${index}] must be [lat, lon]`);
        return [
          number(end[0], "storm.alert.line lat", -90, 90),
          number(end[1], "storm.alert.line lon", -180, 180),
        ];
      }) as TcAlert["line"];
    }
    let center: TcAlert["center"] = null;
    if (Array.isArray(item.center) && item.center.length === 2) {
      center = [
        number(item.center[0], "storm.alert.center lat", -90, 90),
        number(item.center[1], "storm.alert.center lon", -180, 180),
      ];
    }
    alert = {
      time: item.time as string,
      line,
      halfWidth: optionalNumber(item.halfWidth, "storm.alert.halfWidth", 0),
      center,
    };
  }
  return {
    schemaVersion: 1,
    id: value.id,
    level: stormLevel,
    basin: basin(value.basin, "storm.basin"),
    name: optionalString(value.name, "storm.name"),
    sid: optionalString(value.sid, "storm.sid"),
    intl: optionalString(value.intl, "storm.intl"),
    aliases: aliases(value.aliases, "storm.aliases"),
    best: keyed(value.best, "storm.best", validateTcTrack),
    agencies: keyed(value.agencies, "storm.agencies", validateTcForecast),
    models: keyed(value.models, "storm.models", (item, label) =>
      typeof item === "object" && item !== null && "leads" in item
        ? validateTcEnsemble(item, label)
        : validateTcForecast(item, label),
    ),
    impact: object(value.impact, "storm.impact"),
    alert,
    sources: sources(value.sources, "storm.sources"),
  };
}

export function validateTcIndex(input: unknown): TcIndex {
  const value = object(input, "index");
  if (value.schemaVersion !== TC_SCHEMA_VERSION)
    throw new Error("unsupported tc index schema version");
  timestamp(value.issued, "index.issued");
  timestamp(value.generated, "index.generated");
  if (!Array.isArray(value.storms))
    throw new Error("index.storms must be a list");
  const ids = new Set<string>();
  const storms = value.storms.map((item, index) => {
    const label = `index.storms[${index}]`;
    const entry = object(item, label);
    if (!isTcStormId(entry.id)) throw new Error(`${label}.id is malformed`);
    if (ids.has(entry.id)) throw new Error(`index lists ${entry.id} twice`);
    ids.add(entry.id);
    if (
      typeof entry.path !== "string" ||
      entry.path.includes("/") ||
      entry.path.startsWith(".")
    )
      throw new Error(`${label}.path is malformed`);
    let position: TcPosition | null = null;
    if (entry.position !== null && entry.position !== undefined) {
      const item = object(entry.position, `${label}.position`);
      timestamp(item.time, `${label}.position.time`);
      position = {
        time: item.time as string,
        lat: number(item.lat, `${label}.position.lat`, -90, 90),
        lon: number(item.lon, `${label}.position.lon`, -180, 180),
      };
    }
    const list = (key: "agencies" | "models" | "best"): string[] => {
      const values = entry[key];
      if (
        !Array.isArray(values) ||
        values.some((v) => typeof v !== "string" || !KEY.test(v))
      )
        throw new Error(`${label}.${key} must list ids`);
      return values as string[];
    };
    return {
      id: entry.id,
      level: level(entry.level, `${label}.level`),
      basin: basin(entry.basin, `${label}.basin`),
      name: optionalString(entry.name, `${label}.name`),
      path: entry.path,
      byteLength: byteLength(entry.byteLength, `${label}.byteLength`),
      crc32: crc(entry.crc32, `${label}.crc32`),
      aliases: aliases(entry.aliases, `${label}.aliases`),
      position,
      vmax: optionalNumber(entry.vmax, `${label}.vmax`, 0),
      pmin: optionalNumber(entry.pmin, `${label}.pmin`, 800, 1100),
      class: optionalString(entry.class, `${label}.class`),
      classAgency: optionalString(entry.classAgency, `${label}.classAgency`),
      agencies: list("agencies"),
      models: list("models"),
      best: list("best"),
    } satisfies TcIndexEntry;
  });
  const crosswalk = object(value.crosswalk, "index.crosswalk");
  for (const [from, to] of Object.entries(crosswalk)) {
    if (!isTcStormId(from) || !isTcStormId(to) || from === to)
      throw new Error(`index.crosswalk ${from} is malformed`);
  }
  return {
    schemaVersion: 1,
    issued: value.issued as string,
    generated: value.generated as string,
    storms,
    crosswalk: crosswalk as Record<string, string>,
    sources: sources(value.sources, "index.sources"),
  };
}

export function validateTcPointer(input: unknown): TcPointer {
  const value = object(input, "tc pointer");
  if (value.schemaVersion !== TC_SCHEMA_VERSION)
    throw new Error("unsupported tc pointer schema version");
  if (value.product !== "tc") throw new Error("tc pointer product must be tc");
  timestamp(value.issued, "tc pointer issued");
  if (
    typeof value.path !== "string" ||
    !/^tc\.\d{10}\/index\.json$/.test(value.path)
  )
    throw new Error("tc pointer path is malformed");
  return {
    schemaVersion: 1,
    product: "tc",
    issued: value.issued as string,
    path: value.path,
    byteLength: byteLength(value.byteLength, "tc pointer byteLength"),
    crc32: crc(value.crc32, "tc pointer crc32"),
  };
}

/** One ensemble member's track, unpacked from the fixed-point arrays: the
 * leads it has a position at, in seconds, and the positions. A member
 * that never found the system comes back empty. */
export interface TcMemberTrack {
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
