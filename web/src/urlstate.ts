import {
  FORECAST_MODELS,
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

/** Canonical `type` value written into shared URLs, per bundle. */
const CANONICAL_TYPE: Record<ForecastBundleId, string> = {
  tmp2m: "temp",
  prate: "precip",
  dswrf: "solar",
  cref: "radar",
  // The pressure family names itself: the level *is* the layer, so there is
  // no separate `?level=` parameter to keep in step with `?type=`.
  prmsl: "pressure",
  hgt1000: "hgt1000",
  hgt925: "hgt925",
  hgt850: "hgt850",
  hgt700: "hgt700",
  hgt500: "hgt500",
  hgt300: "hgt300",
  hgt250: "hgt250",
  hgt200: "hgt200",
  wind10m: "wind",
};

/** Accepted spellings for each bundle — canonical name, bundle id, and a few
 * common aliases. Matching is case-insensitive. */
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
  hgt1000: "hgt1000",
  hgt925: "hgt925",
  hgt850: "hgt850",
  hgt700: "hgt700",
  // The subtropical high is read off the 500 hPa chart, so the view has the
  // name people look for as well as the level's own.
  hgt500: "hgt500",
  subtropicalhigh: "hgt500",
  hgt300: "hgt300",
  hgt250: "hgt250",
  hgt200: "hgt200",
};

/** Accepted spellings for each model. Matching is case-insensitive. */
const MODEL_ALIASES: Record<string, ForecastModelId> = {
  gfs: "gfs",
  ecmwf: "ecmwf",
  ifs: "ecmwf",
  sflux: "sflux",
  "gfs-sflux": "sflux",
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
 * names a type this app does not serve — a bad link falls back to the
 * default rather than erroring). */
export function parseVariableFromSearch(search: string): ForecastBundleId | null {
  const params = new URLSearchParams(search);
  const model = params.get("model");
  if (model !== null && !(model.toLowerCase() in MODEL_ALIASES)) return null;
  const type = params.get("type");
  if (type === null) return null;
  return TYPE_ALIASES[type.toLowerCase()] ?? null;
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
  const id = TYPE_ALIASES[value.trim().toLowerCase()];
  return id !== undefined && isPressureBundle(id) ? id : null;
}

/** The given query string carrying the lines overlay, or none. Written only
 * when a filled field has lines over it: a view of the lines alone names
 * them in `type`, and a second copy in `lines` would be one parameter
 * contradicting the other the next time either changed. */
export function searchWithLines(search: string, lines: PressureBundleId | null): string {
  const params = new URLSearchParams(search);
  if (lines === null) params.delete("lines");
  else params.set("lines", CANONICAL_TYPE[lines]);
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
  params.set("type", CANONICAL_TYPE[variableId]);
  return `?${params.toString()}`;
}

/** Query string advertising a showcase case and variable. The case names its
 * own dataset, so any `model` param is dropped rather than left to contradict
 * it. Returned with the leading "?". */
export function searchForCaseVariable(variableId: ForecastBundleId, search: string, caseId: string): string {
  const params = new URLSearchParams(search);
  params.delete("model");
  params.set("case", caseId);
  params.set("type", CANONICAL_TYPE[variableId]);
  return `?${params.toString()}`;
}
