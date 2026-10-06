/**
 * The terrain-shadow worker: one `ShadowRequest` in, one sunlit mask out.
 *
 * It fetches the Terrarium tiles a view's shadows need (the view plus a
 * margin toward the sun, `shadow.ts::planMosaic`), decodes them into one
 * elevation mosaic and marches it. The tiles are the ones the basemap's
 * hillshade already loads, so the browser's HTTP cache serves most of them;
 * decoded elevations are kept here too, since decoding a WebP and unpacking
 * it costs more than the march over the same pixels.
 *
 * Requests carry increasing ids and only the newest one matters: a request
 * that has been superseded by the time its tiles arrive is dropped before
 * the march, the step that costs.
 */

import { apparentSun, latitudeAt, marchLit, planMosaic, type MosaicTile } from "./shadow";
import type { ShadowError, ShadowRequest, ShadowResult } from "./shadowtypes";
import { TERRAIN_TILE_SIZE, terrainTileUrl, terrariumElevation } from "./terrain";

const MAX_CACHED_TILES = 96;
const scope = self as unknown as DedicatedWorkerGlobalScope;

/** Decoded tiles by `z/x/y`, least recently used first; null is a tile the
 * archive does not have, kept so the miss is not refetched. */
const tileCache = new Map<string, Promise<Float32Array | null>>();
let newestId = -Infinity;
let canvas: OffscreenCanvas | null = null;

async function fetchBitmap(tile: MosaicTile): Promise<ImageBitmap | null> {
  try {
    const response = await fetch(terrainTileUrl(tile.z, tile.x, tile.y));
    if (!response.ok) return null;
    return await createImageBitmap(await response.blob());
  } catch {
    // Off the archive, offline or undecodable: the ground there is read as
    // sea level rather than failing the whole mask.
    return null;
  }
}

async function decodeTile(tile: MosaicTile): Promise<Float32Array | null> {
  const bitmap = await fetchBitmap(tile);
  if (!bitmap) return null;
  const size = TERRAIN_TILE_SIZE;
  if (!canvas) canvas = new OffscreenCanvas(size, size);
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("no 2d context for terrain decoding");
  context.clearRect(0, 0, size, size);
  context.drawImage(bitmap, 0, 0, size, size);
  bitmap.close();
  const rgba = context.getImageData(0, 0, size, size).data;
  const metres = new Float32Array(size * size);
  for (let i = 0, j = 0; i < metres.length; i++, j += 4) {
    metres[i] = terrariumElevation(rgba[j] as number, rgba[j + 1] as number, rgba[j + 2] as number);
  }
  return metres;
}

function loadTile(tile: MosaicTile): Promise<Float32Array | null> {
  const key = `${tile.z}/${tile.x}/${tile.y}`;
  const cached = tileCache.get(key);
  if (cached) {
    tileCache.delete(key);
    tileCache.set(key, cached);
    return cached;
  }
  const pending = decodeTile(tile);
  pending.catch(() => tileCache.delete(key));
  tileCache.set(key, pending);
  while (tileCache.size > MAX_CACHED_TILES) {
    const oldest = tileCache.keys().next().value;
    if (oldest === undefined) break;
    tileCache.delete(oldest);
  }
  return pending;
}

/** Copies the part of a tile that falls inside the mosaic. */
function blit(mosaic: Float32Array, width: number, height: number, tile: MosaicTile, metres: Float32Array): void {
  const size = TERRAIN_TILE_SIZE;
  const x0 = Math.max(0, tile.offsetX);
  const x1 = Math.min(width, tile.offsetX + size);
  const y0 = Math.max(0, tile.offsetY);
  const y1 = Math.min(height, tile.offsetY + size);
  if (x1 <= x0) return;
  for (let y = y0; y < y1; y++) {
    const from = (y - tile.offsetY) * size + (x0 - tile.offsetX);
    mosaic.set(metres.subarray(from, from + (x1 - x0)), y * width + x0);
  }
}

/** The mask as the basemap draws it: the ink in full shadow, transparent
 * in sun. Built here so the main thread receives a finished picture. */
function inked(lit: Uint8Array, width: number, height: number, ink: NonNullable<ShadowRequest["ink"]>): Promise<ImageBitmap> {
  const rgba = new Uint8ClampedArray(width * height * 4);
  const [r, g, b] = ink.rgb;
  for (let i = 0, j = 0; i < lit.length; i++, j += 4) {
    rgba[j] = r;
    rgba[j + 1] = g;
    rgba[j + 2] = b;
    rgba[j + 3] = (255 - (lit[i] as number)) * ink.alpha;
  }
  return createImageBitmap(new ImageData(rgba, width, height));
}

async function handle(request: ShadowRequest): Promise<void> {
  const plan = planMosaic(request.bounds, request.zoom, request.time, { maxPixels: request.maxPixels });
  const decoded = await Promise.all(plan.tiles.map(loadTile));
  if (request.id < newestId) return;
  const mosaic = new Float32Array(plan.width * plan.height);
  plan.tiles.forEach((tile, i) => {
    const metres = decoded[i];
    if (metres) blit(mosaic, plan.width, plan.height, tile, metres);
  });
  const lit = marchLit(mosaic, plan, request.time);
  const { innerRect } = plan;
  const centreLat = latitudeAt((innerRect.y0 + innerRect.y1) / 2);
  const centreLon = ((innerRect.x0 + innerRect.x1) / 2) * 360 - 180;
  const sun = apparentSun(request.time, centreLat, centreLon);
  const image = request.ink ? await inked(lit, plan.inner.width, plan.inner.height, request.ink) : undefined;
  if (request.id < newestId) return;
  const result: ShadowResult = {
    type: "shadow",
    id: request.id,
    time: request.time,
    demZoom: plan.demZoom,
    rect: innerRect,
    width: plan.inner.width,
    height: plan.inner.height,
    lit,
    image,
    sun,
  };
  scope.postMessage(result, image ? [lit.buffer, image] : [lit.buffer]);
}

scope.onmessage = (event: MessageEvent<ShadowRequest>) => {
  const request = event.data;
  if (request?.type !== "shadow") return;
  newestId = Math.max(newestId, request.id);
  handle(request).catch((error: unknown) => {
    const response: ShadowError = {
      type: "error",
      id: request.id,
      message: error instanceof Error ? error.message : String(error),
    };
    scope.postMessage(response);
  });
};
