import { resolve } from "node:path";

import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  publicDir: "public",
  base: "./",
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

