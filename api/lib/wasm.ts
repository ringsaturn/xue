/** Loads the existing `rust/xue-wasm` decoder (`--target web`, the same
 * artifact the browser uses) into Node. The default export is wasm-bindgen's
 * init; we hand it the wasm bytes explicitly, so no `fetch`/`import.meta.url`
 * path games. */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import init, { decodeChunk } from "../../web/src/wasm/xue";

export type DecodeChunkFn = typeof decodeChunk;

let ready: Promise<DecodeChunkFn> | null = null;

export function loadDecoder(): Promise<DecodeChunkFn> {
  ready ??= (async () => {
    const dir = resolve(process.env.XUE_WASM_DIR ?? resolve(process.cwd(), "web/src/wasm"));
    await init({ module_or_path: readFileSync(resolve(dir, "xue_bg.wasm")) });
    return decodeChunk;
  })();
  return ready;
}
