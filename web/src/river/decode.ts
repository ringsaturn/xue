/**
 * The WASM chunk decoder on the main thread, loaded the first time a river
 * track is opened. A track is a few hundred kilobytes and a frame two
 * inner chunks of 24 KB, so a worker would cost more in messages than the
 * decode does; the grid fields keep theirs.
 */

import type { DecodeChunkFn } from "./store";

let decoder: Promise<DecodeChunkFn> | null = null;

export function loadDecodeChunk(): Promise<DecodeChunkFn> {
  decoder ??= (async () => {
    const [wasm, url] = await Promise.all([import("../wasm/xue"), import("../wasm/xue_bg.wasm?url")]);
    await wasm.default({ module_or_path: url.default });
    return wasm.decodeChunk;
  })();
  decoder.catch(() => {
    decoder = null;
  });
  return decoder;
}
