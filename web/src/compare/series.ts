/**
 * One model at one point: every series the comparison page reads.
 *
 * The page never opens a map or a playback session. For each model it
 * fetches the live pointer and the run's manifest, and for each field it
 * compares it opens that bundle's Zarr store in a decode worker, asks for
 * the pinned cell's whole series (`series`: one chunk per temporal group of
 * the one tile holding the cell, replayed in one round trip) and closes the
 * worker again. Nothing is kept but the codes, so a page comparing six
 * models on five fields holds a few kilobytes of data behind what it draws.
 *
 * The cell is the model's own grid cell nearest the point, resolved by
 * `probeCell` against the bundle's metadata exactly as the viewer's probe
 * does, so the two pages read the same value for the same pin. The
 * canonical (full-resolution) tier is always taken: a series read costs the
 * same handful of chunks at any rung, and only the full one names the
 * nearest cell rather than a coarser one's.
 */

import { domainContains, lambertCone } from "../domain";
import { identifyBundle, type BundleIdentity } from "../identity";
import {
  FORECAST_MODELS,
  axisUnitSeconds,
  fetchManifest,
  frameOffsets,
  parseBundleMetadata,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastModelId,
  type LoadedManifest,
} from "../manifest";
import { decodeValue } from "../palettes";
import { probeCell, wrap, type ProbeCell } from "../probe";
import { spawnZarrWorker, zarrInitMessage, zarrRootUrl, zarrStoreFor } from "../zarr/channel";

/** One bundle's series at the point. `codes` is one array per data variable
 * (a scalar has one, a vector its u/v pair), each with one code per frame of
 * `validTimesMs`. */
export interface PointSeries {
  bundleId: ForecastBundleId;
  metadata: BundleMetadata;
  identity: BundleIdentity | null;
  /** The variables the codes are for, in the bundle's own order. */
  variables: BundleVariable[];
  cell: ProbeCell;
  /** Epoch milliseconds of each frame's valid time, ascending. */
  validTimesMs: number[];
  codes: Uint8Array[];
}

/** Physical values of one series, `null` where the codebook says no data. */
export function seriesValues(series: PointSeries): (number | null)[] {
  const [first, second] = series.codes;
  if (!first) return [];
  return Array.from(first, (code, index) => {
    const a = decodeValue(series.variables[0]!, code);
    if (!second) return a;
    const b = decodeValue(series.variables[1]!, second[index]!);
    return a === null || b === null ? null : Math.hypot(a, b);
  });
}

/** Meteorological wind direction (degrees the wind comes FROM) per frame of
 * a vector series, null where either component is missing; empty for a
 * scalar. */
export function seriesDirections(series: PointSeries): (number | null)[] {
  const [uCodes, vCodes] = series.codes;
  if (!uCodes || !vCodes) return [];
  return Array.from(uCodes, (uCode, index) => {
    const u = decodeValue(series.variables[0]!, uCode);
    const v = decodeValue(series.variables[1]!, vCodes[index]!);
    if (u === null || v === null) return null;
    return wrap((Math.atan2(-u, -v) * 180) / Math.PI, 360);
  });
}

/** The forecast models the page compares: every live source that is a
 * weather forecast (observations and the satellite mosaic have no lead
 * time to compare; a forecast whose core is another quantity — the
 * aerosol run, with no temperature or precipitation — has nothing on the
 * page's rows), in the registry's order. */
export function comparableModels(): ForecastModelId[] {
  return (Object.keys(FORECAST_MODELS) as ForecastModelId[]).filter((id) => {
    const info = FORECAST_MODELS[id];
    return info.latestFilename !== undefined && !info.observation && !info.mosaic && info.coreBundles === undefined;
  });
}

/** Whether a model can say anything about the point: a regional model only
 * inside its domain proper, since the encoder's rectangle repeats edge
 * cells into the corners the conic grid never covered. */
export function modelCoversPoint(model: ForecastModelId, longitude: number, latitude: number): boolean {
  const domain = FORECAST_MODELS[model].domain;
  if (!domain) return true;
  return domainContains(domain, lambertCone(domain), longitude, latitude);
}

/** The run a model is publishing now: its manifest, with the pointer. */
export function fetchModelRun(baseUrl: string, model: ForecastModelId): Promise<LoadedManifest> {
  return fetchManifest(baseUrl, model);
}

/** The bundles of `wanted` the run publishes, in `wanted`'s order. */
export function publishedOf(run: LoadedManifest, wanted: readonly ForecastBundleId[]): ForecastBundleId[] {
  return wanted.filter((id) => run.manifest.bundles.some((bundle) => bundle.variable === id));
}

/** Open one bundle's store, read the point's series for every variable in
 * it, and close the worker. Resolves null when the point is off the
 * bundle's grid; rejects on a store the worker cannot open or a series it
 * cannot read. `signal` aborts a read whose page state has moved on. */
export function readPointSeries(
  run: LoadedManifest,
  bundleId: ForecastBundleId,
  longitude: number,
  latitude: number,
  signal?: AbortSignal,
): Promise<PointSeries | null> {
  const descriptor = run.manifest.bundles.find((bundle) => bundle.variable === bundleId);
  if (!descriptor) return Promise.reject(new Error(`bundle ${bundleId} not published`));
  const store = zarrStoreFor("zarr", descriptor, null);
  if (!store) return Promise.reject(new Error(`bundle ${bundleId} ships no store`));
  const root = zarrRootUrl(store.path, run.manifestUrl);
  return new Promise((resolve, reject) => {
    const worker = spawnZarrWorker();
    let metadata: BundleMetadata | null = null;
    let cell: ProbeCell | null = null;
    let pending = new Map<number, number>();
    const codes: Uint8Array[] = [];
    let settled = false;
    const finish = (error?: Error, value?: PointSeries | null) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      worker.terminate();
      if (error) reject(error);
      else resolve(value ?? null);
    };
    const onAbort = () => finish(new DOMException("aborted", "AbortError"));
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    worker.onerror = (event) => finish(new Error(event.message || "decode worker failed to start"));
    worker.onmessage = (event: MessageEvent) => {
      const message = event.data as Record<string, unknown>;
      switch (message.type) {
        case "booted":
          worker.postMessage(zarrInitMessage(root, store, `${bundleId}@compare`));
          break;
        case "ready": {
          try {
            metadata = parseBundleMetadata(message.metadataJson as string);
          } catch (error) {
            finish(error instanceof Error ? error : new Error(String(error)));
            return;
          }
          cell = probeCell(metadata, longitude, latitude);
          if (!cell) {
            finish(undefined, null);
            return;
          }
          pending = new Map();
          metadata.variables.forEach((variable, index) => {
            pending.set(variable.numericId, index);
            worker.postMessage({
              type: "series",
              requestId: index + 1,
              generation: 0,
              variableId: variable.numericId,
              column: cell!.column,
              row: cell!.row,
            });
          });
          break;
        }
        case "series": {
          const index = pending.get(message.variableId as number);
          if (index === undefined || !metadata || !cell) return;
          pending.delete(message.variableId as number);
          codes[index] = new Uint8Array(message.buffer as ArrayBuffer);
          if (pending.size > 0) return;
          const unit = axisUnitSeconds(metadata.time) * 1000;
          const runMs = Date.parse(metadata.runTime);
          const validTimesMs = frameOffsets(metadata.time).map((offset) => runMs + offset * unit);
          if (codes.some((series) => series.length !== validTimesMs.length)) {
            finish(new Error(`${bundleId}: series length does not match the axis`));
            return;
          }
          finish(undefined, {
            bundleId,
            metadata,
            identity: identifyBundle(metadata.variables),
            variables: metadata.variables,
            cell,
            validTimesMs,
            codes,
          });
          break;
        }
        case "error":
          finish(new Error(String(message.message ?? "decode failed")));
          break;
        default:
          break;
      }
    };
  });
}
