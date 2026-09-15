/**
 * Choosing the Zarr channel.
 *
 * The channel is the default: it is taken whenever the bundle — or the
 * resolution tier picked for it — carries a `zarr` descriptor in the
 * manifest, unless the session asked for the container (`?backend=xue`,
 * read in urlstate.ts). Streamed where the store's origin honours range
 * requests; where it does not, `main.ts` first tries the `.xue` container
 * and comes back to the store in whole-object mode only when the entry
 * ships no container at all. A run without stores, or a link with the
 * parameter, never touches this file's worker.
 */

import type { DataBackend } from "../urlstate";
import type { VariableBundleDescriptor, VariantDescriptor, ZarrStoreDescriptor } from "../manifest";
import type { ZarrInitStreamMessage } from "./protocol";

/** The store a session would open: the tier's own when one was picked,
 * else the bundle's, and none when the container was asked for — except
 * on an entry that ships nothing else, where the store is the only way to
 * open it whatever the parameter says. */
export function zarrStoreFor(
  backend: DataBackend,
  descriptor: VariableBundleDescriptor,
  variant: VariantDescriptor | null,
): ZarrStoreDescriptor | undefined {
  const entry = variant ?? descriptor;
  if (backend !== "zarr" && entry.path !== undefined) return undefined;
  return entry.zarr;
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

export function zarrInitMessage(
  root: string,
  store: ZarrStoreDescriptor,
  variableKey: string,
  ranges = true,
): ZarrInitStreamMessage {
  return {
    type: "init-stream",
    kind: "zarr",
    url: root,
    crc32: store.crc32,
    byteLength: store.byteLength,
    variableKey,
    ...(ranges ? {} : { ranges }),
  };
}

export function spawnZarrWorker(): Worker {
  return new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
}
