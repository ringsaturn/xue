/**
 * The WASM module on the main thread, loaded the first time a river track
 * is opened. A track is a few hundred kilobytes and a frame two
 * inner chunks of 24 KB, so a worker would cost more in messages than the
 * decode does; the grid fields keep theirs.
 */

import type { NetworkCodec } from "./store";

let decoder: Promise<NetworkCodec> | null = null;

export function loadNetworkCodec(): Promise<NetworkCodec> {
  decoder ??= (async () => {
    const [wasm, url] = await Promise.all([import("../wasm/xue"), import("../wasm/xue_bg.wasm?url")]);
    await wasm.default({ module_or_path: url.default });
    return wasm;
  })();
  decoder.catch(() => {
    decoder = null;
  });
  return decoder;
}
