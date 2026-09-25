/** Reading the public data root. One place that knows the origin and how to
 * do a JSON GET and a byte-range GET, so a future R2 binding / Worker port
 * only replaces this file. */

import { HttpError } from "./http";

const DEFAULT_ORIGIN = "https://dataset.ringsaturn.me/xue/";

/** A Node host can point this at a local build; a Worker has no `process`, so
 * the default stands (guarded because `process` does not exist in workerd). */
function originOverride(): string | undefined {
  return typeof process !== "undefined" ? process.env?.XUE_DATA_ORIGIN : undefined;
}

export const DATA_ORIGIN = normalize(originOverride() ?? DEFAULT_ORIGIN);

function normalize(value: string): string {
  return value.endsWith("/") ? value : `${value}/`;
}

export function dataUrl(path: string): string {
  return path.startsWith("http://") || path.startsWith("https://") ? path : DATA_ORIGIN + path;
}

export async function fetchText(path: string): Promise<string> {
  const url = dataUrl(path);
  const response = await fetch(url, { headers: { "cache-control": "no-cache" } });
  if (!response.ok) {
    throw new HttpError(
      response.status === 404 ? 404 : 502,
      response.status === 404 ? "not_found" : "upstream_failed",
      `GET ${url} -> ${response.status}`,
    );
  }
  return await response.text();
}

export async function fetchJson<T = unknown>(path: string): Promise<T> {
  return JSON.parse(await fetchText(path)) as T;
}

export type ByteRange = { offset: number; length: number } | { suffix: number };

/** One ranged GET. A 206 is taken as is; a 200 (an origin that ignored the
 * Range header) is sliced locally, as the point products do. */
export async function fetchRange(path: string, range: ByteRange): Promise<Uint8Array> {
  const url = dataUrl(path);
  const header = "suffix" in range ? `bytes=-${range.suffix}` : `bytes=${range.offset}-${range.offset + range.length - 1}`;
  const response = await fetch(url, { headers: { range: header } });
  if (!response.ok && response.status !== 206) {
    throw new HttpError(502, "upstream_failed", `GET ${url} (${header}) -> ${response.status}`);
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (response.status === 206) return bytes;
  if ("suffix" in range) return bytes.subarray(Math.max(0, bytes.byteLength - range.suffix));
  return bytes.subarray(range.offset, range.offset + range.length);
}
