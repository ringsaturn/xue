import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { fulfillWithRanges, routeStores, storeOnly, withoutStores } from "./artifacts";

/**
 * The Zarr channel end to end — the default path. The synthetic GFS
 * fixture's manifest carries `zarr` descriptors for tmp2m, prate and
 * wind10m and none for the upper-air and pressure bundles, so one run
 * exercises the store, the container a bundle without a store still opens,
 * and `?backend=xue`, the comparison switch back to the container. The
 * store is served the way a range-capable origin serves it — exact 206
 * answers for `bytes=a-b`, the object's tail for `bytes=-n` — and the
 * test watches that the channel reads it in ranges alone, that a frame
 * renders through it, and that the data card names the format.
 *
 * Two shapes of manifest the bucket does not hold yet are driven here too:
 * a run that ships only its stores (what the encoder writes once the
 * container is retired — the shell has to accept it before any encoder
 * writes it), streamed and, over an origin without ranges, read by whole
 * shards. The container's own suite, app.spec.ts, runs on the manifest of
 * a run published before the store existed.
 */

// The Protomaps API key is origin-locked to the production domains, so from
// 127.0.0.1 every tile request dies on CORS — and a map whose tiles never
// settle occasionally never fires "load", which is what gates initialize().
test.beforeEach(async ({ page }) => {
  await page.route("**/api.protomaps.com/**", (route) => route.fulfill({ status: 204, body: "" }));
});

const FIXTURE_ROOT = fileURLToPath(new URL("../fixtures/generated/web/", import.meta.url));
const MANIFEST_FIXTURE = JSON.parse(readFileSync(`${FIXTURE_ROOT}manifest.json`, "utf8"));
const LATEST_FIXTURE = JSON.parse(readFileSync(`${FIXTURE_ROOT}latest.json`, "utf8"));

interface StoreCounters {
  /** Range requests against store objects, and every 206's length in order. */
  ranged: number;
  lengths: number[];
  /** Whole-object requests against the whole-store index, `index.bin`,
   * which is read whole at open like the documents. */
  index: number;
  /** Whole-object requests against any other store object but a zarr.json. */
  full: number;
  /** Range requests against .xue bundles — the container path, which the
   * bundles without a store still take. */
  bundleRanged: number;
  /** Whole downloads of .xue bundles. */
  bundleFull: number;
}

/** Serves the pointer, the manifest and the posters. */
async function routeManifest(page: Page, manifest: unknown = MANIFEST_FIXTURE): Promise<void> {
  await page.route("**/data/latest.json*", (route) => route.fulfill({ json: LATEST_FIXTURE }));
  await page.route("**/data/gfs.*/manifest.json*", (route) => route.fulfill({ json: manifest as object }));
  await page.route("**/data/**/*.poster.bin?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const path = `${FIXTURE_ROOT}${name}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body: readFileSync(path) });
  });
}

/** Serves every store object and every bundle from the fixture directory.
 * `ranges: false` makes the origin one that serves nothing but whole
 * objects — stores and bundles alike. */
async function routeArtifacts(page: Page, counters: StoreCounters, ranges = true): Promise<void> {
  await routeStores(page, "**/data/gfs.*/*.zarr/**", FIXTURE_ROOT, {
    ranges,
    onRange: (_relative, length) => {
      counters.ranged += 1;
      counters.lengths.push(length);
    },
    onFull: (relative) => {
      if (relative.endsWith("/index.bin")) counters.index += 1;
      else counters.full += 1;
    },
  });
  await page.route("**/data/gfs.*/*.xue?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const path = `${FIXTURE_ROOT}${name}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    // The range-support probe (bytes=0-0) is not a read.
    const range = route.request().headers()["range"];
    return fulfillWithRanges(
      readFileSync(path),
      range,
      () => {
        counters.bundleRanged += 1;
      },
      () => {
        if (range === undefined) counters.bundleFull += 1;
      },
      ranges,
    )(route);
  });
}

function newCounters(): StoreCounters {
  return { ranged: 0, lengths: [], index: 0, full: 0, bundleRanged: 0, bundleFull: 0 };
}

async function expectReadyOnStore(page: Page, format = "Zarr"): Promise<void> {
  await expect(page.locator("#preload-format")).toHaveText(format);
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
}

/** A frame decoded through the channel is on screen: the readout follows
 * a scrub, which only completes once the plane arrives. */
async function scrubOneFrame(page: Page): Promise<void> {
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
  await expect(page.locator("#forecast-hour")).toHaveText("F001");
}

test("the default layer plays from its store without being asked", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  // The data card names the channel. The session streams: nothing is
  // downloaded whole, and with reduced motion playback never starts, so the
  // windowed prefetch stays around the playhead and the axis never becomes
  // fully resident — the same shape the container's streaming session has.
  await expectReadyOnStore(page);
  await expect(page.locator("#preload-state")).toHaveText("Streaming on demand");
  await scrubOneFrame(page);
  // Beside the documents only the whole-store index was read whole; every
  // inner chunk came by offset, never as a whole shard, and the seeded
  // index left no per-shard suffix read to make.
  expect(counters.index).toBeGreaterThan(0);
  expect(counters.full).toBe(0);
  expect(counters.ranged).toBeGreaterThan(0);
  // The default layer has a store, so the container was never opened for it.
  expect(counters.bundleRanged).toBe(0);
  expect(counters.bundleFull).toBe(0);
});

test("?backend=xue takes the container of a bundle that ships both", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/?backend=xue");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expectReadyOnStore(page, "Xue");
  await scrubOneFrame(page);
  expect(counters.bundleRanged).toBeGreaterThan(0);
  expect(counters.ranged).toBe(0);
  expect(counters.index).toBe(0);
});

test("a run published before the store existed plays from its container", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page, withoutStores(MANIFEST_FIXTURE));
  await routeArtifacts(page, counters);
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expectReadyOnStore(page, "Xue");
  await scrubOneFrame(page);
  expect(counters.bundleRanged).toBeGreaterThan(0);
  expect(counters.ranged + counters.index + counters.full).toBe(0);
});

test("a bundle without a store stays on the container beside one that has it", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/?type=temp");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expectReadyOnStore(page);
  // The pressure family's tile opens the 500 hPa height, which ships no
  // store in the fixture, so it opens its .xue and the card says so.
  await page.getByRole("button", { name: "PRESSURE FIELD" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  await expect(page.locator("#preload-format")).toHaveText("Xue");
  expect(counters.bundleRanged).toBeGreaterThan(0);
});

test("the wind pair decodes both components through the store", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/?type=wind");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "wind10m");
  await expect(page.locator("#legend-unit")).toHaveText("m/s");
  await expectReadyOnStore(page);
  await scrubOneFrame(page);
  expect(counters.index).toBeGreaterThan(0);
  expect(counters.full).toBe(0);
});

test("a run that ships only stores is accepted and streamed", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page, storeOnly(MANIFEST_FIXTURE));
  await routeArtifacts(page, counters);
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expectReadyOnStore(page);
  await expect(page.locator("#preload-state")).toHaveText("Streaming on demand");
  await scrubOneFrame(page);
  expect(counters.full).toBe(0);
  expect(counters.ranged).toBeGreaterThan(0);
  expect(counters.bundleRanged + counters.bundleFull).toBe(0);
});

test("without ranges a store-only bundle is read by whole shards", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page, storeOnly(MANIFEST_FIXTURE));
  await routeArtifacts(page, counters, false);
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expectReadyOnStore(page);
  await scrubOneFrame(page);
  // Frames 0 and 1 share the first time chunk: one shard, fetched whole
  // once, and never a range.
  expect(counters.ranged).toBe(0);
  expect(counters.full).toBeGreaterThan(0);
  expect(counters.bundleRanged + counters.bundleFull).toBe(0);
});

test("without ranges a bundle that ships both downloads its container before reading whole shards", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters, false);
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expect(page.locator("#preload-format")).toHaveText("Xue");
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 20_000 });
  expect(counters.bundleFull).toBe(1);
  expect(counters.full).toBe(0);
});
