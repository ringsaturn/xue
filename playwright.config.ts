import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  // A test here is a WebGL2 map under software GL plus a decode worker on a
  // four-core runner, and every step is a few rendered frames: a click
  // during playback costs 5-12 s and an ordinary test 20-29 s, a slow
  // runner tips it over. The bound is what a hung test costs, not what a
  // passing one takes, so give it twice the measured worst case.
  timeout: 60_000,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // These tests drive a WebGL2 map and a decode worker, so two of them on a
  // four-core runner starve each other: the streaming residency test took
  // 46-84 s against 53 s alone. One worker trades wall clock for a stable run.
  workers: process.env.CI ? 1 : undefined,
  // The default reporter writes nothing to disk; CI uploads the HTML report.
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["iPhone 13"], browserName: "chromium" } },
  ],
});
