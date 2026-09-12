import { HRRR_DOMAIN, type LambertDomain } from "./domain";
import { fetchImmutable } from "./fetchimmutable";
import { t } from "./i18n";

export type ForecastVariableId = "tmp2m" | "prate";

/** Datasets this app can tune to. Each is its own dataset: its own
 * immutable run directories and its own manifest identity, and — when it has
 * a live feed — its own mutable live pointer at the data root. GFS uses the
 * bare ``latest.json``; the other live models use ``latest-<model>.json``.
 * The radar mosaic has no live feed at all: it is an observation archive
 * that reaches the app only as showcase cases. */
export type ForecastModelId = "gfs" | "ecmwf" | "sflux" | "hrrr" | "radar";

export interface ForecastModelInfo {
  id: ForecastModelId;
  /** Display label and the manifest/pointer ``model`` string. */
  label: string;
  /** The manifest/pointer ``product`` string. */
  product: string;
  /** Mutable live pointer filename at the data root, absent for a dataset
   * with no live feed. */
  latestFilename?: string;
  /** True when the dataset is observations rather than a forecast. It has no
   * run cycle and no lead time — its `runTime` is when the series starts and
   * a frame's offset is time elapsed since — so the viewer labels it as
   * such (mirrors `SourceSpec.observation` in xue/sources.py). */
  observation?: boolean;
  /** The part of the world a regional model covers, as [west, south, east,
   * north] in degrees: where the camera goes when the model is opened on a
   * view that shows none of it. A global model has none. */
  region?: readonly [number, number, number, number];
  /** The model's own grid on its map projection, for a model the encoder
   * resampled onto the regular grid the bundles carry (`domain.ts`): the
   * renderers clip to it, since the rectangle around a conic footprint
   * holds corners the model never forecast. */
  domain?: LambertDomain;
}

export const FORECAST_MODELS: Record<ForecastModelId, ForecastModelInfo> = {
  gfs: { id: "gfs", label: "GFS", product: "pgrb2.0p25", latestFilename: "latest.json" },
  // GFS surface flux on the native ~13 km grid; the
  // only source that ships the dswrf solar-radiation bundle.
  sflux: { id: "sflux", label: "GFS-SFLUX", product: "sfluxgrb", latestFilename: "latest-sflux.json" },
  ecmwf: { id: "ecmwf", label: "ECMWF", product: "ifs-0p25", latestFilename: "latest-ecmwf.json" },
  // NOAA HRRR, 3 km over the contiguous United States, a new cycle every
  // hour: a regional model, resampled by the encoder from its Lambert
  // conformal grid onto a 0.03° one over the domain's footprint.
  hrrr: {
    id: "hrrr",
    label: "HRRR",
    product: "wrfsfc",
    latestFilename: "latest-hrrr.json",
    region: [-134.1, 21.12, -60.9, 52.62],
    domain: HRRR_DOMAIN,
  },
  // CMA weather radar level-3 mosaic composite reflectivity: observations,
  // not a forecast, and published only as showcase cases.
  radar: { id: "radar", label: "CMA-RADAR", product: "l3-mst-cref", observation: true },
};

/** True when a dataset is observations, not a forecast. */
export function isObservationModel(model: ForecastModelId): boolean {
  return FORECAST_MODELS[model].observation === true;
}

/** The live feeds, in model-switch order. The radar archive is not one. */
export const FORECAST_MODEL_IDS: readonly ForecastModelId[] = ["gfs", "sflux", "ecmwf", "hrrr"];

function modelForManifestString(model: unknown): ForecastModelInfo | null {
  for (const info of Object.values(FORECAST_MODELS)) {
    if (info.label === model) return info;
  }
  return null;
}

/** The eight standard isobaric surfaces every isobaric family is registered
 * on, in hPa, in the encoders' `variableId` order (mirrors
 * `ISOBARIC_LEVELS_HPA` in xuebuild/variables.py). One bundle per level —
 * container v2 orders chunks group -> tile -> variable, so packing the levels
 * together would make a viewport pull every level to read one. */
export const ISOBARIC_LEVELS = [1000, 925, 850, 700, 500, 300, 250, 200] as const;
export type IsobaricLevel = (typeof ISOBARIC_LEVELS)[number];

/** The pressure family: mean sea level pressure and geopotential height on
 * the isobaric surfaces. What the viewer draws from them is contours; see
 * pressure.ts. */
export type PressureBundleId = "prmsl" | `hgt${IsobaricLevel}`;

/** The filled isobaric scalars: temperature, relative humidity and specific
 * humidity on the same surfaces (levels.ts). */
export type IsobaricScalarBundleId =
  | `tmp${IsobaricLevel}`
  | `rh${IsobaricLevel}`
  | `spfh${IsobaricLevel}`
  | `vvel${IsobaricLevel}`
  | `thetae${IsobaricLevel}`;

/** The two-variable bundles: a u/v pair the viewer draws as a magnitude field
 * with optional particles — the 10 m wind, the wind on each isobaric surface,
 * and the water vapour flux the encoder derives there. */
export type VectorBundleId = "wind10m" | `wind${IsobaricLevel}` | `qflux${IsobaricLevel}` | "wave";

/** A bundle-level id in a manifest.
 *
 * Deliberately a plain string: the manifest is not a registry. What a bundle
 * *is* lives in the file, as the GRIB2 parameter block on each of its
 * variables (identity.ts); what the manifest carries is a name, admitted on
 * its shape alone (`isBundleVariableId`). A run may therefore publish a
 * bundle this shell has never heard of and still validate — it renders
 * generically rather than taking the whole manifest down with it.
 *
 * The ids the shell has chart knowledge about are `KnownBundleId`, and they
 * are a naming convention (`<family><level>`, `tmp2m`, `wind10m`), not a
 * closed set. */
export type ForecastBundleId = string;

/** The bundle ids this shell draws with a palette, legend and ceiling of
 * its own. Used for typing the chart registries, never for admission. */
export type KnownBundleId =
  | ForecastVariableId
  | "dswrf"
  | "cref"
  | SurfaceDiagnosticId
  | OceanId
  | PressureBundleId
  | IsobaricScalarBundleId
  | VectorBundleId;

/** The surface diagnostics — wind gust, cloud cover (the total and the
 * three layers), surface-based CAPE, visibility, 2 m dew point and 2 m
 * apparent temperature — single layers like `dswrf`, held to the encoders
 * by `tests/fixtures/surface-registry.json`. */
export type SurfaceDiagnosticId = "gust" | "tcdc" | "cape" | "vis" | "dpt2m" | "aptmp2m" | "lcdc" | "mcdc" | "hcdc";
export const SURFACE_DIAGNOSTIC_IDS: readonly SurfaceDiagnosticId[] = [
  "gust",
  "tcdc",
  "cape",
  "vis",
  "dpt2m",
  "aptmp2m",
  "lcdc",
  "mcdc",
  "hcdc",
];

/** The ocean set — the surface (skin) temperature, which is the SST over
 * water, sea ice cover and thickness, and the GFS-Wave significant wave
 * height, primary wave period and direction — single layers held to the
 * encoders by `tests/fixtures/ocean-registry.json`. The same registry
 * carries the components of the `wave` vector bundle the encoders derive
 * from the height and direction (`WAVE_COMPONENT_IDS`). */
export type OceanId = "tmpsfc" | "icec" | "icetk" | "htsgw" | "perpw" | "dirpw";
export const OCEAN_IDS: readonly OceanId[] = ["tmpsfc", "icec", "icetk", "htsgw", "perpw", "dirpw"];

/** A well-formed bundle/variable name: lowercase alphanumeric, starting with
 * a letter. This is the whole admission rule — a manifest is rejected for
 * naming a bundle badly, never for naming one this build does not know. */
export function isBundleVariableId(value: unknown): value is ForecastBundleId {
  return typeof value === "string" && /^[a-z][a-z0-9]*$/.test(value);
}

/** The component variables a vector bundle carries, both on one time axis. */
export type VectorComponentId =
  | "ugrd10m"
  | "vgrd10m"
  | `ugrd${IsobaricLevel}`
  | `vgrd${IsobaricLevel}`
  | `uqflx${IsobaricLevel}`
  | `vqflx${IsobaricLevel}`
  | "uwave"
  | "vwave";

/** Data-level variable ids that can appear inside bundle metadata. A plain
 * string for the same reason `ForecastBundleId` is: a file names its own
 * variables, and the parameter block — not the name — says what they are. */
export type DataVariableId = string;

/** The data-level ids this shell knows by name (the v1/v2 fallback in
 * identity.ts, and the component pairs below). */
export type KnownDataVariableId =
  | ForecastVariableId
  | "dswrf"
  | "cref"
  | SurfaceDiagnosticId
  | OceanId
  | PressureBundleId
  | IsobaricScalarBundleId
  | VectorComponentId;

export const FORECAST_VARIABLE_IDS: readonly ForecastVariableId[] = ["tmp2m", "prate"];

function perLevel<Prefix extends string>(prefix: Prefix): `${Prefix}${IsobaricLevel}`[] {
  return ISOBARIC_LEVELS.map((level) => `${prefix}${level}` as `${Prefix}${IsobaricLevel}`);
}

/** Every bundle id this shell has chart knowledge about, in the rail's
 * order (every scalar, then every vector — the order the encoders write).
 * This is what the layer rail and the level row are built from — *not* a
 * list a manifest is checked against. */
export const KNOWN_BUNDLE_IDS: readonly KnownBundleId[] = [
  "tmp2m",
  "prate",
  "dswrf",
  "cref",
  ...SURFACE_DIAGNOSTIC_IDS,
  ...OCEAN_IDS,
  "prmsl",
  ...perLevel("hgt"),
  ...perLevel("tmp"),
  ...perLevel("rh"),
  ...perLevel("spfh"),
  ...perLevel("vvel"),
  ...perLevel("thetae"),
  "wind10m",
  ...perLevel("wind"),
  ...perLevel("qflux"),
  "wave",
];
export const WIND_COMPONENT_IDS: readonly KnownDataVariableId[] = ["ugrd10m", "vgrd10m"];
/** The wave vector's pair: the significant wave height laid along the
 * direction the waves travel, in metres, in the wind's u/v convention. */
export const WAVE_COMPONENT_IDS: readonly ["uwave", "vwave"] = ["uwave", "vwave"];

/** The u/v component pair of every vector bundle. */
export const VECTOR_BUNDLES: Record<VectorBundleId, readonly [VectorComponentId, VectorComponentId]> = {
  wind10m: ["ugrd10m", "vgrd10m"],
  ...Object.fromEntries(ISOBARIC_LEVELS.map((level) => [`wind${level}`, [`ugrd${level}`, `vgrd${level}`]])),
  ...Object.fromEntries(ISOBARIC_LEVELS.map((level) => [`qflux${level}`, [`uqflx${level}`, `vqflx${level}`]])),
  wave: WAVE_COMPONENT_IDS,
} as unknown as Record<VectorBundleId, readonly [VectorComponentId, VectorComponentId]>;

/** True when a bundle *named* by the convention carries a u/v pair rather
 * than one scalar. A guess from the id string, for use before the bundle is
 * open; an open session answers this from its variables' parameter blocks
 * instead (`VariableSession.vector`). */
export function isVectorBundle(id: ForecastBundleId): id is VectorBundleId {
  return id in VECTOR_BUNDLES;
}

/** The two components a conventionally named vector bundle carries, or null
 * for a scalar. Also a naming-convention guess; an open bundle names its own
 * components. */
export function vectorComponents(id: ForecastBundleId): readonly [VectorComponentId, VectorComponentId] | null {
  return isVectorBundle(id) ? VECTOR_BUNDLES[id] : null;
}

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

export interface ManifestValidationOptions {
  /** Whether the manifest must ship the core tmp2m and prate pair. True for
   * a live run, which always covers every core variable; false for a
   * showcase case, which ships only the bundles its event is about. */
  requireCoreVariables?: boolean;
}

export function validateManifest(
  input: unknown,
  expectedModel?: ForecastModelId,
  options: ManifestValidationOptions = {},
): ForecastManifest {
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
    // Structural, not a registry lookup: a name this build has never seen is
    // a layer it renders generically, not a manifest it refuses. Order is
    // not constrained either — the rail has its own.
    if (!isBundleVariableId(bundle.variable)) throw new Error("malformed bundle variable name");
    if (variables.includes(bundle.variable)) throw new Error("manifest contains duplicate variable bundles");
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
  if (options.requireCoreVariables ?? true) {
    for (const id of FORECAST_VARIABLE_IDS) {
      if (!variables.includes(id)) throw new Error(`manifest has no bundle for variable ${id}`);
    }
  }
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

/** What the session asked for on the resolution ladder: let the viewport and
 * the connection decide (the default), or pin one end of it. `?res=` carries
 * this; see urlstate.ts. */
export type ResolutionPreference = "auto" | "half" | "full";

/** Tier selection (pure so it can be unit-tested): pick the reduced
 * rendition to load instead of the canonical full-resolution bundle, or null
 * to stay on full resolution.
 *
 * - A pinned `preference` short-circuits the heuristic: "full" never takes a
 *   variant, "half" always takes the smallest tier offered. A dataset that
 *   ships no variants at all (showcase cases) has only full resolution, so
 *   "half" gets it too rather than failing.
 * - On a constrained network the smallest tier always wins — the ladder
 *   exists exactly so those clients stop paying for pixels they cannot see.
 * - Otherwise pick the smallest tier that still covers `neededGridWidth`,
 *   the horizontal grid samples the current view can actually display
 *   (world CSS width x devicePixelRatio); if no tier suffices, use full. */
export function pickBundleVariant(
  variants: VariantDescriptor[] | undefined,
  neededGridWidth: number,
  constrained: boolean,
  preference: ResolutionPreference = "auto",
): VariantDescriptor | null {
  if (preference === "full") return null;
  if (!variants || variants.length === 0) return null;
  const sorted = [...variants].sort((a, b) => a.width - b.width);
  if (preference === "half" || constrained) return sorted[0] ?? null;
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
  const latestFilename = FORECAST_MODELS[model].latestFilename;
  if (latestFilename === undefined) throw new Error(`${model} has no live feed`);
  const url = new URL(`${baseUrl}${latestFilename}`, document.baseURI);
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
  const response = await fetchImmutable(url);
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

/** What a variable *is*, in GRIB2's own terms: the parameter triple and the
 * fixed surface it sits on, plus the statistical process a derived field
 * carries. Introduced by bundle metadata schemaVersion 3; absent below.
 * A surface with no value (entire atmosphere) writes both halves of the
 * value as null, the way GRIB2 writes them missing. */
export interface BundleParameter {
  discipline: number;
  parameterCategory: number;
  parameterNumber: number;
  typeOfFirstFixedSurface: number;
  scaleFactorOfFirstFixedSurface: number | null;
  scaledValueOfFirstFixedSurface: number | null;
  /** Code table 4.10; absent for an instantaneous field. */
  typeOfStatisticalProcessing?: number;
}

export interface BundleVariable {
  numericId: number;
  id: DataVariableId;
  label: string;
  unit: string;
  /** Present from schemaVersion 3 onwards. */
  parameter?: BundleParameter;
  quantization: LinearQuantization | LogQuantization;
}

/** The bundle time axis.
 *
 * Metadata schemaVersion 3 states it in units it names: `unitSeconds` (an
 * hour for every forecast source, six minutes for the radar mosaic) plus
 * offsets in that unit, exactly one of `frameStep` (uniform) and
 * `frameOffsets` (listed outright). Versions 1 and 2 used a whole-hour axis
 * with `firstForecastHour` plus `stepHours` or `hours`; both shapes are
 * still read, and `axisUnitSeconds`/`frameOffsets` normalize them
 * (docs/format.md). */
export interface BundleTimeAxis {
  frameCount: number;
  /** Schema v3. */
  unitSeconds?: number;
  firstFrameOffset?: number;
  frameStep?: number;
  frameOffsets?: number[];
  /** Schema v1 and v2. */
  firstForecastHour?: number;
  stepHours?: number;
  hours?: number[];
}

export const HOUR_SECONDS = 3600;

export interface BundleMetadata {
  schemaVersion: 1 | 2 | 3;
  model: string;
  runTime: string;
  time: BundleTimeAxis;
  grid: { width: number; height: number };
  variables: BundleVariable[];
}

/** Seconds per axis unit. A v1/v2 axis is always whole hours. */
export function axisUnitSeconds(time: BundleTimeAxis): number {
  return time.unitSeconds ?? HOUR_SECONDS;
}

/** The materialized frame-offset list of a bundle time axis. Multiply by
 * `axisUnitSeconds` for seconds from the run time. */
export function frameOffsets(time: BundleTimeAxis): number[] {
  if (time.frameOffsets) return time.frameOffsets;
  if (time.hours) return time.hours;
  const first = time.firstFrameOffset ?? time.firstForecastHour ?? 0;
  const step = time.frameStep ?? time.stepHours ?? 1;
  return Array.from({ length: time.frameCount }, (_, index) => first + index * step);
}

/** True when two time axes describe the same frames at the same instants. */
export function sameTimeAxis(a: BundleTimeAxis, b: BundleTimeAxis): boolean {
  if (a === b) return true;
  if (axisUnitSeconds(a) !== axisUnitSeconds(b)) return false;
  const offsetsA = frameOffsets(a);
  const offsetsB = frameOffsets(b);
  return offsetsA.length === offsetsB.length && offsetsA.every((offset, index) => offset === offsetsB[index]);
}

const V3_TIME_FIELDS = ["unitSeconds", "firstFrameOffset", "frameStep", "frameOffsets"] as const;

/** The lowest schema version able to express a time axis: 1 for a uniform
 * whole-hour axis declaring `stepHours`, 2 for one listing its `hours`, and 3
 * for the unit-neutral block. */
function validateTimeAxis(time: Record<string, unknown>, schemaVersion: number): 1 | 2 | 3 {
  const frameCount = time.frameCount;
  if (typeof frameCount !== "number" || !Number.isInteger(frameCount) || frameCount <= 0) {
    throw new Error("invalid bundle time axis");
  }
  if (schemaVersion >= 3) return validateOffsetAxis(time, frameCount);
  if (V3_TIME_FIELDS.some((field) => field in time)) {
    throw new Error("a unit-neutral time axis requires schema version 3");
  }
  const firstForecastHour = time.firstForecastHour;
  if (typeof firstForecastHour !== "number" || !Number.isInteger(firstForecastHour) || firstForecastHour < 0) {
    throw new Error("invalid bundle time axis");
  }
  const stepHours = time.stepHours;
  const hours = time.hours;
  // Exactly one encoding per axis: a uniform axis declares stepHours, an
  // axis that changes step lists its hours outright.
  if ((stepHours === undefined) === (hours === undefined)) throw new Error("invalid bundle time axis");
  if (stepHours !== undefined) {
    if (typeof stepHours !== "number" || !Number.isInteger(stepHours) || stepHours <= 0) {
      throw new Error("invalid bundle time axis");
    }
    return 1;
  }
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
  checkListedSteps(hours as number[]);
  return 2;
}

function checkListedSteps(listed: number[]): void {
  const steps = new Set<number>();
  for (let index = 1; index < listed.length; index += 1) steps.add(listed[index]! - listed[index - 1]!);
  if (steps.size < 2) throw new Error("a uniform axis must be encoded as a step");
}

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b);
}

/** The schemaVersion 3 time block: offsets on a declared unit. */
function validateOffsetAxis(time: Record<string, unknown>, frameCount: number): 3 {
  for (const key of Object.keys(time)) {
    if (key !== "frameCount" && !V3_TIME_FIELDS.includes(key as (typeof V3_TIME_FIELDS)[number])) {
      throw new Error("bundle time block has an unknown field");
    }
  }
  const unitSeconds = time.unitSeconds;
  if (
    typeof unitSeconds !== "number" ||
    !Number.isInteger(unitSeconds) ||
    unitSeconds < 1 ||
    unitSeconds > HOUR_SECONDS ||
    HOUR_SECONDS % unitSeconds !== 0
  ) {
    throw new Error("bundle unitSeconds must be a whole divisor of 3600");
  }
  const first = time.firstFrameOffset;
  if (typeof first !== "number" || !Number.isInteger(first) || first < 0) {
    throw new Error("invalid bundle firstFrameOffset");
  }
  const frameStep = time.frameStep;
  const listed = time.frameOffsets;
  if ((frameStep === undefined) === (listed === undefined)) throw new Error("invalid bundle time axis");
  let offsets: number[];
  if (frameStep !== undefined) {
    if (typeof frameStep !== "number" || !Number.isInteger(frameStep) || frameStep <= 0) {
      throw new Error("invalid bundle frameStep");
    }
    if (first + (frameCount - 1) * frameStep > 65534) throw new Error("frame offsets exceed the u16 range");
    offsets = Array.from({ length: frameCount }, (_, index) => first + index * frameStep);
  } else {
    if (
      !Array.isArray(listed) ||
      listed.length !== frameCount ||
      listed[0] !== first ||
      listed.some(
        (offset, index) =>
          typeof offset !== "number" ||
          !Number.isInteger(offset) ||
          offset > 65534 ||
          (index > 0 && offset <= (listed[index - 1] as number)),
      )
    ) {
      throw new Error("invalid bundle time axis");
    }
    checkListedSteps(listed as number[]);
    offsets = listed as number[];
  }
  // The unit is the coarsest one that expresses every offset exactly, so an
  // axis has one encoding rather than one per divisor of its step.
  if (offsets.reduce((divisor, offset) => gcd(divisor, offset), HOUR_SECONDS / unitSeconds) !== 1) {
    throw new Error("bundle unitSeconds is finer than the axis needs");
  }
  return 3;
}

const PARAMETER_CODE_FIELDS = [
  "discipline",
  "parameterCategory",
  "parameterNumber",
  "typeOfFirstFixedSurface",
] as const;
// Present but nullable: a surface with no value writes both halves as null,
// and only a derived field carries a statistical process at all.
const PARAMETER_NULLABLE_FIELDS = [
  "scaleFactorOfFirstFixedSurface",
  "scaledValueOfFirstFixedSurface",
  "typeOfStatisticalProcessing",
] as const;

/** Validate a variable's GRIB2 identity block, which schemaVersion 3
 * introduces: required at version 3, forbidden below. */
function validateParameter(parameter: unknown, schemaVersion: number): void {
  if (schemaVersion < 3) {
    if (parameter !== undefined) throw new Error("a GRIB2 parameter block requires schema version 3");
    return;
  }
  if (typeof parameter !== "object" || parameter === null || Array.isArray(parameter)) {
    throw new Error("schema version 3 requires a parameter block on every variable");
  }
  const block = parameter as Record<string, unknown>;
  const known = new Set<string>([...PARAMETER_CODE_FIELDS, ...PARAMETER_NULLABLE_FIELDS]);
  for (const key of Object.keys(block)) {
    if (!known.has(key)) throw new Error("bundle parameter block has an unknown field");
  }
  for (const field of PARAMETER_CODE_FIELDS) {
    const code = block[field];
    if (typeof code !== "number" || !Number.isInteger(code) || code < 0 || code > 255) {
      throw new Error(`invalid bundle parameter ${field}`);
    }
  }
  if (!("scaleFactorOfFirstFixedSurface" in block) || !("scaledValueOfFirstFixedSurface" in block)) {
    throw new Error("bundle parameter fixed surface value is incomplete");
  }
  const scaleFactor = block.scaleFactorOfFirstFixedSurface;
  const scaledValue = block.scaledValueOfFirstFixedSurface;
  // GRIB2 encodes a surface with no value by writing both as missing.
  if ((scaleFactor === null) !== (scaledValue === null)) {
    throw new Error("bundle parameter fixed surface must be wholly present or wholly null");
  }
  if (scaleFactor !== null && (!Number.isInteger(scaleFactor) || !Number.isInteger(scaledValue))) {
    throw new Error("invalid bundle parameter fixed surface value");
  }
  const statistical = block.typeOfStatisticalProcessing;
  if (statistical !== undefined && (!Number.isInteger(statistical) || (statistical as number) < 0)) {
    throw new Error("invalid bundle parameter typeOfStatisticalProcessing");
  }
}

export function parseBundleMetadata(json: string): BundleMetadata {
  const value = object(JSON.parse(json));
  const schemaVersion = value.schemaVersion;
  if (schemaVersion !== 1 && schemaVersion !== 2 && schemaVersion !== 3) {
    throw new Error("unsupported bundle metadata schema version");
  }
  const time = object(value.time);
  const grid = object(value.grid);
  const axisVersion = validateTimeAxis(time, schemaVersion);
  if (typeof grid.width !== "number" || typeof grid.height !== "number") {
    throw new Error("invalid bundle grid");
  }
  const variables = value.variables;
  if (!Array.isArray(variables) || variables.length === 0) throw new Error("bundle metadata has no variables");
  let parameters = 0;
  for (const item of variables) {
    const variable = object(item);
    if (typeof variable.numericId !== "number" || typeof variable.id !== "string") {
      throw new Error("invalid bundle variable descriptor");
    }
    validateParameter(variable.parameter, schemaVersion);
    if (variable.parameter !== undefined) parameters += 1;
    const quantization = object(variable.quantization);
    if (quantization.type !== "linear" && quantization.type !== "log1p") {
      throw new Error("unsupported bundle quantization");
    }
  }
  // Every axis and every variable set has exactly one valid encoding: the
  // declared version must be the lowest able to express both.
  const requiredVersion = Math.max(axisVersion, parameters > 0 ? 3 : 1);
  if (schemaVersion !== requiredVersion) {
    throw new Error(`bundle metadata must declare schema version ${requiredVersion}`);
  }
  return value as unknown as BundleMetadata;
}
