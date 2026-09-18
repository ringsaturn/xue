import {
  FORECAST_MODELS,
  isBundleVariableId,
  type ForecastBundleId,
  type ForecastModelId,
  type ResolutionPreference,
} from "./manifest";
import { isPressureBundle, type PressureBundleId } from "./pressure";
import { variableIds, variableSpec } from "./variables";

/** Default model when the URL names none (or names one this app does not
 * serve — a bad link falls back rather than erroring). */
export const DEFAULT_MODEL: ForecastModelId = "gfs";

/** Layer shown when the URL names none, and the one the app falls back to
 * when a dataset does not ship the requested layer. Every live run carries
 * the core tmp2m/prate pair, so this is always available there. */
export const DEFAULT_VARIABLE: ForecastBundleId = "prate";

/** Canonical `type` value for a registered layer (`VariableSpec.urlName`):
 * the surface members have short names of their own, an isobaric field
 * names itself — its level *is* the layer, so there is no separate
 * `?level=` parameter to keep in step with `?type=` — and a bundle this
 * build has never heard of is written out under its own id. */
function canonicalType(id: ForecastBundleId): string {
  return variableSpec(id)?.urlName ?? id;
}

/** Accepted spellings for the registered layers — each one's canonical
 * name, its bundle id and its aliases, from the variable table. Matching
 * is case-insensitive. Built on first use, after the table is. */
let typeAliasTable: Record<string, ForecastBundleId> | null = null;

function typeAliases(): Record<string, ForecastBundleId> {
  if (typeAliasTable) return typeAliasTable;
  typeAliasTable = Object.fromEntries(
    variableIds().flatMap((id) => {
      const spec = variableSpec(id)!;
      return [spec.urlName, id, ...spec.urlAliases].map((alias) => [alias, id] as const);
    }),
  );
  return typeAliasTable;
}

/** The family an isobaric spelling names: the id's own prefix, plus the
 * spellings a chart reader types — `t850`, `z500`, `humidity700`, `q850`,
 * `vapor850` / `vapour850` / `moisture850`. */
const FAMILY_ALIASES: Record<string, string> = {
  hgt: "hgt",
  z: "hgt",
  tmp: "tmp",
  t: "tmp",
  temp: "tmp",
  rh: "rh",
  humidity: "rh",
  spfh: "spfh",
  q: "spfh",
  wind: "wind",
  qflux: "qflux",
  vapor: "qflux",
  vapour: "qflux",
  moisture: "qflux",
  vvel: "vvel",
  omega: "vvel",
  w: "vvel",
  thetae: "thetae",
  thetase: "thetae",
  epot: "thetae",
};

/** `<family alias><level>` resolved by rule rather than by an enumerated
 * table: the levels a run publishes are the run's business, not the shell's,
 * so a spelling for one this build has never rendered still resolves to the
 * id the manifest would carry. */
function isobaricType(value: string): ForecastBundleId | null {
  const match = /^([a-z]+)(\d+)$/.exec(value);
  if (!match) return null;
  const family = FAMILY_ALIASES[match[1]!];
  return family === undefined ? null : `${family}${match[2]}`;
}

/** The bundle id a `?type=` / `?lines=` spelling names. An alias resolves to
 * its canonical id; anything else that is a well-formed bundle name passes
 * through unchanged, so a run may publish a layer this build has never heard
 * of and a link to it still opens. The caller falls back to the default when
 * the manifest does not ship what came back. */
function resolveType(value: string): ForecastBundleId | null {
  const lower = value.trim().toLowerCase();
  return typeAliases()[lower] ?? isobaricType(lower) ?? (isBundleVariableId(lower) ? lower : null);
}

/** Accepted spellings for each model. Matching is case-insensitive. */
const MODEL_ALIASES: Record<string, ForecastModelId> = {
  gfs: "gfs",
  ecmwf: "ecmwf",
  ifs: "ecmwf",
  aifs: "aifs",
  "aifs-single": "aifs",
  sflux: "sflux",
  "gfs-sflux": "sflux",
  hrrr: "hrrr",
  mrms: "mrms",
  jma: "jma",
  hrpns: "jma",
  cma: "cma",
  "cma-radar": "cma",
  radar: "cma",
  himawari: "himawari",
  "himawari-9": "himawari",
  himawari9: "himawari",
  ahi: "himawari",
  goeseast: "goeseast",
  "goes-east": "goeseast",
  "goes-19": "goeseast",
  goes19: "goeseast",
  goeswest: "goeswest",
  "goes-west": "goeswest",
  "goes-18": "goeswest",
  goes18: "goeswest",
  meteosat: "meteosat",
  "meteosat-12": "meteosat",
  meteosat12: "meteosat",
  mtg: "meteosat",
  "mtg-i1": "meteosat",
  fci: "meteosat",
  // The geostationary mosaic, a view over the imagers above.
  geo: "geo",
  mosaic: "geo",
  geostationary: "geo",
};

/** Model requested by the page URL, or the default when the URL names none
 * or names one this app does not serve. */
export function parseModelFromSearch(search: string): ForecastModelId {
  const model = new URLSearchParams(search).get("model");
  if (model === null) return DEFAULT_MODEL;
  return MODEL_ALIASES[model.toLowerCase()] ?? DEFAULT_MODEL;
}

/** Showcase case requested by the page URL, or null for the live feed. A
 * case pins its own model and run, so `model` is ignored while one is open. */
export function parseCaseFromSearch(search: string): string | null {
  const id = new URLSearchParams(search).get("case");
  if (id === null || !/^[a-z0-9-]+$/.test(id)) return null;
  return id;
}

/** Whether this session may use the H.264 companion artifacts. The video
 * path is opt-in: `?use_h264=true` (or `1`) turns it on, and anything else —
 * including no param at all — keeps every variable on the Xue decoder. */
export function parseUseH264FromSearch(search: string): boolean {
  const value = new URLSearchParams(search).get("use_h264");
  if (value === null) return false;
  const normalized = value.trim().toLowerCase();
  return normalized === "true" || normalized === "1";
}

/** The experimental synoptic composite (`?x=`): whether it is on at all
 * — the stepped precipitation key on chart paper, the sea level pressure
 * lines, the particles run by the 850 hPa vapour flux — and which of the
 * two fields the frontend computes from several bundles (composite.ts)
 * are drawn. Nothing is remembered between sessions. */
export interface ExperimentState {
  enabled: boolean;
  inflow: boolean;
  front: boolean;
}

export const EXPERIMENT_OFF: ExperimentState = { enabled: false, inflow: false, front: false };

/** `?x=true` (or any on-spelling) draws both derived fields; `?x=inflow`,
 * `?x=front` or `?x=inflow,front` names the ones to draw; `?x=none` is the
 * experiment with neither. Anything else — no param included — is off. */
export function parseExperimentFromSearch(search: string): ExperimentState {
  const value = new URLSearchParams(search).get("x");
  if (value === null) return EXPERIMENT_OFF;
  const normalized = value.trim().toLowerCase();
  const on = SWITCH_ALIASES[normalized];
  if (on === true) return { enabled: true, inflow: true, front: true };
  if (on === false) return EXPERIMENT_OFF;
  if (normalized === "none") return { enabled: true, inflow: false, front: false };
  const parts = normalized.split(",").map((part) => part.trim());
  const inflow = parts.includes("inflow");
  const front = parts.includes("front");
  return inflow || front ? { enabled: true, inflow, front } : EXPERIMENT_OFF;
}

/** The given query string carrying the experiment: `x=true` with both
 * fields, the named field alone, `x=none` with neither, and nothing at all
 * when the experiment is off. */
export function searchWithExperiment(search: string, state: ExperimentState): string {
  const params = new URLSearchParams(search);
  if (!state.enabled) params.delete("x");
  else if (state.inflow && state.front) params.set("x", "true");
  else if (state.inflow) params.set("x", "inflow");
  else if (state.front) params.set("x", "front");
  else params.set("x", "none");
  return `?${params.toString()}`;
}

/** Accepted spellings of an on/off switch in the query string, so a
 * hand-written link works however the viewer spells it. */
const SWITCH_ALIASES: Record<string, boolean> = {
  on: true,
  "1": true,
  true: true,
  yes: true,
  off: false,
  "0": false,
  false: false,
  no: false,
};

/** Whether this session draws the wind particle overlay over the speed field.
 *
 * Three-valued on purpose: `null` means the URL said nothing this app
 * understands — no param, or a spelling that is not one — and the caller then
 * falls back to the viewer's stored choice and finally to the default (on,
 * or off where the system asks for reduced motion). An explicit
 * `?particles=off` in a shared link is what outranks both. */
export function parseParticlesFromSearch(search: string): boolean | null {
  const value = new URLSearchParams(search).get("particles");
  if (value === null) return null;
  return SWITCH_ALIASES[value.trim().toLowerCase()] ?? null;
}

/** The given query string carrying the particle choice. Only the state the
 * viewer changed is written: a link shared with the overlay off says
 * `particles=off`, and one shared with it on carries nothing, so the
 * everyday URL stays exactly as short as it was. */
export function searchWithParticles(search: string, particles: boolean): string {
  const params = new URLSearchParams(search);
  if (particles) params.delete("particles");
  else params.set("particles", "off");
  return `?${params.toString()}`;
}

/** Accepted spellings for a pinned resolution tier. Matching is
 * case-insensitive. */
const RESOLUTION_ALIASES: Record<string, ResolutionPreference> = {
  auto: "auto",
  half: "half",
  low: "half",
  full: "full",
  high: "full",
};

/** Resolution tier this session asks for: `?res=half` pins the smallest
 * reduced rendition the run ships (the "Xue ½" tier, or the "⅛" of a
 * satellite source) however wide the view is, `?res=full` pins the
 * canonical bundle even on a metered connection, and anything else — an
 * unknown value included — leaves the choice to viewport and network.
 *
 * The tier is chosen once per variable session, so this is read at load like
 * the rest of the URL state; changing it means a reload. */
export function parseResolutionFromSearch(search: string): ResolutionPreference {
  const value = new URLSearchParams(search).get("res");
  if (value === null) return "auto";
  return RESOLUTION_ALIASES[value.trim().toLowerCase()] ?? "auto";
}

/** Where a session reads its bundles from: the Zarr v3 store a run
 * publishes (docs/zarr-profile.md), or the `.xue` container beside it. */
export type DataBackend = "xue" | "zarr";

/** The store is the everyday path; the container is what a run published
 * before the store existed, and what `?backend=xue` asks for on one that
 * ships both. */
export const DEFAULT_BACKEND: DataBackend = "zarr";

/** `?backend=xue` asks for the container — taken for a bundle whose
 * manifest entry carries one, the store otherwise — and anything else, an
 * unknown value included, is the store. A comparison setting rather than
 * shareable state: read once at load like `?res=`. */
export function parseBackendFromSearch(search: string): DataBackend {
  const value = new URLSearchParams(search).get("backend");
  if (value === null) return DEFAULT_BACKEND;
  return value.trim().toLowerCase() === "xue" ? "xue" : DEFAULT_BACKEND;
}

/** Variable requested by the page URL, or null when the URL names none (or
 * spells one so badly it is not a bundle name at all — a bad link falls back
 * to the default rather than erroring). A well-formed name the alias tables
 * do not know is passed through: whether the run ships it is the manifest's
 * answer, not the URL parser's. */
export function parseVariableFromSearch(search: string): ForecastBundleId | null {
  const params = new URLSearchParams(search);
  const model = params.get("model");
  if (model !== null && !(model.toLowerCase() in MODEL_ALIASES)) return null;
  const type = params.get("type");
  if (type === null) return null;
  return resolveType(type);
}

/** The contour lines drawn over a filled field, from `?lines=`. Any spelling
 * `?type=` accepts for a pressure surface works here too (`lines=pressure`,
 * `lines=hgt500`); a value naming a filled field, or nothing this app knows,
 * reads as no overlay rather than an error. The lines slot is only ever a
 * pressure surface: a filled field cannot be drawn as lines, and two filled
 * fields over each other has no reading. */
export function parseLinesFromSearch(search: string): PressureBundleId | null {
  const value = new URLSearchParams(search).get("lines");
  if (value === null) return null;
  const id = resolveType(value);
  return id !== null && isPressureBundle(id) ? id : null;
}

/** The given query string carrying the lines overlay, or none. Written only
 * when a filled field has lines over it: a view of the lines alone names
 * them in `type`, and a second copy in `lines` would be one parameter
 * contradicting the other the next time either changed. */
export function searchWithLines(search: string, lines: PressureBundleId | null): string {
  const params = new URLSearchParams(search);
  if (lines === null) params.delete("lines");
  else params.set("lines", canonicalType(lines));
  return `?${params.toString()}`;
}

/** Query string advertising the given model and variable, preserving any
 * unrelated params already in `search`. Returned with the leading "?". */
export function searchForVariable(
  variableId: ForecastBundleId,
  search: string,
  modelId: ForecastModelId = DEFAULT_MODEL,
): string {
  const params = new URLSearchParams(search);
  params.set("model", FORECAST_MODELS[modelId].id);
  params.set("type", canonicalType(variableId));
  return `?${params.toString()}`;
}

/** Query string advertising a showcase case and variable. The case names its
 * own dataset, so any `model` param is dropped rather than left to contradict
 * it. Returned with the leading "?". */
export function searchForCaseVariable(variableId: ForecastBundleId, search: string, caseId: string): string {
  const params = new URLSearchParams(search);
  params.delete("model");
  params.set("case", caseId);
  params.set("type", canonicalType(variableId));
  return `?${params.toString()}`;
}

/** Where the map is looking, as a shared link carries it. */
export interface MapCamera {
  /** [longitude, latitude] of the view's center, in degrees. */
  center: [number, number];
  zoom: number;
}

/** The camera a link carries in its fragment: `#map=<zoom>/<lat>/<lon>`,
 * MapLibre's own named-hash grammar (bearing and pitch may follow; they are
 * not read here). The map itself reads and writes the fragment — this
 * parser exists so the shell knows *whether* the link fixed the view: a
 * camera the sharer chose outranks the framing a dataset would otherwise
 * be opened on. The query string is untouched, so the camera never reaches
 * the canonical URL and a pan never changes which page this is. A fragment
 * that spells no camera, or one off the globe, reads as none. */
export function parseCameraFromHash(hash: string): MapCamera | null {
  const value = new URLSearchParams(hash.replace(/^#/, "")).get("map");
  if (value === null) return null;
  const parts = value.split("/");
  if (parts.length < 3) return null;
  const [zoom, latitude, longitude] = parts.map(Number) as [number, number, number];
  if (!Number.isFinite(zoom) || !Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  if (zoom < 0 || Math.abs(latitude) > 90) return null;
  return { center: [longitude, latitude], zoom };
}

/** The tropical cyclone marks a link carries. `?tc=<storm id>` focuses one
 * system (an ATCF id or the product's synthetic `x-…` id, followed through
 * the index's crosswalk when the storm has since been numbered);
 * `?tc=off` draws none; no parameter, or one naming nothing this parser
 * recognises, is the default view — every named system, unfocused.
 * `?tcagency=nhc,jtwc` and `?tcmodel=gfs,ecmwf` narrow which forecasts are
 * drawn (absent means all of them); `?tcmembers=on` adds the ensemble
 * members, which are off by default. */
export interface TcUrlState {
  storm: string | null;
  off: boolean;
  agencies: readonly string[] | null;
  models: readonly string[] | null;
  members: boolean;
}

const TC_ID = /^(?:[A-Z]{2}\d{6}|x-[a-z]{2}-\d{10}-\d+)$/;
const TC_KEY = /^[a-z][a-z0-9]*$/;

function keyList(value: string | null): readonly string[] | null {
  if (value === null) return null;
  const keys = value
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter((item) => TC_KEY.test(item));
  return keys.length ? [...new Set(keys)] : null;
}

export function parseTcFromSearch(search: string): TcUrlState {
  const params = new URLSearchParams(search);
  const raw = params.get("tc");
  let storm: string | null = null;
  let off = false;
  if (raw !== null) {
    const trimmed = raw.trim();
    // An ATCF id is written upper-case, a synthetic one lower-case; a link
    // typed either way still opens.
    const id = trimmed.toLowerCase().startsWith("x-") ? trimmed.toLowerCase() : trimmed.toUpperCase();
    if (SWITCH_ALIASES[trimmed.toLowerCase()] === false) off = true;
    else if (TC_ID.test(id)) storm = id;
  }
  return {
    storm,
    off,
    agencies: keyList(params.get("tcagency")),
    models: keyList(params.get("tcmodel")),
    members: SWITCH_ALIASES[(params.get("tcmembers") ?? "").trim().toLowerCase()] === true,
  };
}

/** The given query string carrying the marks state. Only what differs
 * from the default is written, so the everyday link stays as it was. */
/** The station marks a link carries: `?stations=snd` for the radiosonde
 * soundings, `?stations=apt` for the airports, `?stations=snd,apt` for
 * both, and `?stations=off` — or no parameter at all — for neither.
 * `sounding`, `soundings`, `airport`, `airports`, `on` and `all` are
 * accepted spellings, so a link typed out by hand still opens.
 *
 * Off is the default because on is not a neutral choice: five thousand
 * airport marks on the default view are a texture over the field, not a
 * reading of it. A viewer who wants them presses the tile, and the press
 * is what the link then carries. */
export interface StationsUrlState {
  soundings: boolean;
  airports: boolean;
}

export const STATIONS_OFF: StationsUrlState = { soundings: false, airports: false };

const STATION_ALIASES: Record<string, keyof StationsUrlState> = {
  snd: "soundings",
  sonde: "soundings",
  sounding: "soundings",
  soundings: "soundings",
  apt: "airports",
  metar: "airports",
  airport: "airports",
  airports: "airports",
};

export function parseStationsFromSearch(search: string): StationsUrlState {
  const value = new URLSearchParams(search).get("stations");
  if (value === null) return { ...STATIONS_OFF };
  const state = { ...STATIONS_OFF };
  for (const part of value.split(",")) {
    const name = part.trim().toLowerCase();
    if (name === "on" || name === "all") {
      state.soundings = true;
      state.airports = true;
      continue;
    }
    const key = STATION_ALIASES[name];
    if (key) state[key] = true;
  }
  return state;
}

/** The given query string carrying the station marks. Nothing is written
 * for the default (neither product), so an ordinary shared link stays as
 * short as it was. */
export function searchWithStations(search: string, state: StationsUrlState): string {
  const params = new URLSearchParams(search);
  const parts: string[] = [];
  if (state.soundings) parts.push("snd");
  if (state.airports) parts.push("apt");
  if (parts.length) params.set("stations", parts.join(","));
  else params.delete("stations");
  return `?${params.toString()}`;
}

export function searchWithTc(search: string, state: TcUrlState): string {
  const params = new URLSearchParams(search);
  if (state.off) params.set("tc", "off");
  else if (state.storm !== null) params.set("tc", state.storm);
  else params.delete("tc");
  if (state.agencies !== null && !state.off) params.set("tcagency", state.agencies.join(","));
  else params.delete("tcagency");
  if (state.models !== null && !state.off) params.set("tcmodel", state.models.join(","));
  else params.delete("tcmodel");
  if (state.members && !state.off) params.set("tcmembers", "on");
  else params.delete("tcmembers");
  return `?${params.toString()}`;
}
