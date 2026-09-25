/** Local HTTP server around `handle()`. Run: npm run api (bundle + node). */

import { createServer } from "node:http";

import { handle } from "./lib/api";

const port = Number(process.env.PORT ?? 8788);
const host = process.env.HOST ?? "127.0.0.1";

const server = createServer(async (incoming, outgoing) => {
  try {
    const url = new URL(incoming.url ?? "/", `http://${incoming.headers.host ?? `${host}:${port}`}`);
    const request = new Request(url, {
      method: incoming.method ?? "GET",
      headers: incoming.headers as Record<string, string>,
    });
    const response = await handle(request);
    outgoing.statusCode = response.status;
    response.headers.forEach((value, key) => outgoing.setHeader(key, value));
    if (incoming.method === "HEAD") {
      outgoing.end();
      return;
    }
    outgoing.end(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    outgoing.statusCode = 500;
    outgoing.setHeader("content-type", "application/json; charset=utf-8");
    outgoing.end(
      JSON.stringify({ error: { code: "internal", message: error instanceof Error ? error.message : String(error) } }),
    );
  }
});

server.listen(port, host, () => {
  console.log(`xue api listening on http://${host}:${port}`);
});
