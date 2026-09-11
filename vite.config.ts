import { resolve } from "node:path";

import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

import { discoveryFiles } from "./web/tooling/discovery";

export default defineConfig(({ mode }) => ({
  root: "web",
  publicDir: "public",
  base: "./",
  // sitemap.xml and llms-full.txt, derived at build time from the published
  // showcase catalog and the repository's own documentation
  // (web/tooling/discovery.ts). The catalog is read from wherever this mode
  // sends the page for data (web/.env.deploy: the public bucket).
  plugins: [
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

