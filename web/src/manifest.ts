import { t } from "./i18n";

export type ForecastVariableId = "tmp2m" | "prate";

/** Forecast models this app can tune to. Each model is its own dataset: its
 * own mutable live pointer at the data root, its own immutable run
 * directories, and its own manifest identity. GFS uses the bare
 * ``latest.json``; the other models use ``latest-<model>.json``. */
export type ForecastModelId = "gfs" | "ecmwf" | "sflux";

export interface ForecastModelInfo {
  id: ForecastModelId;
  /** Display label and the manifest/pointer ``model`` string. */
  label: string;
  /** The manifest/pointer ``product`` string. */
  product: string;
  /** Mutable live pointer filename at the data root. */
  latestFilename: string;
}

export const FORECAST_MODELS: Record<ForecastModelId, ForecastModelInfo> = {
  gfs: { id: "gfs", label: "GFS", product: "pgrb2.0p25", latestFilename: "latest.json" },
  // GFS surface flux on the native ~13 km grid; the
  // only source that ships the dswrf solar-radiation bundle.
  sflux: { id: "sflux", label: "GFS-SFLUX", product: "sfluxgrb", latestFilename: "latest-sflux.json" },
  ecmwf: { id: "ecmwf", label: "ECMWF", product: "ifs-0p25", latestFilename: "latest-ecmwf.json" },
};

export const FORECAST_MODEL_IDS: readonly ForecastModelId[] = ["gfs", "sflux", "ecmwf"];

function modelForManifestString(model: unknown): ForecastModelInfo | null {
  for (const info of Object.values(FORECAST_MODELS)) {
    if (info.label === model) return info;
  }
  return null;
}

/** Bundle-level ids the manifest can carry. The scalar variables are
 * mandatory; the two-variable wind bundle and the
 * dswrf solar-radiation bundle (sflux only) are
 * optional so pre-existing runs keep validating. */
export type ForecastBundleId = ForecastVariableId | "dswrf" | "wind10m";

/** Data-level variable ids that can appear inside bundle metadata; the wind
 * bundle carries both 10 m components on one time axis. */
export type DataVariableId = ForecastVariableId | "dswrf" | "ugrd10m" | "vgrd10m";

export const FORECAST_VARIABLE_IDS: readonly ForecastVariableId[] = ["tmp2m", "prate"];
export const FORECAST_BUNDLE_IDS: readonly ForecastBundleId[] = ["tmp2m", "prate", "dswrf", "wind10m"];
export const WIND_COMPONENT_IDS: readonly DataVariableId[] = ["ugrd10m", "vgrd10m"];

export interface VideoBundleDescriptor {
  streamPath: string;
  indexPath: string;
  byteLength: number;
  crc32: string;
  codec: string;
  width: number;
  height: number;
  gop: number;
  frameCount: number;
  /** Same shape as the metadata embedded in the .xue file, scoped to this
   * variable — lets the video path skip fetching the .xue just for grid,
   * time axis, and quantization info. Parse with parseBundleMetadata(). */
  metadataJson: string;
}

export interface PosterDescriptor {
  path: string;
  width: number;
  height: number;
  byteLength: number;
  crc32: string;
  /** Bundle-shaped metadata scoped to this variable, with the POSTER grid
   * (half resolution) — enough to configure the WebGL layer and palette
   * before any bundle byte arrives. Parse with parseBundleMetadata(). */
  metadataJson: string;
}

/** One reduced-resolution rendition of a variable's bundle (HLS
 * STREAM-INF semantics). The bundle's own top-level path stays the
 * canonical full-resolution tier; variants are alternates the client may pick
 * by viewport need and network quality. */
export interface VariantDescriptor {
  path: string;
  width: number;
  height: number;
  byteLength: number;
  crc32: string;
  /** Average bits per second needed to sustain 12 fps playback while
   * downloading the whole tier — the STREAM-INF BANDWIDTH analogue. */
  bandwidth: number;
}

export interface VariableBundleDescriptor {
  variable: ForecastBundleId;
  path: string;
  byteLength: number;
  crc32: string;
  /** Reduced-resolution renditions. */
  variants?: VariantDescriptor[];
  /** Alternate WebCodecs-decodable artifact, present per variable when the
   * build had ffmpeg available. */
  video?: VideoBundleDescriptor;
  /** Tiny first-frame artifact for instant paint on variable switch. */
  poster?: PosterDescriptor;
}

export interface ForecastManifest {
  schemaVersion: 5;
  model: string;
  product: string;
  runTime: string;
  /** Last forecast hour of the run (120-hour and 240-hour runs both exist). */
  forecastHours: number;
  bundles: VariableBundleDescriptor[];
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("manifest node must be an object");
  }
  return value as Record<string, unknown>;
}

function timestamp(value: unknown, label: string): number {
  if (typeof value !== "string") throw new Error(`${label} timestamp missing`);
  const result = Date.parse(value);
  if (!Number.isFinite(result)) throw new Error(`${label} timestamp invalid`);
  return result;
}

const BUNDLE_SUFFIX = ".xue";

function relativePath(value: unknown, suffix: string | string[], paths: Set<string>, label: string): string {
  const suffixes = Array.isArray(suffix) ? suffix : [suffix];
  if (
    typeof value !== "string" ||
    !suffixes.some((candidate) => value.endsWith(candidate)) ||
    value.startsWith("/") ||
    value.startsWith("http:") ||
    value.startsWith("https:") ||
    value.split("/").includes("..")
  ) {
    throw new Error(`invalid ${label} path`);
  }
  if (paths.has(value)) throw new Error("duplicate bundle path");
  paths.add(value);
  return value;
}

function metadataJsonField(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`invalid ${label} metadata`);
  try {
    JSON.parse(value);
  } catch {
    throw new Error(`${label} metadata is not valid JSON`);
  }
  return value;
}

function validateVariantDescriptor(input: unknown, paths: Set<string>): VariantDescriptor {
  const variant = object(input);
  relativePath(variant.path, BUNDLE_SUFFIX, paths, "variant");
  for (const key of ["width", "height", "byteLength", "bandwidth"] as const) {
    if (typeof variant[key] !== "number" || !Number.isInteger(variant[key]) || (variant[key] as number) <= 0) {
      throw new Error(`invalid variant ${key}`);
    }
  }
  if (typeof variant.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(variant.crc32)) {
    throw new Error("invalid variant crc32");
  }
  return variant as unknown as VariantDescriptor;
}

function validatePosterDescriptor(input: unknown, paths: Set<string>): PosterDescriptor {
  const poster = object(input);
  relativePath(poster.path, ".poster.bin", paths, "poster");
  for (const key of ["width", "height", "byteLength"] as const) {
    if (typeof poster[key] !== "number" || !Number.isInteger(poster[key]) || (poster[key] as number) <= 0) {
      throw new Error(`invalid poster ${key}`);
    }
  }
  if (typeof poster.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(poster.crc32)) {
    throw new Error("invalid poster crc32");
  }
  metadataJsonField(poster.metadataJson, "poster");
  return poster as unknown as PosterDescriptor;
}

function validateVideoDescriptor(input: unknown, paths: Set<string>): VideoBundleDescriptor {
  const video = object(input);
  relativePath(video.streamPath, ".h264", paths, "video stream");
  relativePath(video.indexPath, ".h264.index.json", paths, "video index");
  if (typeof video.byteLength !== "number" || !Number.isInteger(video.byteLength) || video.byteLength <= 0) {
    throw new Error("invalid video byteLength");
  }
  if (typeof video.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(video.crc32)) {
    throw new Error("invalid video crc32");
  }
  if (typeof video.codec !== "string" || video.codec.length === 0) throw new Error("invalid video codec string");
  for (const key of ["width", "height", "gop", "frameCount"] as const) {
    if (typeof video[key] !== "number" || !Number.isInteger(video[key]) || (video[key] as number) <= 0) {
      throw new Error(`invalid video ${key}`);
    }
  }
  metadataJsonField(video.metadataJson, "video");
  return video as unknown as VideoBundleDescriptor;
}

export function validateManifest(input: unknown, expectedModel?: ForecastModelId): ForecastManifest {
  const value = object(input);
  if (value.schemaVersion !== 5) throw new Error("unsupported manifest schema version");
  const modelInfo = modelForManifestString(value.model);
  if (!modelInfo || value.product !== modelInfo.product) throw new Error("unsupported manifest product");
  if (expectedModel !== undefined && modelInfo.id !== expectedModel) throw new Error("manifest model does not match the request");
  if (
    typeof value.forecastHours !== "number" ||
    !Number.isInteger(value.forecastHours) ||
    value.forecastHours <= 0 ||
    value.forecastHours > 384
  ) {
    throw new Error("invalid manifest forecast range");
  }
  timestamp(value.runTime, "runTime");

  if (!Array.isArray(value.bundles) || value.bundles.length === 0) throw new Error("manifest has no bundle list");
  const variables: string[] = [];
  const paths = new Set<string>();
  for (const item of value.bundles) {
    const bundle = object(item);
    if (typeof bundle.variable !== "string" || !FORECAST_BUNDLE_IDS.includes(bundle.variable as ForecastBundleId)) {
      throw new Error("unsupported bundle variable");
    }
    if (
      typeof bundle.path !== "string" ||
      !bundle.path.endsWith(BUNDLE_SUFFIX) ||
      bundle.path.startsWith("/") ||
      bundle.path.startsWith("http:") ||
      bundle.path.startsWith("https:") ||
      bundle.path.split("/").includes("..")
    ) {
      throw new Error("invalid bundle path");
    }
    if (paths.has(bundle.path)) throw new Error("duplicate bundle path");
    paths.add(bundle.path);
    if (typeof bundle.byteLength !== "number" || !Number.isInteger(bundle.byteLength) || bundle.byteLength <= 0) {
      throw new Error("invalid bundle byteLength");
    }
    if (typeof bundle.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(bundle.crc32)) {
      throw new Error("invalid bundle crc32");
    }
    if (bundle.variants !== undefined) {
      if (!Array.isArray(bundle.variants) || bundle.variants.length === 0) {
        throw new Error("invalid bundle variant list");
      }
      for (const variant of bundle.variants) validateVariantDescriptor(variant, paths);
    }
    if (bundle.video !== undefined) {
      validateVideoDescriptor(bundle.video, paths);
    }
    if (bundle.poster !== undefined) {
      validatePosterDescriptor(bundle.poster, paths);
    }
    variables.push(bundle.variable);
  }
  for (const id of FORECAST_VARIABLE_IDS) {
    if (!variables.includes(id)) throw new Error(`manifest has no bundle for variable ${id}`);
  }
  if (new Set(variables).size !== variables.length) throw new Error("manifest contains duplicate variable bundles");
  return value as unknown as ForecastManifest;
}

/** True when the manifest ships the given optional bundle (wind10m, dswrf). */
export function hasBundle(manifest: ForecastManifest, id: ForecastBundleId): boolean {
  return manifest.bundles.some((bundle) => bundle.variable === id);
}

/** True when the manifest ships the optional two-variable wind bundle. */
export function hasWindBundle(manifest: ForecastManifest): boolean {
  return hasBundle(manifest, "wind10m");
}

/** Tier selection (pure so it can be unit-tested): pick the reduced
 * rendition to load instead of the canonical full-resolution bundle, or null
 * to stay on full resolution.
 *
 * - On a constrained network the smallest tier always wins — the ladder
 *   exists exactly so those clients stop paying for pixels they cannot see.
 * - Otherwise pick the smallest tier that still covers `neededGridWidth`,
 *   the horizontal grid samples the current view can actually display
 *   (world CSS width x devicePixelRatio); if no tier suffices, use full. */
export function pickBundleVariant(
  variants: VariantDescriptor[] | undefined,
  neededGridWidth: number,
  constrained: boolean,
): VariantDescriptor | null {
  if (!variants || variants.length === 0) return null;
  const sorted = [...variants].sort((a, b) => a.width - b.width);
  if (constrained) return sorted[0] ?? null;
  return sorted.find((variant) => variant.width >= neededGridWidth) ?? null;
}

// ---------------------------------------------------------------------------
// Live pointer (latest.json). Two-layer delivery: the
// only mutable object is this tiny pointer; the run manifest and every heavy
// artifact it names are immutable and cache-busted via ?v=<crc32>.

export interface LatestPointer {
  schemaVersion: 1;
  model: string;
  run: string;
  runTime: string;
  manifestPath: string;
  manifestCrc32: string;
}

export function validateLatestPointer(input: unknown, expectedModel?: ForecastModelId): LatestPointer {
  const value = object(input);
  if (value.schemaVersion !== 1) throw new Error("unsupported live pointer schema version");
  const modelInfo = modelForManifestString(value.model);
  if (!modelInfo || value.product !== modelInfo.product) throw new Error("unsupported live pointer product");
  if (expectedModel !== undefined && modelInfo.id !== expectedModel) throw new Error("live pointer model does not match the request");
  if (typeof value.run !== "string" || !/^\d{10}$/.test(value.run)) throw new Error("invalid live pointer run id");
  timestamp(value.runTime, "live pointer");
  relativePath(value.manifestPath, "manifest.json", new Set(), "manifest");
  if (typeof value.manifestCrc32 !== "string" || !/^[0-9a-f]{8}$/.test(value.manifestCrc32)) {
    throw new Error("invalid live pointer manifest crc32");
  }
  return value as unknown as LatestPointer;
}

export async function fetchLatestPointer(baseUrl: string, model: ForecastModelId = "gfs"): Promise<LatestPointer> {
  const url = new URL(`${baseUrl}${FORECAST_MODELS[model].latestFilename}`, document.baseURI);
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(t("pointerRequestFailed", { status: response.status }));
  return validateLatestPointer(await response.json(), model);
}

export interface LoadedManifest {
  manifest: ForecastManifest;
  latest: LatestPointer;
  /** Absolute manifest URL (with its ?v=); artifact paths resolve against it. */
  manifestUrl: string;
}

export async function fetchManifest(baseUrl: string, model: ForecastModelId = "gfs"): Promise<LoadedManifest> {
  const latest = await fetchLatestPointer(baseUrl, model);
  const url = new URL(latest.manifestPath, new URL(baseUrl, document.baseURI));
  url.searchParams.set("v", latest.manifestCrc32);
  const response = await fetch(url);
  if (!response.ok) throw new Error(t("manifestRequestFailed", { status: response.status }));
  return { manifest: validateManifest(await response.json(), model), latest, manifestUrl: url.href };
}

// ---------------------------------------------------------------------------
// Bundle metadata (embedded UTF-8 JSON inside the .xue file).

export interface LinearQuantization {
  type: "linear";
  offset: number;
  scale: number;
  minimumCode: number;
  maximumCode: number;
  nodataCode: number;
}

export interface LogQuantization {
  type: "log1p";
  trace: number;
  scale: number;
  maximum: number;
  minimumCode: number;
  maximumCode: number;
  zeroCode: number;
  overflowCode: number;
  nodataCode: number;
}

export interface BundleVariable {
  numericId: number;
  id: DataVariableId;
  label: string;
  unit: string;
  quantization: LinearQuantization | LogQuantization;
}

/** The bundle time axis. Exactly one of `stepHours` (uniform, metadata
 * schemaVersion 1) and `hours` (mixed-step, listed outright, schemaVersion 2)
 * is present — no source publishes one cadence all the way to 240 hours, so
 * a 240-hour run always carries `hours` (docs/format.md). */
export interface BundleTimeAxis {
  firstForecastHour: number;
  frameCount: number;
  stepHours?: number;
  hours?: number[];
}

export interface BundleMetadata {
  schemaVersion: 1 | 2;
  model: string;
  runTime: string;
  time: BundleTimeAxis;
  grid: { width: number; height: number };
  variables: BundleVariable[];
}

/** The materialized forecast-hour list of a bundle time axis. */
export function timeAxisHours(time: BundleTimeAxis): number[] {
  if (time.hours) return time.hours;
  const step = time.stepHours ?? 1;
  return Array.from({ length: time.frameCount }, (_, index) => time.firstForecastHour + index * step);
}

/** True when two time axes describe the same frame list. */
export function sameTimeAxis(a: BundleTimeAxis, b: BundleTimeAxis): boolean {
  if (a === b) return true;
  const hoursA = timeAxisHours(a);
  const hoursB = timeAxisHours(b);
  return hoursA.length === hoursB.length && hoursA.every((hour, index) => hour === hoursB[index]);
}

function validateTimeAxis(time: Record<string, unknown>, schemaVersion: number): void {
  const frameCount = time.frameCount;
  if (typeof frameCount !== "number" || !Number.isInteger(frameCount) || frameCount <= 0) {
    throw new Error("invalid bundle time axis");
  }
  const firstForecastHour = time.firstForecastHour;
  if (typeof firstForecastHour !== "number" || !Number.isInteger(firstForecastHour) || firstForecastHour < 0) {
    throw new Error("invalid bundle time axis");
  }
  const stepHours = time.stepHours;
  const hours = time.hours;
  // Exactly one encoding per axis: uniform axes declare stepHours under
  // schemaVersion 1, mixed-step axes list their hours under schemaVersion 2.
  if ((stepHours === undefined) === (hours === undefined)) throw new Error("invalid bundle time axis");
  if (stepHours !== undefined) {
    if (schemaVersion !== 1) throw new Error("a uniform time axis must declare schema version 1");
    if (typeof stepHours !== "number" || !Number.isInteger(stepHours) || stepHours <= 0) {
      throw new Error("invalid bundle time axis");
    }
    return;
  }
  if (schemaVersion !== 2) throw new Error("an explicit hours axis requires schema version 2");
  if (
    !Array.isArray(hours) ||
    hours.length !== frameCount ||
    hours[0] !== firstForecastHour ||
    hours.some(
      (hour, index) =>
        typeof hour !== "number" ||
        !Number.isInteger(hour) ||
        hour > 65534 ||
        (index > 0 && hour <= (hours[index - 1] as number)),
    )
  ) {
    throw new Error("invalid bundle time axis");
  }
  const steps = new Set<number>();
  for (let index = 1; index < hours.length; index += 1) steps.add((hours[index] as number) - (hours[index - 1] as number));
  if (steps.size < 2) throw new Error("a uniform hours axis must be encoded as stepHours");
}

export function parseBundleMetadata(json: string): BundleMetadata {
  const value = object(JSON.parse(json));
  if (value.schemaVersion !== 1 && value.schemaVersion !== 2) {
    throw new Error("unsupported bundle metadata schema version");
  }
  const time = object(value.time);
  const grid = object(value.grid);
  validateTimeAxis(time, value.schemaVersion);
  if (typeof grid.width !== "number" || typeof grid.height !== "number") {
    throw new Error("invalid bundle grid");
  }
  const variables = value.variables;
  if (!Array.isArray(variables) || variables.length === 0) throw new Error("bundle metadata has no variables");
  for (const item of variables) {
    const variable = object(item);
    if (typeof variable.numericId !== "number" || typeof variable.id !== "string") {
      throw new Error("invalid bundle variable descriptor");
    }
    const quantization = object(variable.quantization);
    if (quantization.type !== "linear" && quantization.type !== "log1p") {
      throw new Error("unsupported bundle quantization");
    }
  }
  return value as unknown as BundleMetadata;
}
