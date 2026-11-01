/** A reader for PMTiles v3 archives, served to MapLibre as the `pmtiles://`
 * protocol: the warnings' boundary sets (`docs/alerts.md` §9) are one such
 * file each, read tile by tile with HTTP Range requests.
 *
 * Small on purpose — header, directories, the Hilbert tile id, gzip — and
 * no more of the format than the boundary sets use: directories and tiles
 * compressed with gzip or not at all. An archive in another compression is
 * refused with an error naming it. The archive is immutable (`?v=<crc32>`),
 * so its header and directories are cached for the page's life.
 *
 * `pmtiles://<archive url>` answers the TileJSON a vector source asks for
 * first; `pmtiles://<archive url>/{z}/{x}/{y}` one tile, or an empty one
 * where the archive has none. */

import { addProtocol } from "maplibre-gl";
import type { GetResourceResponse, RequestParameters } from "maplibre-gl";

import { fetchImmutable } from "../fetchimmutable";

export const PMTILES_PROTOCOL = "pmtiles";

const HEADER_BYTES = 127;
/** The first read: the header and, in a well-formed archive, the root
 * directory right behind it. */
const INITIAL_BYTES = 16384;

const COMPRESSION_NONE = 1;
const COMPRESSION_GZIP = 2;

export interface PmtilesHeader {
  rootDirectoryOffset: number;
  rootDirectoryLength: number;
  leafDirectoryOffset: number;
  tileDataOffset: number;
  internalCompression: number;
  tileCompression: number;
  tileType: number;
  minZoom: number;
  maxZoom: number;
  /** `[west, south, east, north]`, degrees. */
  bounds: [number, number, number, number];
}

export interface PmtilesEntry {
  tileId: number;
  offset: number;
  length: number;
  /** 0 marks a leaf directory. */
  runLength: number;
}

function uint64(view: DataView, at: number): number {
  return view.getUint32(at + 4, true) * 2 ** 32 + view.getUint32(at, true);
}

export function parseHeader(bytes: Uint8Array): PmtilesHeader {
  if (bytes.length < HEADER_BYTES) throw new Error("pmtiles: archive shorter than its header");
  const magic = new TextDecoder().decode(bytes.subarray(0, 7));
  if (magic !== "PMTiles") throw new Error("pmtiles: not a PMTiles archive");
  if (bytes[7] !== 3) throw new Error(`pmtiles: unsupported version ${bytes[7]}`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return {
    rootDirectoryOffset: uint64(view, 8),
    rootDirectoryLength: uint64(view, 16),
    leafDirectoryOffset: uint64(view, 40),
    tileDataOffset: uint64(view, 56),
    internalCompression: view.getUint8(97),
    tileCompression: view.getUint8(98),
    tileType: view.getUint8(99),
    minZoom: view.getUint8(100),
    maxZoom: view.getUint8(101),
    bounds: [
      view.getInt32(102, true) / 1e7,
      view.getInt32(106, true) / 1e7,
      view.getInt32(110, true) / 1e7,
      view.getInt32(114, true) / 1e7,
    ],
  };
}

/** An unsigned LEB128 varint, without 32-bit shifts so ids past 2³¹ stay
 * exact. */
function readVarint(bytes: Uint8Array, cursor: { at: number }): number {
  let value = 0;
  let scale = 1;
  for (;;) {
    if (cursor.at >= bytes.length) throw new Error("pmtiles: directory ends inside a varint");
    const byte = bytes[cursor.at++]!;
    value += (byte & 0x7f) * scale;
    if (byte < 0x80) return value;
    scale *= 128;
  }
}

/** A decompressed directory: the count, then the delta-coded tile ids, the
 * run lengths, the lengths and the offsets (0 meaning "right after the
 * previous entry"), each a column of varints. */
export function parseDirectory(bytes: Uint8Array): PmtilesEntry[] {
  const cursor = { at: 0 };
  const count = readVarint(bytes, cursor);
  const entries: PmtilesEntry[] = [];
  let tileId = 0;
  for (let i = 0; i < count; i += 1) {
    tileId += readVarint(bytes, cursor);
    entries.push({ tileId, offset: 0, length: 0, runLength: 1 });
  }
  for (const entry of entries) entry.runLength = readVarint(bytes, cursor);
  for (const entry of entries) entry.length = readVarint(bytes, cursor);
  entries.forEach((entry, i) => {
    const value = readVarint(bytes, cursor);
    const previous = entries[i - 1];
    entry.offset = value === 0 && previous ? previous.offset + previous.length : value - 1;
  });
  return entries;
}

/** The tile id of z/x/y: every tile of the lower zooms, then the tile's
 * position along the Hilbert curve at its own zoom. */
export function zxyToTileId(z: number, x: number, y: number): number {
  if (z > 26) throw new Error("pmtiles: zoom past 26");
  const n = 2 ** z;
  if (x < 0 || y < 0 || x >= n || y >= n) throw new Error("pmtiles: tile outside its zoom");
  let base = 0;
  for (let level = 0; level < z; level += 1) base += 4 ** level;
  let tx = x;
  let ty = y;
  let d = 0;
  for (let s = n / 2; s >= 1; s /= 2) {
    const rx = (tx & s) > 0 ? 1 : 0;
    const ry = (ty & s) > 0 ? 1 : 0;
    d += s * s * ((3 * rx) ^ ry);
    if (ry === 0) {
      if (rx === 1) {
        tx = n - 1 - tx;
        ty = n - 1 - ty;
      }
      [tx, ty] = [ty, tx];
    }
  }
  return base + d;
}

/** The entry covering a tile id: an exact one, a run that reaches it, or
 * the leaf directory it falls in. */
export function findEntry(entries: readonly PmtilesEntry[], tileId: number): PmtilesEntry | null {
  let low = 0;
  let high = entries.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const difference = tileId - entries[middle]!.tileId;
    if (difference > 0) low = middle + 1;
    else if (difference < 0) high = middle - 1;
    else return entries[middle]!;
  }
  if (high >= 0) {
    const entry = entries[high]!;
    if (entry.runLength === 0 || tileId - entry.tileId < entry.runLength) return entry;
  }
  return null;
}

async function decompress(bytes: Uint8Array, compression: number): Promise<Uint8Array> {
  if (compression === COMPRESSION_NONE || compression === 0) return bytes;
  if (compression !== COMPRESSION_GZIP)
    throw new Error(`pmtiles: compression ${compression} is not supported`);
  const stream = new Blob([bytes as Uint8Array<ArrayBuffer>]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function readRange(url: string, offset: number, length: number, signal?: AbortSignal): Promise<Uint8Array> {
  const response = await fetchImmutable(url, {
    headers: { Range: `bytes=${offset}-${offset + length - 1}` },
    signal,
  });
  if (!response.ok) throw new Error(`pmtiles: range request failed: ${response.status}`);
  const body = new Uint8Array(await response.arrayBuffer());
  // A server that ignores Range sends the whole archive.
  if (response.status === 200 && body.length > length) return body.subarray(offset, offset + length);
  return body;
}

/** One archive: its header and the directories read so far. */
class Archive {
  private readonly directories = new Map<string, Promise<PmtilesEntry[]>>();
  readonly header: Promise<PmtilesHeader>;
  private initial: Promise<Uint8Array>;

  constructor(readonly url: string) {
    this.initial = readRange(url, 0, INITIAL_BYTES);
    this.header = this.initial.then(parseHeader);
    // A failed first read is not remembered: the next request retries.
    this.header.catch(() => archives.delete(url));
  }

  private directory(offset: number, length: number, header: PmtilesHeader): Promise<PmtilesEntry[]> {
    const key = `${offset}:${length}`;
    let found = this.directories.get(key);
    if (!found) {
      found = (async () => {
        const initial = await this.initial;
        const raw =
          offset + length <= initial.length
            ? initial.subarray(offset, offset + length)
            : await readRange(this.url, offset, length);
        return parseDirectory(await decompress(raw, header.internalCompression));
      })();
      found.catch(() => this.directories.delete(key));
      this.directories.set(key, found);
    }
    return found;
  }

  async tile(z: number, x: number, y: number, signal?: AbortSignal): Promise<Uint8Array | null> {
    const header = await this.header;
    const tileId = zxyToTileId(z, x, y);
    let offset = header.rootDirectoryOffset;
    let length = header.rootDirectoryLength;
    for (let depth = 0; depth < 4; depth += 1) {
      const entry = findEntry(await this.directory(offset, length, header), tileId);
      if (!entry) return null;
      if (entry.runLength > 0) {
        const raw = await readRange(this.url, header.tileDataOffset + entry.offset, entry.length, signal);
        return decompress(raw, header.tileCompression);
      }
      offset = header.leafDirectoryOffset + entry.offset;
      length = entry.length;
    }
    throw new Error("pmtiles: directories nest too deep");
  }
}

const archives = new Map<string, Archive>();

function archive(url: string): Archive {
  let found = archives.get(url);
  if (!found) {
    found = new Archive(url);
    archives.set(url, found);
  }
  return found;
}

const TILE_ADDRESS = new RegExp(`^${PMTILES_PROTOCOL}://(.+)/(\\d+)/(\\d+)/(\\d+)$`);
const ARCHIVE_ADDRESS = new RegExp(`^${PMTILES_PROTOCOL}://(.+)$`);

async function load(
  params: RequestParameters,
  abortController: AbortController,
): Promise<GetResourceResponse<ArrayBuffer | object>> {
  const tile = TILE_ADDRESS.exec(params.url);
  if (tile) {
    const data = await archive(tile[1]!).tile(
      Number(tile[2]),
      Number(tile[3]),
      Number(tile[4]),
      abortController.signal,
    );
    // MapLibre's tile parser takes an ArrayBuffer of its own.
    return { data: data ? data.slice().buffer : new ArrayBuffer(0) };
  }
  const match = ARCHIVE_ADDRESS.exec(params.url);
  if (!match) throw new Error(`pmtiles: not an archive address: ${params.url}`);
  const url = match[1]!;
  const header = await archive(url).header;
  return {
    data: {
      tilejson: "3.0.0",
      scheme: "xyz",
      tiles: [`${PMTILES_PROTOCOL}://${url}/{z}/{x}/{y}`],
      minzoom: header.minZoom,
      maxzoom: header.maxZoom,
      bounds: header.bounds,
    },
  };
}

let registered = false;

/** Register the protocol once; every boundary set goes through it. */
export function registerPmtilesProtocol(): void {
  if (registered) return;
  registered = true;
  addProtocol(PMTILES_PROTOCOL, load);
}

/** The source URL a vector source takes for an archive. */
export function pmtilesSourceUrl(archiveUrl: string): string {
  return `${PMTILES_PROTOCOL}://${archiveUrl}`;
}
