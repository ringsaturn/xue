/** The weather warnings product, schema v1 (`docs/alerts.md`, written by
 * `xuebuild/alerts/schema.py`): a mutable pointer naming an immutable
 * round whose `index.json` lists every warning in force at the round with
 * its category, level, times and box, and one `<network>.jsonl` per
 * warning service beside it with each warning's full record. A network
 * that names areas by code points at a boundary set (PMTiles) published
 * once per upstream release under `alerts-areas/`.
 *
 * Admission is structural, as in `stations/schema.ts`: a network, an
 * event, a category or a native field this build has never seen passes;
 * a wrong shape, a level outside 1 … 5 and a `schemaVersion` above the one
 * implemented do not. Unknown fields are ignored. */

import {
  CRC32,
  integer,
  object,
  pointer,
  schemaVersion,
  string,
  timestamp,
  type ProductPointer,
} from "../schema/validate";

const ALERTS_SCHEMA_VERSION = 1;
export const ALERTS_POINTER_FILENAME = "latest-alerts.json";

/** `alerts/YYYY/MM/DD/alerts.<round>/index.json`, or the flat
 * `alerts.<round>/index.json` rounds were published under before. */
const ALERTS_PATH =
  /^(?:alerts\/(\d{4})\/(\d{2})\/(\d{2})\/alerts\.\1\2\3\d{4}|alerts\.\d{12})\/index\.json$/;
const NETWORK_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
/** `<network>:<the upstream's identifier>`, which may itself hold `:`. */
const ALERT_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):.+$/;
const CATEGORY = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
/** A boundary set's file, climbing from the round to the data root:
 * `../../../../../alerts-areas/<set>.<version>.<ext>` from a dated round,
 * `../alerts-areas/…` from a flat one. */
const AREAS_PATH = /^(?:\.\.\/){1,5}alerts-areas\/[a-z0-9-]+\.[0-9A-Za-z]+\.(?:pmtiles|bbox\.json)$/;

export type AlertsPointer = ProductPointer<"alerts">;

/** The common scale every network maps onto (§2). */
export type AlertLevel = 1 | 2 | 3 | 4 | 5;

export interface AlertsFile {
  path: string;
  byteLength: number;
  crc32: string;
}

/** The boundary set a network's area codes resolve against (§9). */
export interface AlertsAreas {
  set: string;
  version: string;
  /** Relative to the index. */
  path: string;
  byteLength: number;
  crc32: string;
  layers: string[];
}

export interface AlertsNetwork {
  id: string;
  name: string;
  attribution: string;
  license: string;
  licenseUrl: string | null;
  url: string;
  latest: string | null;
  file: AlertsFile;
  areas: AlertsAreas | null;
}

/** One index row, expanded. */
export interface AlertRow {
  id: string;
  /** Position in the index's `networks`. */
  network: number;
  category: string;
  level: AlertLevel;
  event: string;
  sent: string;
  onset: string | null;
  expires: string | null;
  /** `[west, south, east, north]`, or null when the round has no box. */
  bbox: [number, number, number, number] | null;
  offset: number;
  length: number;
}

export interface AlertsIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  networks: AlertsNetwork[];
  categories: string[];
  alerts: AlertRow[];
}

export interface AlertTexts {
  headline?: string;
  description?: string;
  instruction?: string;
  area?: string;
}

export type AlertPolygons = number[][][][];

/** One line of a network file: a warning's whole record. */
export interface AlertLine {
  id: string;
  network: string;
  category: string;
  level: AlertLevel;
  native: { event: string; code: string | null; level: string | null };
  sent: string;
  onset: string | null;
  expires: string | null;
  /** By BCP 47 tag, in the order the network gives them. */
  texts: Record<string, AlertTexts>;
  areas: string[];
  /** A MultiPolygon's coordinates, or null for an area-coded warning. */
  geometry: AlertPolygons | null;
  url: string | null;
  sender: string | null;
}

function list(input: unknown, label: string): unknown[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input;
}

function optionalString(value: unknown, label: string): string | null {
  return value === null || value === undefined ? null : string(value, label);
}

function optionalTimestamp(value: unknown, label: string): string | null {
  return value === null || value === undefined ? null : timestamp(value, label);
}

function level(value: unknown, label: string): AlertLevel {
  const parsed = integer(value, label, 1);
  if (parsed > 5) throw new Error(`${label} must be 1 … 5`);
  return parsed as AlertLevel;
}

function file(input: unknown, label: string, name: string): AlertsFile {
  const value = object(input, label);
  if (value.path !== name)
    throw new Error(`${label}.path must be ${name}, the file beside the index`);
  return {
    path: name,
    byteLength: integer(value.byteLength, `${label}.byteLength`),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

function areas(input: unknown, label: string): AlertsAreas | null {
  if (input === null || input === undefined) return null;
  const value = object(input, label);
  return {
    set: string(value.set, `${label}.set`, NETWORK_ID),
    version: string(value.version, `${label}.version`, /^[0-9A-Za-z]+$/),
    path: string(value.path, `${label}.path`, AREAS_PATH),
    byteLength: integer(value.byteLength, `${label}.byteLength`, 1),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
    layers: list(value.layers, `${label}.layers`).map((item, index) =>
      string(item, `${label}.layers[${index}]`, /^[A-Za-z0-9_-]+$/),
    ),
  };
}

function coordinate(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error(`${label} must be a number`);
  return value;
}

function geometry(input: unknown, label: string): AlertPolygons | null {
  if (input === null || input === undefined) return null;
  const value = object(input, label);
  if (value.type !== "MultiPolygon") throw new Error(`${label} must be a MultiPolygon`);
  return list(value.coordinates, `${label}.coordinates`).map((polygon, p) =>
    list(polygon, `${label}[${p}]`).map((ring, r) => {
      const points = list(ring, `${label}[${p}][${r}]`).map((position, i) => {
        const pair = list(position, `${label}[${p}][${r}][${i}]`);
        if (pair.length !== 2) throw new Error(`${label}[${p}][${r}][${i}] must be [lon, lat]`);
        return [coordinate(pair[0], `${label} lon`), coordinate(pair[1], `${label} lat`)];
      });
      if (points.length < 4) throw new Error(`${label}[${p}][${r}] must close a ring`);
      return points;
    }),
  );
}

export function parseAlertsPointer(input: unknown): AlertsPointer {
  return pointer(input, "alerts", ALERTS_PATH, ALERTS_SCHEMA_VERSION);
}

/** `alerts.<round>/index.json`: the networks and one row per warning, each
 * checked against its network's file the way the station rows are. */
export function parseAlertsIndex(input: unknown): AlertsIndex {
  const value = object(input, "alerts index");
  const version = schemaVersion(value.schemaVersion, "alerts index", ALERTS_SCHEMA_VERSION);
  const networks = list(value.networks, "index.networks").map((item, index): AlertsNetwork => {
    const label = `index.networks[${index}]`;
    const entry = object(item, label);
    const id = string(entry.id, `${label}.id`, NETWORK_ID);
    return {
      id,
      name: string(entry.name, `${label}.name`),
      attribution: string(entry.attribution, `${label}.attribution`),
      license: string(entry.license, `${label}.license`),
      licenseUrl: optionalString(entry.licenseUrl, `${label}.licenseUrl`),
      url: string(entry.url, `${label}.url`),
      latest: optionalTimestamp(entry.latest, `${label}.latest`),
      file: file(entry.file, `${label}.file`, `${id}.jsonl`),
      areas: areas(entry.areas, `${label}.areas`),
    };
  });
  if (new Set(networks.map((network) => network.id)).size !== networks.length)
    throw new Error("index.networks lists a network twice");
  const categories = list(value.categories, "index.categories").map((item, index) =>
    string(item, `index.categories[${index}]`, CATEGORY),
  );
  const ends = networks.map(() => 0);
  let previous = "";
  const alerts = list(value.alerts, "index.alerts").map((item, index): AlertRow => {
    const label = `index.alerts[${index}]`;
    const row = list(item, label);
    if (row.length !== 14) throw new Error(`${label} must be a row of 14 values`);
    const id = string(row[0], `${label}.id`, ALERT_ID);
    if (id <= previous) throw new Error("index.alerts must be sorted by id and unique");
    previous = id;
    const network = integer(row[1], `${label}.network`);
    if (network >= networks.length) throw new Error(`${label}.network names no network`);
    if (ALERT_ID.exec(id)![1] !== networks[network]!.id)
      throw new Error(`${label}.id does not belong to its network`);
    const category = string(row[2], `${label}.category`, CATEGORY);
    const bounds = row.slice(8, 12);
    let bbox: AlertRow["bbox"] = null;
    if (bounds.every((bound) => bound !== null)) {
      bbox = bounds.map((bound, at) => coordinate(bound, `${label}.bbox[${at}]`)) as AlertRow["bbox"];
    } else if (!bounds.every((bound) => bound === null)) {
      throw new Error(`${label} must have four bounds or none`);
    }
    const offset = integer(row[12], `${label}.offset`);
    const length = integer(row[13], `${label}.length`, 1);
    if (offset < ends[network]!) throw new Error(`${label}.offset precedes the previous alert's span`);
    if (offset + length > networks[network]!.file.byteLength)
      throw new Error(`${label} spans past the end of the file it indexes`);
    ends[network] = offset + length;
    return {
      id,
      network,
      category,
      level: level(row[3], `${label}.level`),
      event: string(row[4], `${label}.event`),
      sent: timestamp(row[5], `${label}.sent`),
      onset: optionalTimestamp(row[6], `${label}.onset`),
      expires: optionalTimestamp(row[7], `${label}.expires`),
      bbox,
      offset,
      length,
    };
  });
  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    networks,
    categories,
    alerts,
  };
}

/** One line of a network file. */
export function parseAlertLine(input: unknown): AlertLine {
  const value = object(input, "alert");
  const id = string(value.id, "alert.id", ALERT_ID);
  const network = string(value.network, "alert.network", NETWORK_ID);
  if (ALERT_ID.exec(id)![1] !== network) throw new Error("alert.id does not belong to its network");
  const native = object(value.native, "alert.native");
  const texts: Record<string, AlertTexts> = {};
  for (const [tag, entry] of Object.entries(object(value.texts, "alert.texts"))) {
    const fields = object(entry, `alert.texts.${tag}`);
    const text: AlertTexts = {};
    for (const key of ["headline", "description", "instruction", "area"] as const) {
      if (fields[key] !== undefined && fields[key] !== null)
        text[key] = string(fields[key], `alert.texts.${tag}.${key}`);
    }
    texts[tag] = text;
  }
  return {
    id,
    network,
    category: string(value.category, "alert.category", CATEGORY),
    level: level(value.level, "alert.level"),
    native: {
      event: string(native.event, "alert.native.event"),
      code: optionalString(native.code, "alert.native.code"),
      level: optionalString(native.level, "alert.native.level"),
    },
    sent: timestamp(value.sent, "alert.sent"),
    onset: optionalTimestamp(value.onset, "alert.onset"),
    expires: optionalTimestamp(value.expires, "alert.expires"),
    texts,
    areas: list(value.areas, "alert.areas").map((item, index) => string(item, `alert.areas[${index}]`)),
    geometry: geometry(value.geometry, "alert.geometry"),
    url: optionalString(value.url, "alert.url"),
    sender: optionalString(value.sender, "alert.sender"),
  };
}

/** Every line of a network file, each checked against the network it was
 * read for. */
export function parseAlertLines(text: string, network: string): AlertLine[] {
  const lines: AlertLine[] = [];
  for (const raw of text.split("\n")) {
    if (raw.trim() === "") continue;
    const line = parseAlertLine(JSON.parse(raw) as unknown);
    if (line.network !== network) throw new Error(`${network}.jsonl holds ${line.id}`);
    lines.push(line);
  }
  return lines;
}
