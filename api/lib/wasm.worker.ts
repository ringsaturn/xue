/** The `decodeChunk` a Worker host provides, from the `--target bundler` wasm
 * (`make wasm-worker`).
 *
 * Importing a `.wasm` in a Worker yields a `WebAssembly.Module`, but the
 * bundler target's loader expects an Instance's exports (`wasm.__wbindgen_start`
 * was undefined until we wired it). So instantiate the module ourselves with
 * the generated `_bg.js` as its imports and set the exports, the patch
 * developers.cloudflare.com/workers/languages/rust prescribes. */

import * as bindings from "../wasm-bundler/xue_bg.js";
import wasmModule from "../wasm-bundler/xue_bg.wasm";
import type { DecodeChunk } from "./read";

const instance = new WebAssembly.Instance(wasmModule, { "./xue_bg.js": bindings });
bindings.__wbg_set_wasm(instance.exports);
(instance.exports as { __wbindgen_start?: () => void }).__wbindgen_start?.();

export function loadDecoder(): Promise<DecodeChunk> {
  return Promise.resolve(bindings.decodeChunk);
}
