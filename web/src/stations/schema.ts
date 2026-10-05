/** The three point products the shell draws as station marks, schema v1 —
 * the radiosonde soundings (`docs/sounding.md`, written by
 * `xuebuild/sounding/schema.py`), the airports (`docs/airport.md`,
 * `xuebuild/airport/schema.py`) and the surface stations (`docs/synop.md`,
 * `xuebuild/synop/schema.py`). All take the same shape as the storm
 * tracks: a mutable pointer names an immutable directory, the directory's
 * `index.json` is what a marker layer reads, and one line of the `.jsonl`
 * beside it — addressed by the index's own byte span — is one station's
 * whole record.
 *
 * Admission is structural, the posture the manifest and `tc/schema.ts`
 * take: a station identifier, a sonde type or a source id this build has
 * never seen passes; a shape that is wrong, a number outside the range
 * the contract admits, and a `schemaVersion` above the one implemented do
 * not. Unknown fields are ignored rather than refused, so a v1 reader
 * survives an added optional field. */

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

const STATION_SCHEMA_VERSION = 1;
export const SOUNDING_POINTER_FILENAME = "latest-sounding.json";
export const AIRPORT_POINTER_FILENAME = "latest-airport.json";
export const SYNOP_POINTER_FILENAME = "latest-synop.json";

/** The missing value in every one of a sounding's level arrays, `sig`
 * included. A JSON number, not a 16-bit field. */
const SOUNDING_MISSING = -32768;

/** The seven parallel level arrays, in the order the contract lists them,
 * with the fixed-point range each stays inside (`MISSING` aside). The
 * encoder enforces the same table (`xuebuild/sounding/bufr.py`), so
 * anything it writes is something this admits. */
const LEVEL_BOUNDS: Record<string, [number, number]> = {
  p: [1, 120000],
  z: [-1000, 100000],
  t: [10000, 40000],
  td: [10000, 40000],
  wd: [0, 360],
  ws: [0, 3000],
  sig: [0, 262143],
};
const LEVEL_ARRAYS = ["p", "z", "t", "td", "wd", "ws", "sig"] as const;
type LevelArray = (typeof LEVEL_ARRAYS)[number];

/** A WIGOS identifier written out: series, issuer, issue number and the
 * local identifier — digits, hyphens and (rarely) letters. */
const STATION_ID = /^\d+-\d+-\d+-[0-9A-Za-z_]+$/;
const WMO_NUMBER = /^\d{5}$/;
const ICAO = /^[A-Z0-9]{2,4}$/;
/** A gateway id (`jp-jma-gts-to-wis2`) or a native WIS2 topic source
 * (`wis2:jp-jma`), and the airport product's own (`awc-metars`). */
const SOURCE_ID = /^[a-z][a-z0-9]*(?:[.:-][a-z0-9]+)*$/;
/** `sounding/YYYY/MM/DD/sounding.<hour>/index.json`, or the flat
 * `sounding.<hour>/index.json` issues were published under before. */
const SOUNDING_PATH =
  /^(?:sounding\/(\d{4})\/(\d{2})\/(\d{2})\/sounding\.\1\2\3\d{2}|sounding\.\d{10})\/index\.json$/;
const AIRPORT_PATH = /^airport\.\d{12}\/index\.json$/;
const SYNOP_PATH = /^synop\.\d{12}\/index\.json$/;
/** `<network>:<the network's own station number>`; the first group is the
 * network. */
const SYNOP_STATION_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$/;
const NETWORK_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const ELEMENT_KEY = /^[a-z][a-z0-9]*$/;

const FLIGHT_CATEGORIES = ["VFR", "MVFR", "IFR", "LIFR"] as const;
export type FlightCategory = (typeof FLIGHT_CATEGORIES)[number];

/** The ranges the contract admits, shared by both products' readers. A
 * value outside one is not a measurement: the publisher writes null
 * instead of it, and a file that carries one is malformed. */
const LATITUDE: [number, number] = [-90, 90];
const LONGITUDE: [number, number] = [-180, 180];
const ELEVATION: [number, number] = [-500, 9000];
const TEMPERATURE: [number, number] = [-100, 70];
const WIND_DIRECTION: [number, number] = [0, 360];
const WIND_SPEED: [number, number] = [0, 150];
const PRESSURE: [number, number] = [800, 1100];
const HEIGHT: [number, number] = LEVEL_BOUNDS.z!;

/** The pointer both products publish, which is the run pointer's shape
 * with another product name. */
export type SoundingPointer = ProductPointer<"sounding">;
export type AirportPointer = ProductPointer<"airport">;
export type SynopPointer = ProductPointer<"synop">;

/** The file beside the index — `soundings.jsonl`, `history.jsonl` — with
 * the `?v=` a reader requests it under. */
export interface StationFile {
  path: string;
  byteLength: number;
  crc32: string;
}

/** A product's account of one of its sources. Kept as the index carries
 * it: the ids are admitted on shape, so a gateway added after this build
 * still reads. */
interface StationSource {
  id: string;
  ok: boolean;
  url?: string;
  fetched?: string;
  error?: string;
}

/** What a sounding marker draws without fetching the soundings file. */
interface SoundingHeadline {
  /** 500 hPa temperature and dew point, °C to a tenth. */
  t500: number | null;
  td500: number | null;
  /** Geopotential metres; may be negative where the station is. */
  freezingLevel: number | null;
  /** Precipitable water, mm. */
  pw: number | null;
  /** The newest sounding's published level count. */
  levels: number;
}

export interface SoundingStationEntry {
  id: string;
  wmo: string | null;
  name: string | null;
  lat: number;
  lon: number;
  elev: number | null;
  /** The byte span of this station's line in `soundings.jsonl`, the JSON
   * object alone — the span stops before the newline, so the slice parses
   * on its own. */
  offset: number;
  length: number;
  /** The newest nominal time, which equals `times[0]`. */
  latest: string;
  times: string[];
  headline: SoundingHeadline;
}

export interface SoundingIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  soundings: StationFile;
  stations: SoundingStationEntry[];
  sources: StationSource[];
}

/** One sounding of one station, as `soundings.jsonl` carries it: the
 * seven fixed-point level arrays of length `n` and the four derived
 * numbers. Scaling is the contract's (§2): `t` and `td` are K × 100, `ws`
 * m/s × 10, and `SOUNDING_MISSING` is missing in every array. */
export interface Sounding {
  /** The nominal (synoptic) time, not the launch. */
  time: string;
  launched: string | null;
  sondeType: number | null;
  bulletin: string | null;
  gateway: string | null;
  arrived: string;
  n: number;
  /** What the bulletin held before thinning. */
  reported: number;
  p: number[];
  z: number[];
  t: number[];
  td: number[];
  wd: number[];
  ws: number[];
  sig: number[];
  derived: SoundingDerived;
}

interface SoundingDerived {
  freezingLevel: number | null;
  pw: number | null;
  lapse850_500: number | null;
  tropopause: number | null;
}

export interface SoundingStation {
  id: string;
  wmo: string | null;
  name: string | null;
  lat: number;
  lon: number;
  elev: number | null;
  /** Strictly decreasing in `time`, newest first, at most four. */
  soundings: Sounding[];
}

/** One index row of the airport product, expanded from the compact
 * sixteen-value row the file carries. The values from `obsTime` on are
 * the station's newest observation. */
export interface AirportStation {
  icao: string;
  lat: number;
  lon: number;
  elev: number | null;
  obsTime: string;
  /** °C, to a tenth. */
  t: number | null;
  td: number | null;
  /** Degrees true; null when the direction is variable. */
  wd: number | null;
  /** m/s, to a tenth. */
  ws: number | null;
  gust: number | null;
  /** Metres; 10000 means *at least* ten kilometres. */
  vis: number | null;
  /** The altimeter setting, hPa. */
  qnh: number | null;
  category: FlightCategory | null;
  /** Whether the station has a current TAF. */
  tafPresent: boolean;
  offset: number;
  length: number;
}

export interface AirportIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  history: StationFile;
  stations: AirportStation[];
  sources: StationSource[];
}

export type CloudLayer = { cover: string; base: number | null };

export interface Metar {
  time: string;
  raw: string;
  t: number | null;
  td: number | null;
  wd: number | null;
  ws: number | null;
  gust: number | null;
  vis: number | null;
  qnh: number | null;
  slp: number | null;
  /** The present-weather string verbatim (`-SHRA BR`). */
  wx: string | null;
  cloud: CloudLayer[];
  category: FlightCategory | null;
  auto: boolean;
  type: string;
}

export interface TafPeriod {
  from: string;
  to: string;
  /** `FM`, `BECMG`, `TEMPO`, `PROB`, or null on the prevailing group. */
  change: string | null;
  prob: number | null;
  wd: number | null;
  ws: number | null;
  gust: number | null;
  vis: number | null;
  wx: string | null;
  cloud: CloudLayer[];
}

export interface Taf {
  issued: string;
  from: string;
  to: string;
  raw: string;
  amended: boolean;
  periods: TafPeriod[];
}

/** One line of `history.jsonl`: a station's last 24 hours and its current
 * forecast. */
export interface AirportStationHistory {
  icao: string;
  name: string | null;
  lat: number;
  lon: number;
  elev: number | null;
  iata: string | null;
  wmo: string | null;
  /** Strictly decreasing in `time`, newest first. */
  metars: Metar[];
  taf: Taf | null;
}

// ---------------------------------------------------------------------------
// The little validators the parsers are written in, beside the shared ones
// in `schema/validate.ts`. Each names the field it refused, so a rejected
// product says which value cost it.

function list(input: unknown, label: string): unknown[] {
  if (!Array.isArray(input)) throw new Error(`${label} must be a list`);
  return input;
}

function optionalTimestamp(value: unknown, label: string): string | null {
  return value === null || value === undefined ? null : timestamp(value, label);
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

function optionalInteger(value: unknown, label: string, range?: [number, number]): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isInteger(value))
    throw new Error(`${label} must be an integer or null`);
  if (range && (value < range[0] || value > range[1]))
    throw new Error(`${label} is outside ${range[0]}..${range[1]}`);
  return value;
}

function optionalString(value: unknown, label: string, pattern?: RegExp): string | null {
  return value === null || value === undefined ? null : string(value, label, pattern);
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be a boolean`);
  return value;
}

function stationFile(input: unknown, label: string, name: string): StationFile {
  const value = object(input, label);
  if (value.path !== name)
    throw new Error(`${label}.path must be ${name}, the file beside the index`);
  return {
    path: name,
    byteLength: integer(value.byteLength, `${label}.byteLength`),
    crc32: string(value.crc32, `${label}.crc32`, CRC32),
  };
}

function sources(input: unknown, label: string): StationSource[] {
  return list(input, label).map((item, index) => {
    const value = object(item, `${label}[${index}]`);
    const status: StationSource = {
      id: string(value.id, `${label}[${index}].id`, SOURCE_ID),
      ok: boolean(value.ok, `${label}[${index}].ok`),
    };
    if (!status.ok && typeof value.error !== "string")
      throw new Error(`${label}[${index}] failed without an error`);
    if (typeof value.url === "string") status.url = value.url;
    if (typeof value.fetched === "string") status.fetched = value.fetched;
    if (typeof value.error === "string") status.error = value.error;
    return status;
  });
}

/** The byte span of one station's line, checked against the file the
 * index measured: spans are in row order, do not overlap, and lie inside
 * it. `previousEnd` is where the last station's line ended. */
function span(
  value: Record<string, unknown>,
  label: string,
  previousEnd: number,
  fileBytes: number,
): { offset: number; length: number } {
  const offset = integer(value.offset, `${label}.offset`);
  const length = integer(value.length, `${label}.length`, 1);
  if (offset < previousEnd)
    throw new Error(`${label}.offset precedes the previous station's span`);
  if (offset + length > fileBytes)
    throw new Error(`${label} spans past the end of the file it indexes`);
  return { offset, length };
}

export function parseSoundingPointer(input: unknown): SoundingPointer {
  return pointer(input, "sounding", SOUNDING_PATH, STATION_SCHEMA_VERSION);
}

export function parseAirportPointer(input: unknown): AirportPointer {
  return pointer(input, "airport", AIRPORT_PATH, STATION_SCHEMA_VERSION);
}

/** `sounding.<issue>/index.json`: the stations with their headline
 * summary, and the span each one's soundings occupy in the file beside
 * it. */
export function parseSoundingIndex(input: unknown): SoundingIndex {
  const value = object(input, "sounding index");
  const version = schemaVersion(value.schemaVersion, "sounding index", STATION_SCHEMA_VERSION);
  const soundings = stationFile(value.soundings, "index.soundings", "soundings.jsonl");
  let end = 0;
  const stations = list(value.stations, "index.stations").map((item, index) => {
    const label = `index.stations[${index}]`;
    const entry = object(item, label);
    const { offset, length } = span(entry, label, end, soundings.byteLength);
    // The newline the span excludes: the next station's line starts after it.
    end = offset + length + 1;
    const latest = timestamp(entry.latest, `${label}.latest`);
    const times = list(entry.times, `${label}.times`).map((time, position) =>
      timestamp(time, `${label}.times[${position}]`),
    );
    if (times.length === 0) throw new Error(`${label}.times must not be empty`);
    if (times[0] !== latest)
      throw new Error(`${label}.latest must equal the first of its times`);
    return {
      id: string(entry.id, `${label}.id`, STATION_ID),
      wmo: optionalString(entry.wmo, `${label}.wmo`, WMO_NUMBER),
      name: optionalString(entry.name, `${label}.name`),
      lat: number(entry.lat, `${label}.lat`, LATITUDE),
      lon: number(entry.lon, `${label}.lon`, LONGITUDE),
      elev: optionalNumber(entry.elev, `${label}.elev`, ELEVATION),
      offset,
      length,
      latest,
      times,
      headline: headline(entry.headline, `${label}.headline`),
    } satisfies SoundingStationEntry;
  });
  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    soundings,
    stations,
    sources: sources(value.sources, "index.sources"),
  };
}

function headline(input: unknown, label: string): SoundingHeadline {
  const value = object(input, label);
  return {
    t500: optionalNumber(value.t500, `${label}.t500`, [-120, 60]),
    td500: optionalNumber(value.td500, `${label}.td500`, [-150, 60]),
    freezingLevel: optionalNumber(value.freezingLevel, `${label}.freezingLevel`, HEIGHT),
    pw: optionalNumber(value.pw, `${label}.pw`, [0, 300]),
    levels: integer(value.levels, `${label}.levels`, 1),
  };
}

/** One line of `soundings.jsonl`, the slice an index span reads out. */
export function parseSoundingStation(input: unknown): SoundingStation {
  const value = object(input, "station");
  const soundings = list(value.soundings, "station.soundings");
  if (soundings.length === 0)
    throw new Error("station.soundings must not be empty");
  return {
    id: string(value.id, "station.id", STATION_ID),
    wmo: optionalString(value.wmo, "station.wmo", WMO_NUMBER),
    name: optionalString(value.name, "station.name"),
    lat: number(value.lat, "station.lat", LATITUDE),
    lon: number(value.lon, "station.lon", LONGITUDE),
    elev: optionalNumber(value.elev, "station.elev", ELEVATION),
    soundings: soundings.map((item, index) =>
      sounding(item, `station.soundings[${index}]`),
    ),
  };
}

function sounding(input: unknown, label: string): Sounding {
  const value = object(input, label);
  const n = integer(value.n, `${label}.n`, 1);
  const reported = integer(value.reported, `${label}.reported`, n);
  const arrays = {} as Record<LevelArray, number[]>;
  for (const key of LEVEL_ARRAYS) {
    const [low, high] = LEVEL_BOUNDS[key]!;
    const values = list(value[key], `${label}.${key}`);
    if (values.length !== n)
      throw new Error(`${label}.${key} must hold n = ${n} integers`);
    arrays[key] = values.map((item, index) => {
      const level = integer(item, `${label}.${key}[${index}]`, SOUNDING_MISSING);
      if (level !== SOUNDING_MISSING && (level < low || level > high))
        throw new Error(`${label}.${key}[${index}] is out of range`);
      return level;
    });
  }
  // Pressure is the vertical axis: present on every level and strictly
  // descending, so a reader can walk the profile without sorting it.
  for (let index = 0; index < n; index += 1) {
    const pressure = arrays.p[index]!;
    if (pressure === SOUNDING_MISSING)
      throw new Error(`${label}.p is missing on a level`);
    if (index > 0 && pressure >= arrays.p[index - 1]!)
      throw new Error(`${label}.p must be strictly descending`);
  }
  const derived = object(value.derived, `${label}.derived`);
  return {
    time: timestamp(value.time, `${label}.time`),
    launched: optionalTimestamp(value.launched, `${label}.launched`),
    sondeType: optionalInteger(value.sondeType, `${label}.sondeType`, [0, 1023]),
    bulletin: optionalString(value.bulletin, `${label}.bulletin`),
    gateway: optionalString(value.gateway, `${label}.gateway`, SOURCE_ID),
    arrived: timestamp(value.arrived, `${label}.arrived`),
    n,
    reported,
    ...arrays,
    derived: {
      freezingLevel: optionalNumber(derived.freezingLevel, `${label}.derived.freezingLevel`, HEIGHT),
      pw: optionalNumber(derived.pw, `${label}.derived.pw`, [0, 300]),
      lapse850_500: optionalNumber(derived.lapse850_500, `${label}.derived.lapse850_500`, [-30, 30]),
      tropopause: optionalNumber(derived.tropopause, `${label}.derived.tropopause`, HEIGHT),
    },
  };
}

function category(value: unknown, label: string): FlightCategory | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !(FLIGHT_CATEGORIES as readonly string[]).includes(value))
    throw new Error(`${label} must be a flight category or null`);
  return value as FlightCategory;
}

/** `airport.<round>/index.json`: the compact rows expanded into the
 * station objects the layer and the card read. */
export function parseAirportIndex(input: unknown): AirportIndex {
  const value = object(input, "airport index");
  const version = schemaVersion(value.schemaVersion, "airport index", STATION_SCHEMA_VERSION);
  const history = stationFile(value.history, "index.history", "history.jsonl");
  let end = 0;
  const stations = list(value.stations, "index.stations").map((item, index) => {
    const label = `index.stations[${index}]`;
    const row = list(item, label);
    if (row.length !== 16)
      throw new Error(`${label} must be a row of 16 values`);
    const [icao, lat, lon, elev, obsTime, t, td, wd, ws, gust, vis, qnh, flight, taf] = row;
    const { offset, length } = span(
      { offset: row[14], length: row[15] },
      label,
      end,
      history.byteLength,
    );
    end = offset + length;
    if (taf !== 0 && taf !== 1)
      throw new Error(`${label}.tafPresent must be 0 or 1`);
    return {
      icao: string(icao, `${label}.icao`, ICAO),
      lat: number(lat, `${label}.lat`, LATITUDE),
      lon: number(lon, `${label}.lon`, LONGITUDE),
      elev: optionalNumber(elev, `${label}.elev`, ELEVATION),
      obsTime: timestamp(obsTime, `${label}.obsTime`),
      t: optionalNumber(t, `${label}.t`, TEMPERATURE),
      td: optionalNumber(td, `${label}.td`, TEMPERATURE),
      wd: optionalInteger(wd, `${label}.wd`, WIND_DIRECTION),
      ws: optionalNumber(ws, `${label}.ws`, WIND_SPEED),
      gust: optionalNumber(gust, `${label}.gust`, WIND_SPEED),
      vis: optionalInteger(vis, `${label}.vis`, [0, Infinity]),
      qnh: optionalNumber(qnh, `${label}.qnh`, PRESSURE),
      category: category(flight, `${label}.category`),
      tafPresent: taf === 1,
      offset,
      length,
    } satisfies AirportStation;
  });
  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    history,
    stations,
    sources: sources(value.sources, "index.sources"),
  };
}

function cloud(input: unknown, label: string): CloudLayer[] {
  return list(input, label).map((item, index) => {
    const layer = list(item, `${label}[${index}]`);
    if (layer.length !== 2)
      throw new Error(`${label}[${index}] must be [cover, base]`);
    return {
      cover: string(layer[0], `${label}[${index}].cover`),
      base: optionalNumber(layer[1], `${label}[${index}].base`, [0, 30000]),
    };
  });
}

function metar(input: unknown, label: string): Metar {
  const value = object(input, label);
  return {
    time: timestamp(value.time, `${label}.time`),
    raw: string(value.raw, `${label}.raw`),
    t: optionalNumber(value.t, `${label}.t`, TEMPERATURE),
    td: optionalNumber(value.td, `${label}.td`, TEMPERATURE),
    wd: optionalInteger(value.wd, `${label}.wd`, WIND_DIRECTION),
    ws: optionalNumber(value.ws, `${label}.ws`, WIND_SPEED),
    gust: optionalNumber(value.gust, `${label}.gust`, WIND_SPEED),
    vis: optionalInteger(value.vis, `${label}.vis`, [0, Infinity]),
    qnh: optionalNumber(value.qnh, `${label}.qnh`, PRESSURE),
    slp: optionalNumber(value.slp, `${label}.slp`, PRESSURE),
    wx: optionalString(value.wx, `${label}.wx`),
    cloud: cloud(value.cloud, `${label}.cloud`),
    category: category(value.category, `${label}.category`),
    auto: boolean(value.auto, `${label}.auto`),
    type: string(value.type, `${label}.type`),
  };
}

function taf(input: unknown, label: string): Taf {
  const value = object(input, label);
  return {
    issued: timestamp(value.issued, `${label}.issued`),
    from: timestamp(value.from, `${label}.from`),
    to: timestamp(value.to, `${label}.to`),
    raw: string(value.raw, `${label}.raw`),
    amended: boolean(value.amended, `${label}.amended`),
    periods: list(value.periods, `${label}.periods`).map((item, index) => {
      const where = `${label}.periods[${index}]`;
      const period = object(item, where);
      return {
        from: timestamp(period.from, `${where}.from`),
        to: timestamp(period.to, `${where}.to`),
        change: optionalString(period.change, `${where}.change`),
        prob: optionalInteger(period.prob, `${where}.prob`, [0, 100]),
        wd: optionalInteger(period.wd, `${where}.wd`, WIND_DIRECTION),
        ws: optionalNumber(period.ws, `${where}.ws`, WIND_SPEED),
        gust: optionalNumber(period.gust, `${where}.gust`, WIND_SPEED),
        vis: optionalInteger(period.vis, `${where}.vis`, [0, Infinity]),
        wx: optionalString(period.wx, `${where}.wx`),
        cloud: cloud(period.cloud, `${where}.cloud`),
      } satisfies TafPeriod;
    }),
  };
}

/** One line of `history.jsonl`, the slice an index row's span reads out. */
export function parseAirportStation(input: unknown): AirportStationHistory {
  const value = object(input, "station");
  const metars = list(value.metars, "station.metars");
  if (metars.length === 0) throw new Error("station.metars must not be empty");
  let previous = Infinity;
  const observations = metars.map((item, index) => {
    const parsed = metar(item, `station.metars[${index}]`);
    const time = Date.parse(parsed.time);
    if (time >= previous)
      throw new Error("station.metars must be newest first, strictly decreasing");
    previous = time;
    return parsed;
  });
  return {
    icao: string(value.icao, "station.icao", ICAO),
    name: optionalString(value.name, "station.name"),
    lat: number(value.lat, "station.lat", LATITUDE),
    lon: number(value.lon, "station.lon", LONGITUDE),
    elev: optionalNumber(value.elev, "station.elev", ELEVATION),
    iata: optionalString(value.iata, "station.iata"),
    wmo: optionalString(value.wmo, "station.wmo", WMO_NUMBER),
    metars: observations,
    taf: value.taf === null || value.taf === undefined ? null : taf(value.taf, "station.taf"),
  };
}

// ---------------------------------------------------------------------------
// The surface stations (`docs/synop.md`): one index over many networks, a
// `<network>.jsonl` per network beside it, and every station's window as
// columns on its line.

/** The ranges v1 holds its elements to (`docs/synop.md` §2). An element
 * this build does not know is admitted as any number: a network added
 * after this build still reads. */
const SYNOP_BOUNDS: Record<string, [number, number]> = {
  t: TEMPERATURE,
  rh: [0, 100],
  p: [300, 1100],
  slp: PRESSURE,
  wd: WIND_DIRECTION,
  ws: WIND_SPEED,
  gust: WIND_SPEED,
  pr: [0, 1000],
  pr1h: [0, 1000],
  sun: [0, 60],
  snow: [0, 2000],
  vis: [0, Infinity],
};

/** How prominent a station is, which the map thins by: 0 a principal
 * station, 1 a multi-element automatic one, 2 a precipitation gauge. */
export type SynopRank = 0 | 1 | 2;

export interface SynopNetwork {
  id: string;
  name: string;
  /** Seconds between a station's reports. */
  cadence: number;
  /** Seconds `pr` and `sun` accumulate over. */
  prPeriod: number | null;
  /** What a display must show beside the network's data. */
  attribution: string;
  license: string;
  url: string;
  latest: string | null;
  file: StationFile;
}

/** One index row, expanded. `values` are the station's current values by
 * element key — each element's newest within the hour before `obsTime`. */
export interface SynopStation {
  id: string;
  /** Position in the index's `networks`. */
  network: number;
  lat: number;
  lon: number;
  elev: number | null;
  rank: SynopRank;
  name: string | null;
  obsTime: string;
  values: Record<string, number | null>;
  offset: number;
  length: number;
}

export interface SynopIndex {
  schemaVersion: number;
  issued: string;
  generated: string;
  networks: SynopNetwork[];
  elements: string[];
  stations: SynopStation[];
  sources: StationSource[];
}

/** One line of a network file: a station's window, column by column.
 * `time` is epoch seconds, oldest first; each array of `obs` is as long. */
export interface SynopStationSeries {
  id: string;
  name: string | null;
  /** The network's own spellings by language tag. */
  names: Record<string, string> | null;
  lat: number;
  lon: number;
  elev: number | null;
  wmo: string | null;
  rank: SynopRank;
  time: number[];
  obs: Record<string, (number | null)[]>;
}

function rank(value: unknown, label: string): SynopRank {
  if (value !== 0 && value !== 1 && value !== 2) throw new Error(`${label} must be 0, 1 or 2`);
  return value;
}

function elementValue(key: string, value: unknown, label: string): number | null {
  return optionalNumber(value, label, SYNOP_BOUNDS[key]);
}

export function parseSynopPointer(input: unknown): SynopPointer {
  return pointer(input, "synop", SYNOP_PATH, STATION_SCHEMA_VERSION);
}

/** `synop.<round>/index.json`: the compact rows expanded, each checked
 * against its network's file the way the airport rows are against theirs. */
export function parseSynopIndex(input: unknown): SynopIndex {
  const value = object(input, "synop index");
  const version = schemaVersion(value.schemaVersion, "synop index", STATION_SCHEMA_VERSION);
  const networks = list(value.networks, "index.networks").map((item, index) => {
    const label = `index.networks[${index}]`;
    const entry = object(item, label);
    const id = string(entry.id, `${label}.id`, NETWORK_ID);
    return {
      id,
      name: string(entry.name, `${label}.name`),
      cadence: integer(entry.cadence, `${label}.cadence`, 60),
      prPeriod: entry.prPeriod === null || entry.prPeriod === undefined
        ? null
        : integer(entry.prPeriod, `${label}.prPeriod`, 60),
      attribution: string(entry.attribution, `${label}.attribution`),
      license: string(entry.license, `${label}.license`),
      url: string(entry.url, `${label}.url`),
      latest: optionalTimestamp(entry.latest, `${label}.latest`),
      file: stationFile(entry.file, `${label}.file`, `${id}.jsonl`),
    } satisfies SynopNetwork;
  });
  if (new Set(networks.map((network) => network.id)).size !== networks.length)
    throw new Error("index.networks lists a network twice");
  const elements = list(value.elements, "index.elements").map((item, index) =>
    string(item, `index.elements[${index}]`, ELEMENT_KEY),
  );
  if (new Set(elements).size !== elements.length)
    throw new Error("index.elements lists an element twice");
  const width = 8 + elements.length + 2;
  const ends = networks.map(() => 0);
  let previous = "";
  const stations = list(value.stations, "index.stations").map((item, index) => {
    const label = `index.stations[${index}]`;
    const row = list(item, label);
    if (row.length !== width) throw new Error(`${label} must be a row of ${width} values`);
    const id = string(row[0], `${label}.id`, SYNOP_STATION_ID);
    if (id <= previous) throw new Error("index.stations must be sorted by id and unique");
    previous = id;
    const network = integer(row[1], `${label}.network`);
    if (network >= networks.length) throw new Error(`${label}.network names no network`);
    if (SYNOP_STATION_ID.exec(id)![1] !== networks[network]!.id)
      throw new Error(`${label}.id does not belong to its network`);
    const { offset, length } = span(
      { offset: row[width - 2], length: row[width - 1] },
      label,
      ends[network]!,
      networks[network]!.file.byteLength,
    );
    ends[network] = offset + length;
    const values: Record<string, number | null> = {};
    elements.forEach((key, position) => {
      values[key] = elementValue(key, row[8 + position], `${label}.${key}`);
    });
    return {
      id,
      network,
      lat: number(row[2], `${label}.lat`, LATITUDE),
      lon: number(row[3], `${label}.lon`, LONGITUDE),
      elev: optionalNumber(row[4], `${label}.elev`, ELEVATION),
      rank: rank(row[5], `${label}.rank`),
      name: optionalString(row[6], `${label}.name`),
      obsTime: timestamp(row[7], `${label}.obsTime`),
      values,
      offset,
      length,
    } satisfies SynopStation;
  });
  return {
    schemaVersion: version,
    issued: timestamp(value.issued, "index.issued"),
    generated: timestamp(value.generated, "index.generated"),
    networks,
    elements,
    stations,
    sources: sources(value.sources, "index.sources"),
  };
}

/** One line of a network file, the slice an index row's span reads out. */
export function parseSynopStation(input: unknown): SynopStationSeries {
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
    const entries = Object.entries(object(value.names, "station.names"));
    names = Object.fromEntries(
      entries.map(([tag, name]) => [tag, string(name, `station.names.${tag}`)]),
    );
  }
  return {
    id: string(value.id, "station.id", SYNOP_STATION_ID),
    name: optionalString(value.name, "station.name"),
    names,
    lat: number(value.lat, "station.lat", LATITUDE),
    lon: number(value.lon, "station.lon", LONGITUDE),
    elev: optionalNumber(value.elev, "station.elev", ELEVATION),
    wmo: optionalString(value.wmo, "station.wmo"),
    rank: rank(value.rank, "station.rank"),
    time,
    obs,
  };
}
