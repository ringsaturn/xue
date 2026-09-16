import { statSync } from "node:fs";
import { join, normalize, resolve } from "node:path";

import { loadEnv } from "vite";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

import { discoveryFiles } from "./web/tooling/discovery";

/** The static server behind `vite preview` (sirv) misreads a suffix range
 * — `bytes=-N`, which the Zarr reader uses for a shard index — as
 * `bytes=0-N` and answers with the object's head, one byte too long, where
 * every CDN and bucket answers with its tail. The shell then reports a
 * "range response length mismatch". This rewrites a suffix range into the
 * absolute range it means, from the file's size, before sirv sees it; a
 * file that is not there falls through to sirv's own 404. */
function suffixRanges(): Plugin {
  return {
    name: "xue:suffix-ranges",
    configurePreviewServer(server) {
      const outDir = resolve(server.config.root, server.config.build.outDir);
      server.middlewares.use((req, _res, next) => {
        const match = /^bytes=-(\d+)$/.exec(req.headers.range ?? "");
        if (match && req.url) {
          const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
          const file = normalize(join(outDir, pathname));
          if (file.startsWith(outDir)) {
            try {
              const size = statSync(file).size;
              const length = Math.min(Number(match[1]), size);
              req.headers.range = `bytes=${size - length}-${size - 1}`;
            } catch {
              // not a file: sirv answers
            }
          }
        }
        next();
      });
    },
  };
}

export default defineConfig(({ mode }) => ({
  root: "web",
  publicDir: "public",
  base: "./",
  // sitemap.xml and llms-full.txt, derived at build time from the published
  // showcase catalog and the repository's own documentation
  // (web/tooling/discovery.ts). The catalog is read from wherever this mode
  // sends the page for data (web/.env.deploy: the public bucket).
  plugins: [
    suffixRanges(),
    discoveryFiles({
      repoRoot: import.meta.dirname,
      dataBaseUrl: loadEnv(mode, resolve(import.meta.dirname, "web"), "VITE_").VITE_DATA_BASE_URL,
    }),
  ],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    sourcemap: true,
    // Two pages: the live viewer (which also renders ?case=<id>) and the
    // historical showcase list.
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, "web/index.html"),
        showcase: resolve(import.meta.dirname, "web/showcase.html"),
      },
    },
  },
  server: {
    host: "127.0.0.1",
  },
  preview: {
    host: "127.0.0.1",
  },
  test: {
    environment: "jsdom",
    include: ["../tests/web/**/*.test.ts"],
  },
}));

