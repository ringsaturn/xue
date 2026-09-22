/**
 * Mapterhorn's Terrarium relief, sampled at a point.
 *
 * The map draws this source as a hillshade (`main.ts::buildBasemapStyle`); the
 * pin reads it here, one tile at a time, so the DEM's own elevation can stand
 * beside the model's orography — the two numbers a terrain correction is the
 * difference of.
 *
 * The tiles are the 512 px WebP the style already fetches and they answer with
 * `access-control-allow-origin: *`, so a plain `fetch` decodes one into an
 * `ImageBitmap` with no origin to taint a canvas and no proxy in the way.
 *
 * A sample is the value of the pixel that holds the point, not an
 * interpolation of its neighbours: the model's orography is read the same way,
 * at the one cell that holds the point, and two elevations meant to be
 * compared should be read alike.
 */

/** The relief source the basemap style builds, and its tiles. 30 m global to
 * z12; the finer archives Mapterhorn publishes are regional, so the readout
 * samples the ceiling that covers everywhere. */
export const TERRAIN_SOURCE = "mapterhorn";
const TERRAIN_TILE_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp";
export const TERRAIN_TILES = [TERRAIN_TILE_URL];
export const TERRAIN_MAX_ZOOM = 12;
export const TERRAIN_ATTRIBUTION = "<a href='https://mapterhorn.com/attribution/'>© Mapterhorn</a>";

/** Terrarium tiles are 512 px square and carry no `tileSize` of their own for
 * a raster-dem source to read: MapLibre assumes this one. */
export const TERRAIN_TILE_SIZE = 512;

/** Terrarium packs metres into three bytes: red is the high byte of metres
 * plus 32768, green the low byte, and blue a 1/256 m fraction. */
export function terrariumElevation(red: number, green: number, blue: number): number {
  return red * 256 + green + blue / 256 - 32768;
}

/** One tile and the pixel inside it that holds a point. */
export interface DemSample {
  zoom: number;
  x: number;
  y: number;
  pixelX: number;
  pixelY: number;
}

export function terrainTileUrl(zoom: number, x: number, y: number): string {
  return TERRAIN_TILE_URL.replace("{z}", String(zoom))
    .replace("{x}", String(x))
    .replace("{y}", String(y));
}

/** The tile holding a point at `zoom`, and the pixel within it, in the Web
 * Mercator scheme the map and the tiles share. A point past the poles is
 * clamped onto the edge tile rather than left with no tile at all. */
export function demSampleFor(latitude: number, longitude: number, zoom: number): DemSample {
  const tiles = 2 ** zoom;
  const worldX = ((longitude + 180) / 360) * tiles;
  const worldY = ((1 - Math.asinh(Math.tan((latitude * Math.PI) / 180)) / Math.PI) / 2) * tiles;
  const x = Math.min(tiles - 1, Math.max(0, Math.floor(worldX)));
  const y = Math.min(tiles - 1, Math.max(0, Math.floor(worldY)));
  return {
    zoom,
    x,
    y,
    pixelX: Math.min(TERRAIN_TILE_SIZE - 1, Math.max(0, Math.floor((worldX - x) * TERRAIN_TILE_SIZE))),
    pixelY: Math.min(TERRAIN_TILE_SIZE - 1, Math.max(0, Math.floor((worldY - y) * TERRAIN_TILE_SIZE))),
  };
}

/** The elevation one pixel of a decoded RGBA tile carries. Split out from the
 * canvas the pixel is read off so the arithmetic is testable without one. */
export function elevationFromRgba(
  data: Uint8ClampedArray | Uint8Array,
  width: number,
  pixelX: number,
  pixelY: number,
): number {
  const at = (pixelY * width + pixelX) * 4;
  return terrariumElevation(data[at] ?? 0, data[at + 1] ?? 0, data[at + 2] ?? 0);
}

/** Decoded tiles by `z/x/y`. A pin's neighbourhood is a handful of tiles and a
 * session that pans far should not hold the world, so the map is bounded and
 * drops the oldest key — insertion order, which is all a `Map` needs here. */
const decodedTiles = new Map<string, Promise<ImageBitmap | null>>();
const MAX_DECODED_TILES = 48;

let sampleCanvas: HTMLCanvasElement | null = null;

async function loadTile(sample: DemSample): Promise<ImageBitmap | null> {
  const key = `${sample.zoom}/${sample.x}/${sample.y}`;
  const cached = decodedTiles.get(key);
  if (cached) return cached;
  const pending = (async (): Promise<ImageBitmap | null> => {
    try {
      const response = await fetch(terrainTileUrl(sample.zoom, sample.x, sample.y));
      if (!response.ok) return null;
      return await createImageBitmap(await response.blob());
    } catch {
      // A tile off the archive's coverage, or the network down: the readout
      // simply has no DEM number, the way a run with no orography has none.
      return null;
    }
  })();
  decodedTiles.set(key, pending);
  if (decodedTiles.size > MAX_DECODED_TILES) {
    const oldest = decodedTiles.keys().next().value;
    if (oldest !== undefined) decodedTiles.delete(oldest);
  }
  return pending;
}

/** The DEM's elevation under a point, in metres, or null where the tile does
 * not answer. */
export async function demElevationAt(latitude: number, longitude: number): Promise<number | null> {
  const sample = demSampleFor(latitude, longitude, TERRAIN_MAX_ZOOM);
  const bitmap = await loadTile(sample);
  if (!bitmap) return null;
  if (!sampleCanvas) sampleCanvas = document.createElement("canvas");
  sampleCanvas.width = 1;
  sampleCanvas.height = 1;
  const context = sampleCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  // Shift the tile so the wanted pixel lands in the 1x1 canvas.
  context.drawImage(bitmap, -sample.pixelX, -sample.pixelY);
  const pixel = context.getImageData(0, 0, 1, 1).data;
  return terrariumElevation(pixel[0] ?? 0, pixel[1] ?? 0, pixel[2] ?? 0);
}
