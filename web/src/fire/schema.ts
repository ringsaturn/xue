/** The active fire product, schema v1 (`docs/fire.md`, written by
 * `xuebuild/fire/schema.py`): NASA FIRMS detections in a seven-day window,
 * a mutable pointer naming an immutable round whose `index.json` lists the
 * sensors and one row per sensor-hour, each addressing its line in the
 * sensor's `.jsonl` by byte span.
 *
 * The shapes are `docs/schemas/fire-{index,line}.schema.json`, checked by
 * the generated validators; this module adds what a schema cannot say and
 * expands the rows. Admission is structural: a sensor id this build has
 * never seen passes; a wrong shape, a value outside the contract's range
 * and a `schemaVersion` above the one implemented do not. Unknown fields
 * are ignored. */

import { checkFireIndex, type FireSensor as FireSensorRecord } from "../schema/generated/fire-index";
import { checkFireLine } from "../schema/generated/fire-line";
import { pointer, schemaVersion, type ProductPointer } from "../schema/validate";

const FIRE_SCHEMA_VERSION = 1;
export const FIRE_POINTER_FILENAME = "latest-fire.json";

const FIRE_PATH = /^fire\.\d{10}00\/index\.json$/;

export type FirePointer = ProductPointer<"fire">;

/** One entry of `sensors[]`. */
export type FireSensor = FireSensorRecord;

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

export function parseFirePointer(input: unknown): FirePointer {
  return pointer(input, "fire", FIRE_PATH, FIRE_SCHEMA_VERSION);
}

/** `fire.<round>/index.json`: the schema's shape, then what it cannot say —
 * each file is its sensor's, and the hour rows are sorted by sensor then
 * hour, each sensor's spans consecutive (one newline apart) and inside its
 * file. */
export function parseFireIndex(input: unknown): FireIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "fire index", FIRE_SCHEMA_VERSION);
  checkFireIndex(input, "index");
  const sensors = input.sensors;
  sensors.forEach((sensor, index) => {
    if (sensor.file.path !== `${sensor.id}.jsonl`)
      throw new Error(`index.sensors[${index}].file.path must be ${sensor.id}.jsonl, the file beside the index`);
  });
  if (new Set(sensors.map((sensor) => sensor.id)).size !== sensors.length)
    throw new Error("index.sensors lists a sensor twice");

  let previous: FireHour | null = null;
  const hours = input.hours.map((row, index) => {
    const label = `index.hours[${index}]`;
    const [sensor, hourText, count, frpMax, offset, length] = row;
    if (sensor >= sensors.length) throw new Error(`${label} names no sensor`);
    const hour = Date.parse(hourText);
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
    issued: input.issued,
    generated: input.generated,
    window: { start: input.window.start, end: input.window.end },
    sensors,
    hours,
  };
}

/** One line of a `<sensor>.jsonl`: the schema's shape and ranges, every
 * column `n` long, decoded to degrees, MW, kelvin and epoch ms. */
export function parseFireLine(input: unknown): FireLine {
  checkFireLine(input, "line");
  const { sensor, hour, n, t, lat, lon, frp, bt, conf, dn } = input;
  const columns = { t, lat, lon, frp, bt, conf, dn };
  for (const [key, column] of Object.entries(columns))
    if (column.length !== n) throw new Error(`line.${key} is not ${n} long`);
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
