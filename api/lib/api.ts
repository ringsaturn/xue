/** The route table and the platform-neutral `handle(Request) → Response`.
 * `api/server.ts` bridges it to `node:http`; the same function can be dropped
 * into a Cloudflare Pages Function / Worker later. */

import { DATA_ORIGIN, fetchJson } from "./bucket";
import { HttpError, errorResponse, json } from "./http";
import { readPoint, type DecodeChunk } from "./read";
import { readSource } from "./source";

/** What a host must supply to serve the API: the wasm decoder. Node and a
 * Worker each provide it from their own wasm build (`wasm.ts` /
 * `wasm.worker.ts`), so nothing below imports a host-specific module. */
export interface ApiDeps {
  decodeChunk: DecodeChunk;
}

const OPTIONS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-allow-headers": "*",
  "access-control-max-age": "86400",
} as const;

export async function handle(request: Request, deps: ApiDeps): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  try {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: OPTIONS_HEADERS });
    if (request.method !== "GET" && request.method !== "HEAD") {
      throw new HttpError(405, "method_not_allowed", "only GET is supported");
    }
    if (path === "/health") {
      return json({ status: "ok", now: new Date().toISOString() }, { cacheControl: "no-store" });
    }
    if (path === "/v1/catalog") return await catalog();
    const source = /^\/v1\/sources\/([^/]+)$/.exec(path);
    if (source) return await sourceDetail(source[1]!);
    if (path === "/v1/point") return await point(url.searchParams, deps.decodeChunk);
    throw new HttpError(404, "not_found", `no route for ${path}`);
  } catch (error) {
    return errorResponse(error);
  }
}

async function catalog(): Promise<Response> {
  const root = await fetchJson<{ links?: Array<{ rel: string; href: string; title?: string }> }>("catalog.json");
  const collections = (root.links ?? [])
    .filter((link) => link.rel === "child")
    .map((link) => ({
      id: link.href.split("/")[0]!,
      title: link.title ?? null,
      href: link.href,
    }));
  return json({ dataOrigin: DATA_ORIGIN, collections }, { cacheControl: "public, max-age=60" });
}

async function sourceDetail(id: string): Promise<Response> {
  const summary = await readSource(id);
  return json(summary, { cacheControl: "public, max-age=60" });
}

async function point(params: URLSearchParams, decodeChunk: DecodeChunk): Promise<Response> {
  const source = params.get("source");
  const latRaw = params.get("lat");
  const lonRaw = params.get("lon");
  if (source === null) throw new HttpError(400, "invalid_parameter", "source is required");
  if (latRaw === null) throw new HttpError(400, "invalid_parameter", "lat is required");
  if (lonRaw === null) throw new HttpError(400, "invalid_parameter", "lon is required");
  const variables = params
    .get("variables")
    ?.split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const result = await readPoint(
    {
      source,
      lat: Number(latRaw),
      lon: Number(lonRaw),
      variables,
      time: params.get("time") ?? undefined,
      run: params.get("run") ?? undefined,
    },
    decodeChunk,
  );
  // A `run`-addressed response is stable for that run, so it caches long; the
  // live path can change with the next cycle, so it caches briefly.
  const run = (result as { run?: string }).run;
  return json(result, {
    cacheControl: params.get("run") ? "public, max-age=86400" : "public, max-age=30",
    headers: run ? { "x-xue-run": run } : undefined,
  });
}
