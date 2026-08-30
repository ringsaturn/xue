import {
  FORECAST_BUNDLE_IDS,
  FORECAST_MODELS,
  validateManifest,
  type ForecastBundleId,
  type ForecastManifest,
  type ForecastModelId,
} from "./manifest";
import { latitudeOf, longitudeOf, mercatorX, mercatorY, worldPixels, zoomForSpan } from "./mercator";

/**
 * The historical showcase catalog: a list of past weather events, each one a
 * spatially cropped slice of one archived forecast run.
 *
 * Delivery mirrors the live feed. `showcase.json` is the only mutable object
 * — it is the list page's index and the viewer's case lookup — while every
 * per-case `manifest.json` it names is immutable and addressed with
 * `?v=<crc32>`, exactly like a run manifest behind `latest.json`.
 *
 * A case manifest is an ordinary schema v5 manifest, so the viewer reuses the
 * whole decode and render path; it simply carries a regional grid and only
 * the bundles the event is about (`validateManifest(..., { requireCoreVariables: false })`).
 */

/** Bounding box as [west, south, east, north] in degrees. `east < west`
 * crosses the antimeridian. */
export type CaseBounds = [number, number, number, number];

export interface ShowcaseCase {
  id: string;
  /** Human-facing strings, one per UI locale. */
  title: Record<string, string>;
  summary: Record<string, string>;
  modelId: ForecastModelId;
  /** Manifest/pointer `model` string, e.g. "GFS-SFLUX". */
  model: string;
  product: string;
  run: string;
  runTime: string;
  forecastHours: number;
  /** The region the case is framed on (what the map fits to). */
  bbox: CaseBounds;
  /** The region the cropped grid's cell centers actually cover — at least
   * `bbox`, rounded outward to whole cells. */
  dataBbox: CaseBounds;
  grid: { width: number; height: number };
  variables: ForecastBundleId[];
  defaultVariable: ForecastBundleId;
  manifestPath: string;
  manifestCrc32: string;
  byteLength: number;
  /** When the event itself peaked, if the case names one. */
  eventTime?: string;
  tags?: string[];
  credit?: string;
}

export interface ShowcaseCatalog {
  schemaVersion: 1;
  generatedAt: string;
  cases: ShowcaseCase[];
}

export const CATALOG_FILENAME = "showcase.json";

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("showcase catalog node must be an object");
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`showcase case ${label} missing`);
  return value;
}

function localized(value: unknown, label: string): Record<string, string> {
  const strings = object(value);
  // The viewer falls back across locales, so one usable string is enough to
  // render a card; an entry with none is unrenderable.
  const usable = Object.values(strings).filter((item) => typeof item === "string" && item.length > 0);
  if (usable.length === 0) throw new Error(`showcase case ${label} has no text`);
  return strings as Record<string, string>;
}

function bounds(value: unknown, label: string): CaseBounds {
  if (!Array.isArray(value) || value.length !== 4 || value.some((item) => typeof item !== "number" || !Number.isFinite(item))) {
    throw new Error(`showcase case ${label} must be [west, south, east, north]`);
  }
  const [, south, , north] = value as number[];
  if (!(south! < north!)) throw new Error(`showcase case ${label} is not north of its south edge`);
  return value as CaseBounds;
}

function modelIdForManifestString(model: string): ForecastModelId | null {
  for (const info of Object.values(FORECAST_MODELS)) {
    if (info.label === model) return info.id;
  }
  return null;
}

function validateCase(input: unknown): ShowcaseCase {
  const value = object(input);
  const id = text(value.id, "id");
  if (!/^[a-z0-9-]+$/.test(id)) throw new Error("showcase case id must be a slug");
  const modelId = modelIdForManifestString(text(value.model, "model"));
  if (!modelId || value.product !== FORECAST_MODELS[modelId].product) {
    throw new Error(`showcase case ${id} names an unsupported dataset`);
  }
  // modelId is derived from the manifest strings, so a mismatching shorthand
  // is a broken catalog rather than something to paper over.
  if (value.modelId !== modelId) throw new Error(`showcase case ${id} has an inconsistent modelId`);
  if (typeof value.run !== "string" || !/^\d{10}$/.test(value.run)) {
    throw new Error(`showcase case ${id} has an invalid run id`);
  }
  if (!Number.isFinite(Date.parse(text(value.runTime, "runTime")))) {
    throw new Error(`showcase case ${id} has an invalid runTime`);
  }
  if (typeof value.forecastHours !== "number" || !Number.isInteger(value.forecastHours) || value.forecastHours <= 0) {
    throw new Error(`showcase case ${id} has an invalid forecast range`);
  }
  const manifestPath = text(value.manifestPath, "manifestPath");
  if (
    manifestPath !== `showcase/${id}/manifest.json` ||
    manifestPath.startsWith("/") ||
    manifestPath.split("/").includes("..")
  ) {
    throw new Error(`showcase case ${id} names a manifest outside its own directory`);
  }
  if (typeof value.manifestCrc32 !== "string" || !/^[0-9a-f]{8}$/.test(value.manifestCrc32)) {
    throw new Error(`showcase case ${id} has an invalid manifest crc32`);
  }
  const variables = value.variables;
  if (
    !Array.isArray(variables) ||
    variables.length === 0 ||
    variables.some((item) => !FORECAST_BUNDLE_IDS.includes(item as ForecastBundleId))
  ) {
    throw new Error(`showcase case ${id} lists unsupported variables`);
  }
  const defaultVariable = value.defaultVariable;
  if (!variables.includes(defaultVariable)) {
    throw new Error(`showcase case ${id} defaults to a variable it does not ship`);
  }
  const grid = object(value.grid);
  for (const key of ["width", "height"] as const) {
    if (typeof grid[key] !== "number" || !Number.isInteger(grid[key]) || (grid[key] as number) <= 0) {
      throw new Error(`showcase case ${id} has an invalid grid`);
    }
  }
  return {
    ...(value as unknown as ShowcaseCase),
    title: localized(value.title, "title"),
    summary: localized(value.summary, "summary"),
    bbox: bounds(value.bbox, "bbox"),
    dataBbox: bounds(value.dataBbox, "dataBbox"),
  };
}

export function validateCatalog(input: unknown): ShowcaseCatalog {
  const value = object(input);
  if (value.schemaVersion !== 1) throw new Error("unsupported showcase catalog schema version");
  if (!Array.isArray(value.cases)) throw new Error("showcase catalog has no case list");
  const cases = value.cases.map(validateCase);
  if (new Set(cases.map((item) => item.id)).size !== cases.length) {
    throw new Error("showcase catalog contains duplicate case ids");
  }
  return { schemaVersion: 1, generatedAt: String(value.generatedAt ?? ""), cases };
}

export async function fetchCatalog(baseUrl: string): Promise<ShowcaseCatalog> {
  const url = new URL(`${baseUrl}${CATALOG_FILENAME}`, document.baseURI);
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(`showcase catalog request failed (${response.status})`);
  return validateCatalog(await response.json());
}

/** The case string for `locale`, falling back to any other locale the catalog
 * carries rather than rendering an empty card. */
export function localizedText(strings: Record<string, string>, locale: string): string {
  return strings[locale] ?? strings.en ?? Object.values(strings).find((item) => item.length > 0) ?? "";
}

/** Longitude span of a box, handling the antimeridian. */
export function boundsWidth([west, , east]: CaseBounds): number {
  const span = (east - west) % 360;
  return span === 0 ? 360 : ((span % 360) + 360) % 360;
}

/** MapLibre `fitBounds` corners for a case. A box crossing the antimeridian
 * is expressed with an east beyond +180 so the fitted view stays contiguous
 * instead of wrapping the long way round. */
export function fitBoundsCorners(box: CaseBounds): [[number, number], [number, number]] {
  const [west, south, , north] = box;
  return [
    [west, south],
    [west + boundsWidth(box), north],
  ];
}

export interface LoadedCase {
  case: ShowcaseCase;
  manifest: ForecastManifest;
  /** Absolute manifest URL (with its ?v=); artifact paths resolve against it. */
  manifestUrl: string;
}

/** Load one case's manifest. The catalog entry plays the part `latest.json`
 * plays for a live run: it names the immutable manifest and its crc32. */
export async function fetchCaseManifest(baseUrl: string, showcaseCase: ShowcaseCase): Promise<LoadedCase> {
  const url = new URL(showcaseCase.manifestPath, new URL(baseUrl, document.baseURI));
  url.searchParams.set("v", showcaseCase.manifestCrc32);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`showcase manifest request failed (${response.status})`);
  const manifest = validateManifest(await response.json(), showcaseCase.modelId, { requireCoreVariables: false });
  for (const variable of showcaseCase.variables) {
    if (!manifest.bundles.some((bundle) => bundle.variable === variable)) {
      throw new Error(`showcase case ${showcaseCase.id} is missing its ${variable} bundle`);
    }
  }
  return { case: showcaseCase, manifest, manifestUrl: url.href };
}

/** How much breathing room a case's region keeps around the viewport edge. */
export const CASE_VIEW_PADDING = 28;

export interface CaseCameraLimits {
  /** Where the case sits, as [lng, lat]. */
  center: [number, number];
  /** Zoom at which the region just fills the padded viewport — the point
   * past which zooming out would only add empty map. */
  minZoom: number;
  /** The region the viewport covers at `minZoom`: what panning is held to. */
  bounds: [[number, number], [number, number]];
}

/**
 * Camera limits that keep a case's own region on screen: it is the whole
 * dataset, so there is nothing to see outside it and no detail to gain by
 * zooming past it. Both limits depend on the viewport, so a resize recomputes
 * them.
 *
 * Pure geometry, in Mercator world units — no map instance, so it is
 * unit-testable and the caller decides when to apply it.
 */
export function caseCameraLimits(
  box: CaseBounds,
  viewport: { width: number; height: number },
  padding: number = CASE_VIEW_PADDING,
): CaseCameraLimits {
  const [[west, south], [east, north]] = fitBoundsCorners(box);
  const left = mercatorX(west);
  const right = mercatorX(east);
  const top = mercatorY(north);
  const bottom = mercatorY(south);
  const centerX = (left + right) / 2;
  const centerY = (top + bottom) / 2;

  // Never demand more room than the viewport has; a padding wider than the
  // canvas would otherwise ask for an infinite zoom-out.
  const usableWidth = Math.max(1, viewport.width - 2 * padding);
  const usableHeight = Math.max(1, viewport.height - 2 * padding);
  // The tighter axis decides the fit, and zoom 0 is the floor: a case wider
  // than the world at zoom 0 simply cannot be zoomed out any further.
  const minZoom = Math.max(
    0,
    Math.min(zoomForSpan(right - left, usableWidth), zoomForSpan(bottom - top, usableHeight)),
  );

  // The world rect actually on screen at that zoom, padding included — the
  // viewport can then pan nowhere at minZoom and only within the region as it
  // zooms in.
  const pixels = worldPixels(minZoom);
  const halfWidth = viewport.width / pixels / 2;
  const halfHeight = viewport.height / pixels / 2;
  return {
    center: [longitudeOf(centerX), latitudeOf(centerY)],
    minZoom,
    bounds: [
      [longitudeOf(centerX - halfWidth), latitudeOf(Math.min(1, centerY + halfHeight))],
      [longitudeOf(centerX + halfWidth), latitudeOf(Math.max(0, centerY - halfHeight))],
    ],
  };
}
