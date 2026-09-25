/** The `decodeChunk` a Node host provides, from the `--target web` wasm the
 * browser also uses. A Worker host uses `wasm.worker.ts` instead (the
 * `--target bundler` build), so the read path never imports a Node built-in. */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import init, { decodeChunk } from "../../web/src/wasm/xue";
import type { DecodeChunk } from "./read";

let ready: Promise<DecodeChunk> | null = null;

export function loadDecoder(): Promise<DecodeChunk> {
  ready ??= (async () => {
    const dir = resolve(process.env.XUE_WASM_DIR ?? resolve(process.cwd(), "web/src/wasm"));
    await init({ module_or_path: readFileSync(resolve(dir, "xue_bg.wasm")) });
    return decodeChunk;
  })();
  return ready;
}
