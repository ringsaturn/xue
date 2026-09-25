/** Pages Function: everything under `/api/*` is served by the API, whose own
 * routes are `/health`, `/v1/…` — so `/api` is stripped before `handle()`.
 *
 * The decoder comes from the `--target bundler` wasm (`make wasm-worker`),
 * the only form Workers can instantiate. Typed minimally instead of with
 * `@cloudflare/workers-types`; the runtime only needs `onRequest`. */

import { handle } from "../../api/lib/api";
import { loadDecoder } from "../../api/lib/wasm.worker";

interface PagesContext {
  request: Request;
}

export async function onRequest(context: PagesContext): Promise<Response> {
  const url = new URL(context.request.url);
  url.pathname = url.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
  const request = new Request(url, context.request);
  return handle(request, { decodeChunk: await loadDecoder() });
}
