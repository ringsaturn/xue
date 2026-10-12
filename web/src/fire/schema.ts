/** The active fire product, schema v1 (`docs/fire.md`, written by
 * `xuebuild/fire/schema.py`): NASA FIRMS detections in a seven-day window,
 * a mutable pointer naming an immutable round whose `index.json` lists the
 * sensors and one row per sensor-hour, each addressing its line in the
 * sensor's `.jsonl` by byte span.
 *
 * Admission is structural, as in `stations/schema.ts`: a sensor id this
 * build has never seen passes; a wrong shape, a value outside the
 * contract's range and a `schemaVersion` above the one implemented do not.
 * Unknown fields are ignored. */

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

const FIRE_SCHEMA_VERSION = 1;
export const FIRE_POINTER_FILENAME = "latest-fire.json";

const FIRE_PATH = /^fire\.\d{10}00\/index\.json$/;
const SENSOR_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;

const HOUR_SECONDS = 3600;
const LATITUDE_E4: [number, number] = [-900000, 900000];
const LONGITUDE_E4: [number, number] = [-1800000, 1800000];
const BT_DK: [number, number] = [2000, 6000];
const CONFIDENCE = /^[lnh]*$/;
const DAY_NIGHT = /^[DN]*$/;

export type FirePointer = ProductPointer<"fire">;

export interface FireFile {
  path: string;
  byteLength: number;
  crc32: string;
}

export interface FireSensor {
  id: string;
  name: string;
  /** Nominal pixel size, metres. */
  resolution: number;
  attribution: string;
  license: string;
  url: string;
  latest: string | null;
  count: number;
  file: FireFile;
}

/** One sensor-hour: `hours[]` expanded. */
export interface FireHour {
  sensor: number;
  /** Epoch milliseconds of the hour's start. */
  hour: number;
  count: number;
  /** MW. */
  frpMax: number;
  offset: number;
  length: number;
}

export interface FireIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  window: { start: string; end: string };
  sensors: FireSensor[];
  hours: FireHour[];
}

/** One sensor-hour's detections, decoded to display units. */
export interface FireLine {
  sensor: string;
  /** Epoch seconds of the hour. */
  hour: number;
  /** Epoch milliseconds of each acquisition. */
  time: number[];
  /** Degrees. */
  lat: number[];
  lon: number[];
  /** MW. */
  frp: number[];
  /** Kelvin. */
  bt: number[];
  conf: string;
  dn: string;
}

function list(input: unknown, label: string): unknown[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input;
}

function bounded(value: unknown, label: string, range: [number, number]): number {
  if (typeof value !== "number" || !Number.isInteger(value))
    throw new Error(`${label} must be an integer`);
  if (value < range[0] || value > range[1])
    throw new Error(`${label} is outside ${range[0]}..${range[1]}`);
  return value;
}

function fireFile(input: unknown, label: string, name: string): FireFile {
  const value = object(input, label);
  if (value.path !== name)
    throw new Error(`${label}.path must be ${name}, the file beside the index`);
  return {
    path: name,
    byteLength: integer(value.byteLength, `${label}.byteLength`),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

export function parseFirePointer(input: unknown): FirePointer {
  return pointer(input, "fire", FIRE_PATH, FIRE_SCHEMA_VERSION);
}

/** `fire.<round>/index.json`: the sensors, and the hour rows checked
 * against the file each addresses — sorted by sensor then hour, each
 * sensor's spans consecutive (one newline apart) and inside its file. */
export function parseFireIndex(input: unknown): FireIndex {
  const value = object(input, "fire index");
  const version = schemaVersion(value.schemaVersion, "fire index", FIRE_SCHEMA_VERSION);
  const window = object(value.window, "index.window");
  const sensors = list(value.sensors, "index.sensors").map((item, index) => {
    const label = `index.sensors[${index}]`;
    const entry = object(item, label);
    const id = string(entry.id, `${label}.id`, SENSOR_ID);
    return {
      id,
      name: string(entry.name, `${label}.name`),
      resolution: integer(entry.resolution, `${label}.resolution`, 1),
      attribution: string(entry.attribution, `${label}.attribution`),
      license: string(entry.license, `${label}.license`),
      url: string(entry.url, `${label}.url`),
      latest: entry.latest === null || entry.latest === undefined ? null : timestamp(entry.latest, `${label}.latest`),
      count: integer(entry.count, `${label}.count`),
      file: fireFile(entry.file, `${label}.file`, `${id}.jsonl`),
    } satisfies FireSensor;
  });
  if (new Set(sensors.map((sensor) => sensor.id)).size !== sensors.length)
    throw new Error("index.sensors lists a sensor twice");

  let previous: FireHour | null = null;
  const hours = list(value.hours, "index.hours").map((item, index) => {
    const label = `index.hours[${index}]`;
    const row = list(item, label);
    if (row.length < 6) throw new Error(`${label} must have six columns`);
    const sensor = integer(row[0], `${label}[0]`);
    if (sensor >= sensors.length) throw new Error(`${label} names no sensor`);
    const hourText = timestamp(row[1], `${label}[1]`);
    const hour = Date.parse(hourText);
    if (hour % (HOUR_SECONDS * 1000) !== 0) throw new Error(`${label}[1] is not on the hour`);
    const count = integer(row[2], `${label}[2]`, 1);
    const frpMax = row[3];
    if (typeof frpMax !== "number" || !Number.isFinite(frpMax) || frpMax < 0)
      throw new Error(`${label}[3] must be a non-negative number`);
    const offset = integer(row[4], `${label}[4]`);
    const length = integer(row[5], `${label}[5]`, 1);
    const file = sensors[sensor]!.file;
    if (offset + length > file.byteLength)
      throw new Error(`${label} spans past the end of ${file.path}`);
    if (previous !== null && previous.sensor === sensor) {
      if (hour <= previous.hour) throw new Error(`${label} is not after the previous hour`);
      if (offset !== previous.offset + previous.length + 1)
        throw new Error(`${label} does not follow the previous line`);
    } else if (previous !== null && sensor < previous.sensor) {
      throw new Error(`${label} is not sorted by sensor`);
    } else if (offset !== 0) {
      throw new Error(`${label} is a sensor's first line but does not start its file`);
    }
    const parsed: FireHour = { sensor, hour, count, frpMax, offset, length };
    previous = parsed;
    return parsed;
  });

  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    window: {
      start: timestamp(window.start, "index.window.start"),
      end: timestamp(window.end, "index.window.end"),
    },
    sensors,
    hours,
  };
}

/** One line of a `<sensor>.jsonl`: the column arrays checked against `n`
 * and §2's ranges, and decoded to degrees, MW, kelvin and epoch ms. */
export function parseFireLine(input: unknown): FireLine {
  const value = object(input, "fire line");
  const sensor = string(value.sensor, "line.sensor", SENSOR_ID);
  const hour = integer(value.hour, "line.hour");
  if (hour % HOUR_SECONDS !== 0) throw new Error("line.hour is not on the hour");
  const n = integer(value.n, "line.n", 1);
  const column = (key: string, range: [number, number]): number[] => {
    const items = list(value[key], `line.${key}`);
    if (items.length !== n) throw new Error(`line.${key} is not ${n} long`);
    return items.map((item, index) => bounded(item, `line.${key}[${index}]`, range));
  };
  const t = column("t", [0, HOUR_SECONDS - 60]);
  for (const [index, seconds] of t.entries())
    if (seconds % 60 !== 0) throw new Error(`line.t[${index}] is not a whole minute`);
  const lat = column("lat", LATITUDE_E4);
  const lon = column("lon", LONGITUDE_E4);
  const frp = column("frp", [0, Number.MAX_SAFE_INTEGER]);
  const bt = column("bt", BT_DK);
  const conf = string(value.conf, "line.conf", CONFIDENCE);
  const dn = string(value.dn, "line.dn", DAY_NIGHT);
  if (conf.length !== n) throw new Error(`line.conf is not ${n} long`);
  if (dn.length !== n) throw new Error(`line.dn is not ${n} long`);
  return {
    sensor,
    hour,
    time: t.map((seconds) => (hour + seconds) * 1000),
    lat: lat.map((v) => v / 1e4),
    lon: lon.map((v) => v / 1e4),
    frp: frp.map((v) => v / 10),
    bt: bt.map((v) => v / 10),
    conf,
    dn,
  };
}
