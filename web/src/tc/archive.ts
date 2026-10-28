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
import type { TcEnsemble, TcForecast, TcStorm, TcTrack } from "./schema";

export const TC_ARCHIVE_DIRECTORY = "tc/archive/";
export const TC_ARCHIVE_CATALOG = `${TC_ARCHIVE_DIRECTORY}catalog.json`;
const ARCHIVE_SCHEMA_VERSION = 1;
const SCORE_SCHEMA_VERSION = 1;
const ATCF_ID = /^[A-Z]{2}\d{6}$/;
const CRC32 = /^[0-9a-f]{8}$/;

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
  /** Absent from an index written before the summary was added. */
  first: string | null;
  last: string | null;
  vmax: number | null;
  months: string[];
  sources: string[];
}

export interface SeasonIndex {
  season: number;
  best: FileRef;
  storms: SeasonStorm[];
}

interface MonthStorm {
  id: string;
  offset: number;
  length: number;
}

interface MonthIndex {
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

interface ScoresDocument {
  season: number;
  leads: number[];
  errors: FileRef;
  storms: { id: string; offset: number; length: number }[];
}

// --- validation --------------------------------------------------------

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error(`${label} must be an object`);
  return value as Record<string, unknown>;
}

function list(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be a list`);
  return value;
}

function fileRef(value: unknown, label: string): FileRef {
  const entry = object(value, label);
  if (typeof entry.path !== "string" || entry.path.includes(".."))
    throw new Error(`${label}.path must be a relative path`);
  if (!Number.isInteger(entry.byteLength) || (entry.byteLength as number) < 0)
    throw new Error(`${label}.byteLength must be a non-negative integer`);
  if (typeof entry.crc32 !== "string" || !CRC32.test(entry.crc32))
    throw new Error(`${label}.crc32 must be 8 lowercase hex characters`);
  return { path: entry.path, byteLength: entry.byteLength as number, crc32: entry.crc32 };
}

function head(value: unknown, label: string, field: string, version: number): Record<string, unknown> {
  const document = object(value, label);
  const declared = document[field];
  if (typeof declared !== "number" || declared > version)
    throw new Error(`${label} ${field} ${String(declared)} is newer than this reader (${version})`);
  return document;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function validateArchiveCatalog(input: unknown): ArchiveCatalog {
  const catalog = head(input, "tc archive catalog", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  return {
    months: list(catalog.months, "catalog.months").map((value, index) => {
      const entry = object(value, `catalog.months[${index}]`);
      if (typeof entry.month !== "string" || !/^\d{4}-\d{2}$/.test(entry.month))
        throw new Error(`catalog.months[${index}].month must be YYYY-MM`);
      return {
        ...fileRef(entry, `catalog.months[${index}]`),
        month: entry.month,
        storms: Number(entry.storms) || 0,
        records: Number(entry.records) || 0,
      };
    }),
    seasons: list(catalog.seasons, "catalog.seasons").map((value, index) => {
      const entry = object(value, `catalog.seasons[${index}]`);
      if (!Number.isInteger(entry.season)) throw new Error(`catalog.seasons[${index}].season must be a year`);
      return { ...fileRef(entry, `catalog.seasons[${index}]`), season: entry.season as number, storms: Number(entry.storms) || 0 };
    }),
    scores: list(catalog.scores ?? [], "catalog.scores").map((value, index) => {
      const entry = object(value, `catalog.scores[${index}]`);
      if (!Number.isInteger(entry.season)) throw new Error(`catalog.scores[${index}].season must be a year`);
      return { ...fileRef(entry, `catalog.scores[${index}]`), season: entry.season as number };
    }),
  };
}

function span(entry: Record<string, unknown>, label: string, optional: boolean): { offset: number | null; length: number | null } {
  if (optional && entry.offset === null && entry.length === null) return { offset: null, length: null };
  if (!Number.isInteger(entry.offset) || !Number.isInteger(entry.length) || (entry.length as number) <= 0)
    throw new Error(`${label} must carry a byte span`);
  return { offset: entry.offset as number, length: entry.length as number };
}

export function validateSeasonIndex(input: unknown): SeasonIndex {
  const index = head(input, "tc season index", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  if (!Number.isInteger(index.season)) throw new Error("season index season must be a year");
  return {
    season: index.season as number,
    best: fileRef(index.best, "season index best"),
    storms: list(index.storms, "season index storms").map((value, position) => {
      const label = `season index storms[${position}]`;
      const entry = object(value, label);
      if (typeof entry.id !== "string" || !ATCF_ID.test(entry.id)) throw new Error(`${label}.id must be an ATCF id`);
      const strings = (field: unknown) => (Array.isArray(field) ? field.filter((item): item is string => typeof item === "string") : []);
      return {
        id: entry.id,
        basin: typeof entry.basin === "string" ? entry.basin : entry.id.slice(0, 2),
        name: optionalText(entry.name),
        sid: optionalText(entry.sid),
        atcf: strings(entry.atcf),
        ...span(entry, label, true),
        best: strings(entry.best),
        first: optionalText(entry.first),
        last: optionalText(entry.last),
        vmax: typeof entry.vmax === "number" ? entry.vmax : null,
        months: strings(entry.months),
        sources: strings(entry.sources),
      };
    }),
  };
}

function validateMonthIndex(input: unknown): MonthIndex {
  const index = head(input, "tc month index", "archiveSchemaVersion", ARCHIVE_SCHEMA_VERSION);
  if (typeof index.month !== "string") throw new Error("month index month must be YYYY-MM");
  return {
    month: index.month,
    forecasts: fileRef(index.forecasts, "month index forecasts"),
    storms: list(index.storms, "month index storms").map((value, position) => {
      const entry = object(value, `month index storms[${position}]`);
      const found = span(entry, `month index storms[${position}]`, false);
      return { id: String(entry.id), offset: found.offset!, length: found.length! };
    }),
  };
}

function validateScores(input: unknown): ScoresDocument {
  const scores = head(input, "tc scores", "scoreSchemaVersion", SCORE_SCHEMA_VERSION);
  return {
    season: Number(scores.season),
    leads: list(scores.leads, "scores.leads").filter((lead): lead is number => typeof lead === "number"),
    errors: fileRef(scores.errors, "scores.errors"),
    storms: list(scores.storms, "scores.storms").map((value, position) => {
      const entry = object(value, `scores.storms[${position}]`);
      const found = span(entry, `scores.storms[${position}]`, false);
      return { id: String(entry.id), offset: found.offset!, length: found.length! };
    }),
  };
}

function validateRecord(input: unknown): ArchiveRecord {
  const record = object(input, "tc archive record");
  if (typeof record.storm !== "string" || typeof record.source !== "string")
    throw new Error("tc archive record names no storm or source");
  const kind = record.kind;
  if (kind !== "agency" && kind !== "model" && kind !== "ensemble")
    throw new Error(`tc archive record kind ${String(kind)} is unknown`);
  const forecast = object(record.forecast, "tc archive record forecast");
  if (typeof forecast.base !== "string") throw new Error("tc archive record forecast has no base");
  if (kind === "ensemble" ? !Array.isArray(forecast.leads) : !Array.isArray(forecast.points))
    throw new Error(`tc archive ${kind} record has the wrong forecast shape`);
  return {
    storm: record.storm,
    basin: String(record.basin),
    name: optionalText(record.name),
    source: record.source,
    kind,
    origin: String(record.origin),
    forecast: forecast as unknown as TcForecast | TcEnsemble,
  };
}

function validateBest(input: unknown): BestRecord {
  const record = object(input, "tc best record");
  if (typeof record.storm !== "string") throw new Error("tc best record names no storm");
  const best = object(record.best, "tc best record best");
  for (const [key, track] of Object.entries(best)) list(object(track, `best.${key}`).points, `best.${key}.points`);
  return {
    storm: record.storm,
    basin: String(record.basin),
    name: optionalText(record.name),
    sid: optionalText(record.sid),
    atcf: Array.isArray(record.atcf) ? record.atcf.map(String) : [record.storm],
    best: best as Record<string, TcTrack>,
  };
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
    if (line === undefined) return null;
    const document = object(line, "tc storm errors");
    return { storm: String(document.storm), truths: object(document.truths, "tc storm errors truths") as StormErrors["truths"] };
  }
}

// --- what a page does with it ---------------------------------------------

/** The season a storm id is numbered in (its last four digits). */
export function seasonOfStorm(id: string): number {
  return Number(id.slice(-4));
}

export function isArchiveStormId(value: string | null): value is string {
  return value !== null && ATCF_ID.test(value);
}

/** `WP06`: the basin and number the centres speak a storm by. */
export function stormCode(id: string): string {
  return id.slice(0, 4);
}

/** The distinct times the storm's forecasts start from, ascending. */
export function issueTimes(records: readonly ArchiveRecord[]): string[] {
  return [...new Set(records.map((record) => record.forecast.base))].sort();
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

/** The bounds `[[west, south], [east, north]]` of a storm's best tracks and
 * forecast starts, longitudes unwrapped about the first so a storm across
 * the antimeridian frames as one box; null for a storm with no position. */
export function archiveBounds(best: BestRecord | null, records: readonly ArchiveRecord[]): [[number, number], [number, number]] | null {
  const points: [number, number][] = [];
  for (const track of Object.values(best?.best ?? {})) for (const point of track.points) points.push([point.lon, point.lat]);
  if (points.length === 0)
    for (const record of records)
      if (record.kind !== "ensemble") for (const point of (record.forecast as TcForecast).points) points.push([point.lon, point.lat]);
  if (points.length === 0) return null;
  const anchor = points[0]![0];
  let west = Infinity;
  let east = -Infinity;
  let south = Infinity;
  let north = -Infinity;
  for (const [lon, lat] of points) {
    const unwrapped = anchor + ((((lon - anchor + 540) % 360) + 360) % 360) - 180;
    west = Math.min(west, unwrapped);
    east = Math.max(east, unwrapped);
    south = Math.min(south, lat);
    north = Math.max(north, lat);
  }
  return [
    [west, south],
    [east, north],
  ];
}
