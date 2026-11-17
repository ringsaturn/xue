/**
 * A network store (`zarr-profile.md`, "Network store"): one track of a
 * river case, one `uint8` array `[time, feature]` in one shard whose inner
 * chunks are six frames of one GeoParquet row group's reaches, plus the
 * `time` and `baseflow` side arrays.
 *
 * It rides the grid reader's pieces rather than a second reader: the
 * range-coalescing `ZarrStore` (a frame's row groups, or a reach's time
 * chunks, asked for in one microtask go out as merged ranges) and the WASM
 * module's shard index parser, with its CRC-32C, and `decodeChunk`, which
 * verifies each Zstandard frame's checksum and replays the delta chain. The
 * data array's document is read by the WASM module as well
 * (`xue::network`), against the group. What is this file's own is the
 * `xue_network` block as the shell uses it and the two-dimensional read.
 */

import { checkNetworkGroup } from "../schema/generated/network-group";
import type { ZarrStore } from "../zarr/store";
import type * as Wasm from "../wasm/xue";
import type { RiverGeometry } from "./index";

/** Predictor codes as `decodeChunk` takes them — the container's own
 * numbering (docs/format.md). */
const PREDICTOR_RAW = 0;
const PREDICTOR_PREVIOUS = 2;

/** What a network store needs of the WASM module. */
export type NetworkCodec = Pick<typeof Wasm, "decodeChunk" | "parseNetworkArray" | "parseShardIndex" | "shardIndexLength">;

/** One inner chunk's byte span within the shard. */
interface ShardIndexEntry {
  offset: number;
  length: number;
}

/** The WASM parser's flat `[offset, length, ...]` as entries, `null` for a
 * chunk the shard never held (the pair `[-1, -1]`). */
function indexEntries(flat: Float64Array): (ShardIndexEntry | null)[] {
  const entries: (ShardIndexEntry | null)[] = [];
  for (let at = 0; at + 1 < flat.length; at += 2) {
    entries.push(flat[at]! < 0 ? null : { offset: flat[at]!, length: flat[at + 1]! });
  }
  return entries;
}

export const NETWORK_VERSION = 1;
export const NETWORK_ARRAY = "streamflow";

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
  /** Inner chunks in the one shard, and pairs in its index. */
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

function finite(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${what} must be a finite number`);
  return value;
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
  // docs/schemas/network-group.schema.json over the group this reads; the
  // consolidated arrays are each read, and held, where they are opened.
  checkNetworkGroup({ zarr_format: group.zarr_format, node_type: group.node_type, attributes: { xue_network: block } }, "group metadata");
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

/** The data array's layout, read by the WASM module against the group:
 * `uint8 [time, feature]` of the group's frame and reach counts, one
 * `sharding_indexed` codec with inner chunk `[6, rowGroupRows]`, inner chain
 * `[xue.delta, bytes, zstd]` or `[bytes, zstd]`, index `[bytes, crc32c]`,
 * one shard of the whole array in whole inner chunks, the codebook's
 * no-data code as its fill value, and the group's variable repeated in its
 * attributes. `arrayText` is the store's `streamflow/zarr.json`; without it
 * the document is the one the group carries in its consolidated metadata,
 * taken there by the reader so that its numbers stay as written. */
export function readNetworkArray(codec: NetworkCodec, groupText: string, arrayText?: string): NetworkLayout {
  const layout = codec.parseNetworkArray(groupText, arrayText);
  try {
    return {
      frameCount: layout.frameCount,
      featureCount: layout.featureCount,
      timeChunk: layout.timeChunk,
      rowGroupRows: layout.rowGroupRows,
      rowGroups: layout.rowGroups,
      timeChunks: layout.timeChunks,
      chunksPerShard: layout.chunksPerShard,
      delta: layout.delta,
      indexLocation: layout.indexAtEnd ? "end" : "start",
      fillValue: layout.fillValue,
    };
  } finally {
    layout.free();
  }
}

/** Every code's value in m³/s: the `log1p` codebook of `bundle.md`, code 0
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
    private readonly codec: NetworkCodec,
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
  static async open(store: ZarrStore, codec: NetworkCodec, geometry?: RiverGeometry): Promise<NetworkStore> {
    const groupText = new TextDecoder().decode(await store.get("zarr.json"));
    const group = parseNetworkGroup(groupText);
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
    // The group's own copy of the array document when it carries one, which
    // saves the request; else the store's.
    const arrayText =
      group.consolidated?.[NETWORK_ARRAY] !== undefined ? undefined : new TextDecoder().decode(await store.get(`${NETWORK_ARRAY}/zarr.json`));
    const layout = readNetworkArray(codec, groupText, arrayText);
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
    return new NetworkStore(store, codec, group, layout, times, baseflow);
  }

  /** The shard index, read once (`docs/zarr-profile.md`, "Reading" 4). */
  private shardIndex(shard: number): Promise<(ShardIndexEntry | null)[]> {
    if (shard !== 0) throw new Error("network store holds one shard");
    if (!this.index) {
      const length = this.codec.shardIndexLength(this.layout.chunksPerShard);
      const path = `${NETWORK_ARRAY}/c/0/0`;
      const range = this.layout.indexLocation === "end" ? { suffixLength: length } : { offset: 0, length };
      const promise = this.store.getRange(path, range).then((bytes) => indexEntries(this.codec.parseShardIndex(bytes, this.layout.chunksPerShard)));
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
                    this.codec.decodeChunk(bytes, layout.timeChunk, 1, layout.rowGroupRows, layout.delta ? PREDICTOR_PREVIOUS : PREDICTOR_RAW),
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
