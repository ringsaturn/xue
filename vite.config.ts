import { resolve } from "node:path";

import { defineConfig } from "vitest/config";

import { discoveryFiles } from "./web/tooling/discovery";

export default defineConfig({
  root: "web",
  publicDir: "public",
  base: "./",
  // sitemap.xml and llms-full.txt, derived from the case definitions and the
  // repository's own documentation at build time (web/tooling/discovery.ts).
  plugins: [discoveryFiles({ repoRoot: import.meta.dirname })],
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
});

