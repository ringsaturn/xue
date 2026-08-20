import { FORECAST_MODELS, type ForecastBundleId, type ForecastModelId } from "./manifest";

/** Default model when the URL names none (or names one this app does not
 * serve — a bad link falls back rather than erroring). */
export const DEFAULT_MODEL: ForecastModelId = "gfs";

/** Canonical `type` value written into shared URLs, per bundle. */
const CANONICAL_TYPE: Record<ForecastBundleId, string> = {
  tmp2m: "temp",
  prate: "precip",
  dswrf: "solar",
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
