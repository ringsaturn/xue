import {
  FORECAST_MODELS,
  isBundleVariableId,
  type ForecastBundleId,
  type ForecastModelId,
  type ResolutionPreference,
} from "./manifest";
import { isPressureBundle, type PressureBundleId } from "./pressure";

/** Default model when the URL names none (or names one this app does not
 * serve — a bad link falls back rather than erroring). */
export const DEFAULT_MODEL: ForecastModelId = "gfs";

/** Layer shown when the URL names none, and the one the app falls back to
 * when a dataset does not ship the requested layer. Every live run carries
 * the core tmp2m/prate pair, so this is always available there. */
export const DEFAULT_VARIABLE: ForecastBundleId = "prate";

/** Canonical `type` value for the surface members, which have short names of
 * their own. Everything else names itself — an isobaric field's level *is*
 * the layer, so there is no separate `?level=` parameter to keep in step
 * with `?type=`, and a bundle this build has never heard of is written out
 * under its own id. */
const CANONICAL_TYPE: Record<string, string> = {
  tmp2m: "temp",
  prate: "precip",
  dswrf: "solar",
  cref: "radar",
  prmsl: "pressure",
  wind10m: "wind",
  gust: "gust",
  tcdc: "cloud",
  lcdc: "lowcloud",
  mcdc: "midcloud",
  hcdc: "highcloud",
  cape: "cape",
  vis: "visibility",
  dpt2m: "dewpoint",
  aptmp2m: "feelslike",
  tmpsfc: "sst",
  icec: "seaice",
  icetk: "icethickness",
  wave: "waves",
  htsgw: "waveheight",
  perpw: "waveperiod",
  dirpw: "wavedirection",
};

function canonicalType(id: ForecastBundleId): string {
  return CANONICAL_TYPE[id] ?? id;
}

/** Accepted spellings for the named layers — canonical name, bundle id, and
 * a few common aliases. Matching is case-insensitive. */
const TYPE_ALIASES: Record<string, ForecastBundleId> = {
  temp: "tmp2m",
  temperature: "tmp2m",
  tmp: "tmp2m",
  tmp2m: "tmp2m",
  t2m: "tmp2m",
  precip: "prate",
  precipitation: "prate",
  rain: "prate",
  prate: "prate",
  wind: "wind10m",
  wind10m: "wind10m",
  solar: "dswrf",
  radiation: "dswrf",
  dswrf: "dswrf",
  radar: "cref",
  reflectivity: "cref",
  cref: "cref",
  pressure: "prmsl",
  mslp: "prmsl",
  msl: "prmsl",
  prmsl: "prmsl",
  gust: "gust",
  gusts: "gust",
  cloud: "tcdc",
  clouds: "tcdc",
  cloudcover: "tcdc",
  tcdc: "tcdc",
  cape: "cape",
  instability: "cape",
  lowcloud: "lcdc",
  lcdc: "lcdc",
  midcloud: "mcdc",
  middlecloud: "mcdc",
  mcdc: "mcdc",
  highcloud: "hcdc",
  hcdc: "hcdc",
  visibility: "vis",
  vis: "vis",
  fog: "vis",
  dewpoint: "dpt2m",
  dew: "dpt2m",
  td: "dpt2m",
  dpt2m: "dpt2m",
  feelslike: "aptmp2m",
  apparent: "aptmp2m",
  aptmp: "aptmp2m",
  aptmp2m: "aptmp2m",
  // The ocean set. `sst` is what the skin temperature is searched for,
  // though over land it is the ground's skin.
  sst: "tmpsfc",
  skin: "tmpsfc",
  skintemp: "tmpsfc",
  tsfc: "tmpsfc",
  tmpsfc: "tmpsfc",
  seaice: "icec",
  ice: "icec",
  icecover: "icec",
  iceconcentration: "icec",
  icec: "icec",
  icethickness: "icetk",
  icetk: "icetk",
  waves: "wave",
  wave: "wave",
  waveheight: "htsgw",
  swh: "htsgw",
  hs: "htsgw",
  htsgw: "htsgw",
  waveperiod: "perpw",
  period: "perpw",
  perpw: "perpw",
  wavedirection: "dirpw",
  wavedir: "dirpw",
  dirpw: "dirpw",
  // The subtropical high is read off the 500 hPa chart, so the view has the
  // name people look for as well as the level's own.
  subtropicalhigh: "hgt500",
};

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
  return TYPE_ALIASES[lower] ?? isobaricType(lower) ?? (isBundleVariableId(lower) ? lower : null);
}

/** Accepted spellings for each model. Matching is case-insensitive. */
const MODEL_ALIASES: Record<string, ForecastModelId> = {
  gfs: "gfs",
  ecmwf: "ecmwf",
  ifs: "ecmwf",
  sflux: "sflux",
  "gfs-sflux": "sflux",
  hrrr: "hrrr",
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

/** Resolution tier this session asks for: `?res=half` pins the reduced
 * rendition (the "Xue ½" tier) however wide the view is, `?res=full` pins the
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
