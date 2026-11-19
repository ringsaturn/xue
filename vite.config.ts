import { statSync } from "node:fs";
import { join, normalize, resolve } from "node:path";

import { loadEnv } from "vite";
import type { Connect, Plugin } from "vite";
import { defineConfig } from "vitest/config";

import { discoveryFiles } from "./web/tooling/discovery";
import { modelPageIds, modelPagePaths, modelPages, modelPathRewrite } from "./web/tooling/modelpages";

/** The static server behind `vite preview` and the dev server's public
 * directory (both sirv) misread a suffix range — `bytes=-N`, which the Zarr
 * reader uses for a shard index — as `bytes=0-N` and answer with the
 * object's head, one byte too long, where every CDN and bucket answers with
 * its tail. The shell then reports a "range response length mismatch". This
 * rewrites a suffix range into the absolute range it means, from the file's
 * size, before sirv sees it; a file that is not there falls through to
 * sirv's own 404. */
function suffixRanges(): Plugin {
  const rewrite = (roots: string[]): Connect.NextHandleFunction => (req, _res, next) => {
    const match = /^bytes=-(\d+)$/.exec(req.headers.range ?? "");
    if (match && req.url) {
      const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
      for (const root of roots) {
        const file = normalize(join(root, pathname));
        if (!file.startsWith(root)) continue;
        try {
          const size = statSync(file).size;
          const length = Math.min(Number(match[1]), size);
          req.headers.range = `bytes=${size - length}-${size - 1}`;
          break;
        } catch {
          // not a file here: try the next root, else sirv answers
        }
      }
    }
    next();
  };
  return {
    name: "xue:suffix-ranges",
    configureServer(server) {
      server.middlewares.use(rewrite([server.config.publicDir, server.config.root]));
    },
    configurePreviewServer(server) {
      server.middlewares.use(rewrite([resolve(server.config.root, server.config.build.outDir)]));
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, resolve(import.meta.dirname, "web"), "VITE_");
  // The datasets with a page of their own (`/hrrr`), as this mode's
  // VITE_SOURCES serves them (web/tooling/modelpages.ts).
  const modelIds = modelPageIds(env.VITE_SOURCES);
  return {
    root: "web",
    publicDir: "public",
    base: "./",
    // sitemap.xml and llms-full.txt, derived at build time from the published
    // showcase catalog and the repository's own documentation
    // (web/tooling/discovery.ts). The catalog is read from wherever this mode
    // sends the page for data (web/.env.deploy: the public bucket). Each
    // dataset's page is the built viewer under that dataset's head, and the
    // dev and preview servers answer its path as Pages does.
    plugins: [
      suffixRanges(),
      modelPathRewrite(modelIds),
      discoveryFiles({
        repoRoot: import.meta.dirname,
        dataBaseUrl: env.VITE_DATA_BASE_URL,
        modelPaths: modelPagePaths(modelIds),
      }),
      modelPages({
        ids: modelIds,
        publicDir: resolve(import.meta.dirname, "web/public"),
        pagesDir: resolve(import.meta.dirname, "web"),
      }),
    ],
    build: {
      outDir: "../dist",
      emptyOutDir: true,
      sourcemap: true,
      // The live viewer (which also renders ?case=<id>), the historical
      // showcase list, the point comparison, the run metrics, the typhoon
      // page and the API console.
      rollupOptions: {
        input: {
          main: resolve(import.meta.dirname, "web/index.html"),
          showcase: resolve(import.meta.dirname, "web/showcase.html"),
          compare: resolve(import.meta.dirname, "web/compare.html"),
          metrics: resolve(import.meta.dirname, "web/metrics.html"),
          typhoon: resolve(import.meta.dirname, "web/typhoon.html"),
          api: resolve(import.meta.dirname, "web/api.html"),
          // The static airport pages' live block (web/tooling/airportpages.ts
          // writes the pages and links this entry's file).
          airport: resolve(import.meta.dirname, "web/src/airports/live.ts"),
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
      // jsdom's browser reads en-US, which would put every expectation in
      // Fahrenheit and miles; the suite reads in metric unless a test says.
      setupFiles: ["../tests/web/setup.ts"],
    },
  };
});

