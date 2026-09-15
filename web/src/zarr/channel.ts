/**
 * Choosing the Zarr channel.
 *
 * The channel is additive and off by default: it is taken only when the
 * session asked for it (`?backend=zarr`, read in urlstate.ts) *and* the
 * bundle — or the resolution tier picked for it — carries a `zarr`
 * descriptor in the manifest, and the store's origin honours range
 * requests, which is all the channel ever issues. Anything else leaves the
 * `.xue` path exactly as it was, so a run without stores, or a link without
 * the parameter, never touches this file's worker.
 */

import type { DataBackend } from "../urlstate";
import type { VariableBundleDescriptor, VariantDescriptor, ZarrStoreDescriptor } from "../manifest";
import type { ZarrInitStreamMessage } from "./protocol";

/** The store a session would open: the tier's own when one was picked,
 * else the bundle's, and none unless the backend was asked for. */
export function zarrStoreFor(
  backend: DataBackend,
  descriptor: VariableBundleDescriptor,
  variant: VariantDescriptor | null,
): ZarrStoreDescriptor | undefined {
  if (backend !== "zarr") return undefined;
  return (variant ?? descriptor).zarr;
}

/** The store's root URL, resolved against the manifest the way every
 * artifact path is; the manifest URL's own `?v=` does not carry over. */
export function zarrRootUrl(path: string, manifestUrl: string): string {
  return new URL(path, manifestUrl).href.replace(/\/+$/, "");
}

/** The URL of one object in the store, as the worker will request it —
 * what the main thread probes for range support before opening. */
export function zarrObjectUrl(root: string, path: string, crc32: string): string {
  return `${root}/${path}?v=${crc32}`;
}

export function zarrInitMessage(root: string, store: ZarrStoreDescriptor, variableKey: string): ZarrInitStreamMessage {
  return { type: "init-stream", kind: "zarr", url: root, crc32: store.crc32, byteLength: store.byteLength, variableKey };
}

export function spawnZarrWorker(): Worker {
  return new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
}
