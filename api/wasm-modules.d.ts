/** A `.wasm` import in a Worker/Pages Function is a `WebAssembly.Module`
 * (see developers.cloudflare.com/workers/languages/rust). The bundler build's
 * `_bg.js` glues the wasm exports; only what the worker loader uses is named. */

declare module "*.wasm" {
  const module: WebAssembly.Module;
  export default module;
}

declare module "*/xue_bg.js" {
  export function decodeChunk(
    bytes: Uint8Array,
    frames: number,
    height: number,
    width: number,
    predictor: number,
  ): Uint8Array;
  export function __wbg_set_wasm(exports: WebAssembly.Exports): void;
}
