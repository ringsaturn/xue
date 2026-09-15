import { expect, test, type Page, type Route } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * The Zarr channel end to end: `?backend=zarr` on a run whose manifest
 * carries `zarr` descriptors (the synthetic GFS fixture ships stores for
 * tmp2m, prate and wind10m, and none for the upper-air and pressure
 * bundles). The store is served the way a range-capable origin serves it —
 * exact 206 answers for `bytes=a-b`, the object's tail for `bytes=-n` — and
 * the test watches that the channel reads it in ranges alone, that a frame
 * renders through it, and that the data card names the format.
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
}

/** Serves the pointer, the manifest and the posters. */
async function routeManifest(page: Page): Promise<void> {
  await page.route("**/data/latest.json*", (route) => route.fulfill({ json: LATEST_FIXTURE }));
  await page.route("**/data/gfs.*/manifest.json*", (route) => route.fulfill({ json: MANIFEST_FIXTURE }));
  await page.route("**/data/**/*.poster.bin?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const path = `${FIXTURE_ROOT}${name}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body: readFileSync(path) });
  });
}

/** One range-capable answer for a fixture file, or the whole file. */
function fulfillWithRanges(
  body: Buffer,
  range: string | undefined,
  onRange: (length: number) => void,
  onFull: () => void,
): (route: Route) => Promise<void> {
  return async (route) => {
    const suffix = /^bytes=-(\d+)$/.exec(range ?? "");
    const span = /^bytes=(\d+)-(\d+)$/.exec(range ?? "");
    if (suffix || span) {
      const start = suffix ? Math.max(0, body.length - Number(suffix[1])) : Number(span![1]);
      const end = suffix ? body.length - 1 : Math.min(Number(span![2]), body.length - 1);
      onRange(end - start + 1);
      return route.fulfill({
        status: 206,
        contentType: "application/octet-stream",
        headers: { "accept-ranges": "bytes", "content-range": `bytes ${start}-${end}/${body.length}` },
        body: body.subarray(start, end + 1),
      });
    }
    onFull();
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
  };
}

/** Serves every store object and every bundle from the fixture directory. */
async function routeArtifacts(page: Page, counters: StoreCounters): Promise<void> {
  await page.route("**/data/gfs.*/*.zarr/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const relative = pathname.slice(pathname.indexOf("/data/gfs.") + "/data/".length).replace(/^gfs\.[^/]+\//, "");
    const path = `${FIXTURE_ROOT}${relative}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    const range = route.request().headers()["range"];
    const isDocument = relative.endsWith("zarr.json");
    return fulfillWithRanges(
      readFileSync(path),
      range,
      (length) => {
        // The range-support probe (bytes=0-0 on zarr.json) is not a read.
        if (isDocument) return;
        counters.ranged += 1;
        counters.lengths.push(length);
      },
      () => {
        if (isDocument) return;
        if (relative.endsWith("/index.bin")) counters.index += 1;
        else counters.full += 1;
      },
    )(route);
  });
  await page.route("**/data/gfs.*/*.xue?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const path = `${FIXTURE_ROOT}${name}`;
    if (!existsSync(path)) return route.fulfill({ status: 404, body: "missing" });
    return fulfillWithRanges(
      readFileSync(path),
      route.request().headers()["range"],
      () => {
        counters.bundleRanged += 1;
      },
      () => {},
    )(route);
  });
}

function newCounters(): StoreCounters {
  return { ranged: 0, lengths: [], index: 0, full: 0, bundleRanged: 0 };
}

test("?backend=zarr plays the default layer from its store", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/?backend=zarr");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  // The data card names the channel. The session streams: nothing is
  // downloaded whole, and with reduced motion playback never starts, so the
  // windowed prefetch stays around the playhead and the axis never becomes
  // fully resident — the same shape the container's streaming session has.
  await expect(page.locator("#preload-format")).toHaveText("Zarr");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  await expect(page.locator("#preload-state")).toHaveText("Streaming on demand");
  // A frame decoded through the channel is on screen: the readout follows a
  // scrub, which only completes once the plane arrives.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
  await expect(page.locator("#forecast-hour")).toHaveText("F001");
  // Beside the documents only the whole-store index was read whole; every
  // inner chunk came by offset, never as a whole shard, and the seeded
  // index left no per-shard suffix read to make.
  expect(counters.index).toBeGreaterThan(0);
  expect(counters.full).toBe(0);
  expect(counters.ranged).toBeGreaterThan(0);
  // The default layer has a store, so the container was never opened for it.
  expect(counters.bundleRanged).toBe(0);
});

test("a bundle without a store stays on the container under ?backend=zarr", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeManifest(page);
  await routeArtifacts(page, counters);
  await page.goto("/?backend=zarr&type=temp");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expect(page.locator("#preload-format")).toHaveText("Zarr");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
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
  await page.goto("/?backend=zarr&type=wind");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "wind10m");
  await expect(page.locator("#legend-unit")).toHaveText("m/s");
  await expect(page.locator("#preload-format")).toHaveText("Zarr");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
  expect(counters.index).toBeGreaterThan(0);
  expect(counters.full).toBe(0);
});
