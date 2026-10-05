/**
 * The GIF encoder off the main thread: the captured frames in, one file out.
 * The palette is one table for the whole loop, quantized from a sample of
 * every frame, so a colour that only appears late (a band of rain moving
 * in) is in it and the frames do not flicker between tables.
 */

import { applyPalette, GIFEncoder, quantize } from "gifenc";

export interface GifWorkerRequest {
  width: number;
  height: number;
  /** RGBA rows, one buffer per frame. */
  frames: ArrayBuffer[];
  /** Hold of each frame in milliseconds. */
  delays: number[];
}

export type GifWorkerResponse =
  | { type: "progress"; done: number; total: number }
  | { type: "done"; bytes: ArrayBuffer };

/** Every pixel of every frame would be a 50 MB quantizer input for a two-day
 * loop; one in this many is enough to find its colours. */
const SAMPLE_STRIDE = 7;

function sampleFrames(frames: Uint8Array[]): Uint8Array {
  const pixels = frames.reduce((sum, frame) => sum + Math.ceil(frame.length / 4 / SAMPLE_STRIDE), 0);
  const sample = new Uint8Array(pixels * 4);
  let at = 0;
  for (const frame of frames) {
    for (let pixel = 0; pixel < frame.length / 4; pixel += SAMPLE_STRIDE) {
      sample.set(frame.subarray(pixel * 4, pixel * 4 + 4), at);
      at += 4;
    }
  }
  return sample.subarray(0, at);
}

self.onmessage = (event: MessageEvent<GifWorkerRequest>) => {
  const { width, height, delays } = event.data;
  const frames = event.data.frames.map((buffer) => new Uint8Array(buffer));
  const post = (response: GifWorkerResponse, transfer: Transferable[] = []) =>
    (self as unknown as DedicatedWorkerGlobalScope).postMessage(response, transfer);
  const palette = quantize(sampleFrames(frames), 256);
  const gif = GIFEncoder();
  for (const [position, frame] of frames.entries()) {
    gif.writeFrame(applyPalette(frame, palette), width, height, {
      palette: position === 0 ? palette : undefined,
      delay: delays[position],
    });
    post({ type: "progress", done: position + 1, total: frames.length });
  }
  gif.finish();
  const bytes = gif.bytes();
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  post({ type: "done", bytes: buffer }, [buffer]);
};
