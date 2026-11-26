import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";

import { fulfillWithRanges, routeStores, storeOnly, withoutStores } from "./artifacts";
import { fixtureJson, fixturePath, stubBasemap } from "./fixtures";

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
 * The fixture's stores are shared (profile 2): every bundle of a tier
 * names one `bundles.zarr` (or `bundles.half.zarr`) under one `?v=`, and a
 * session reads its own bundle out of it.
 *
 * Two shapes of manifest the bucket does not hold yet are driven here too:
 * a run that ships only its stores (what the encoder writes once the
 * container is retired — the shell has to accept it before any encoder
 * writes it), streamed and, over an origin without ranges, read by whole
 * shards. The container's own suite, app.spec.ts, runs on the manifest of
 * a run published before the store existed.
 */

test.beforeEach(({ page }) => stubBasemap(page));

const FIXTURE_ROOT = fixturePath("");
const MANIFEST_FIXTURE = fixtureJson("manifest.json");
const LATEST_FIXTURE = fixtureJson("latest.json");

interface StoreCounters {
  /** Range requests against store objects, and every 206's length in order. */
  ranged: number;
  lengths: number[];
  /** Whole-object requests against any store object but a zarr.json. */
  full: number;
  /** Range requests against .xue bundles — the container path, which the
   * bundles without a store still take. */
  bundleRanged: number;
  /** Whole downloads of .xue bundles. */
  bundleFull: number;
  /** Every store request's store path and `?v=`, in order. */
  stores: { store: string; version: string | null }[];
}

interface ManifestStores {
  bundles: {
    variable: string;
    zarr?: { path?: string; crc32?: string };
    series?: { path: string; crc32: string };
    variants?: { zarr?: { path?: string; crc32?: string } }[];
  }[];
}

/** Every store path the manifest names, with the `?v=` of each entry naming it. */
function manifestStores(manifest: ManifestStores): Map<string, { versions: Set<string>; bundles: string[] }> {
  const stores = new Map<string, { versions: Set<string>; bundles: string[] }>();
  for (const bundle of manifest.bundles) {
    for (const zarr of [bundle.zarr, ...(bundle.variants ?? []).map((variant) => variant.zarr)]) {
      if (zarr?.path === undefined || zarr.crc32 === undefined) continue;
      const entry = stores.get(zarr.path) ?? { versions: new Set<string>(), bundles: [] };
      entry.versions.add(zarr.crc32);
      entry.bundles.push(bundle.variable);
      stores.set(zarr.path, entry);
    }
  }
  return stores;
}

/** The fixture names one shared store per tier, each under one `?v=` on
 * every entry, and the channel read nothing but those, under those. */
function expectSharedStoreReads(counters: StoreCounters): void {
  const stores = manifestStores(MANIFEST_FIXTURE);
  expect([...stores.keys()].sort()).toEqual(["bundles.half.zarr", "bundles.zarr"]);
  for (const path of ["bundles.zarr", "bundles.half.zarr"]) {
    expect(stores.get(path)!.versions.size).toBe(1);
    expect(stores.get(path)!.bundles).toEqual(["tmp2m", "prate", "wind10m"]);
  }
  // A probe session reads a bundle's own series store, which stays one
  // bundle's; anything else read is a shared map store.
  const series = new Map(MANIFEST_FIXTURE.bundles.flatMap((bundle: ManifestStores["bundles"][number]) =>
    bundle.series ? [[bundle.series.path, bundle.series.crc32] as const] : [],
  ));
  expect(counters.stores.length).toBeGreaterThan(0);
  for (const { store, version } of counters.stores) {
    if (series.has(store)) {
      expect(version).toBe(series.get(store));
      continue;
    }
    expect(stores.has(store)).toBe(true);
    expect(version).toBe([...stores.get(store)!.versions][0]);
  }
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
    onRequest: (store, version) => {
      counters.stores.push({ store, version });
    },
    onRange: (_relative, length) => {
      counters.ranged += 1;
      counters.lengths.push(length);
    },
    onFull: () => {
      counters.full += 1;
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
  return { ranged: 0, lengths: [], full: 0, bundleRanged: 0, bundleFull: 0, stores: [] };
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
  // Nothing but the documents was read whole: the shard's index came as a
  // suffix range and every inner chunk by offset, never the whole shard.
  expect(counters.full).toBe(0);
  expect(counters.ranged).toBeGreaterThan(0);
  // The default layer has a store, so the container was never opened for it.
  expect(counters.bundleRanged).toBe(0);
  expect(counters.bundleFull).toBe(0);
  expectSharedStoreReads(counters);
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
  expect(counters.ranged + counters.full).toBe(0);
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
  // The pressure switch draws the 500 hPa height over the field, and the
  // level row's ALONE member leaves it by itself; it ships no store in the
  // fixture, so it opens its .xue and the card says so.
  await page.getByRole("button", { name: "PRESSURE LINES" }).click();
  await page.locator("#level-row button[data-alone]").click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  // The lines' session, opened at the overlay's half tier, stays resident
  // as the view: still the container, whichever tier.
  await expect(page.locator("#preload-format")).toContainText("Xue");
  await expect(page.locator("#preload-format")).not.toContainText("Zarr");
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
  expect(counters.full).toBe(0);
  expect(counters.ranged).toBeGreaterThan(0);
  // Both components are the wind bundle's arrays in the tier's shared store.
  expectSharedStoreReads(counters);
  expect(counters.stores.some(({ store }) => store === "bundles.zarr" || store === "bundles.half.zarr")).toBe(true);
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
  // The array is one shard, fetched whole once — the whole bundle, as the
  // container's whole download was — and never a range.
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

test("a pinned point reads a run's column store in place of a store per bundle", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeArtifacts(page, counters);
  const columns = fixtureJson("columns.json") as { path: string; crc32: string };
  const row = (name: string) => page.locator(`#probe-rows .probe-row[data-row="${name}"]`);
  /** Pin the same point on a run and give what its rows read. */
  const readings = async (manifest: unknown): Promise<string[]> => {
    await routeManifest(page, manifest);
    await page.goto("/");
    await expectReadyOnStore(page);
    await page.locator("#map").click({ position: { x: 620, y: 300 } });
    for (const name of ["temperature", "precipitation", "wind"]) {
      await expect(row(name)).toHaveAttribute("data-state", "complete", { timeout: 20_000 });
    }
    // The wind row also waits on the gust, which no store of either kind holds.
    await expect(row("wind").locator(".probe-row-value")).toHaveText(/^\d+\.\d+ · \d+\.\d+$/, { timeout: 20_000 });
    return Promise.all(["temperature", "precipitation", "wind"].map((name) => row(name).locator(".probe-row-value").innerText()));
  };

  const perBundle = await readings(MANIFEST_FIXTURE);
  expect(counters.stores.some(({ store }) => store === "tmp2m.series.zarr")).toBe(true);
  expect(counters.stores.some(({ store }) => store === columns.path)).toBe(false);

  // A runner slow enough to outlast the wait for the store reads the run as
  // one without it, which the page says on its console; the page is then
  // opened again.
  let unopened = true;
  page.on("console", (message) => {
    if (message.text().startsWith("column store: not opened")) unopened = true;
  });
  let fromColumns: string[] = [];
  for (let attempt = 0; unopened && attempt < 4; attempt += 1) {
    unopened = false;
    counters.stores = [];
    fromColumns = await readings({ ...MANIFEST_FIXTURE, columns });
  }
  // The same numbers, with no session opened for a bundle the store serves.
  expect(fromColumns).toEqual(perBundle);
  const opened = counters.stores.filter(({ store }) => store === columns.path);
  expect(opened.length).toBeGreaterThan(0);
  expect(opened.every(({ version }) => version === columns.crc32)).toBe(true);
  expect(counters.stores.filter(({ store }) => store.endsWith(".series.zarr"))).toEqual([]);
  // Nothing but the documents was read whole.
  expect(counters.full).toBe(0);
});

test("a link's cross-section is read out of the column store, and an Escape drops it", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeArtifacts(page, counters);
  const columns = fixtureJson("section.json") as { path: string; crc32: string };
  await routeStores(page, `**/data/gfs.*/${columns.path}/**`, `${FIXTURE_ROOT}section/`, {
    onRequest: (store, version) => {
      counters.stores.push({ store, version });
    },
    onFull: () => {
      counters.full += 1;
    },
  });
  await routeManifest(page, { ...MANIFEST_FIXTURE, columns });
  await page.goto("/?section=39.90,116.40,35.70,139.70");
  await expectReadyOnStore(page);

  const panel = page.locator("#section-panel");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  // The whole panel, and nothing left to say of the read.
  await expect(panel).toHaveAttribute("data-collapsed", "false", { timeout: 20_000 });
  await expect(panel.locator(".section-notice")).toHaveText("", { timeout: 20_000 });
  await expect(panel.locator(".section-ends")).toHaveText("A 39.90°N 116.40°E – B 35.70°N 139.70°E");
  await expect(panel.locator(".section-pressures .section-tick").filter({ hasText: /^500$/ })).toBeVisible();
  // Heights come of a frame that was drawn.
  await expect(panel.locator(".section-heights .section-tick").first()).toBeVisible({ timeout: 20_000 });
  const read = counters.stores.filter(({ store }) => store === columns.path);
  expect(read.length).toBeGreaterThan(0);
  expect(read.every(({ version }) => version === columns.crc32)).toBe(true);
  expect(counters.full).toBe(0);
  expect(new URL(page.url()).searchParams.get("section")).toBe("39.90,116.40,35.70,139.70");
  await expect(page.locator(".view-control-section")).toHaveAttribute("aria-pressed", "true");

  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect.poll(() => new URL(page.url()).searchParams.has("section")).toBe(false);
});

test("the workbench reads a line's column store into three charts, and its gestures are history", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters = newCounters();
  await routeArtifacts(page, counters);
  const columns = fixtureJson("section.json") as { path: string; crc32: string };
  await routeStores(page, `**/data/gfs.*/${columns.path}/**`, `${FIXTURE_ROOT}section/`, {
    onRequest: (store, version) => {
      counters.stores.push({ store, version });
    },
    onFull: () => {
      counters.full += 1;
    },
  });
  await routeManifest(page, { ...MANIFEST_FIXTURE, columns });
  await page.goto("/workbench?section=39.90,116.40,35.70,139.70&p=500");

  const wb = page.locator("#workbench");
  const readout = wb.locator(".wb-column-profile output");
  const param = (name: string): string | null => new URL(page.url()).searchParams.get(name);
  // Read whole, with nothing left to say of it.
  await expect(readout).toHaveText(/^500 hPa · T .* · \d+ % · /, { timeout: 20_000 });
  await expect(page.locator("#wb-notice")).toHaveText("", { timeout: 20_000 });
  await expect(wb).toHaveAttribute("data-shape", "line");
  await expect(wb.locator(".wb-number")).toHaveCount(4);
  const read = counters.stores.filter(({ store }) => store === columns.path);
  expect(read.length).toBeGreaterThan(0);
  expect(read.every(({ version }) => version === columns.crc32)).toBe(true);
  expect(counters.full).toBe(0);
  const requests = read.length;

  // One press of a step is one entry, and no read.
  await wb.getByRole("button", { name: "Higher" }).click();
  await expect.poll(() => param("p")).toBe("300");
  await expect(readout).not.toHaveText(/^500 hPa/);
  await page.goBack();
  await expect.poll(() => param("p")).toBe("500");
  await expect(readout).toHaveText(/^500 hPa/);

  // Keys on a chart move its own coordinate, and come to rest together.
  const along = wb.locator(".wb-plane.wb-where");
  await along.focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => param("point"), { timeout: 5_000 }).not.toBeNull();
  await page.goBack();
  await expect.poll(() => param("point")).toBeNull();

  // A press is a crosshair: it moves the probe and the height together, as
  // one entry.
  const box = (await along.boundingBox())!;
  const pressed = await page.evaluate(() => window.history.length);
  await page.mouse.click(box.x + box.width * 0.25, box.y + box.height * 0.3);
  await expect.poll(() => param("point")).not.toBeNull();
  expect(param("p")).not.toBe("500");
  expect(await page.evaluate(() => window.history.length)).toBe(pressed + 1);
  // How it is drawn is no entry.
  const entries = await page.evaluate(() => window.history.length);
  await wb.locator("select").selectOption("rh");
  await expect.poll(() => param("fill")).toBe("rh");
  expect(await page.evaluate(() => window.history.length)).toBe(entries);
  expect(counters.stores.filter(({ store }) => store === columns.path)).toHaveLength(requests);

  // The other form is a read of its own, of one cell.
  await wb.getByRole("button", { name: "Point", exact: true }).click();
  await expect(wb).toHaveAttribute("data-shape", "point");
  await expect(readout).toHaveText(/hPa · T /, { timeout: 20_000 });
  expect(param("section")).toBeNull();
});

test("the section panel and a pinned point lead to the workbench and back, at the same place and time", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeArtifacts(page, newCounters());
  const columns = fixtureJson("section.json") as { path: string; crc32: string };
  await routeStores(page, `**/data/gfs.*/${columns.path}/**`, `${FIXTURE_ROOT}section/`, { onRequest: () => {}, onFull: () => {} });
  await routeManifest(page, { ...MANIFEST_FIXTURE, columns });
  const param = (name: string): string | null => new URL(page.url()).searchParams.get(name);

  // The fixture's run is of 06Z; the link opens it an hour in, paused.
  await page.goto("/?section=39.90,116.40,35.70,139.70&time=2026-08-15T07Z");
  await expectReadyOnStore(page);
  await expect(page.locator("#forecast-hour")).toHaveText("F001");
  await expect.poll(() => param("time")).toBeNull();
  const link = page.locator("#section-panel .section-workbench");
  await expect(link).toBeVisible({ timeout: 20_000 });
  await link.click();
  await expect(page).toHaveURL(/\/workbench\?/);
  expect(param("section")).toBe("39.90,116.40,35.70,139.70");
  expect(param("time")).toBe("2026-08-15T07Z");
  await expect(page.locator(".wb-when .wb-coordinate-note").first()).toHaveText(/^F001/, { timeout: 20_000 });

  // The time has moved there, and comes back.
  await page.getByRole("button", { name: "Later" }).click();
  await expect.poll(() => param("time")).toBe("2026-08-15T08Z");
  await page.locator("#wb-open-map").click();
  await expectReadyOnStore(page);
  await expect(page.locator("#forecast-hour")).toHaveText("F002");
  await expect(page.locator("#section-panel")).toBeVisible({ timeout: 20_000 });

  // A pinned point is in the address, opens from it, and goes from it.
  await page.goto("/?point=36.60,136.64&time=2026-08-15T07Z");
  await expectReadyOnStore(page);
  await expect(page.locator("#probe-panel")).toBeVisible();
  await expect(page.locator("#probe-coords")).toContainText("36.6");
  await page.locator("#probe-workbench").click();
  await expect(page).toHaveURL(/\/workbench\?/);
  expect(param("point")).toBe("36.60,136.64");
  await expect(page.locator("#workbench")).toHaveAttribute("data-shape", "point");
  await page.goBack();
  await expectReadyOnStore(page);
  await expect(page.locator("#probe-panel")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect.poll(() => param("point")).toBeNull();
});
