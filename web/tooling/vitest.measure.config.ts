// The backend measurement harness alone (`npm run measure:backends`). It is
// kept out of the main vitest include so `npm run test:web` never depends on
// a run directory being present.
import { defineConfig } from "vitest/config";

export default defineConfig({
  root: "web",
  test: {
    environment: "jsdom",
    include: ["tooling/measure-backends.test.ts"],
  },
});
