import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  publicDir: "public",
  base: "./",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    sourcemap: true,
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

