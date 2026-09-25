/** One point's series, read straight from the published Zarr stores.
 *
 * Resolution is the published chain — Collection (`xue:pointer`) → pointer →
 * manifest — and decoding reuses `web/src/zarr/shard.ts` plus `decodeChunk`
 * from the existing wasm. No new format, no new artifact. */

import {
  PREDICTOR_PREVIOUS,
  PREDICTOR_RAW,
  parseArrayMetadata,
  parseGroupMetadata,
  parseShardIndex,
  shardIndexLength,
  shardOf,
  tileOf,
  tileOrigin,
  type ShardIndexEntry,
} from "../../web/src/zarr/shard";
import { fetchJson, fetchRange, fetchText } from "./bucket";
import { probeCell, type ProbeCell } from "./grid";
import { HttpError } from "./http";
import { decodeValue, type Quantization } from "./quantize";
import { loadDecoder } from "./wasm";

interface Pointer {
  run: string;
  runTime?: string;
  manifestPath: string;
  manifestCrc32: string;
}

interface ManifestBundle {
  variable: string;
  zarr?: { path: string; crc32: string };
}

interface Manifest {
  runTime?: string;
  bundles: ManifestBundle[];
}

interface BundleVariable {
  id: string;
  numericId: number;
  label?: string;
  unit?: string;
  quantization: Quantization;
}

interface BundleMetadata {
  runTime?: string;
  grid?: Record<string, unknown>;
  time: {
    unitSeconds: number;
    frameCount: number;
    firstFrameOffset?: number;
    frameStep?: number;
    frameOffsets?: number[] | null;
  };
  variables: BundleVariable[];
}

export interface ReadPointOptions {
  source: string;
  lat: number;
  lon: number;
  variables?: string[];
  time?: string;
  run?: string;
}

function withVersion(path: string, crc: string): string {
  return `${path}?v=${crc}`;
}

/** A run directory under the data root. Accepts the STAC Item id form with a
 * 4-digit round (`mrms.2026092509.1455` → `mrms.2026092509/1455`), the run
 * directory itself (`mrms.2026092509/1455`), or a plain run id
 * (`gfs.2026092506`). `?run=` uses this, so a query can name a run instead of
 * following the live pointer — which is what makes it cacheable. */
export function runDirectory(run: string): string {
  if (!/^[A-Za-z0-9._/-]+$/.test(run)) {
    throw new HttpError(400, "invalid_parameter", `invalid run ${run}`);
  }
  if (run.includes("/")) return run;
  const parts = run.split(".");
  const last = parts[parts.length - 1] ?? "";
  return parts.length >= 2 && /^\d{4}$/.test(last) ? `${parts.slice(0, -1).join(".")}/${last}` : run;
}

/** Resolve a run to its manifest. With `run`, the manifest is read directly
 * and the live pointer is skipped (fewer reads, and a stable URL); without it,
 * the published chain Collection → pointer → manifest is followed. */
export async function resolveRun(
  source: string,
  run?: string,
): Promise<{ manifest: Manifest; runDir: string; runTime: string | null }> {
  if (run) {
    const runDir = runDirectory(run);
    let manifest: Manifest;
    try {
      manifest = await fetchJson<Manifest>(`${runDir}/manifest.json`);
    } catch (error) {
      if (error instanceof HttpError && error.status === 404) {
        throw new HttpError(404, "unknown_run", `no manifest at ${runDir}/manifest.json`);
      }
      throw error;
    }
    return { manifest, runDir, runTime: manifest.runTime ?? null };
  }
  let collection: Record<string, unknown>;
  try {
    collection = await fetchJson<Record<string, unknown>>(`${source}/collection.json`);
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) {
      throw new HttpError(404, "unknown_source", `unknown source ${source}`);
    }
    throw error;
  }
  const pointerName = collection["xue:pointer"];
  if (typeof pointerName !== "string") {
    throw new HttpError(404, "no_live_run", `source ${source} has no live pointer`);
  }
  const pointer = await fetchJson<Pointer>(pointerName);
  const manifest = await fetchJson<Manifest>(withVersion(pointer.manifestPath, pointer.manifestCrc32));
  const runDir = pointer.manifestPath.slice(0, pointer.manifestPath.lastIndexOf("/"));
  return { manifest, runDir, runTime: pointer.runTime ?? manifest.runTime ?? null };
}

/** Frame offsets of the axis, from `frameOffsets` or the uniform step. */
function frameOffsets(metadata: BundleMetadata): number[] {
  const time = metadata.time;
  if (Array.isArray(time.frameOffsets) && time.frameOffsets.length === time.frameCount) {
    return time.frameOffsets;
  }
  const first = time.firstFrameOffset ?? 0;
  const step = time.frameStep ?? 1;
  return Array.from({ length: time.frameCount }, (_, index) => first + index * step);
}

/** Frame indices to read: all, or the one nearest the requested valid time. */
function frameIndices(metadata: BundleMetadata, runTime: string | undefined, time: string | undefined): number[] {
  if (!time) return Array.from({ length: metadata.time.frameCount }, (_, index) => index);
  const offsets = frameOffsets(metadata);
  const unitMs = metadata.time.unitSeconds * 1000;
  const runMs = runTime ? Date.parse(runTime) : 0;
  const target = Date.parse(time);
  if (!Number.isFinite(target)) throw new HttpError(400, "invalid_parameter", `time must be ISO 8601, got ${time}`);
  let best = 0;
  let bestDistance = Infinity;
  for (let index = 0; index < offsets.length; index += 1) {
    const distance = Math.abs(runMs + offsets[index]! * unitMs - target);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = index;
    }
  }
  return [best];
}

function axisTimes(metadata: BundleMetadata, runTime: string | undefined, indices: number[]): string[] {
  const offsets = frameOffsets(metadata);
  const unitMs = metadata.time.unitSeconds * 1000;
  const runMs = runTime ? Date.parse(runTime) : 0;
  return indices.map((index) => new Date(runMs + offsets[index]! * unitMs).toISOString());
}

async function mapLimit<T, R>(items: T[], limit: number, run: (item: T, index: number) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      results[index] = await run(items[index]!, index);
    }
  });
  await Promise.all(workers);
  return results;
}

async function readBundleSeries(
  storeBase: string,
  crc: string,
  variable: BundleVariable,
  cell: ProbeCell,
  indices: number[],
  decodeChunk: Awaited<ReturnType<typeof loadDecoder>>,
): Promise<(number | null)[]> {
  const layout = parseArrayMetadata(await fetchText(`${storeBase}/${variable.id}/zarr.json?v=${crc}`));
  const tile = tileOf(layout, cell.row, cell.column);
  const origin = tileOrigin(layout, tile);

  // Index per shard the requested frames touch (the exporter writes one shard,
  // so this is one read; kept general for the earlier shape).
  const shards = new Map<number, (ShardIndexEntry | null)[]>();
  const needed = new Set<number>();
  for (const index of indices) needed.add(shardOf(layout, Math.floor(index / layout.timeChunk)).shard);
  for (const shard of needed) {
    const bytes = await fetchRange(`${storeBase}/${variable.id}/c/${shard}/0/0?v=${crc}`, {
      suffix: shardIndexLength(layout.chunksPerShard),
    });
    shards.set(shard, parseShardIndex(bytes, layout.chunksPerShard));
  }

  return await mapLimit(indices, 6, async (index) => {
    const timeChunk = Math.floor(index / layout.timeChunk);
    const { shard, position } = shardOf(layout, timeChunk);
    const entry = shards.get(shard)![position + tile];
    if (!entry) return null;
    const payload = await fetchRange(`${storeBase}/${variable.id}/c/${shard}/0/0?v=${crc}`, {
      offset: entry.offset,
      length: entry.length,
    });
    const codes = decodeChunk(
      payload,
      layout.timeChunk,
      layout.tileHeight,
      layout.tileWidth,
      layout.delta ? PREDICTOR_PREVIOUS : PREDICTOR_RAW,
    );
    const frameInChunk = index - timeChunk * layout.timeChunk;
    const offset =
      frameInChunk * layout.tileHeight * layout.tileWidth +
      (cell.row - origin.row) * layout.tileWidth +
      (cell.column - origin.column);
    return decodeValue(variable.quantization, codes[offset]!);
  });
}

export async function readPoint(options: ReadPointOptions): Promise<unknown> {
  const { source, lat, lon } = options;
  if (typeof source !== "string" || source.length === 0) {
    throw new HttpError(400, "invalid_parameter", "source is required");
  }
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    throw new HttpError(400, "invalid_parameter", "lat must be within [-90, 90]");
  }
  if (!Number.isFinite(lon) || lon < -180 || lon > 360) {
    throw new HttpError(400, "invalid_parameter", "lon must be within [-180, 360]");
  }

  const { manifest, runDir, runTime } = await resolveRun(source, options.run);
  const bundleIds = options.variables?.length
    ? options.variables
    : manifest.bundles.length
      ? [manifest.bundles[0]!.variable]
      : [];
  if (bundleIds.length === 0) {
    throw new HttpError(404, "no_live_run", `source ${source} publishes no bundles`);
  }

  const decodeChunk = await loadDecoder();
  const variables: Record<string, unknown> = {};
  let cell: ProbeCell | null = null;
  let times: string[] | null = null;
  let unitSeconds: number | null = null;

  for (const bundleId of bundleIds) {
    const bundle = manifest.bundles.find((candidate) => candidate.variable === bundleId);
    if (!bundle) {
      throw new HttpError(404, "unknown_variable", `source ${source} has no bundle ${bundleId}`, {
        available: manifest.bundles.map((candidate) => candidate.variable),
      });
    }
    if (!bundle.zarr) {
      throw new HttpError(404, "unknown_variable", `bundle ${bundleId} ships no zarr store`);
    }
    const storeBase = `${runDir}/${bundle.zarr.path}`;
    const crc = bundle.zarr.crc32;
    const group = parseGroupMetadata(await fetchText(`${storeBase}/zarr.json?v=${crc}`));
    const metadata = group.metadata as unknown as BundleMetadata;
    const bundleCell = probeCell(metadata, lon, lat);
    if (!bundleCell) {
      throw new HttpError(422, "point_off_grid", `point is outside the ${source}/${bundleId} grid`);
    }
    cell ??= bundleCell;
    const indices = frameIndices(metadata, runTime ?? metadata.runTime, options.time);
    times ??= axisTimes(metadata, runTime ?? metadata.runTime, indices);
    unitSeconds ??= metadata.time.unitSeconds;

    const decoded: Record<string, { label?: string; unit?: string; values: (number | null)[] }> = {};
    for (const variable of metadata.variables) {
      const values = await readBundleSeries(storeBase, crc, variable, bundleCell, indices, decodeChunk);
      decoded[variable.id] = { label: variable.label, unit: variable.unit, values };
    }

    variables[bundleId] =
      metadata.variables.length === 1 && metadata.variables[0]!.id === bundleId
        ? decoded[bundleId]
        : metadata.variables.length === 1
          ? decoded[metadata.variables[0]!.id]
          : { components: decoded };
  }

  return {
    source,
    run: runDir,
    runTime: runTime ?? null,
    request: { lat, lon, variables: bundleIds },
    cell,
    time: { unitSeconds, times },
    variables,
  };
}
