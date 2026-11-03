/** The air-quality product, schema v1 (`docs/aq.md`, written by
 * `xuebuild/aq/schema.py`): a mutable pointer naming an immutable round
 * whose `index.json` lists the networks, one `<network>.stations.json` per
 * network with every station's position and current values, and one
 * `<network>.jsonl` beside it whose lines — addressed by the stations
 * file's own byte spans — are each station's 24-hour window.
 *
 * The shapes are `docs/schemas/aq-{index,stations,line}.schema.json`,
 * checked by the generated validators; this module adds what a schema
 * cannot say and expands the rows, finding each column by the stations
 * file's `columns` (by position in a file from before the field). Admission
 * is structural: a network, a station or an element key this build has
 * never seen passes; a wrong shape, a value of a known element outside its
 * range and a `schemaVersion` above the one implemented do not. Unknown
 * fields are ignored. */

import { checkAqIndex } from "../schema/generated/aq-index";
import { checkAqLine } from "../schema/generated/aq-line";
import { checkAqStationsFile } from "../schema/generated/aq-stations";
import { pointer, schemaVersion, type ProductPointer } from "../schema/validate";

const AQ_SCHEMA_VERSION = 1;
export const AQ_POINTER_FILENAME = "latest-aq.json";

/** `aq/YYYY/MM/DD/aq.<round>/index.json`, or the flat `aq.<round>/index.json`
 * rounds were published under before. */
const AQ_PATH = /^(?:aq\/(\d{4})\/(\d{2})\/(\d{2})\/aq\.\1\2\3\d{4}|aq\.\d{12})\/index\.json$/;
/** `<network>:<the network's own station identifier>`; the first group is
 * the network. */
const AQ_STATION_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$/;
const ELEMENT_KEY = /^[a-z][a-z0-9]*$/;

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

/** The columns of a v1 row, in the order a file without `columns` lays
 * them out: the station, its current value per element, its byte span. */
const ROW_HEAD = ["id", "lat", "lon", "elev", "rank", "name", "agency", "obsTime"] as const;
const ROW_TAIL = ["offset", "length"] as const;

function inRange(key: string, value: number | null, label: string): number | null {
  const range = AQ_BOUNDS[key];
  if (value !== null && range && (value < range[0] || value > range[1]))
    throw new Error(`${label} is outside ${range[0]}..${range[1]}`);
  return value;
}

function unique(values: readonly string[], label: string): void {
  if (new Set(values).size !== values.length) throw new Error(`${label} lists a key twice`);
}

export function parseAqPointer(input: unknown): AqPointer {
  return pointer(input, "aq", AQ_PATH, AQ_SCHEMA_VERSION);
}

/** `aq.<round>/index.json`: the schema's shape, then what it cannot say —
 * each file is its network's, networks and elements are unique. The
 * stations come from the tables beside it, so the index comes back with
 * `stations` empty. */
export function parseAqIndex(input: unknown): AqIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "aq index", AQ_SCHEMA_VERSION);
  checkAqIndex(input, "index");
  const networks = input.networks.map((entry, index): AqNetwork => {
    for (const [key, name] of [["file", `${entry.id}.jsonl`], ["stations", `${entry.id}.stations.json`]] as const)
      if (entry[key].path !== name)
        throw new Error(`index.networks[${index}].${key}.path must be ${name}, the file beside the index`);
    return {
      id: entry.id,
      name: entry.name,
      cadence: entry.cadence,
      avgPeriod: entry.avgPeriod,
      preliminary: entry.preliminary,
      attribution: entry.attribution,
      license: entry.license,
      licenseUrl: entry.licenseUrl ?? null,
      url: entry.url,
      latest: entry.latest ?? null,
      file: entry.file,
      stations: entry.stations,
    };
  });
  unique(networks.map((network) => network.id), "index.networks");
  unique(input.elements, "index.elements");
  const elementTable: Record<string, AqElementInfo> = {};
  for (const key of input.elements) {
    const entry = input.elementTable?.[key];
    if (entry) elementTable[key] = { unit: entry.unit, cell: entry.cell };
  }
  return {
    schemaVersion: version,
    issued: input.issued,
    generated: input.generated,
    networks,
    elements: input.elements,
    elementTable,
    stations: [],
  };
}

/** Where each column of a row is: by `columns` when the file names them,
 * else by the v1 layout a file from before the field has. */
function columnPositions(columns: readonly string[] | undefined, elements: readonly string[], what: string) {
  const layout = [...ROW_HEAD, ...elements, ...ROW_TAIL];
  const names = columns ?? layout;
  const at = (name: string): number => {
    const found = names.indexOf(name);
    if (found < 0) throw new Error(`${what}.columns has no ${name}`);
    return found;
  };
  unique(names, `${what}.columns`);
  return {
    width: names.length,
    id: at("id"),
    lat: at("lat"),
    lon: at("lon"),
    elev: at("elev"),
    rank: at("rank"),
    name: at("name"),
    agency: at("agency"),
    obsTime: at("obsTime"),
    offset: at("offset"),
    length: at("length"),
    elements: elements.map((key) => [key, at(key)] as const),
  };
}

/** A `<network>.stations.json`: the schema's shape, then checked against
 * the index that names it — the network it says it is, the index's element
 * list exactly, the row count the index gave, rows sorted and unique under
 * the network's prefix, each known element in its range, and spans in row
 * order inside the network's `.jsonl`. */
export function parseAqStationTable(input: unknown, index: AqIndex, position: number): AqStation[] {
  const network = index.networks[position];
  if (!network) throw new Error(`aq index has no network ${position}`);
  const what = network.stations.path;
  schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, what, AQ_SCHEMA_VERSION);
  checkAqStationsFile(input, what);
  if (input.network !== network.id) throw new Error(`${what}.network must be ${network.id}`);
  if (
    input.elements.length !== index.elements.length ||
    input.elements.some((key, at) => key !== index.elements[at])
  )
    throw new Error(`${what}.elements must equal the index's`);
  const rows = input.stations;
  if (rows.length !== network.stations.count)
    throw new Error(`${what} has ${rows.length} rows, the index says ${network.stations.count}`);
  const column = columnPositions(input.columns, index.elements, what);
  let previous = "";
  let end = 0;
  return rows.map((row, at): AqStation => {
    const label = `${what}.stations[${at}]`;
    if (row.length !== column.width) throw new Error(`${label} must be a row of ${column.width} values`);
    const cell = (position: number) => row[position] as unknown;
    const id = cell(column.id);
    if (typeof id !== "string") throw new Error(`${label}.id must be a string`);
    if (id <= previous) throw new Error(`${what}.stations must be sorted by id and unique`);
    previous = id;
    if (AQ_STATION_ID.exec(id)?.[1] !== network.id)
      throw new Error(`${label}.id does not belong to its network`);
    const offset = cell(column.offset) as number;
    const length = cell(column.length) as number;
    if (!Number.isInteger(offset) || !Number.isInteger(length) || offset < 0 || length < 1)
      throw new Error(`${label} has no byte span`);
    if (offset < end) throw new Error(`${label}.offset precedes the previous station's span`);
    if (offset + length > network.file.byteLength)
      throw new Error(`${label} spans past the end of the file it indexes`);
    end = offset + length;
    const values: Record<string, number | null> = {};
    for (const [key, place] of column.elements)
      values[key] = inRange(key, cell(place) as number | null, `${label}.${key}`);
    return {
      id,
      network: position,
      lat: cell(column.lat) as number,
      lon: cell(column.lon) as number,
      elev: cell(column.elev) as number | null,
      rank: cell(column.rank) as number,
      name: cell(column.name) as string | null,
      agency: cell(column.agency) as string | null,
      obsTime: cell(column.obsTime) as string,
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

/** One line of a network file, the slice a row's span reads out: the
 * schema's shape, `time` strictly increasing and every `obs` array an
 * element's and as long as it. */
export function parseAqStation(input: unknown): AqStationSeries {
  checkAqLine(input, "station");
  const time = input.time;
  for (let index = 1; index < time.length; index += 1)
    if (time[index]! <= time[index - 1]!) throw new Error("station.time must be strictly increasing");
  const obs: Record<string, (number | null)[]> = {};
  for (const [key, values] of Object.entries(input.obs)) {
    if (!ELEMENT_KEY.test(key)) throw new Error(`station.obs has a malformed key ${key}`);
    if (values.length !== time.length) throw new Error(`station.obs.${key} must be as long as time`);
    obs[key] = values;
  }
  return {
    id: input.id,
    name: input.name ?? null,
    names: input.names ?? null,
    lat: input.lat,
    lon: input.lon,
    agency: input.agency ?? null,
    time,
    obs,
  };
}

/** The unit an element is published in: the index's own table, else §2. */
export function aqUnit(index: AqIndex, key: string): string {
  return index.elementTable[key]?.unit ?? AQ_DEFAULT_UNITS[key] ?? "";
}
