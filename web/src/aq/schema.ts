/** The air-quality product, schema v1 (`docs/aq.md`, written by
 * `xuebuild/aq/schema.py`): a mutable pointer naming an immutable round
 * whose `index.json` lists the networks, one `<network>.stations.json` per
 * network with every station's position and current values, and one
 * `<network>.jsonl` beside it whose lines — addressed by the stations
 * file's own byte spans — are each station's 24-hour window.
 *
 * Admission is structural, as in `stations/schema.ts`: a network, a station
 * or an element key this build has never seen passes; a wrong shape, a
 * value of a known element outside its range and a `schemaVersion` above
 * the one implemented do not. Unknown fields are ignored. */

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

const AQ_SCHEMA_VERSION = 1;
export const AQ_POINTER_FILENAME = "latest-aq.json";

/** `aq/YYYY/MM/DD/aq.<round>/index.json`, or the flat `aq.<round>/index.json`
 * rounds were published under before. */
const AQ_PATH = /^(?:aq\/(\d{4})\/(\d{2})\/(\d{2})\/aq\.\1\2\3\d{4}|aq\.\d{12})\/index\.json$/;
/** `<network>:<the network's own station identifier>`; the first group is
 * the network. */
const AQ_STATION_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$/;
const NETWORK_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const ELEMENT_KEY = /^[a-z][a-z0-9]*$/;

const LATITUDE: [number, number] = [-90, 90];
const LONGITUDE: [number, number] = [-180, 180];
const ELEVATION: [number, number] = [-500, 9000];

/** The ranges §2 holds its elements to. An element this build does not
 * know is admitted as any number. */
const AQ_BOUNDS: Record<string, [number, number]> = {
  pm25: [-5, 10000],
  pm10: [-5, 10000],
  o3: [-5, 1000],
  no2: [-5, 2000],
  so2: [-5, 5000],
  co: [-0.5, 100],
};

/** §2's units, which a reader falls back to where the index carries no
 * `elementTable` entry for an element. */
export const AQ_DEFAULT_UNITS: Record<string, string> = {
  pm25: "µg/m³",
  pm10: "µg/m³",
  o3: "ppb",
  no2: "ppb",
  so2: "ppb",
  co: "ppm",
};

export type AqPointer = ProductPointer<"aq">;

export interface AqFile {
  path: string;
  byteLength: number;
  crc32: string;
}

export interface AqNetwork {
  id: string;
  name: string;
  cadence: number;
  avgPeriod: number;
  /** Unvalidated data, which a display must say. */
  preliminary: boolean;
  /** What a display must show beside the network's data. */
  attribution: string;
  license: string;
  licenseUrl: string | null;
  url: string;
  latest: string | null;
  file: AqFile;
  stations: AqFile & { count: number };
}

export interface AqElementInfo {
  unit: string;
  cell: string;
}

/** One stations-file row, expanded. `values` are the station's current
 * values by element key. */
export interface AqStation {
  id: string;
  /** Position in the index's `networks`. */
  network: number;
  lat: number;
  lon: number;
  elev: number | null;
  rank: number;
  name: string | null;
  agency: string | null;
  obsTime: string;
  values: Record<string, number | null>;
  offset: number;
  length: number;
}

export interface AqIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  networks: AqNetwork[];
  elements: string[];
  /** By element key; empty when the index carries no table. */
  elementTable: Record<string, AqElementInfo>;
  /** Filled in from the stations files (`assembleAqIndex`). */
  stations: AqStation[];
}

/** One line of a network file: a station's window, column by column. */
export interface AqStationSeries {
  id: string;
  name: string | null;
  names: Record<string, string> | null;
  lat: number;
  lon: number;
  agency: string | null;
  /** Epoch seconds UTC, the end of each hour, oldest first. */
  time: number[];
  obs: Record<string, (number | null)[]>;
}

function list(input: unknown, label: string): unknown[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input;
}

function number(value: unknown, label: string, range?: [number, number]): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error(`${label} must be a number`);
  if (range && (value < range[0] || value > range[1]))
    throw new Error(`${label} is outside ${range[0]}..${range[1]}`);
  return value;
}

function optionalNumber(value: unknown, label: string, range?: [number, number]): number | null {
  return value === null || value === undefined ? null : number(value, label, range);
}

function optionalString(value: unknown, label: string): string | null {
  return value === null || value === undefined ? null : string(value, label);
}

function file(input: unknown, label: string, name: string): AqFile {
  const value = object(input, label);
  if (value.path !== name)
    throw new Error(`${label}.path must be ${name}, the file beside the index`);
  return {
    path: name,
    byteLength: integer(value.byteLength, `${label}.byteLength`),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

function elementValue(key: string, value: unknown, label: string): number | null {
  return optionalNumber(value, label, AQ_BOUNDS[key]);
}

export function parseAqPointer(input: unknown): AqPointer {
  return pointer(input, "aq", AQ_PATH, AQ_SCHEMA_VERSION);
}

/** `aq.<round>/index.json`. The stations come from the tables beside it,
 * so the index comes back with `stations` empty. */
export function parseAqIndex(input: unknown): AqIndex {
  const value = object(input, "aq index");
  const version = schemaVersion(value.schemaVersion, "aq index", AQ_SCHEMA_VERSION);
  const networks = list(value.networks, "index.networks").map((item, index): AqNetwork => {
    const label = `index.networks[${index}]`;
    const entry = object(item, label);
    const id = string(entry.id, `${label}.id`, NETWORK_ID);
    const stations = file(entry.stations, `${label}.stations`, `${id}.stations.json`);
    if (typeof entry.preliminary !== "boolean")
      throw new Error(`${label}.preliminary must be a boolean`);
    return {
      id,
      name: string(entry.name, `${label}.name`),
      cadence: integer(entry.cadence, `${label}.cadence`, 60),
      avgPeriod: integer(entry.avgPeriod, `${label}.avgPeriod`, 60),
      preliminary: entry.preliminary,
      attribution: string(entry.attribution, `${label}.attribution`),
      license: string(entry.license, `${label}.license`),
      licenseUrl: optionalString(entry.licenseUrl, `${label}.licenseUrl`),
      url: string(entry.url, `${label}.url`),
      latest:
        entry.latest === null || entry.latest === undefined
          ? null
          : timestamp(entry.latest, `${label}.latest`),
      file: file(entry.file, `${label}.file`, `${id}.jsonl`),
      stations: {
        ...stations,
        count: integer(object(entry.stations, `${label}.stations`).count, `${label}.stations.count`),
      },
    };
  });
  if (new Set(networks.map((network) => network.id)).size !== networks.length)
    throw new Error("index.networks lists a network twice");
  const elements = list(value.elements, "index.elements").map((item, index) =>
    string(item, `index.elements[${index}]`, ELEMENT_KEY),
  );
  if (new Set(elements).size !== elements.length)
    throw new Error("index.elements lists an element twice");
  const elementTable: Record<string, AqElementInfo> = {};
  if (value.elementTable !== null && value.elementTable !== undefined) {
    const table = object(value.elementTable, "index.elementTable");
    for (const key of elements) {
      if (table[key] === undefined) continue;
      const entry = object(table[key], `index.elementTable.${key}`);
      elementTable[key] = {
        unit: string(entry.unit, `index.elementTable.${key}.unit`),
        cell: string(entry.cell, `index.elementTable.${key}.cell`),
      };
    }
  }
  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    networks,
    elements,
    elementTable,
    stations: [],
  };
}

/** A `<network>.stations.json`, checked against the index that names it:
 * the network it says it is, the index's element list exactly, the row
 * count the index gave, rows sorted and unique under the network's prefix,
 * and spans in row order inside the network's `.jsonl`. */
export function parseAqStationTable(input: unknown, index: AqIndex, position: number): AqStation[] {
  const network = index.networks[position];
  if (!network) throw new Error(`aq index has no network ${position}`);
  const what = network.stations.path;
  const value = object(input, what);
  schemaVersion(value.schemaVersion, what, AQ_SCHEMA_VERSION);
  if (value.network !== network.id) throw new Error(`${what}.network must be ${network.id}`);
  timestamp(value.issued, `${what}.issued`);
  const elements = list(value.elements, `${what}.elements`);
  if (
    elements.length !== index.elements.length ||
    elements.some((key, at) => key !== index.elements[at])
  )
    throw new Error(`${what}.elements must equal the index's`);
  const rows = list(value.stations, `${what}.stations`);
  if (rows.length !== network.stations.count)
    throw new Error(`${what} has ${rows.length} rows, the index says ${network.stations.count}`);
  const width = 8 + index.elements.length + 2;
  let previous = "";
  let end = 0;
  return rows.map((item, at): AqStation => {
    const label = `${what}.stations[${at}]`;
    const row = list(item, label);
    if (row.length !== width) throw new Error(`${label} must be a row of ${width} values`);
    const id = string(row[0], `${label}.id`, AQ_STATION_ID);
    if (id <= previous) throw new Error(`${what}.stations must be sorted by id and unique`);
    previous = id;
    if (AQ_STATION_ID.exec(id)![1] !== network.id)
      throw new Error(`${label}.id does not belong to its network`);
    const offset = integer(row[width - 2], `${label}.offset`);
    const length = integer(row[width - 1], `${label}.length`, 1);
    if (offset < end) throw new Error(`${label}.offset precedes the previous station's span`);
    if (offset + length > network.file.byteLength)
      throw new Error(`${label} spans past the end of the file it indexes`);
    end = offset + length;
    const values: Record<string, number | null> = {};
    index.elements.forEach((key, column) => {
      values[key] = elementValue(key, row[8 + column], `${label}.${key}`);
    });
    return {
      id,
      network: position,
      lat: number(row[1], `${label}.lat`, LATITUDE),
      lon: number(row[2], `${label}.lon`, LONGITUDE),
      elev: optionalNumber(row[3], `${label}.elev`, ELEVATION),
      rank: integer(row[4], `${label}.rank`),
      name: optionalString(row[5], `${label}.name`),
      agency: optionalString(row[6], `${label}.agency`),
      obsTime: timestamp(row[7], `${label}.obsTime`),
      values,
      offset,
      length,
    };
  });
}

/** The index with its networks' rows filled in: `tables[i]` is network
 * `i`'s rows, or null when its table failed, which drops the network (and
 * renumbers the rest) rather than the index. */
export function assembleAqIndex(index: AqIndex, tables: readonly (AqStation[] | null)[]): AqIndex {
  const networks: AqNetwork[] = [];
  const stations: AqStation[] = [];
  index.networks.forEach((network, position) => {
    const rows = tables[position];
    if (!rows) return;
    const renumbered = networks.length;
    networks.push(network);
    for (const row of rows) stations.push({ ...row, network: renumbered });
  });
  return { ...index, networks, stations };
}

/** One line of a network file, the slice a row's span reads out. */
export function parseAqStation(input: unknown): AqStationSeries {
  const value = object(input, "station");
  const time = list(value.time, "station.time").map((item, index) =>
    integer(item, `station.time[${index}]`),
  );
  if (time.length === 0) throw new Error("station.time must not be empty");
  for (let index = 1; index < time.length; index += 1)
    if (time[index]! <= time[index - 1]!) throw new Error("station.time must be strictly increasing");
  const columns = object(value.obs, "station.obs");
  const obs: Record<string, (number | null)[]> = {};
  for (const [key, column] of Object.entries(columns)) {
    if (!ELEMENT_KEY.test(key)) throw new Error(`station.obs has a malformed key ${key}`);
    const values = list(column, `station.obs.${key}`);
    if (values.length !== time.length) throw new Error(`station.obs.${key} must be as long as time`);
    obs[key] = values.map((item, index) => elementValue(key, item, `station.obs.${key}[${index}]`));
  }
  let names: Record<string, string> | null = null;
  if (value.names !== null && value.names !== undefined) {
    names = Object.fromEntries(
      Object.entries(object(value.names, "station.names")).map(([tag, name]) => [
        tag,
        string(name, `station.names.${tag}`),
      ]),
    );
  }
  return {
    id: string(value.id, "station.id", AQ_STATION_ID),
    name: optionalString(value.name, "station.name"),
    names,
    lat: number(value.lat, "station.lat", LATITUDE),
    lon: number(value.lon, "station.lon", LONGITUDE),
    agency: optionalString(value.agency, "station.agency"),
    time,
    obs,
  };
}

/** The unit an element is published in: the index's own table, else §2. */
export function aqUnit(index: AqIndex, key: string): string {
  return index.elementTable[key]?.unit ?? AQ_DEFAULT_UNITS[key] ?? "";
}
