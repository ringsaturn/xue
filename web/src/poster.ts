/**
 * First-frame poster decode.
 *
 * A poster is one quantized code plane, decimated 2x in both axes, with each
 * row delta-filtered against the previous row (PNG "Up", uint8 wraparound)
 * and zlib-deflated. Inflation uses the browser-native
 * `DecompressionStream("deflate")` so this path needs neither WASM nor a
 * Worker — it exists to put pixels on screen before either is ready.
 */

import { crc32Of } from "./crc32";
import type { PosterDescriptor } from "./manifest";

export function isPosterSupported(): boolean {
  return typeof DecompressionStream !== "undefined";
}

/** Inflate and unfilter one poster payload into a width*height code plane. */
export async function decodePosterPlane(payload: Uint8Array, width: number, height: number): Promise<Uint8Array> {
  const stream = new ReadableStream<BufferSource>({
    start(controller) {
      controller.enqueue(payload as Uint8Array<ArrayBuffer>);
      controller.close();
    },
  }).pipeThrough<Uint8Array>(new DecompressionStream("deflate"));
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.byteLength;
  }
  if (total !== width * height) throw new Error("poster plane length does not match its descriptor");
  const plane = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    plane.set(chunk, offset);
    offset += chunk.byteLength;
  }
  for (let row = 1; row < height; row += 1) {
    const previous = (row - 1) * width;
    const current = row * width;
    for (let column = 0; column < width; column += 1) {
      plane[current + column] = (plane[current + column]! + plane[previous + column]!) & 0xff;
    }
  }
  return plane;
}

/** Download, CRC32-verify, and decode one manifest-declared poster. */
export async function fetchPoster(url: string, descriptor: PosterDescriptor): Promise<Uint8Array> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`poster request returned HTTP ${response.status}`);
  const payload = new Uint8Array(await response.arrayBuffer());
  if (payload.byteLength !== descriptor.byteLength) throw new Error("poster length does not match the manifest");
  if (crc32Of(payload) !== descriptor.crc32) throw new Error("poster crc32 mismatch");
  return decodePosterPlane(payload, descriptor.width, descriptor.height);
}
