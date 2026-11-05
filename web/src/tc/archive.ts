/** Reading the tropical cyclone archive (`docs/tc.md` §8–9): the mutable
 * `tc/archive/catalog.json`, a season's and a month's index, and one
 * storm's span of the season's best tracks, the months' forecasts and the
 * season's errors — each span a Range read of a `?v=<crc32>`-addressed
 * file, so a storm costs a few requests whatever the size of the month.
 *
 * And the little a page does with them: the issue times a storm's
 * forecasts start from, the storm as the live product would have carried it
 * at one of them (so the map's `StormLayers` draws it unchanged), and the
 * error means of the scores. Nothing here touches the map. */

import { fetchImmutable } from "../fetchimmutable";
import { checkTcArchiveBest } from "../schema/generated/tc-archive-best-line";
import { checkTcArchiveCatalog } from "../schema/generated/tc-archive-catalog";
import { checkTcArchiveRecord } from "../schema/generated/tc-archive-forecast-line";
import { checkTcArchiveMonthIndex } from "../schema/generated/tc-archive-month-index";
import { checkTcArchiveOverview } from "../schema/generated/tc-archive-overview";
import { checkTcArchiveSeasonIndex } from "../schema/generated/tc-archive-season-index";
import { checkTcStormErrors } from "../schema/generated/tc-errors-line";
import { checkTcScores } from "../schema/generated/tc-scores";
import type { TcEnsemble, TcForecast, TcPoint, TcStorm, TcTrack } from "./schema";

export const TC_ARCHIVE_DIRECTORY = "tc/archive/";
export const TC_ARCHIVE_CATALOG = `${TC_ARCHIVE_DIRECTORY}catalog.json`;
const ARCHIVE_SCHEMA_VERSION = 1;
const SCORE_SCHEMA_VERSION = 1;
/** An ATCF id, or a history id (`WP-1951123N10140-1951`, `docs/tc.md` §8.2)
 * for a storm that never had a number: both open on the basin and end on the
 * season. */
const STORM_ID = /^[A-Z]{2}(\d{6}|-\d{7}[NS]\d{5}-\d{4})$/;

export interface FileRef {
  path: string;
  byteLength: number;
  crc32: string;
}

interface CatalogMonth extends FileRef {
  month: string;
  storms: number;
  records: number;
}

interface CatalogSeason extends FileRef {
  season: number;
  storms: number;
}

export interface ArchiveCatalog {
  months: CatalogMonth[];
  seasons: CatalogSeason[];
  scores: (FileRef & { season: number })[];
}

export interface SeasonStorm {
  id: string;
  basin: string;
  name: string | null;
  sid: string | null;
  atcf: string[];
  offset: number | null;
  length: number | null;
  best: string[];
  /** Null too in an index written before the summary was added. */
  first: string | null;
  last: string | null;
  vmax: number | null;
  months: string[];
  sources: string[];
}

export interface SeasonIndex {
  season: number;
  best: FileRef;
  /** The season's tracks for a picture of it; null in an index written
   * before it. */
  overview: FileRef | null;
  storms: SeasonStorm[];
}

/** A storm's track at its synoptic times, `[lon×10, lat×10, class]`
 * (`docs/tc.md` §8.3): class 0 (TD) to 6 (H5), −1 for no wind. */
export interface SeasonOverview {
  season: number;
  storms: { id: string; track: [number, number, number][] }[];
}

export interface MonthStorm {
  id: string;
  offset: number;
  length: number;
}

export interface MonthIndex {
  month: string;
  forecasts: FileRef;
  storms: MonthStorm[];
}

export type ArchiveKind = "agency" | "model" | "ensemble";

export interface ArchiveRecord {
  storm: string;
  basin: string;
  name: string | null;
  source: string;
  kind: ArchiveKind;
  origin: string;
  forecast: TcForecast | TcEnsemble;
}

export interface BestRecord {
  storm: string;
  basin: string;
  name: string | null;
  sid: string | null;
  atcf: string[];
  best: Record<string, TcTrack>;
}

/** One source's verified cases against one truth: a column per field, a
 * row per (base, lead), `lead` in hours. */
export interface ErrorColumns {
  base: string[];
  lead: number[];
  track: number[];
  along: (number | null)[];
  cross: (number | null)[];
  vmax: (number | null)[];
  pmin: (number | null)[];
}

export interface StormErrors {
  storm: string;
  truths: Record<string, Record<string, ErrorColumns>>;
}

export interface ScoresDocument {
  season: number;
  leads: number[];
  errors: FileRef;
  storms: { id: string; offset: number; length: number }[];
}

// --- validation --------------------------------------------------------
//
// Each document in three steps, as `tc/schema.ts` reads the hourly product:
// the version gate, the check generated from its schema
// (`docs/schemas/tc-archive-*.schema.json`, `tc-scores`, `tc-errors-line`),
// then what a schema cannot say.

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error(`${label} must be an object`);
  return value as Record<string, unknown>;
}

/** A version above the one implemented is refused before the shape is
 * looked at, so a newer document says so rather than naming a field. */
function gate(input: unknown, label: string, field: string, version: number): void {
  const declared = object(input, label)[field];
  if (typeof declared === "number" && declared > version)
    throw new Error(`${label} ${field} ${String(declared)} is newer than this reader (${version})`);
}

function fileRef(entry: FileRef): FileRef {
  return { path: entry.path, byteLength: entry.byteLength, crc32: entry.crc32 };
}

function unique(ids: readonly string[], label: string): void {
  const seen = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) throw new Error(`${label} lists ${id} twice`);
    seen.add(id);
  }
}

/** `catalog.json`: entries unique by month or season, each path naming
 * its own. */
export function validateArchiveCatalog(input: unknown): ArchiveCatalog {
  gate(input, "tc archive catalog", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  checkTcArchiveCatalog(input, "catalog");
  const months = input.months.map((entry, index) => {
    if (entry.path !== `${entry.month.slice(0, 4)}/${entry.month.slice(5, 7)}/index.json`)
      throw new Error(`catalog.months[${index}].path does not name its month`);
    return { ...fileRef(entry), month: entry.month, storms: entry.storms, records: entry.records };
  });
  const seasons = input.seasons.map((entry, index) => {
    if (entry.path !== `${String(entry.season).padStart(4, "0")}/index.json`)
      throw new Error(`catalog.seasons[${index}].path does not name its season`);
    return { ...fileRef(entry), season: entry.season, storms: entry.storms };
  });
  const scores = (input.scores ?? []).map((entry, index) => {
    if (entry.path !== `${String(entry.season).padStart(4, "0")}/scores.json`)
      throw new Error(`catalog.scores[${index}].path does not name its season`);
    return { ...fileRef(entry), season: entry.season };
  });
  unique(months.map((entry) => entry.month), "catalog.months");
  unique(seasons.map((entry) => String(entry.season)), "catalog.seasons");
  unique(scores.map((entry) => String(entry.season)), "catalog.scores");
  return { months, seasons, scores };
}

/** A season's `index.json`: storms unique and of the season, and a span
 * whose offset and length are null together. */
export function validateSeasonIndex(input: unknown): SeasonIndex {
  gate(input, "tc season index", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  checkTcArchiveSeasonIndex(input, "season index");
  const season = input.season;
  unique(input.storms.map((entry) => entry.id), "season index storms");
  return {
    season,
    best: fileRef(input.best),
    overview: input.overview === undefined ? null : fileRef(input.overview),
    storms: input.storms.map((entry, position) => {
      const label = `season index storms[${position}]`;
      if (seasonOfStorm(entry.id) !== season) throw new Error(`${label} is not a storm of season ${season}`);
      if ((entry.offset === null) !== (entry.length === null))
        throw new Error(`${label} must carry a whole byte span or none`);
      return {
        id: entry.id,
        basin: entry.basin,
        name: entry.name,
        sid: entry.sid,
        atcf: entry.atcf,
        offset: entry.offset,
        length: entry.length,
        best: entry.best,
        first: entry.first ?? null,
        last: entry.last ?? null,
        vmax: entry.vmax ?? null,
        months: entry.months,
        sources: entry.sources ?? [],
      };
    }),
  };
}

export function validateSeasonOverview(input: unknown): SeasonOverview {
  gate(input, "tc season overview", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  checkTcArchiveOverview(input, "overview");
  return {
    season: input.season,
    storms: input.storms.map((entry) => ({
      id: entry.id,
      track: entry.track.map((point): [number, number, number] => [point[0], point[1], point[2]]),
    })),
  };
}

/** A month's `index.json`: storms unique. */
export function validateMonthIndex(input: unknown): MonthIndex {
  gate(input, "tc month index", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  checkTcArchiveMonthIndex(input, "month index");
  unique(input.storms.map((entry) => entry.id), "month index storms");
  return {
    month: input.month,
    forecasts: fileRef(input.forecasts),
    storms: input.storms.map((entry) => ({ id: entry.id, offset: entry.offset, length: entry.length })),
  };
}

/** A season's `scores.json`: storms unique. */
export function validateScores(input: unknown): ScoresDocument {
  gate(input, "tc scores", "scoreSchemaVersion", SCORE_SCHEMA_VERSION);
  checkTcScores(input, "scores");
  unique(input.storms.map((entry) => entry.id), "scores.storms");
  return {
    season: input.season,
    leads: input.leads,
    errors: fileRef(input.errors),
    storms: input.storms.map((entry) => ({ id: entry.id, offset: entry.offset, length: entry.length })),
  };
}

/** A line of `forecasts.jsonl`: the forecast an ensemble exactly when the
 * record says so. */
export function validateRecord(input: unknown): ArchiveRecord {
  checkTcArchiveRecord(input, "tc archive record");
  const { kind, forecast } = input;
  if ((kind === "ensemble") !== "leads" in forecast) throw new Error(`tc archive ${kind} record has the wrong forecast shape`);
  return {
    storm: input.storm,
    basin: input.basin,
    name: input.name,
    source: input.source,
    kind,
    origin: input.origin,
    forecast: forecast as unknown as TcForecast | TcEnsemble,
  };
}

/** A line of `best.jsonl`. */
export function validateBest(input: unknown): BestRecord {
  checkTcArchiveBest(input, "tc best record");
  return {
    storm: input.storm,
    basin: input.basin,
    name: input.name,
    sid: input.sid,
    atcf: input.atcf,
    best: input.best as unknown as Record<string, TcTrack>,
  };
}

/** A line of `errors.jsonl`: every source's columns one length. */
export function validateStormErrors(input: unknown): StormErrors {
  checkTcStormErrors(input, "tc storm errors");
  for (const [truth, sources] of Object.entries(input.truths))
    for (const [source, columns] of Object.entries(sources)) {
      const rows = columns.lead.length;
      if (Object.values(columns).some((column) => column.length !== rows))
        throw new Error(`tc storm errors ${truth}.${source} columns differ in length`);
    }
  return { storm: input.storm, truths: input.truths };
}

// --- reading -----------------------------------------------------------

/** The archive under one data root, its catalog read once per page. */
export class TcArchive {
  private catalogPromise: Promise<ArchiveCatalog> | null = null;
  private readonly documents = new Map<string, Promise<unknown>>();

  constructor(private readonly baseUrl: string) {}

  private url(relative: string, crc?: string): URL {
    const url = new URL(`${TC_ARCHIVE_DIRECTORY}${relative}`, new URL(this.baseUrl, document.baseURI));
    if (crc) url.searchParams.set("v", crc);
    return url;
  }

  catalog(): Promise<ArchiveCatalog> {
    this.catalogPromise ??= (async () => {
      const response = await fetch(this.url("catalog.json"), { cache: "no-cache" });
      if (!response.ok) throw new Error(`tc archive catalog request failed: ${response.status}`);
      return validateArchiveCatalog(await response.json());
    })();
    return this.catalogPromise;
  }

  private json<T>(ref: FileRef, parse: (input: unknown) => T): Promise<T> {
    const key = `${ref.path}?v=${ref.crc32}`;
    let found = this.documents.get(key) as Promise<T> | undefined;
    if (!found) {
      found = (async () => {
        const response = await fetchImmutable(this.url(ref.path, ref.crc32));
        if (!response.ok) throw new Error(`tc archive ${ref.path} request failed: ${response.status}`);
        return parse(await response.json());
      })();
      this.documents.set(key, found);
    }
    return found;
  }

  /** Lines `[offset, offset + length)` of a file, by Range; a server that
   * answers with the whole file is sliced instead. */
  private async lines(directory: string, ref: FileRef, offset: number, length: number): Promise<unknown[]> {
    const response = await fetchImmutable(this.url(`${directory}${ref.path}`, ref.crc32), {
      headers: { Range: `bytes=${offset}-${offset + length - 1}` },
    });
    if (!response.ok) throw new Error(`tc archive ${directory}${ref.path} request failed: ${response.status}`);
    let bytes = new Uint8Array(await response.arrayBuffer());
    if (response.status === 200) bytes = bytes.subarray(offset, offset + length);
    if (bytes.length !== length) throw new Error(`tc archive ${directory}${ref.path} span came back short`);
    return new TextDecoder()
      .decode(bytes)
      .split("\n")
      .filter((line) => line.length > 0)
      .map((line) => JSON.parse(line) as unknown);
  }

  async season(season: number): Promise<SeasonIndex | null> {
    const entry = (await this.catalog()).seasons.find((item) => item.season === season);
    return entry ? this.json(entry, validateSeasonIndex) : null;
  }

  /** The season's tracks for a picture of it, or null when its index
   * names none. */
  async overview(season: number): Promise<SeasonOverview | null> {
    const index = await this.season(season);
    if (!index?.overview) return null;
    return this.json({ ...index.overview, path: `${String(season).padStart(4, "0")}/${index.overview.path}` }, validateSeasonOverview);
  }

  /** The storm's best tracks, or null when the season has none for it. */
  async best(index: SeasonIndex, storm: SeasonStorm): Promise<BestRecord | null> {
    if (storm.offset === null || storm.length === null) return null;
    const [line] = await this.lines(`${String(index.season).padStart(4, "0")}/`, index.best, storm.offset, storm.length);
    return line === undefined ? null : validateBest(line);
  }

  /** Every archived forecast of the storm, over the months it appears in. */
  async forecasts(storm: SeasonStorm): Promise<ArchiveRecord[]> {
    const catalog = await this.catalog();
    const perMonth = await Promise.all(
      storm.months.map(async (month) => {
        const entry = catalog.months.find((item) => item.month === month);
        if (!entry) return [];
        const index = await this.json(entry, validateMonthIndex);
        const span = index.storms.find((item) => item.id === storm.id);
        if (!span) return [];
        const directory = `${month.slice(0, 4)}/${month.slice(5, 7)}/`;
        return (await this.lines(directory, index.forecasts, span.offset, span.length)).map(validateRecord);
      }),
    );
    return perMonth.flat();
  }

  /** The storm's verified errors, or null when its season is not scored
   * or the storm verified nowhere. */
  async errors(season: number, id: string): Promise<StormErrors | null> {
    const entry = (await this.catalog()).scores.find((item) => item.season === season);
    if (!entry) return null;
    const scores = await this.json(entry, validateScores);
    const span = scores.storms.find((item) => item.id === id);
    if (!span) return null;
    const [line] = await this.lines(`${String(season).padStart(4, "0")}/`, scores.errors, span.offset, span.length);
    return line === undefined ? null : validateStormErrors(line);
  }
}

// --- what a page does with it ---------------------------------------------

/** The season a storm id is numbered in (its last four digits). */
export function seasonOfStorm(id: string): number {
  return Number(id.slice(-4));
}

export function isArchiveStormId(value: string | null): value is string {
  return value !== null && STORM_ID.test(value);
}

/** `WP06`: the basin and number the centres speak a storm by; a storm with
 * no number has only its basin. */
export function stormCode(id: string): string {
  return id.slice(0, id[2] === "-" ? 2 : 4);
}

/** Whether any storm of the season has an archived forecast. The seasons
 * that do are the ones with static pages and scores; the rest are best
 * tracks alone. */
export function seasonHasForecasts(index: Pick<SeasonIndex, "storms">): boolean {
  return index.storms.some((storm) => storm.months.length > 0);
}

/** What a season's list has to say about its record, or null for a season of
 * the modern satellite era. IBTrACS names the two years: its `since1980` subset
 * is "the modern era of satellite observations", and its known issues say
 * storms before 1950 were not all matched, so one can be counted several times. */
export function seasonQuality(
  season: number,
  storms: readonly Pick<SeasonStorm, "vmax">[],
): { count: number; missing: number; duplicates: boolean } | null {
  if (season >= 1980) return null;
  return { count: storms.length, missing: storms.filter((storm) => storm.vmax === null).length, duplicates: season < 1950 };
}

/** The distinct times the storm's forecasts start from, ascending. */
export function issueTimes(records: readonly ArchiveRecord[]): string[] {
  return [...new Set(records.map((record) => record.forecast.base))].sort();
}

function isoHour(value: string | number): string {
  return new Date(typeof value === "number" ? value : Date.parse(value)).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** The times a storm can be looked at: every synoptic time a best track
 * places it (whether or not anyone forecast it then — a storm that leaves
 * a centre's area is still tracked) and every time a forecast starts,
 * ascending. */
export function storyTimes(records: readonly ArchiveRecord[], best: BestRecord | null): string[] {
  const times = new Set(records.map((record) => isoHour(record.forecast.base)));
  for (const track of Object.values(best?.best ?? {}))
    for (const point of track.points) {
      const at = Date.parse(point.time);
      if (at % (6 * 3600 * 1000) === 0) times.add(isoHour(at));
    }
  return [...times].sort();
}

/** Whether any forecast is drawn at `time` (`stormAt`'s window). */
export function forecastsAt(records: readonly ArchiveRecord[], time: string): boolean {
  const at = Date.parse(time);
  return records.some((record) => {
    const base = Date.parse(record.forecast.base);
    return base <= at && at - base <= ISSUE_WINDOW_MS;
  });
}

/** How far before an issue time a forecast still counts as current for
 * it: one synoptic step. */
const ISSUE_WINDOW_MS = 6 * 3600 * 1000;

/** The storm as the live product would have carried it at `issue`: for
 * each source, its newest forecast starting at or up to six hours before
 * that time, agencies apart from models, and every best track. */
export function stormAt(
  id: string,
  name: string | null,
  records: readonly ArchiveRecord[],
  best: BestRecord | null,
  issue: string,
): TcStorm {
  const at = Date.parse(issue);
  const chosen = new Map<string, ArchiveRecord>();
  for (const record of records) {
    const base = Date.parse(record.forecast.base);
    if (base > at || at - base > ISSUE_WINDOW_MS) continue;
    const current = chosen.get(record.source);
    if (!current || Date.parse(current.forecast.base) < base) chosen.set(record.source, record);
  }
  const agencies: Record<string, TcForecast> = {};
  const models: Record<string, TcForecast | TcEnsemble> = {};
  for (const [source, record] of chosen) {
    if (record.kind === "agency") agencies[source] = record.forecast as TcForecast;
    else models[source] = record.forecast;
  }
  return {
    schemaVersion: 1,
    id,
    level: "A",
    basin: id.slice(0, 2),
    name,
    sid: best?.sid ?? null,
    intl: null,
    aliases: {},
    best: best?.best ?? {},
    agencies,
    models,
    impact: {},
    alert: null,
    sources: [],
  };
}

/** The sources a storm has forecasts from, agencies first, each group in
 * the order given. */
export function sourcesOf(records: readonly ArchiveRecord[], order: readonly string[]): { agencies: string[]; models: string[] } {
  const rank = (source: string) => {
    const at = order.indexOf(source);
    return at < 0 ? order.length : at;
  };
  const sort = (items: Set<string>) => [...items].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const agencies = new Set<string>();
  const models = new Set<string>();
  for (const record of records) (record.kind === "agency" ? agencies : models).add(record.source);
  return { agencies: sort(agencies), models: sort(models) };
}

export interface ErrorCell {
  /** Mean track error, km. */
  km: number;
  n: number;
}

/** Per source and lead, the mean track error over the storm against one
 * truth (`issue` given: of the one forecast starting then). */
export function trackErrors(
  errors: StormErrors | null,
  truth: string,
  leads: readonly number[],
  issue?: string,
): Map<string, Map<number, ErrorCell>> {
  const result = new Map<string, Map<number, ErrorCell>>();
  const sources = errors?.truths[truth] ?? {};
  for (const [source, columns] of Object.entries(sources)) {
    const sums = new Map<number, { total: number; n: number }>();
    for (let row = 0; row < columns.lead.length; row += 1) {
      const lead = columns.lead[row]!;
      if (!leads.includes(lead)) continue;
      if (issue !== undefined && Date.parse(columns.base[row]!) !== Date.parse(issue)) continue;
      const sum = sums.get(lead) ?? { total: 0, n: 0 };
      sum.total += columns.track[row]!;
      sum.n += 1;
      sums.set(lead, sum);
    }
    if (sums.size === 0) continue;
    result.set(
      source,
      new Map([...sums].map(([lead, sum]) => [lead, { km: sum.total / sum.n, n: sum.n }])),
    );
  }
  return result;
}

/** The truths a storm verified against, `usa` first. */
export function truthsOf(errors: StormErrors | null): string[] {
  return Object.keys(errors?.truths ?? {}).sort((a, b) => (a === "usa" ? -1 : b === "usa" ? 1 : a.localeCompare(b)));
}

/** The widest wind radius a point reports, km, or 0. */
function widestRadius(point: TcPoint): number {
  let widest = 0;
  for (const quadrants of Object.values(point.radii ?? {}))
    for (const value of quadrants ?? []) if (value !== null && value > widest) widest = value;
  return widest;
}

const KM_PER_DEGREE = 111.195;

/** The bounds `[[west, south], [east, north]]` of a storm's best tracks,
 * their wind radii included, and its forecast starts — longitudes
 * unwrapped about the first so a storm across the antimeridian frames as
 * one box; null for a storm with no position. */
export function archiveBounds(best: BestRecord | null, records: readonly ArchiveRecord[]): [[number, number], [number, number]] | null {
  const points: { lon: number; lat: number; km: number }[] = [];
  for (const track of Object.values(best?.best ?? {}))
    for (const point of track.points) points.push({ lon: point.lon, lat: point.lat, km: widestRadius(point) });
  if (points.length === 0)
    for (const record of records)
      if (record.kind !== "ensemble")
        for (const point of (record.forecast as TcForecast).points) points.push({ lon: point.lon, lat: point.lat, km: 0 });
  if (points.length === 0) return null;
  const anchor = points[0]!.lon;
  let west = Infinity;
  let east = -Infinity;
  let south = Infinity;
  let north = -Infinity;
  for (const { lon, lat, km } of points) {
    const unwrapped = anchor + ((((lon - anchor + 540) % 360) + 360) % 360) - 180;
    const dLat = km / KM_PER_DEGREE;
    const dLon = km / (KM_PER_DEGREE * Math.max(0.2, Math.cos((lat * Math.PI) / 180)));
    west = Math.min(west, unwrapped - dLon);
    east = Math.max(east, unwrapped + dLon);
    south = Math.max(-85, Math.min(south, lat - dLat));
    north = Math.min(85, Math.max(north, lat + dLat));
  }
  return [
    [west, south],
    [east, north],
  ];
}
