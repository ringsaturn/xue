/** The radar's decoder: one unit of a site's sweeps, fetched and decoded
 * off the main thread.
 *
 * Two kinds of unit (`feed.ts`). A polar store's inner chunk, read by range
 * out of the store's shard, is a Zstandard frame of `scans × 720 × gates`
 * codes with no predictor (`docs/zarr-profile.md`, "Polar store"), which is
 * exactly what the container's chunk path decodes: `decodeChunk` with the
 * RAW predictor. A live sweep is one Level 3 object from the source bucket,
 * read whole and decoded by `decodeLevel3`, the port of the encoder's own
 * reader, into the same codes on the same beam grid. The fetch is here too,
 * so the bytes never visit the main thread compressed. */

import wasmInit, { decodeChunk, decodeLevel3 } from "../wasm/xue";
import wasmUrl from "../wasm/xue_bg.wasm?url";
import { fetchImmutable } from "../fetchimmutable";
import type { RadarProduct } from "./schema";

const PREDICTOR_RAW = 0;
const BEAMS = 720;

export interface RadarChunkDecode {
  kind: "chunk";
  url: string;
  offset: number;
  length: number;
  scans: number;
  /** Real sweeps at the head of the chunk: only these are handed back. */
  sweeps: number;
  gates: number;
}

export interface RadarLevel3Decode {
  kind: "level3";
  url: string;
  product: RadarProduct;
  gates: number;
}

export type RadarDecodeRequest = RadarChunkDecode | RadarLevel3Decode;
export type RadarChunkRequest = RadarDecodeRequest & { id: number };

export type RadarChunkReply =
  | { id: number; ok: true; codes: ArrayBuffer; bytes: number }
  | { id: number; ok: false; error: string };

const ready = wasmInit(wasmUrl);
const PRODUCT_CODES: Record<RadarProduct, number> = { n0b: 153, n0g: 154 };

async function decodeSweep(request: RadarLevel3Decode & { id: number }): Promise<RadarChunkReply> {
  // A sweep is immutable once in the bucket: whatever cache holds it is
  // right.
  const response = await fetch(request.url, { cache: "force-cache" });
  if (!response.ok) throw new Error(`radar sweep request failed: ${response.status}`);
  const body = new Uint8Array(await response.arrayBuffer());
  await ready;
  const sweep = decodeLevel3(body);
  try {
    if (sweep.productCode !== PRODUCT_CODES[request.product] || sweep.gates !== request.gates)
      throw new Error(`radar sweep is product ${sweep.productCode} with ${sweep.gates} gates, not ${request.product}`);
    const codes = sweep.takeCodes();
    return { id: request.id, ok: true, codes: codes.buffer as ArrayBuffer, bytes: body.length };
  } finally {
    sweep.free();
  }
}

async function decode(request: RadarChunkRequest): Promise<RadarChunkReply> {
  if (request.kind === "level3") return decodeSweep(request);
  const response = await fetchImmutable(request.url, {
    headers: { Range: `bytes=${request.offset}-${request.offset + request.length - 1}` },
  });
  if (!response.ok) throw new Error(`radar chunk request failed: ${response.status}`);
  let body = new Uint8Array(await response.arrayBuffer());
  // A server that ignores Range sent the whole shard: slice the span out of
  // it at the same offsets, as the station reader does.
  if (response.status === 200 && body.length !== request.length) {
    if (body.length < request.offset + request.length) throw new Error("radar shard is shorter than its span");
    body = body.slice(request.offset, request.offset + request.length);
  }
  if (body.length !== request.length) throw new Error("radar chunk length mismatch");
  await ready;
  const decoded = decodeChunk(body, request.scans, BEAMS, request.gates, PREDICTOR_RAW);
  // A window store pads every chunk to its busiest round; the padding is
  // never drawn, so it is dropped here rather than held resident.
  const real = request.sweeps * BEAMS * request.gates;
  const codes = real < decoded.length ? decoded.slice(0, real) : decoded;
  return { id: request.id, ok: true, codes: codes.buffer as ArrayBuffer, bytes: body.length };
}

self.onmessage = (event: MessageEvent<RadarChunkRequest>) => {
  const request = event.data;
  decode(request).then(
    (reply) => {
      if (reply.ok) (self as unknown as Worker).postMessage(reply, [reply.codes]);
      else (self as unknown as Worker).postMessage(reply);
    },
    (error: unknown) => {
      (self as unknown as Worker).postMessage({
        id: request.id,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      } satisfies RadarChunkReply);
    },
  );
};
