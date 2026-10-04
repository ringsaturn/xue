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

/** The pointer, then the index it names. Null when the root publishes no
 * such product (404, or a bucket that answers 403 for a missing key): a
 * shell deployed before the product exists asks once per poll and draws
 * nothing. `what` names the product in the errors. */
export async function loadPointerIndex<P extends { path: string; crc32: string }, I>(
  baseUrl: string,
  filename: string,
  parsePointer: (input: unknown) => P,
  parseIndex: (input: unknown) => I,
  what: string,
): Promise<LoadedIndex<P, I> | null> {
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
