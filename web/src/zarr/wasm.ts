/**
 * The WASM module of a Zarr worker, instantiated once however many channels
 * the worker hosts: a second instantiation started before the first has
 * finished would replace the instance under the readers the first one's
 * channel is already holding.
 */

import wasmInit from "../wasm/xue";
import wasmUrl from "../wasm/xue_bg.wasm?url";

let wasmReady: Promise<unknown> | null = null;

export function loadWasm(): Promise<unknown> {
  wasmReady ??= wasmInit(wasmUrl).catch((error: unknown) => {
    // A load that failed is not the module: the next channel tries again,
    // rather than every channel of the worker failing with this one.
    wasmReady = null;
    throw error;
  });
  return wasmReady;
}
