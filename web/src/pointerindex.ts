/** The pointer → index read the point products (storm tracks, soundings,
 * airports) share with the runs: the pointer with caching disabled, the
 * index `?v=<crc32>` addressed and pointer-relative. */

import { fetchImmutable } from "./fetchimmutable";

export interface LoadedIndex<P, I> {
  pointer: P;
  index: I;
  /** Absolute index URL (with its `?v=`); the files beside it resolve
   * against this. */
  indexUrl: string;
}

/** The point products this build reads: `VITE_POINT_PRODUCTS`, comma-separated
 * names (`tc`, `sounding`, `airport`, `synop`, `fire`, `aq`, `alerts`), for a
 * deployment whose data root publishes only some of them, `none` for none of
 * them; empty reads every one. */
export function parsePointProducts(value: string | undefined): ReadonlySet<string> | null {
  const names = (value ?? "")
    .split(",")
    .map((name) => name.trim())
    .filter((name) => name !== "");
  return names.length > 0 ? new Set(names) : null;
}

// `?.`: Playwright specs import this module outside Vite, where `env` is unset.
const SERVED_POINT_PRODUCTS = parsePointProducts(import.meta.env?.VITE_POINT_PRODUCTS);

/** The pointer, then the index it names. Null when the build does not read
 * the product, without a request, or when the root publishes no such product
 * (404, or a bucket that answers 403 for a missing key): a shell deployed
 * before the product exists asks once per poll and draws nothing. `what` names
 * the product, in `VITE_POINT_PRODUCTS` and in the errors. */
export async function loadPointerIndex<P extends { path: string; crc32: string }, I>(
  baseUrl: string,
  filename: string,
  parsePointer: (input: unknown) => P,
  parseIndex: (input: unknown) => I,
  what: string,
): Promise<LoadedIndex<P, I> | null> {
  if (SERVED_POINT_PRODUCTS !== null && !SERVED_POINT_PRODUCTS.has(what)) return null;
  const pointerUrl = new URL(`${baseUrl}${filename}`, document.baseURI);
  const response = await fetch(pointerUrl, { cache: "no-cache" });
  if (response.status === 404 || response.status === 403) return null;
  if (!response.ok)
    throw new Error(`${what} pointer request failed: ${response.status}`);
  const pointer = parsePointer(await response.json());
  const indexUrl = new URL(pointer.path, new URL(baseUrl, document.baseURI));
  indexUrl.searchParams.set("v", pointer.crc32);
  const indexResponse = await fetchImmutable(indexUrl);
  if (!indexResponse.ok)
    throw new Error(`${what} index request failed: ${indexResponse.status}`);
  return {
    pointer,
    index: parseIndex(await indexResponse.json()),
    indexUrl: indexUrl.href,
  };
}
