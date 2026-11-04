/** The three point products the shell draws as station marks — the
 * radiosonde soundings (`docs/sounding.md`, written by
 * `xuebuild/sounding/schema.py`), the airports (`docs/airport.md`,
 * `xuebuild/airport/schema.py`) and the surface stations (`docs/synop.md`,
 * `xuebuild/synop/schema.py`). All take the same shape as the storm
 * tracks: a mutable pointer names an immutable directory, the directory's
 * `index.json` is what a marker layer reads, and one line of the `.jsonl`
 * beside it — addressed by the index's own byte span — is one station's
 * whole record.
 *
 * The shapes are `docs/schemas/{sounding,airport,synop}-*.schema.json`,
 * checked by the generated validators after each file's version gate; this
 * module adds what a schema cannot say and expands the rows, finding each
 * column by the file's `columns` (by position in a file from before the
 * field). Admission is structural, the posture the manifest and
 * `tc/schema.ts` take: a station identifier, a sonde type or a source id
 * this build has never seen passes; a shape that is wrong, a number outside
 * the range the contract admits, and a `schemaVersion` above the one
 * implemented do not. Unknown fields are ignored rather than refused, so a
 * reader survives an added optional field. */

import { checkAirportIndex } from "../schema/generated/airport-index";
import { checkAirportLine, type CloudLayer as CloudLayerRecord } from "../schema/generated/airport-line";
import { checkSoundingIndex } from "../schema/generated/sounding-index";
import { checkSoundingLine, type Sounding as SoundingRecord } from "../schema/generated/sounding-line";
import { checkSynopIndex } from "../schema/generated/synop-index";
import { checkSynopIndexV1 } from "../schema/generated/synop-index-v1";
import { checkSynopLine } from "../schema/generated/synop-line";
import { checkSynopStationsFile } from "../schema/generated/synop-stations";
import { integer, object, pointer, schemaVersion, type ProductPointer } from "../schema/validate";

const STATION_SCHEMA_VERSION = 1;
export const SOUNDING_POINTER_FILENAME = "latest-sounding.json";
export const AIRPORT_POINTER_FILENAME = "latest-airport.json";
export const SYNOP_POINTER_FILENAME = "latest-synop.json";

/** A sounding's seven level arrays; -32768 is missing in every one but
 * `p`, which the schema holds present (`docs/sounding.md` section 2). */
const LEVEL_ARRAYS = ["p", "z", "t", "td", "wd", "ws", "sig"] as const;

/** `sounding/YYYY/MM/DD/sounding.<hour>/index.json`, or the flat
 * `sounding.<hour>/index.json` issues were published under before. */
const SOUNDING_PATH =
  /^(?:sounding\/(\d{4})\/(\d{2})\/(\d{2})\/sounding\.\1\2\3\d{2}|sounding\.\d{10})\/index\.json$/;
/** `airport/YYYY/MM/DD/airport.<round>/index.json`, or the flat
 * `airport.<round>/index.json` rounds were published under before. */
const AIRPORT_PATH =
  /^(?:airport\/(\d{4})\/(\d{2})\/(\d{2})\/airport\.\1\2\3\d{4}|airport\.\d{12})\/index\.json$/;
/** `synop/YYYY/MM/DD/synop.<round>/index.json`, or the flat
 * `synop.<round>/index.json` rounds were published under before. */
const SYNOP_PATH =
  /^(?:synop\/(\d{4})\/(\d{2})\/(\d{2})\/synop\.\1\2\3\d{4}|synop\.\d{12})\/index\.json$/;
/** `<network>:<the network's own station number>`; the first group is the
 * network. */
const SYNOP_STATION_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$/;
const ELEMENT_KEY = /^[a-z][a-z0-9]*$/;

const FLIGHT_CATEGORIES = ["VFR", "MVFR", "IFR", "LIFR"] as const;
export type FlightCategory = (typeof FLIGHT_CATEGORIES)[number];

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
// What the schemas cannot say, shared by the three products' readers. Each
// names the field it refused, so a rejected product says which value cost
// it.

/** A file beside the index, by the one name it may have. */
function stationFile(value: StationFile, label: string, name: string): StationFile {
  if (value.path !== name)
    throw new Error(`${label}.path must be ${name}, the file beside the index`);
  return { path: name, byteLength: value.byteLength, crc32: value.crc32 };
}

function sources(
  input: readonly { id: string; ok: boolean; url?: string; fetched?: string | null; error?: string }[],
  label: string,
): StationSource[] {
  return input.map((value, index) => {
    if (!value.ok && value.error === undefined)
      throw new Error(`${label}[${index}] failed without an error`);
    const status: StationSource = { id: value.id, ok: value.ok };
    if (value.url !== undefined) status.url = value.url;
    if (typeof value.fetched === "string") status.fetched = value.fetched;
    if (value.error !== undefined) status.error = value.error;
    return status;
  });
}

/** The byte span of one station's line, checked against the file the
 * index measured: spans are in row order, do not overlap, and lie inside
 * it. `previousEnd` is where the last station's line ended. */
function span(offset: number, length: number, label: string, previousEnd: number, fileBytes: number): void {
  if (offset < previousEnd)
    throw new Error(`${label}.offset precedes the previous station's span`);
  if (offset + length > fileBytes)
    throw new Error(`${label} spans past the end of the file it indexes`);
}

/** Where each of `names` sits in a row: by the file's `columns` when it
 * names them, else in place. Returns the positions and the rows' width. */
function columnPositions(
  columns: readonly unknown[] | undefined,
  names: readonly string[],
  what: string,
): { at: Record<string, number>; width: number } {
  const held = (columns as readonly string[] | undefined) ?? names;
  if (new Set(held).size !== held.length) throw new Error(`${what}.columns lists a column twice`);
  const at: Record<string, number> = {};
  for (const name of names) {
    const found = held.indexOf(name);
    if (found < 0) throw new Error(`${what}.columns has no ${name}`);
    at[name] = found;
  }
  return { at, width: held.length };
}

export function parseSoundingPointer(input: unknown): SoundingPointer {
  return pointer(input, "sounding", SOUNDING_PATH, STATION_SCHEMA_VERSION);
}

export function parseAirportPointer(input: unknown): AirportPointer {
  return pointer(input, "airport", AIRPORT_PATH, STATION_SCHEMA_VERSION);
}

/** `sounding.<issue>/index.json`: the schema's shape, then the stations
 * with their headline summary and the span each one's soundings occupy in
 * the file beside it, in order and inside it. */
export function parseSoundingIndex(input: unknown): SoundingIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "sounding index", STATION_SCHEMA_VERSION);
  checkSoundingIndex(input, "index");
  const soundings = stationFile(input.soundings, "index.soundings", "soundings.jsonl");
  let end = 0;
  const stations = input.stations.map((entry, index): SoundingStationEntry => {
    const label = `index.stations[${index}]`;
    span(entry.offset, entry.length, label, end, soundings.byteLength);
    // The newline the span excludes: the next station's line starts after it.
    end = entry.offset + entry.length + 1;
    if (entry.times[0] !== entry.latest)
      throw new Error(`${label}.latest must equal the first of its times`);
    const headline = entry.headline;
    return {
      id: entry.id,
      wmo: entry.wmo ?? null,
      name: entry.name ?? null,
      lat: entry.lat,
      lon: entry.lon,
      elev: entry.elev ?? null,
      offset: entry.offset,
      length: entry.length,
      latest: entry.latest,
      times: entry.times,
      headline: {
        t500: headline.t500 ?? null,
        td500: headline.td500 ?? null,
        freezingLevel: headline.freezingLevel ?? null,
        pw: headline.pw ?? null,
        levels: headline.levels,
      },
    };
  });
  return {
    schemaVersion: version,
    issued: input.issued,
    generated: input.generated,
    soundings,
    stations,
    sources: sources(input.sources, "index.sources"),
  };
}

/** One line of `soundings.jsonl`, the slice an index span reads out: the
 * schema's shape, then every level array `n` long and pressure present
 * and strictly descending. */
export function parseSoundingStation(input: unknown): SoundingStation {
  checkSoundingLine(input, "station");
  return {
    id: input.id,
    wmo: input.wmo ?? null,
    name: input.name ?? null,
    lat: input.lat,
    lon: input.lon,
    elev: input.elev ?? null,
    soundings: input.soundings.map((item, index) => sounding(item, `station.soundings[${index}]`)),
  };
}

function sounding(value: SoundingRecord, label: string): Sounding {
  const { n } = value;
  if (value.reported < n)
    throw new Error(`${label}.reported must be at least ${label}.n`);
  for (const key of LEVEL_ARRAYS)
    if (value[key].length !== n)
      throw new Error(`${label}.${key} must hold n = ${n} integers`);
  // Pressure is the vertical axis: present on every level and strictly
  // descending, so a reader can walk the profile without sorting it.
  for (let index = 1; index < n; index += 1)
    if (value.p[index]! >= value.p[index - 1]!)
      throw new Error(`${label}.p must be strictly descending`);
  const derived = value.derived;
  return {
    time: value.time,
    launched: value.launched ?? null,
    sondeType: value.sondeType ?? null,
    bulletin: value.bulletin ?? null,
    gateway: value.gateway ?? null,
    arrived: value.arrived,
    n,
    reported: value.reported,
    p: value.p,
    z: value.z,
    t: value.t,
    td: value.td,
    wd: value.wd,
    ws: value.ws,
    sig: value.sig,
    derived: {
      freezingLevel: derived.freezingLevel ?? null,
      pw: derived.pw ?? null,
      lapse850_500: derived.lapse850_500 ?? null,
      tropopause: derived.tropopause ?? null,
    },
  };
}

/** The airport index row's columns in the order of an index written before
 * `columns` named them. */
const AIRPORT_ROW = [
  "icao",
  "lat",
  "lon",
  "elev",
  "obsTime",
  "t",
  "td",
  "wd",
  "ws",
  "gust",
  "vis",
  "qnh",
  "category",
  "tafPresent",
  "offset",
  "length",
] as const;

/** `airport.<round>/index.json`: the schema's shape, then the compact rows
 * expanded into the station objects the layer and the card read — found by
 * the index's `columns`, sorted by id, each span inside the history file. */
export function parseAirportIndex(input: unknown): AirportIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "airport index", STATION_SCHEMA_VERSION);
  checkAirportIndex(input, "index");
  const history = stationFile(input.history, "index.history", "history.jsonl");
  const column = columnPositions(input.columns, AIRPORT_ROW, "index");
  let end = 0;
  let previous = "";
  const stations = input.stations.map((row, index): AirportStation => {
    const label = `index.stations[${index}]`;
    if (row.length !== column.width) throw new Error(`${label} must be a row of ${column.width} values`);
    const cell = (name: (typeof AIRPORT_ROW)[number]): unknown => row[column.at[name]!];
    const icao = cell("icao") as string;
    if (icao <= previous) throw new Error("index.stations must be sorted by icao and unique");
    previous = icao;
    const offset = cell("offset") as number;
    const length = cell("length") as number;
    span(offset, length, label, end, history.byteLength);
    end = offset + length;
    return {
      icao,
      lat: cell("lat") as number,
      lon: cell("lon") as number,
      elev: cell("elev") as number | null,
      obsTime: cell("obsTime") as string,
      t: cell("t") as number | null,
      td: cell("td") as number | null,
      wd: cell("wd") as number | null,
      ws: cell("ws") as number | null,
      gust: cell("gust") as number | null,
      vis: cell("vis") as number | null,
      qnh: cell("qnh") as number | null,
      category: cell("category") as FlightCategory | null,
      tafPresent: cell("tafPresent") === 1,
      offset,
      length,
    };
  });
  return {
    schemaVersion: version,
    issued: input.issued,
    generated: input.generated,
    history,
    stations,
    sources: sources(input.sources, "index.sources"),
  };
}

function cloud(layers: readonly CloudLayerRecord[]): CloudLayer[] {
  return layers.map(([cover, base]) => ({ cover, base }));
}

/** One line of `history.jsonl`, the slice an index row's span reads out:
 * the schema's shape, then the METARs newest first and a forecast that
 * does not end before it starts. */
export function parseAirportStation(input: unknown): AirportStationHistory {
  checkAirportLine(input, "station");
  let previous = Infinity;
  const metars = input.metars.map((metar): Metar => {
    const time = Date.parse(metar.time);
    if (time >= previous)
      throw new Error("station.metars must be newest first, strictly decreasing");
    previous = time;
    return {
      time: metar.time,
      raw: metar.raw,
      t: metar.t ?? null,
      td: metar.td ?? null,
      wd: metar.wd ?? null,
      ws: metar.ws ?? null,
      gust: metar.gust ?? null,
      vis: metar.vis ?? null,
      qnh: metar.qnh ?? null,
      slp: metar.slp ?? null,
      wx: metar.wx ?? null,
      cloud: cloud(metar.cloud),
      category: metar.category ?? null,
      auto: metar.auto,
      type: metar.type,
    };
  });
  let forecast: Taf | null = null;
  if (input.taf !== null && input.taf !== undefined) {
    const value = input.taf;
    if (Date.parse(value.to) < Date.parse(value.from)) throw new Error("station.taf ends before it starts");
    forecast = {
      issued: value.issued,
      from: value.from,
      to: value.to,
      raw: value.raw,
      amended: value.amended,
      periods: value.periods.map((period, index): TafPeriod => {
        if (Date.parse(period.to) < Date.parse(period.from))
          throw new Error(`station.taf.periods[${index}] ends before it starts`);
        return {
          from: period.from,
          to: period.to,
          change: period.change ?? null,
          prob: period.prob ?? null,
          wd: period.wd ?? null,
          ws: period.ws ?? null,
          gust: period.gust ?? null,
          vis: period.vis ?? null,
          wx: period.wx ?? null,
          cloud: cloud(period.cloud),
        };
      }),
    };
  }
  return {
    icao: input.icao,
    name: input.name ?? null,
    lat: input.lat,
    lon: input.lon,
    elev: input.elev ?? null,
    iata: input.iata ?? null,
    wmo: input.wmo ?? null,
    metars,
    taf: forecast,
  };
}

// ---------------------------------------------------------------------------
// The surface stations (`docs/synop.md`): one index over many networks, a
// `<network>.jsonl` per network beside it, and every station's window as
// columns on its line.

/** The synop index versions this build reads: v1 carries every station's
 * row itself, v2 moves them into one `<network>.stations.json` per network
 * beside it. The pointer stays at `STATION_SCHEMA_VERSION`. */
const SYNOP_INDEX_SCHEMA_VERSION = 2;
/** A v2 station table's own version. */
const SYNOP_STATIONS_SCHEMA_VERSION = 2;

/** The ranges the contract holds its elements to (`docs/synop.md` §2),
 * which a row's values are checked against by element key: the schemas
 * place them in a line's `obs` but cannot in a row, whose element columns
 * follow `elements`. An element this build does not know is admitted as
 * any number: a network added after this build still reads. */
const SYNOP_BOUNDS: Record<string, [number, number]> = {
  t: [-100, 70],
  td: [-100, 60],
  rh: [0, 100],
  p: [300, 1100],
  slp: [800, 1100],
  wd: [0, 360],
  ws: [0, 150],
  gust: [0, 150],
  pr: [0, 1000],
  pr1h: [0, 1000],
  sun: [0, 60],
  snow: [0, 2000],
  vis: [0, Infinity],
};
const SYNOP_INTEGERS = new Set(["rh", "wd", "snow", "vis"]);

/** A v2 station table row's leading columns; the elements follow, then the
 * byte span. A v1 index row has `network` second. */
const SYNOP_ROW_HEAD = ["id", "lat", "lon", "elev", "rank", "name", "obsTime"] as const;
const SYNOP_ROW_TAIL = ["offset", "length"] as const;

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
  /** v2 only: the network's station table beside the index. Absent from a
   * v1 index, whose rows are in the index itself. */
  stations?: SynopStationsFile;
}

/** `<network>.stations.json` as a v2 index names it. */
export interface SynopStationsFile extends StationFile {
  /** Rows in the table. */
  count: number;
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
  /** The index's version, 1 or 2. Whichever it is, an index the shell
   * holds has its `stations` filled in (`assembleSynopIndex` for v2). */
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

/** A row's element value, held to its range where §2 gives one. */
function elementValue(key: string, value: unknown, label: string): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${label} must be a number`);
  if (SYNOP_INTEGERS.has(key) && !Number.isInteger(value)) throw new Error(`${label} must be an integer`);
  const range = SYNOP_BOUNDS[key];
  if (range && (value < range[0] || value > range[1]))
    throw new Error(`${label} is outside ${range[0]}..${range[1]}`);
  return value;
}

export function parseSynopPointer(input: unknown): SynopPointer {
  return pointer(input, "synop", SYNOP_PATH, STATION_SCHEMA_VERSION);
}

/** One compact station row, v1's or a v2 table's, expanded: its columns
 * found at `at`, the rows `width` wide, `network` the row's network
 * position. Checks the span against the network's file. */
function synopRow(
  row: readonly unknown[],
  label: string,
  column: { at: Record<string, number>; width: number },
  network: number,
  networkFile: StationFile,
  elements: readonly string[],
  previousEnd: number,
): SynopStation {
  if (row.length !== column.width) throw new Error(`${label} must be a row of ${column.width} values`);
  const cell = (name: string): unknown => row[column.at[name]!];
  const offset = cell("offset") as number;
  const length = cell("length") as number;
  if (!Number.isInteger(offset) || offset < 0 || !Number.isInteger(length) || length < 1)
    throw new Error(`${label} has no byte span`);
  span(offset, length, label, previousEnd, networkFile.byteLength);
  const values: Record<string, number | null> = {};
  for (const key of elements) values[key] = elementValue(key, cell(key), `${label}.${key}`);
  return {
    id: cell("id") as string,
    network,
    lat: cell("lat") as number,
    lon: cell("lon") as number,
    elev: cell("elev") as number | null,
    rank: cell("rank") as SynopRank,
    name: cell("name") as string | null,
    obsTime: cell("obsTime") as string,
    values,
    offset,
    length,
  };
}

/** `synop.<round>/index.json`, v1 or v2: the version gate, the version's
 * schema (`synop-index-v1`, `synop-index`), then file names and unique
 * networks and elements. A v1 index comes back whole, its compact rows
 * expanded and each checked against its network's file the way the airport
 * rows are against theirs. A v2 index carries no rows: it comes back with
 * `stations` empty and each network's `stations` table named, for
 * `parseSynopStationTable` and `assembleSynopIndex` to fill in. */
export function parseSynopIndex(input: unknown): SynopIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "synop index", SYNOP_INDEX_SCHEMA_VERSION);
  let value: {
    issued: string;
    generated: string;
    elements: string[];
    sources: { id: string; ok: boolean; url?: string; fetched?: string; error?: string }[];
    networks: {
      id: string;
      name: string;
      cadence: number;
      prPeriod?: number | null;
      attribution: string;
      license: string;
      url: string;
      latest?: string | null;
      file: StationFile;
      stations?: SynopStationsFile;
    }[];
  };
  let rows: unknown[][] = [];
  if (version === 1) {
    checkSynopIndexV1(input, "index");
    value = input;
    rows = input.stations;
  } else {
    checkSynopIndex(input, "index");
    value = input;
  }
  const networks = value.networks.map((entry, index): SynopNetwork => {
    const label = `index.networks[${index}]`;
    const network: SynopNetwork = {
      id: entry.id,
      name: entry.name,
      cadence: entry.cadence,
      prPeriod: entry.prPeriod ?? null,
      attribution: entry.attribution,
      license: entry.license,
      url: entry.url,
      latest: entry.latest ?? null,
      file: stationFile(entry.file, `${label}.file`, `${entry.id}.jsonl`),
    };
    if (entry.stations !== undefined)
      network.stations = { ...stationFile(entry.stations, `${label}.stations`, `${entry.id}.stations.json`), count: entry.stations.count };
    return network;
  });
  if (new Set(networks.map((network) => network.id)).size !== networks.length)
    throw new Error("index.networks lists a network twice");
  const elements = value.elements;
  if (new Set(elements).size !== elements.length)
    throw new Error("index.elements lists an element twice");
  const stations: SynopStation[] = [];
  if (version === 1) {
    const column = columnPositions(undefined, [SYNOP_ROW_HEAD[0], "network", ...SYNOP_ROW_HEAD.slice(1), ...elements, ...SYNOP_ROW_TAIL], "index");
    const ends = networks.map(() => 0);
    let previous = "";
    rows.forEach((row, index) => {
      const label = `index.stations[${index}]`;
      const id = row[0] as string;
      if (id <= previous) throw new Error("index.stations must be sorted by id and unique");
      previous = id;
      const network = row[1] as number;
      if (network >= networks.length) throw new Error(`${label}.network names no network`);
      if (SYNOP_STATION_ID.exec(id)![1] !== networks[network]!.id)
        throw new Error(`${label}.id does not belong to its network`);
      const station = synopRow(row, label, column, network, networks[network]!.file, elements, ends[network]!);
      ends[network] = station.offset + station.length;
      stations.push(station);
    });
  }
  return {
    schemaVersion: version,
    issued: value.issued,
    generated: value.generated,
    networks,
    elements,
    stations,
    sources: sources(value.sources, "index.sources"),
  };
}

/** A v2 `<network>.stations.json`, checked against the index that names
 * it: its own version, `docs/schemas/synop-stations.schema.json`, the
 * network it says it is, the index's element list exactly, the row count
 * the index gave, and v1's row rules (sorted, unique, the network's prefix,
 * spans in row order inside its `.jsonl`), each column found by the file's
 * `columns`. The rows come back as v1's, `network` set to `position`. */
export function parseSynopStationTable(
  input: unknown,
  index: SynopIndex,
  position: number,
): SynopStation[] {
  const network = index.networks[position];
  if (!network) throw new Error(`synop index has no network ${position}`);
  const what = network.stations?.path ?? `${network.id}.stations.json`;
  const version = integer(object(input, what).schemaVersion, `${what}.schemaVersion`, 1);
  if (version !== SYNOP_STATIONS_SCHEMA_VERSION)
    throw new Error(`unsupported ${what} schema version ${version}`);
  checkSynopStationsFile(input, what);
  if (input.network !== network.id)
    throw new Error(`${what}.network must be ${network.id}`);
  const elements = input.elements;
  if (
    elements.length !== index.elements.length ||
    elements.some((key, at) => key !== index.elements[at])
  )
    throw new Error(`${what}.elements must equal the index's`);
  const rows = input.stations;
  if (network.stations && rows.length !== network.stations.count)
    throw new Error(`${what} has ${rows.length} rows, the index says ${network.stations.count}`);
  const column = columnPositions(input.columns, [...SYNOP_ROW_HEAD, ...index.elements, ...SYNOP_ROW_TAIL], what);
  let previous = "";
  let end = 0;
  return rows.map((row, at) => {
    const label = `${what}.stations[${at}]`;
    const id = row[column.at.id!] as string;
    if (typeof id !== "string" || id <= previous) throw new Error(`${what}.stations must be sorted by id and unique`);
    previous = id;
    if (SYNOP_STATION_ID.exec(id)?.[1] !== network.id)
      throw new Error(`${label}.id does not belong to its network`);
    const station = synopRow(row, label, column, position, network.file, index.elements, end);
    end = station.offset + station.length;
    return station;
  });
}

/** The v1-shaped index a v2 one and its station tables make: `tables[i]`
 * is network `i`'s rows, or null when its table failed, which drops the
 * network (and renumbers the rest) rather than the index. The stations are
 * merged in id order, as a v1 index lists them. */
export function assembleSynopIndex(
  index: SynopIndex,
  tables: readonly (SynopStation[] | null)[],
): SynopIndex {
  const networks: SynopNetwork[] = [];
  const stations: SynopStation[] = [];
  index.networks.forEach((network, position) => {
    const rows = tables[position];
    if (!rows) return;
    const renumbered = networks.length;
    networks.push(network);
    for (const row of rows) stations.push({ ...row, network: renumbered });
  });
  stations.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return { ...index, networks, stations };
}

/** One line of a network file, the slice an index row's span reads out:
 * `docs/schemas/synop-line.schema.json`, then `time` strictly increasing
 * and every `obs` array as long. */
export function parseSynopStation(input: unknown): SynopStationSeries {
  checkSynopLine(input, "station");
  const time = input.time;
  for (let index = 1; index < time.length; index += 1)
    if (time[index]! <= time[index - 1]!) throw new Error("station.time must be strictly increasing");
  const obs: Record<string, (number | null)[]> = {};
  for (const [key, column] of Object.entries(input.obs)) {
    if (!ELEMENT_KEY.test(key)) throw new Error(`station.obs has a malformed key ${key}`);
    if (column.length !== time.length) throw new Error(`station.obs.${key} must be as long as time`);
    obs[key] = column;
  }
  return {
    id: input.id,
    name: input.name ?? null,
    names: input.names ?? null,
    lat: input.lat,
    lon: input.lon,
    elev: input.elev ?? null,
    wmo: input.wmo ?? null,
    rank: input.rank,
    time,
    obs,
  };
}
