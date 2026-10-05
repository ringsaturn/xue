/** The radar's decoder: one site's inner chunk of one round, read by range
 * out of the store's shard and decoded off the main thread.
 *
 * A polar store's inner chunk is a Zstandard frame of `scans × 720 × gates`
 * codes with no predictor (`docs/zarr-profile.md`, "Polar store"), which is
 * exactly what the container's chunk path decodes: `decodeChunk` from the
 * WASM decoder, with the RAW predictor. The fetch is here too, so the bytes
 * never visit the main thread compressed. */

import wasmInit, { decodeChunk } from "../wasm/xue";
import wasmUrl from "../wasm/xue_bg.wasm?url";
import { fetchImmutable } from "../fetchimmutable";

const PREDICTOR_RAW = 0;
const BEAMS = 720;

export interface RadarChunkRequest {
  id: number;
  url: string;
  offset: number;
  length: number;
  scans: number;
  /** Real sweeps at the head of the chunk: only these are handed back. */
  sweeps: number;
  gates: number;
}

export type RadarChunkReply =
  | { id: number; ok: true; codes: ArrayBuffer; bytes: number }
  | { id: number; ok: false; error: string };

const ready = wasmInit(wasmUrl);

async function decode(request: RadarChunkRequest): Promise<RadarChunkReply> {
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
