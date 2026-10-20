/**
 * A network store (`zarr-profile.md`, "Network store"): one track of a
 * river case, one `uint8` array `[time, feature]` in one shard whose inner
 * chunks are six frames of one GeoParquet row group's reaches, plus the
 * `time` and `baseflow` side arrays.
 *
 * It rides the grid reader's pieces rather than a second reader: the
 * range-coalescing `ZarrStore` (a frame's row groups, or a reach's time
 * chunks, asked for in one microtask go out as merged ranges), the shard
 * index parser with its CRC-32C, and the WASM `decodeChunk` that verifies
 * each Zstandard frame's checksum and replays the delta chain. What is its
 * own is the two-dimensional cut and the `xue_network` block.
 */

import { parseShardIndex, shardIndexLength, PREDICTOR_PREVIOUS, PREDICTOR_RAW, type ShardIndexEntry } from "../zarr/shard";
import type { ZarrStore } from "../zarr/store";
import type { RiverGeometry } from "./index";

export type DecodeChunkFn = (bytes: Uint8Array, frames: number, height: number, width: number, predictor: number) => Uint8Array;

export const NETWORK_VERSION = 1;
export const NETWORK_ARRAY = "streamflow";
/** Frames per inner chunk, as on a grid store. */
export const NETWORK_TIME_CHUNK = 6;

export interface NetworkCodebook {
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

export interface NetworkGroup {
  model: string;
  product: string;
  runTime: string;
  runTimeMs: number;
  frameCount: number;
  geometry: RiverGeometry;
  variable: { id: string; unit: string; quantization: NetworkCodebook };
  /** The group's consolidated array documents, when it carries them. */
  consolidated: Record<string, unknown> | null;
}

export interface NetworkLayout {
  frameCount: number;
  featureCount: number;
  timeChunk: number;
  rowGroupRows: number;
  rowGroups: number;
  timeChunks: number;
  shardFrames: number;
  chunksPerShard: number;
  delta: boolean;
  indexLocation: "start" | "end";
  fillValue: number;
}

function object(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`${what} is not an object`);
  return value as Record<string, unknown>;
}

function positiveInt(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) throw new Error(`${what} must be a positive integer`);
  return value;
}

function positiveInts(value: unknown, count: number, what: string): number[] {
  if (!Array.isArray(value) || value.length !== count) throw new Error(`${what} must list ${count} entries`);
  return value.map((item, index) => positiveInt(item, `${what}[${index}]`));
}

function finite(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${what} must be a finite number`);
  return value;
}

function names(value: unknown, what: string): { names: string[]; codecs: Record<string, unknown>[] } {
  if (!Array.isArray(value)) throw new Error(`${what} must be a list`);
  const codecs = value.map((item) => object(item, `${what} entry`));
  return { names: codecs.map((codec) => String(codec.name)), codecs };
}

function same(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

function codebook(value: unknown): NetworkCodebook {
  const q = object(value, "streamflow quantization");
  if (q.type !== "log1p") throw new Error("streamflow quantization must be log1p");
  const book: NetworkCodebook = {
    type: "log1p",
    trace: finite(q.trace, "trace"),
    scale: finite(q.scale, "scale"),
    maximum: finite(q.maximum, "maximum"),
    minimumCode: finite(q.minimumCode, "minimumCode"),
    maximumCode: finite(q.maximumCode, "maximumCode"),
    zeroCode: finite(q.zeroCode, "zeroCode"),
    overflowCode: finite(q.overflowCode, "overflowCode"),
    nodataCode: finite(q.nodataCode, "nodataCode"),
  };
  const codes = [book.zeroCode, book.minimumCode, book.maximumCode, book.overflowCode, book.nodataCode];
  if (codes.some((code) => !Number.isInteger(code) || code < 0 || code > 255)) throw new Error("codebook codes must be bytes");
  if (!(book.trace > 0 && book.scale > 0 && book.maximum > book.trace && book.maximumCode > book.minimumCode)) {
    throw new Error("codebook bounds are not increasing");
  }
  return book;
}

/** The group's `zarr.json`: a `xue_network` block of a version this reader
 * implements, and neither of the other branches' blocks beside it. */
export function parseNetworkGroup(text: string): NetworkGroup {
  const group = object(JSON.parse(text), "group metadata");
  if (group.zarr_format !== 3 || group.node_type !== "group") throw new Error("not a Zarr v3 group");
  const attributes = object(group.attributes, "group attributes");
  const blocks = ["xue", "xue_polar", "xue_network"].filter((key) => key in attributes);
  if (blocks.length !== 1 || blocks[0] !== "xue_network") throw new Error("group is not a network store");
  const block = object(attributes.xue_network, "xue_network");
  if (block.version !== NETWORK_VERSION) throw new Error(`xue_network version ${String(block.version)} is not one this reader implements`);
  const runTime = block.runTime;
  if (typeof runTime !== "string" || !Number.isFinite(Date.parse(runTime))) throw new Error("xue_network runTime is not a timestamp");
  const time = object(block.time, "xue_network time");
  const geometry = object(block.geometry, "xue_network geometry");
  const variables = block.variables;
  if (!Array.isArray(variables) || variables.length !== 1) throw new Error("xue_network lists one variable");
  const variable = object(variables[0], "xue_network variable");
  if (variable.id !== NETWORK_ARRAY) throw new Error(`xue_network variable must be ${NETWORK_ARRAY}`);
  const consolidated = group.consolidated_metadata;
  const metadata = typeof consolidated === "object" && consolidated !== null ? (consolidated as Record<string, unknown>).metadata : undefined;
  return {
    model: String(block.model ?? ""),
    product: String(block.product ?? ""),
    runTime,
    runTimeMs: Date.parse(runTime),
    frameCount: positiveInt(time.frameCount, "time.frameCount"),
    geometry: {
      path: String(geometry.path),
      byteLength: positiveInt(geometry.byteLength, "geometry.byteLength"),
      crc32: String(geometry.crc32),
      featureCount: positiveInt(geometry.featureCount, "geometry.featureCount"),
      rowGroupRows: positiveInt(geometry.rowGroupRows, "geometry.rowGroupRows"),
    },
    variable: { id: NETWORK_ARRAY, unit: String(variable.unit ?? "m3/s"), quantization: codebook(variable.quantization) },
    consolidated: typeof metadata === "object" && metadata !== null ? (metadata as Record<string, unknown>) : null,
  };
}

/** The data array's document, held to the profile: `uint8 [time,
 * feature]`, one `sharding_indexed` codec with inner chunk `[6,
 * rowGroupRows]`, inner chain `[xue.delta, bytes, zstd]` or `[bytes,
 * zstd]`, index `[bytes, crc32c]`, and a shard of whole inner chunks
 * spanning every feature. */
export function parseNetworkArrayMetadata(input: unknown): NetworkLayout {
  const array = object(input, "array metadata");
  if (array.zarr_format !== 3 || array.node_type !== "array") throw new Error("not a Zarr v3 array");
  if (array.data_type !== "uint8") throw new Error(`array data type must be uint8, not ${String(array.data_type)}`);
  const [frameCount, featureCount] = positiveInts(array.shape, 2, "array shape") as [number, number];
  const dimensions = array.dimension_names;
  if (dimensions !== undefined && !(Array.isArray(dimensions) && same(dimensions.map(String), ["time", "feature"]))) {
    throw new Error("array dimensions must be [time, feature]");
  }
  const grid = object(array.chunk_grid, "chunk grid");
  if (grid.name !== "regular") throw new Error("chunk grid must be regular");
  const [shardFrames, shardFeatures] = positiveInts(object(grid.configuration, "chunk grid configuration").chunk_shape, 2, "chunk shape") as [number, number];
  const keys = object(array.chunk_key_encoding, "chunk key encoding");
  if (keys.name !== "default") throw new Error("chunk key encoding must be default");
  const fillValue = array.fill_value;
  if (typeof fillValue !== "number" || !Number.isInteger(fillValue) || fillValue < 0 || fillValue > 255) throw new Error("fill value must be a uint8 code");
  const outer = names(array.codecs, "codecs");
  if (!same(outer.names, ["sharding_indexed"])) throw new Error("array must carry exactly one sharding_indexed codec");
  const sharding = object(outer.codecs[0]!.configuration, "sharding configuration");
  const [timeChunk, rowGroupRows] = positiveInts(sharding.chunk_shape, 2, "inner chunk shape") as [number, number];
  if (timeChunk !== NETWORK_TIME_CHUNK) throw new Error(`inner chunk must hold ${NETWORK_TIME_CHUNK} frames`);
  const inner = names(sharding.codecs, "inner codecs");
  let delta = false;
  if (same(inner.names, ["xue.delta", "bytes", "zstd"])) {
    if (object(inner.codecs[0]!.configuration ?? {}, "delta configuration").axis !== 0) throw new Error("xue.delta must run along axis 0");
    delta = true;
  } else if (!same(inner.names, ["bytes", "zstd"])) {
    throw new Error(`inner codec chain [${inner.names.join(", ")}] is not one this profile reads`);
  }
  const index = names(sharding.index_codecs, "index codecs");
  if (!same(index.names, ["bytes", "crc32c"])) throw new Error("index codecs must be [bytes, crc32c]");
  const endian = index.codecs[0]!.configuration === undefined ? "little" : object(index.codecs[0]!.configuration, "index bytes").endian;
  if (endian !== "little") throw new Error("shard index must be little-endian");
  const indexLocation = sharding.index_location ?? "end";
  if (indexLocation !== "start" && indexLocation !== "end") throw new Error(`unknown index location ${String(indexLocation)}`);
  const rowGroups = Math.ceil(featureCount / rowGroupRows);
  if (shardFrames % timeChunk !== 0 || shardFeatures !== rowGroups * rowGroupRows) {
    throw new Error("outer chunk must be whole time chunks of every row group");
  }
  // The profile writes the whole axis as one shard, `c/0/0`.
  if (shardFrames < frameCount) throw new Error("network array must be one shard over the whole axis");
  return {
    frameCount,
    featureCount,
    timeChunk,
    rowGroupRows,
    rowGroups,
    timeChunks: Math.ceil(frameCount / timeChunk),
    shardFrames,
    chunksPerShard: (shardFrames / timeChunk) * rowGroups,
    delta,
    indexLocation,
    fillValue,
  };
}

/** Every code's value in m³/s: the `log1p` codebook of `format.md`, code 0
 * a flow under the trace (read as 0), the overflow code the maximum, the
 * no-data code NaN. */
export function codebookTable(book: NetworkCodebook): Float32Array {
  const table = new Float32Array(256).fill(Number.NaN);
  const lo = Math.log1p(book.trace / book.scale);
  const hi = Math.log1p(book.maximum / book.scale);
  const span = book.maximumCode - book.minimumCode;
  for (let code = book.minimumCode; code <= book.maximumCode; code += 1) {
    const unit = (code - book.minimumCode) / span;
    table[code] = book.scale * Math.expm1(lo + unit * (hi - lo));
  }
  table[book.zeroCode] = 0;
  table[book.overflowCode] = book.maximum;
  table[book.nodataCode] = Number.NaN;
  return table;
}

/** Where the playhead falls on a track's axis: the latest frame at or
 * before it, the one after, and the fraction of the way between them.
 * Null before the first frame (nothing is drawn); past the last, the last
 * frame held. */
export function framePair(times: ArrayLike<number>, playhead: number): { index: number; next: number; weight: number } | null {
  const count = times.length;
  if (count === 0 || !(playhead >= times[0]!)) return null;
  let low = 0;
  let high = count - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (times[middle]! <= playhead) low = middle;
    else high = middle - 1;
  }
  if (low === count - 1) return { index: low, next: low, weight: 0 };
  const span = times[low + 1]! - times[low]!;
  return { index: low, next: low + 1, weight: span > 0 ? Math.min(1, (playhead - times[low]!) / span) : 0 };
}

/** Two frames' values blended per reach, linearly in m³/s. A reach with no
 * value in the earlier frame has none; one losing its value in the later
 * frame holds the earlier one. */
export function blendValues(a: Float32Array, b: Float32Array, weight: number, out: Float32Array = new Float32Array(a.length)): Float32Array {
  for (let index = 0; index < a.length; index += 1) {
    const va = a[index]!;
    const vb = b[index]!;
    out[index] = Number.isNaN(vb) || weight <= 0 ? va : va + (vb - va) * weight;
  }
  return out;
}

/** A store opened for reading. */
export class NetworkStore {
  /** Each frame's valid time, epoch milliseconds. */
  readonly times: Float64Array;
  /** Each reach's usual flow before the event, m³/s; NaN where unknown. */
  readonly baseflow: Float32Array;
  readonly table: Float32Array;
  private index: Promise<(ShardIndexEntry | null)[]> | null = null;
  /** Decoded inner chunks by `timeChunk:rowGroup`; a track is a few
   * hundred kilobytes decoded, so nothing is evicted. */
  private readonly chunks = new Map<string, Promise<Uint8Array | null>>();

  private constructor(
    private readonly store: ZarrStore,
    private readonly decodeChunk: DecodeChunkFn,
    readonly group: NetworkGroup,
    readonly layout: NetworkLayout,
    times: Float64Array,
    baseflow: Float32Array,
  ) {
    this.times = times;
    this.baseflow = baseflow;
    this.table = codebookTable(group.variable.quantization);
  }

  /** Open a store: the group, the array (from the consolidated metadata
   * when the group carries it), the two side arrays. `geometry`, when
   * given, is the index's, which every store of a case must repeat. */
  static async open(store: ZarrStore, decodeChunk: DecodeChunkFn, geometry?: RiverGeometry): Promise<NetworkStore> {
    const group = parseNetworkGroup(new TextDecoder().decode(await store.get("zarr.json")));
    if (geometry) {
      const own = group.geometry;
      if (
        own.path !== geometry.path ||
        own.byteLength !== geometry.byteLength ||
        own.crc32 !== geometry.crc32 ||
        own.featureCount !== geometry.featureCount ||
        own.rowGroupRows !== geometry.rowGroupRows
      ) {
        throw new Error("network store names a different GeoParquet from the river index");
      }
    }
    const arrayDocument = async (name: string): Promise<unknown> =>
      group.consolidated?.[name] ?? JSON.parse(new TextDecoder().decode(await store.get(`${name}/zarr.json`)));
    const layout = parseNetworkArrayMetadata(await arrayDocument(NETWORK_ARRAY));
    if (layout.frameCount !== group.frameCount || layout.featureCount !== group.geometry.featureCount || layout.rowGroupRows !== group.geometry.rowGroupRows) {
      throw new Error("network array shape disagrees with its group");
    }
    if (layout.fillValue !== group.variable.quantization.nodataCode) throw new Error("network array fill value must be the no-data code");
    const [timeBytes, baseBytes] = await Promise.all([store.get("time/c/0"), store.get("baseflow/c/0")]);
    if (timeBytes.byteLength !== layout.frameCount * 8) throw new Error("time side array is not one int64 per frame");
    if (baseBytes.byteLength !== layout.featureCount * 4) throw new Error("baseflow side array is not one float32 per reach");
    const timeView = new DataView(timeBytes.buffer, timeBytes.byteOffset, timeBytes.byteLength);
    const times = new Float64Array(layout.frameCount);
    for (let frame = 0; frame < layout.frameCount; frame += 1) {
      times[frame] = Number(timeView.getBigInt64(frame * 8, true)) * 1000;
      if (frame > 0 && times[frame]! <= times[frame - 1]!) throw new Error("time side array is not increasing");
    }
    const baseView = new DataView(baseBytes.buffer, baseBytes.byteOffset, baseBytes.byteLength);
    const baseflow = new Float32Array(layout.featureCount);
    for (let feature = 0; feature < layout.featureCount; feature += 1) baseflow[feature] = baseView.getFloat32(feature * 4, true);
    return new NetworkStore(store, decodeChunk, group, layout, times, baseflow);
  }

  /** The shard index, read once (`docs/zarr-profile.md`, "Reading" 4). */
  private shardIndex(shard: number): Promise<(ShardIndexEntry | null)[]> {
    if (shard !== 0) throw new Error("network store holds one shard");
    if (!this.index) {
      const length = shardIndexLength(this.layout.chunksPerShard);
      const path = `${NETWORK_ARRAY}/c/0/0`;
      const range = this.layout.indexLocation === "end" ? { suffixLength: length } : { offset: 0, length };
      const promise = this.store.getRange(path, range).then((bytes) => parseShardIndex(bytes, this.layout.chunksPerShard));
      promise.catch(() => {
        if (this.index === promise) this.index = null;
      });
      this.index = promise;
    }
    return this.index;
  }

  /** The inner chunks `timeChunk × groups`, decoded: every one not yet held
   * is asked for in the same microtask, so the store merges them. */
  private async chunksFor(pairs: readonly [number, number][]): Promise<(Uint8Array | null)[]> {
    const entries = await this.shardIndex(0);
    const path = `${NETWORK_ARRAY}/c/0/0`;
    const { layout } = this;
    return Promise.all(
      pairs.map(([timeChunk, group]) => {
        const key = `${timeChunk}:${group}`;
        let held = this.chunks.get(key);
        if (!held) {
          const position = timeChunk * layout.rowGroups + group;
          const entry = entries[position];
          if (entry === undefined) throw new Error(`no index entry for chunk ${key}`);
          held =
            entry === null
              ? Promise.resolve(null)
              : this.store
                  .getRange(path, entry)
                  .then((bytes) =>
                    this.decodeChunk(bytes, layout.timeChunk, 1, layout.rowGroupRows, layout.delta ? PREDICTOR_PREVIOUS : PREDICTOR_RAW),
                  );
          held.catch(() => this.chunks.delete(key));
          this.chunks.set(key, held);
        }
        return held;
      }),
    );
  }

  /** One frame's codes over every reach. `groups` limits the read to those
   * row groups (the view's); the others are the no-data code. */
  async readFrame(frame: number, groups: readonly number[] | null = null): Promise<Uint8Array> {
    const { layout } = this;
    if (!Number.isInteger(frame) || frame < 0 || frame >= layout.frameCount) throw new Error(`frame ${frame} is off the axis`);
    const timeChunk = Math.floor(frame / layout.timeChunk);
    const within = frame % layout.timeChunk;
    const wanted = groups ?? Array.from({ length: layout.rowGroups }, (_, group) => group);
    const chunks = await this.chunksFor(wanted.map((group) => [timeChunk, group] as [number, number]));
    const codes = new Uint8Array(layout.featureCount).fill(layout.fillValue);
    wanted.forEach((group, position) => {
      const chunk = chunks[position];
      if (!chunk) return;
      const start = group * layout.rowGroupRows;
      const rows = Math.min(layout.rowGroupRows, layout.featureCount - start);
      codes.set(chunk.subarray(within * layout.rowGroupRows, within * layout.rowGroupRows + rows), start);
    });
    return codes;
  }

  /** One frame's values in m³/s (NaN for no value). */
  async readFrameValues(frame: number, groups: readonly number[] | null = null): Promise<Float32Array> {
    const codes = await this.readFrame(frame, groups);
    const values = new Float32Array(codes.length);
    for (let index = 0; index < codes.length; index += 1) values[index] = this.table[codes[index]!]!;
    return values;
  }

  /** One reach's codes over the whole axis: one chunk per time chunk. */
  async readSeries(feature: number): Promise<Uint8Array> {
    const { layout } = this;
    if (!Number.isInteger(feature) || feature < 0 || feature >= layout.featureCount) throw new Error(`feature ${feature} is not on the network`);
    const group = Math.floor(feature / layout.rowGroupRows);
    const column = feature % layout.rowGroupRows;
    const chunks = await this.chunksFor(Array.from({ length: layout.timeChunks }, (_, timeChunk) => [timeChunk, group] as [number, number]));
    const codes = new Uint8Array(layout.frameCount).fill(layout.fillValue);
    for (let frame = 0; frame < layout.frameCount; frame += 1) {
      const chunk = chunks[Math.floor(frame / layout.timeChunk)];
      if (chunk) codes[frame] = chunk[(frame % layout.timeChunk) * layout.rowGroupRows + column]!;
    }
    return codes;
  }

  /** One reach's values over the axis, m³/s, NaN for no value. */
  async readSeriesValues(feature: number): Promise<Float32Array> {
    const codes = await this.readSeries(feature);
    return Float32Array.from(codes, (code) => this.table[code]!);
  }
}
