/**
 * Serving a run's artifacts to the shell under test.
 *
 * Shared by the specs that put a run in front of the viewer: one answer
 * shaped like a range-capable origin's (R2's), a route for every object of
 * a Zarr store read off the fixture directory, and the manifest of a run
 * published before stores existed — which is what the bucket's older runs,
 * every showcase case and every MRMS round are, and stay, so the container
 * path keeps its own suite (app.spec.ts) on that shape while zarr.spec.ts
 * drives the default.
 */

import type { Page, Route } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";

/** One range-capable answer for a fixture file — an exact 206 for
 * `bytes=a-b`, the object's tail for `bytes=-n` — or the whole file.
 * `ranges: false` answers every request whole, the way a static host
 * without `Accept-Ranges` does. */
export function fulfillWithRanges(
  body: Buffer,
  range: string | undefined,
  onRange: (length: number) => void = () => {},
  onFull: () => void = () => {},
  ranges = true,
): (route: Route) => Promise<void> {
  return async (route) => {
    const suffix = /^bytes=-(\d+)$/.exec(range ?? "");
    const span = /^bytes=(\d+)-(\d+)$/.exec(range ?? "");
    if (ranges && (suffix || span)) {
      const start = suffix ? Math.max(0, body.length - Number(suffix[1])) : Number(span![1]);
      const end = suffix ? body.length - 1 : Math.min(Number(span![2]), body.length - 1);
      onRange(end - start + 1);
      return route.fulfill({
        status: 206,
        contentType: "application/octet-stream",
        headers: { "accept-ranges": "bytes", "content-range": `bytes ${start}-${end}/${body.length}` },
        body: body.subarray(start, end + 1),
      });
    }
    onFull();
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
  };
}

export interface StoreRouteHooks {
  /** A 206 answered for a store object other than a `zarr.json`, with its length. */
  onRange?: (relative: string, length: number) => void;
  /** A whole object answered for a store object other than a `zarr.json`. */
  onFull?: (relative: string) => void;
  /** False serves every object whole, as an origin without ranges. */
  ranges?: boolean;
}

/** Serve every object of the stores under `pattern` (a glob ending in
 * `*.zarr/**`) from `fixtureRoot`, where `relative` is the store path plus
 * the object's key, as the manifest names it. The range-support probe the
 * shell sends (`bytes=0-0` on a `zarr.json`) is answered but never
 * counted. */
export async function routeStores(page: Page, pattern: string, fixtureRoot: string, hooks: StoreRouteHooks = {}): Promise<void> {
  await page.route(pattern, (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const relative = pathname.slice(pathname.lastIndexOf("/", pathname.indexOf(".zarr/")) + 1);
    const path = `${fixtureRoot}${relative}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    const isDocument = relative.endsWith("zarr.json");
    return fulfillWithRanges(
      readFileSync(path),
      route.request().headers()["range"],
      (length) => {
        if (!isDocument) hooks.onRange?.(relative, length);
      },
      () => {
        if (!isDocument) hooks.onFull?.(relative);
      },
      hooks.ranges ?? true,
    )(route);
  });
}

interface StoreBearing {
  bundles: { zarr?: unknown; variants?: { zarr?: unknown }[] }[];
}

/** The manifest as a run published before the store existed: every `zarr`
 * descriptor dropped, on the bundles and on their tiers. */
export function withoutStores<T extends StoreBearing>(manifest: T): T {
  const copy = structuredClone(manifest);
  for (const bundle of copy.bundles) {
    delete bundle.zarr;
    for (const variant of bundle.variants ?? []) delete variant.zarr;
  }
  return copy;
}

/** The manifest as the encoder writes it once the container is retired:
 * every entry that has a store keeps only the store. Entries without one
 * (the fixture's upper-air and pressure bundles) keep their container. */
export function storeOnly<T extends StoreBearing>(manifest: T): T {
  const copy = structuredClone(manifest);
  for (const bundle of copy.bundles) {
    for (const entry of [bundle, ...(bundle.variants ?? [])] as Record<string, unknown>[]) {
      if (entry.zarr === undefined) continue;
      delete entry.path;
      delete entry.byteLength;
      delete entry.crc32;
    }
  }
  return copy;
}
