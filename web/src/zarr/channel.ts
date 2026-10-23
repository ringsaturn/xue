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
import { zarrSegments, type VariableBundleDescriptor, type VariantDescriptor, type ZarrDescriptor } from "../manifest";
import type { ZarrInitStreamMessage } from "./protocol";

/** The store a session would open: the tier's own when one was picked,
 * else the bundle's, and none when the container was asked for — except
 * on an entry that ships nothing else, where the store is the only way to
 * open it whatever the parameter says. */
export function zarrStoreFor(
  backend: DataBackend,
  descriptor: VariableBundleDescriptor,
  variant: VariantDescriptor | null,
): ZarrDescriptor | undefined {
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

/** The URL of the store's first root document, as the worker will request
 * it: what the main thread probes for range support before opening. A
 * segmented store's segments share an origin, so the first speaks for all. */
export function zarrProbeUrl(store: ZarrDescriptor, manifestUrl: string): string {
  const first = zarrSegments(store)[0]!;
  return zarrObjectUrl(zarrRootUrl(first.path, manifestUrl), "zarr.json", first.crc32);
}

/** Open `bundle` out of a store, or out of a window's segments, with every
 * root resolved against the manifest. The bundle id travels with the store
 * because a shared store's path names every bundle of its tier. */
export function zarrInitMessage(
  store: ZarrDescriptor,
  bundle: string,
  manifestUrl: string,
  variableKey: string,
  ranges = true,
  payloadBudgetBytes?: number,
): ZarrInitStreamMessage {
  const roots = zarrSegments(store).map((segment) => ({
    url: zarrRootUrl(segment.path, manifestUrl),
    crc32: segment.crc32,
  }));
  return {
    type: "init-stream",
    kind: "zarr",
    url: roots[0]!.url,
    crc32: roots[0]!.crc32,
    bundle,
    byteLength: store.byteLength,
    variableKey,
    ...("segments" in store ? { segments: roots } : {}),
    ...(ranges ? {} : { ranges }),
    ...(payloadBudgetBytes === undefined ? {} : { payloadBudgetBytes }),
  };
}

export function spawnZarrWorker(): Worker {
  return new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
}
