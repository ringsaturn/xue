/**
 * Synthetic river-case fixtures for the web tests: a GeoParquet of reaches
 * cut into row groups, and a network store over it (docs/zarr-profile.md,
 * "Network store"). Both are written here byte by byte — a thrift-compact
 * Parquet footer with PLAIN, uncompressed pages, and a sharded Zarr array
 * with the xue.delta chain under Node's own zstd — so the tests need no
 * Python, no pyarrow and no network. See README.md beside this file.
 *
 * Imported by tests/web/river-parquet.test.ts and river-store.test.ts, and
 * runnable on its own (Node 22.15 or later, which strips the types and has
 * zlib zstd) to look at the bytes:
 *
 *     node tests/web/fixtures/river.ts <output directory>
 *
 * Only Node built-ins are imported, so the file runs without a bundler.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { constants, zstdCompressSync } from "node:zlib";

// -- checksums -----------------------------------------------------------------

function table(polynomial: number): Uint32Array {
  const out = new Uint32Array(256);
  for (let byte = 0; byte < 256; byte += 1) {
    let value = byte;
    for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? (value >>> 1) ^ polynomial : value >>> 1;
    out[byte] = value >>> 0;
  }
  return out;
}
const CRC32_TABLE = table(0xedb88320);
const CRC32C_TABLE = table(0x82f63b78);

function crc(bytes: Uint8Array, lookup: Uint32Array): number {
  let value = 0xffffffff;
  for (const byte of bytes) value = lookup[(value ^ byte) & 0xff]! ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

export function crc32Hex(bytes: Uint8Array): string {
  return crc(bytes, CRC32_TABLE).toString(16).padStart(8, "0");
}

function concat(parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.byteLength;
  }
  return out;
}

const encoder = new TextEncoder();

// -- WKB -----------------------------------------------------------------------

export function wkbLineString(coordinates: readonly number[]): Uint8Array {
  const points = coordinates.length / 2;
  const bytes = new Uint8Array(9 + points * 16);
  const view = new DataView(bytes.buffer);
  view.setUint8(0, 1);
  view.setUint32(1, 2, true);
  view.setUint32(5, points, true);
  coordinates.forEach((value, index) => view.setFloat64(9 + index * 8, value, true));
  return bytes;
}

// -- thrift compact ------------------------------------------------------------

const T = { I32: 5, I64: 6, BINARY: 8, LIST: 9, STRUCT: 12 } as const;

type Field =
  | { id: number; type: "i32" | "i64"; value: number }
  | { id: number; type: "binary"; value: Uint8Array | string }
  | { id: number; type: "struct"; value: Field[] }
  | { id: number; type: "list"; element: "i32" | "binary" | "struct"; value: (number | string | Field[])[] };

class Writer {
  bytes: number[] = [];
  varint(value: bigint): void {
    let rest = value;
    while (rest >= 0x80n) {
      this.bytes.push(Number(rest & 0x7fn) | 0x80);
      rest >>= 7n;
    }
    this.bytes.push(Number(rest));
  }
  zigzag(value: number): void {
    const big = BigInt(value);
    this.varint(big >= 0n ? big << 1n : ((-big) << 1n) - 1n);
  }
  binary(value: Uint8Array | string): void {
    const raw = typeof value === "string" ? encoder.encode(value) : value;
    this.varint(BigInt(raw.byteLength));
    for (const byte of raw) this.bytes.push(byte);
  }
  struct(fields: readonly Field[]): void {
    let last = 0;
    for (const field of [...fields].sort((a, b) => a.id - b.id)) {
      const type =
        field.type === "i32" ? T.I32 : field.type === "i64" ? T.I64 : field.type === "binary" ? T.BINARY : field.type === "list" ? T.LIST : T.STRUCT;
      const delta = field.id - last;
      if (delta > 0 && delta <= 15) this.bytes.push((delta << 4) | type);
      else {
        this.bytes.push(type);
        this.zigzag(field.id);
      }
      last = field.id;
      this.value(field);
    }
    this.bytes.push(0);
  }
  private value(field: Field): void {
    if (field.type === "i32" || field.type === "i64") this.zigzag(field.value);
    else if (field.type === "binary") this.binary(field.value);
    else if (field.type === "struct") this.struct(field.value);
    else if (field.type === "list") {
      const element = field.element === "i32" ? T.I32 : field.element === "binary" ? T.BINARY : T.STRUCT;
      const size = field.value.length;
      if (size < 15) this.bytes.push((size << 4) | element);
      else {
        this.bytes.push(0xf0 | element);
        this.varint(BigInt(size));
      }
      for (const item of field.value) {
        if (field.element === "i32") this.zigzag(item as number);
        else if (field.element === "binary") this.binary(item as string);
        else this.struct(item as Field[]);
      }
    }
  }
}

function thrift(fields: readonly Field[]): Uint8Array {
  const writer = new Writer();
  writer.struct(fields);
  return Uint8Array.from(writer.bytes);
}

// -- GeoParquet ----------------------------------------------------------------

const PARQUET = { INT64: 2, FLOAT: 4, BYTE_ARRAY: 6, REQUIRED: 0, PLAIN: 0, RLE: 3, UNCOMPRESSED: 0, DATA_PAGE: 0 } as const;

export interface ReachFixture {
  /** Interleaved `[lon, lat, …]`. */
  line: number[];
}

function f32(value: number): Uint8Array {
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setFloat32(0, value, true);
  return bytes;
}

/** A GeoParquet of the reaches, `rowGroupRows` rows per row group: columns
 * `feature_id` (INT64), `geometry` (WKB), and the `bbox` covering struct of
 * four FLOATs with min/max statistics per row group. */
export function buildReachesParquet(reaches: readonly ReachFixture[], rowGroupRows: number): Uint8Array {
  const parts: Uint8Array[] = [encoder.encode("PAR1")];
  let offset = 4;
  const rowGroups: Field[][] = [];
  for (let start = 0; start < reaches.length; start += rowGroupRows) {
    const rows = reaches.slice(start, start + rowGroupRows);
    const boxes = rows.map(({ line }) => {
      const xs = line.filter((_, index) => index % 2 === 0);
      const ys = line.filter((_, index) => index % 2 === 1);
      return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
    });
    const columns: { path: string[]; type: number; values: Uint8Array; min?: Uint8Array; max?: Uint8Array }[] = [];
    const ids = new Uint8Array(rows.length * 8);
    rows.forEach((_, index) => new DataView(ids.buffer).setBigInt64(index * 8, BigInt(900_000 + start + index), true));
    columns.push({ path: ["feature_id"], type: PARQUET.INT64, values: ids });
    const geometry = concat(
      rows.flatMap(({ line }) => {
        const wkb = wkbLineString(line);
        const length = new Uint8Array(4);
        new DataView(length.buffer).setUint32(0, wkb.byteLength, true);
        return [length, wkb];
      }),
    );
    columns.push({ path: ["geometry"], type: PARQUET.BYTE_ARRAY, values: geometry });
    ["xmin", "ymin", "xmax", "ymax"].forEach((name, axis) => {
      const values = boxes.map((box) => box[axis]!);
      columns.push({
        path: ["bbox", name],
        type: PARQUET.FLOAT,
        values: concat(values.map(f32)),
        min: f32(Math.min(...values)),
        max: f32(Math.max(...values)),
      });
    });
    const chunks: Field[][] = [];
    let groupBytes = 0;
    for (const column of columns) {
      const header = thrift([
        { id: 1, type: "i32", value: PARQUET.DATA_PAGE },
        { id: 2, type: "i32", value: column.values.byteLength },
        { id: 3, type: "i32", value: column.values.byteLength },
        {
          id: 5,
          type: "struct",
          value: [
            { id: 1, type: "i32", value: rows.length },
            { id: 2, type: "i32", value: PARQUET.PLAIN },
            { id: 3, type: "i32", value: PARQUET.RLE },
            { id: 4, type: "i32", value: PARQUET.RLE },
          ],
        },
      ]);
      const size = header.byteLength + column.values.byteLength;
      const meta: Field[] = [
        { id: 1, type: "i32", value: column.type },
        { id: 2, type: "list", element: "i32", value: [PARQUET.PLAIN] },
        { id: 3, type: "list", element: "binary", value: column.path },
        { id: 4, type: "i32", value: PARQUET.UNCOMPRESSED },
        { id: 5, type: "i64", value: rows.length },
        { id: 6, type: "i64", value: size },
        { id: 7, type: "i64", value: size },
        { id: 9, type: "i64", value: offset },
      ];
      if (column.min && column.max) {
        meta.push({
          id: 12,
          type: "struct",
          value: [
            { id: 5, type: "binary", value: column.max },
            { id: 6, type: "binary", value: column.min },
          ],
        });
      }
      chunks.push([
        { id: 2, type: "i64", value: offset },
        { id: 3, type: "struct", value: meta },
      ]);
      parts.push(header, column.values);
      offset += size;
      groupBytes += size;
    }
    rowGroups.push([
      { id: 1, type: "list", element: "struct", value: chunks },
      { id: 2, type: "i64", value: groupBytes },
      { id: 3, type: "i64", value: rows.length },
    ]);
  }
  const element = (name: string, extra: Field[]): Field[] => [
    { id: 3, type: "i32", value: PARQUET.REQUIRED },
    { id: 4, type: "binary", value: name },
    ...extra,
  ];
  const geo = JSON.stringify({
    version: "1.1.0",
    primary_column: "geometry",
    columns: { geometry: { encoding: "WKB", geometry_types: ["LineString"], covering: { bbox: { xmin: ["bbox", "xmin"], ymin: ["bbox", "ymin"], xmax: ["bbox", "xmax"], ymax: ["bbox", "ymax"] } } } },
  });
  const footer = thrift([
    { id: 1, type: "i32", value: 2 },
    {
      id: 2,
      type: "list",
      element: "struct",
      value: [
        [{ id: 4, type: "binary", value: "schema" }, { id: 5, type: "i32", value: 3 }],
        [{ id: 1, type: "i32", value: PARQUET.INT64 }, ...element("feature_id", [])],
        [{ id: 1, type: "i32", value: PARQUET.BYTE_ARRAY }, ...element("geometry", [])],
        element("bbox", [{ id: 5, type: "i32", value: 4 }]),
        ...["xmin", "ymin", "xmax", "ymax"].map((name) => [{ id: 1, type: "i32", value: PARQUET.FLOAT } as Field, ...element(name, [])]),
      ],
    },
    { id: 3, type: "i64", value: reaches.length },
    { id: 4, type: "list", element: "struct", value: rowGroups },
    {
      id: 5,
      type: "list",
      element: "struct",
      value: [[{ id: 1, type: "binary", value: "geo" }, { id: 2, type: "binary", value: geo }]],
    },
  ]);
  const length = new Uint8Array(4);
  new DataView(length.buffer).setUint32(0, footer.byteLength, true);
  parts.push(footer, length, encoder.encode("PAR1"));
  return concat(parts);
}

/** Reaches in three clusters a row group each: west, middle and east of a
 * 3° box, each reach a short zigzag of `points` vertices. */
export function sampleReaches(featureCount: number, rowGroupRows: number, points = 20): ReachFixture[] {
  return Array.from({ length: featureCount }, (_, feature) => {
    const group = Math.floor(feature / rowGroupRows);
    const west = -100 + group * 1.2 + ((feature % rowGroupRows) % 8) * 0.1;
    const south = 30 + Math.floor((feature % rowGroupRows) / 8) * 0.1;
    const line: number[] = [];
    for (let point = 0; point < points; point += 1) line.push(west + point * 0.004, south + (point % 2) * 0.003);
    return { line };
  });
}

// -- network store -------------------------------------------------------------

export interface NetworkStoreFixture {
  files: Map<string, Uint8Array>;
  crc32: string;
  /** Every code written, `[frame][feature]`, before any padding. */
  codes: Uint8Array[];
  times: number[];
  baseflow: Float32Array;
}

export interface NetworkStoreOptions {
  frameCount: number;
  featureCount: number;
  rowGroupRows: number;
  delta: boolean;
  /** `timeChunk:rowGroup` keys of inner chunks the shard never holds. */
  missing?: string[];
  runTime?: string;
  geometry?: { path: string; byteLength: number; crc32: string };
}

export const LOG1P_CODEBOOK = {
  type: "log1p",
  trace: 0.01,
  scale: 1.0,
  maximum: 100000.0,
  minimumCode: 1,
  maximumCode: 253,
  zeroCode: 0,
  overflowCode: 254,
  nodataCode: 255,
} as const;

/** The code a fixture store holds at a frame and a reach: every reach on
 * its own slow ramp, with a no-data reach every 17th. */
export function fixtureCode(frame: number, feature: number): number {
  if (feature % 17 === 5) return 255;
  return ((feature * 7 + frame * 3) % 250) + 1;
}

export function buildNetworkStore(options: NetworkStoreOptions): NetworkStoreFixture {
  const { frameCount, featureCount, rowGroupRows, delta } = options;
  const missing = new Set(options.missing ?? []);
  const timeChunk = 6;
  const timeChunks = Math.ceil(frameCount / timeChunk);
  const rowGroups = Math.ceil(featureCount / rowGroupRows);
  const runTime = options.runTime ?? "2025-07-04T09:00:00Z";
  const runSeconds = Date.parse(runTime) / 1000;
  const times = Array.from({ length: frameCount }, (_, frame) => (runSeconds + (frame + 1) * 3600) * 1000);
  const codes = Array.from({ length: frameCount }, (_, frame) => Uint8Array.from({ length: featureCount }, (_, feature) => fixtureCode(frame, feature)));
  const baseflow = Float32Array.from({ length: featureCount }, (_, feature) => (feature % 13 === 0 ? Number.NaN : 1 + (feature % 9)));

  const chunks: Uint8Array[] = [];
  const entries: ({ offset: number; length: number } | null)[] = [];
  let offset = 0;
  for (let chunk = 0; chunk < timeChunks; chunk += 1) {
    for (let group = 0; group < rowGroups; group += 1) {
      if (missing.has(`${chunk}:${group}`)) {
        entries.push(null);
        continue;
      }
      const raw = new Uint8Array(timeChunk * rowGroupRows).fill(255);
      for (let within = 0; within < timeChunk; within += 1) {
        const frame = chunk * timeChunk + within;
        for (let column = 0; column < rowGroupRows; column += 1) {
          const feature = group * rowGroupRows + column;
          if (frame < frameCount && feature < featureCount) raw[within * rowGroupRows + column] = codes[frame]![feature]!;
        }
      }
      const stored = new Uint8Array(raw);
      if (delta) {
        for (let within = timeChunk - 1; within > 0; within -= 1) {
          for (let column = 0; column < rowGroupRows; column += 1) {
            const at = within * rowGroupRows + column;
            stored[at] = (raw[at]! - raw[at - rowGroupRows]! + 256) & 0xff;
          }
        }
      }
      const compressed = new Uint8Array(
        zstdCompressSync(stored, { params: { [constants.ZSTD_c_compressionLevel]: 15, [constants.ZSTD_c_checksumFlag]: 1 } }),
      );
      entries.push({ offset, length: compressed.byteLength });
      chunks.push(compressed);
      offset += compressed.byteLength;
    }
  }
  const index = new Uint8Array(entries.length * 16 + 4);
  const view = new DataView(index.buffer);
  entries.forEach((entry, position) => {
    view.setBigUint64(position * 16, entry ? BigInt(entry.offset) : 0xffffffffffffffffn, true);
    view.setBigUint64(position * 16 + 8, entry ? BigInt(entry.length) : 0xffffffffffffffffn, true);
  });
  view.setUint32(entries.length * 16, crc(index.subarray(0, entries.length * 16), CRC32C_TABLE), true);

  const geometry = {
    path: "reaches.parquet",
    byteLength: options.geometry?.byteLength ?? 1,
    crc32: options.geometry?.crc32 ?? "00000000",
    featureCount,
    rowGroupRows,
  };
  const variable = { numericId: 1, id: "streamflow", label: "Streamflow", unit: "m3/s", quantization: LOG1P_CODEBOOK };
  const streamflow = {
    zarr_format: 3,
    node_type: "array",
    shape: [frameCount, featureCount],
    data_type: "uint8",
    chunk_grid: { name: "regular", configuration: { chunk_shape: [timeChunks * timeChunk, rowGroups * rowGroupRows] } },
    chunk_key_encoding: { name: "default" },
    fill_value: 255,
    codecs: [
      {
        name: "sharding_indexed",
        configuration: {
          chunk_shape: [timeChunk, rowGroupRows],
          codecs: [
            ...(delta ? [{ name: "xue.delta", configuration: { axis: 0 } }] : []),
            { name: "bytes" },
            { name: "zstd", configuration: { level: 15, checksum: true } },
          ],
          index_codecs: [{ name: "bytes", configuration: { endian: "little" } }, { name: "crc32c" }],
          index_location: "end",
        },
      },
    ],
    attributes: { xue_network: { variable }, _FillValue: 255 },
    dimension_names: ["time", "feature"],
  };
  const side = (shape: number, dataType: string, fill: unknown) => ({
    zarr_format: 3,
    node_type: "array",
    shape: [shape],
    data_type: dataType,
    chunk_grid: { name: "regular", configuration: { chunk_shape: [shape] } },
    chunk_key_encoding: { name: "default" },
    fill_value: fill,
    codecs: [{ name: "bytes", configuration: { endian: "little" } }],
  });
  const arrays = {
    streamflow,
    time: side(frameCount, "int64", 0),
    baseflow: side(featureCount, "float32", "NaN"),
  };
  const group = {
    zarr_format: 3,
    node_type: "group",
    attributes: {
      xue_network: {
        version: 1,
        model: "fixture",
        product: "short_range",
        runTime,
        time: { unitSeconds: 3600, firstFrameOffset: 1, frameCount, frameStep: 1 },
        geometry: { ...geometry, path: options.geometry?.path ?? geometry.path },
        variables: [variable],
      },
    },
    consolidated_metadata: { kind: "inline", must_understand: false, metadata: arrays },
  };
  const groupBytes = encoder.encode(JSON.stringify(group));
  const timeBytes = new Uint8Array(frameCount * 8);
  times.forEach((time, frame) => new DataView(timeBytes.buffer).setBigInt64(frame * 8, BigInt(time / 1000), true));
  const files = new Map<string, Uint8Array>([
    ["zarr.json", groupBytes],
    ["streamflow/zarr.json", encoder.encode(JSON.stringify(streamflow))],
    ["streamflow/c/0/0", concat([...chunks, index])],
    ["time/zarr.json", encoder.encode(JSON.stringify(arrays.time))],
    ["time/c/0", timeBytes],
    ["baseflow/zarr.json", encoder.encode(JSON.stringify(arrays.baseflow))],
    ["baseflow/c/0", new Uint8Array(baseflow.buffer.slice(0))],
  ]);
  return { files, crc32: crc32Hex(groupBytes), codes, times, baseflow };
}

// -- CLI -----------------------------------------------------------------------

function main(argv: readonly string[]): void {
  const output = argv[2];
  if (!output) {
    console.error("usage: node tests/web/fixtures/river.ts <output directory>");
    process.exit(2);
  }
  const reaches = buildReachesParquet(sampleReaches(168, 64), 64);
  const store = buildNetworkStore({ frameCount: 14, featureCount: 168, rowGroupRows: 64, delta: true, missing: ["2:2"] });
  mkdirSync(output, { recursive: true });
  writeFileSync(join(output, "reaches.parquet"), reaches);
  for (const [path, bytes] of store.files) {
    const target = join(output, "fixture.zarr", path);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, bytes);
  }
  console.log(`wrote reaches.parquet (${reaches.byteLength} B) and fixture.zarr (crc32 ${store.crc32}) to ${output}`);
}

if (typeof process !== "undefined" && process.argv[1] && /fixtures[\\/]river\.ts$/.test(process.argv[1])) main(process.argv);
