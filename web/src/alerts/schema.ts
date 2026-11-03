/** The weather warnings product, schema v1 (`docs/alerts.md`, written by
 * `xuebuild/alerts/schema.py`): a mutable pointer naming an immutable
 * round whose `index.json` lists every warning in force at the round with
 * its category, level, times and box, and one `<network>.jsonl` per
 * warning service beside it with each warning's full record. A network
 * that names areas by code points at a boundary set (PMTiles) published
 * once per upstream release under `alerts-areas/`.
 *
 * The shapes are `docs/schemas/alerts-{index,line}.schema.json`, checked
 * by the generated validators; this module adds what a schema cannot say
 * and expands the rows, finding each column by the index's `columns` (by
 * position in an index from before the field). Admission is structural: a
 * network, an event, a category or a native field this build has never
 * seen passes; a wrong shape, a level outside 1 … 5 and a `schemaVersion`
 * above the one implemented do not. Unknown fields are ignored. */

import { checkAlertsIndex } from "../schema/generated/alerts-index";
import { checkAlertLine } from "../schema/generated/alerts-line";
import { pointer, schemaVersion, type ProductPointer } from "../schema/validate";

const ALERTS_SCHEMA_VERSION = 1;
export const ALERTS_POINTER_FILENAME = "latest-alerts.json";

/** `alerts/YYYY/MM/DD/alerts.<round>/index.json`, or the flat
 * `alerts.<round>/index.json` rounds were published under before. */
const ALERTS_PATH =
  /^(?:alerts\/(\d{4})\/(\d{2})\/(\d{2})\/alerts\.\1\2\3\d{4}|alerts\.\d{12})\/index\.json$/;
/** `<network>:<the upstream's identifier>`, which may itself hold `:`. */
const ALERT_ID = /^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):.+$/;

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

/** An index row's columns in the order of an index written before
 * `columns` named them. */
const ROW_FIELDS = [
  "id",
  "network",
  "category",
  "level",
  "event",
  "sent",
  "onset",
  "expires",
  "west",
  "south",
  "east",
  "north",
  "offset",
  "length",
] as const;

function unique(values: readonly string[], label: string): void {
  if (new Set(values).size !== values.length) throw new Error(`${label} lists a value twice`);
}

export function parseAlertsPointer(input: unknown): AlertsPointer {
  return pointer(input, "alerts", ALERTS_PATH, ALERTS_SCHEMA_VERSION);
}

/** Where each of `ROW_FIELDS` sits in the rows: by `columns` when the
 * index names them, else in place. */
function columnPositions(columns: readonly string[] | undefined): Record<(typeof ROW_FIELDS)[number], number> & { width: number } {
  const names: readonly string[] = columns ?? ROW_FIELDS;
  unique(names, "index.columns");
  const positions = { width: names.length } as Record<(typeof ROW_FIELDS)[number], number> & { width: number };
  for (const field of ROW_FIELDS) {
    const found = names.indexOf(field);
    if (found < 0) throw new Error(`index.columns has no ${field}`);
    positions[field] = found;
  }
  return positions;
}

/** `alerts.<round>/index.json`: the schema's shape, then what it cannot
 * say — file names, unique networks, rows sorted under their network with
 * four bounds or none and spans in order inside the network's file. */
export function parseAlertsIndex(input: unknown): AlertsIndex {
  const version = schemaVersion((input as { schemaVersion?: unknown } | null)?.schemaVersion, "alerts index", ALERTS_SCHEMA_VERSION);
  checkAlertsIndex(input, "index");
  const networks = input.networks.map((entry, index): AlertsNetwork => {
    if (entry.file.path !== `${entry.id}.jsonl`)
      throw new Error(`index.networks[${index}].file.path must be ${entry.id}.jsonl, the file beside the index`);
    const areas = entry.areas ?? null;
    return {
      id: entry.id,
      name: entry.name,
      attribution: entry.attribution,
      license: entry.license,
      licenseUrl: entry.licenseUrl ?? null,
      url: entry.url,
      latest: entry.latest ?? null,
      file: entry.file,
      areas: areas && {
        set: areas.set,
        version: areas.version,
        path: areas.path,
        byteLength: areas.byteLength,
        crc32: areas.crc32,
        layers: areas.layers,
      },
    };
  });
  unique(networks.map((network) => network.id), "index.networks");
  const column = columnPositions(input.columns as readonly string[] | undefined);
  const ends = networks.map(() => 0);
  let previous = "";
  const alerts = input.alerts.map((row, index): AlertRow => {
    const label = `index.alerts[${index}]`;
    if (row.length !== column.width) throw new Error(`${label} must be a row of ${column.width} values`);
    const cell = (field: (typeof ROW_FIELDS)[number]): unknown => row[column[field]];
    const id = cell("id") as string;
    if (id <= previous) throw new Error("index.alerts must be sorted by id and unique");
    previous = id;
    const network = cell("network") as number;
    if (network >= networks.length) throw new Error(`${label}.network names no network`);
    if (ALERT_ID.exec(id)?.[1] !== networks[network]!.id)
      throw new Error(`${label}.id does not belong to its network`);
    const bounds = (["west", "south", "east", "north"] as const).map((field) => cell(field) as number | null);
    let bbox: AlertRow["bbox"] = null;
    if (bounds.every((bound) => bound !== null)) bbox = bounds as AlertRow["bbox"];
    else if (!bounds.every((bound) => bound === null)) throw new Error(`${label} must have four bounds or none`);
    const offset = cell("offset") as number;
    const length = cell("length") as number;
    if (offset < ends[network]!) throw new Error(`${label}.offset precedes the previous alert's span`);
    if (offset + length > networks[network]!.file.byteLength)
      throw new Error(`${label} spans past the end of the file it indexes`);
    ends[network] = offset + length;
    return {
      id,
      network,
      category: cell("category") as string,
      level: cell("level") as AlertLevel,
      event: cell("event") as string,
      sent: cell("sent") as string,
      onset: cell("onset") as string | null,
      expires: cell("expires") as string | null,
      bbox,
      offset,
      length,
    };
  });
  return {
    schemaVersion: version,
    issued: input.issued,
    generated: input.generated,
    networks,
    categories: input.categories,
    alerts,
  };
}

/** One line of a network file: the schema's shape, and the network its id
 * names. */
export function parseAlertLine(input: unknown): AlertLine {
  checkAlertLine(input, "alert");
  if (ALERT_ID.exec(input.id)?.[1] !== input.network) throw new Error("alert.id does not belong to its network");
  const texts: Record<string, AlertTexts> = {};
  for (const [tag, fields] of Object.entries(input.texts)) {
    const text: AlertTexts = {};
    for (const key of ["headline", "description", "instruction", "area"] as const)
      if (fields[key] !== undefined) text[key] = fields[key];
    texts[tag] = text;
  }
  return {
    id: input.id,
    network: input.network,
    category: input.category,
    level: input.level,
    native: { event: input.native.event, code: input.native.code ?? null, level: input.native.level ?? null },
    sent: input.sent,
    onset: input.onset ?? null,
    expires: input.expires ?? null,
    texts,
    areas: input.areas,
    geometry: input.geometry ? (input.geometry.coordinates as AlertPolygons) : null,
    url: input.url ?? null,
    sender: input.sender ?? null,
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
