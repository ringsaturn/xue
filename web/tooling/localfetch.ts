/**
 * A `fetch` over a directory on disk, honouring byte ranges the way a
 * range-capable origin does (R2, the Vite dev server): an exact 206 for
 * `bytes=a-b`, the object's tail for `bytes=-n`, 200 with the whole file
 * otherwise, 404 for a missing path. What the unit tests and the
 * measurement harness serve the Zarr store and the `.xue` bundle through,
 * so request counts and bytes are those the browser would see without a
 * server in the loop. Node only.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

export interface LocalFetchLog {
  requests: number;
  bytes: number;
  /** Every request as `path` plus its Range header, in order. */
  entries: { path: string; range: string | null }[];
}

export function newFetchLog(): LocalFetchLog {
  return { requests: 0, bytes: 0, entries: [] };
}

/** `fetch` for URLs of the form `local://<relative path>?v=...`, read
 * relative to `root`. */
export function localFetch(root: string, log: LocalFetchLog = newFetchLog()) {
  return async (url: string, init?: RequestInit): Promise<Response> => {
    const parsed = new URL(url);
    const relative = decodeURIComponent(parsed.hostname + parsed.pathname).replace(/^\/+/, "");
    const headers = new Headers(init?.headers);
    const range = headers.get("Range");
    log.requests += 1;
    log.entries.push({ path: relative, range });
    let body: Buffer;
    try {
      body = readFileSync(join(root, relative));
    } catch {
      return new Response("missing", { status: 404 });
    }
    if (range === null) {
      log.bytes += body.byteLength;
      return new Response(new Uint8Array(body), { status: 200, headers: { "content-length": String(body.byteLength) } });
    }
    const suffix = /^bytes=-(\d+)$/.exec(range);
    const span = /^bytes=(\d+)-(\d+)$/.exec(range);
    let start: number;
    let end: number;
    if (suffix) {
      start = Math.max(0, body.byteLength - Number(suffix[1]));
      end = body.byteLength - 1;
    } else if (span) {
      start = Number(span[1]);
      end = Math.min(Number(span[2]), body.byteLength - 1);
    } else {
      return new Response("bad range", { status: 416 });
    }
    const slice = body.subarray(start, end + 1);
    log.bytes += slice.byteLength;
    return new Response(new Uint8Array(slice), {
      status: 206,
      headers: { "accept-ranges": "bytes", "content-range": `bytes ${start}-${end}/${body.byteLength}` },
    });
  };
}
