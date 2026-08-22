import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The Protomaps API key is origin-locked to the production domains, so from
// 127.0.0.1 every tile request dies on CORS — and a map whose tiles never
// settle occasionally never fires "load", which is what gates initialize().
// Empty tiles keep the basemap (and the network) out of the tests entirely.
test.beforeEach(async ({ page }) => {
  await page.route("**/api.protomaps.com/**", (route) => route.fulfill({ status: 204, body: "" }));
});

const TMP2M_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/tmp2m.xue", import.meta.url)),
);
const PRATE_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/prate.xue", import.meta.url)),
);
const WIND_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/wind10m.xue", import.meta.url)),
);
const MANIFEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/manifest.json", import.meta.url)),
    "utf8",
  ),
);
const LATEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/latest.json", import.meta.url)),
    "utf8",
  ),
);
const POSTER_FIXTURES: Record<string, Buffer> = {
  "tmp2m.poster.bin": readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/tmp2m.poster.bin", import.meta.url)),
  ),
  "prate.poster.bin": readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/prate.poster.bin", import.meta.url)),
  ),
};
// The ECMWF model is its own dataset: own live pointer, own run directory,
// own 3-hourly time axis.
const ECMWF_TMP2M_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/ecmwf/tmp2m.xue", import.meta.url)),
);
const ECMWF_PRATE_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/ecmwf/prate.xue", import.meta.url)),
);
const ECMWF_MANIFEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/ecmwf/manifest.json", import.meta.url)),
    "utf8",
  ),
);
const ECMWF_LATEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/latest-ecmwf.json", import.meta.url)),
    "utf8",
  ),
);
// The GFS-SFLUX model: hourly axis, prate without an
// analysis frame, and the optional dswrf solar-radiation bundle.
const SFLUX_FIXTURES: Record<string, Buffer> = {
  "tmp2m.xue": readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/sflux/tmp2m.xue", import.meta.url)),
  ),
  "prate.xue": readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/sflux/prate.xue", import.meta.url)),
  ),
  "dswrf.xue": readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/sflux/dswrf.xue", import.meta.url)),
  ),
};
const SFLUX_MANIFEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/sflux/manifest.json", import.meta.url)),
    "utf8",
  ),
);
const SFLUX_LATEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../fixtures/generated/web/latest-sflux.json", import.meta.url)),
    "utf8",
  ),
);

interface BundleCounters {
  tmp2m: number;
  prate: number;
}

/** Serves the two-layer manifest contract: the mutable latest.json pointer,
 * the immutable per-run manifest it names, and the per-variable posters. */
async function routeManifest(page: Page): Promise<void> {
  await page.route("**/data/latest.json*", (route) => route.fulfill({ json: LATEST_FIXTURE }));
  await page.route("**/data/gfs.*/manifest.json*", (route) => route.fulfill({ json: MANIFEST_FIXTURE }));
  await page.route("**/data/latest-ecmwf.json*", (route) => route.fulfill({ json: ECMWF_LATEST_FIXTURE }));
  await page.route("**/data/ecmwf.*/manifest.json*", (route) => route.fulfill({ json: ECMWF_MANIFEST_FIXTURE }));
  await page.route("**/data/latest-sflux.json*", (route) => route.fulfill({ json: SFLUX_LATEST_FIXTURE }));
  await page.route("**/data/sflux.*/manifest.json*", (route) => route.fulfill({ json: SFLUX_MANIFEST_FIXTURE }));
  await page.route("**/data/**/*.poster.bin?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const body = POSTER_FIXTURES[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
  });
}

async function routeBundle(
  page: Page,
  counters?: BundleCounters,
  prateBody?: Buffer,
): Promise<void> {
  // Bundle URLs carry a ?v=<crc32> cache-busting query, so match with a
  // trailing wildcard and test the pathname, not the full URL.
  await page.route("**/data/**/*.xue?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    // Replying 200 to the app's range-support probe selects the full-download
    // path; probes are not full downloads, so keep them out of the counters.
    const isProbe = route.request().headers()["range"] !== undefined;
    if (counters && !isProbe && (name === "tmp2m.xue" || name === "prate.xue")) {
      counters[name === "tmp2m.xue" ? "tmp2m" : "prate"] += 1;
    }
    const pathname = new URL(route.request().url()).pathname;
    const isEcmwf = pathname.includes("/ecmwf.");
    const isSflux = pathname.includes("/sflux.");
    const body = isSflux
      ? SFLUX_FIXTURES[name]
      : isEcmwf
        ? name === "tmp2m.xue"
          ? ECMWF_TMP2M_FIXTURE
          : ECMWF_PRATE_FIXTURE
        : name === "tmp2m.xue"
          ? TMP2M_FIXTURE
          : name === "wind10m.xue"
            ? WIND_FIXTURE
            : (prateBody ?? PRATE_FIXTURE);
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      body,
    });
  });
}

interface RangeCounters {
  ranged: number;
  full: number;
}

/** Serves bundles like a range-capable host (R2): exact 206 responses for
 * single byte ranges, 200 with the whole body otherwise. */
async function routeBundleWithRanges(page: Page, counters?: RangeCounters): Promise<void> {
  await page.route("**/data/**/*.xue?*", (route) => {
    const isTemperature = new URL(route.request().url()).pathname.endsWith("tmp2m.xue");
    const body = isTemperature ? TMP2M_FIXTURE : PRATE_FIXTURE;
    const match = /^bytes=(\d+)-(\d+)$/.exec(route.request().headers()["range"] ?? "");
    if (match) {
      const start = Number(match[1]);
      const end = Math.min(Number(match[2]), body.length - 1);
      if (counters) counters.ranged += 1;
      return route.fulfill({
        status: 206,
        contentType: "application/octet-stream",
        headers: {
          "accept-ranges": "bytes",
          "content-range": `bytes ${start}-${end}/${body.length}`,
        },
        body: body.subarray(start, end + 1),
      });
    }
    if (counters) counters.full += 1;
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
  });
}

async function waitForReady(page: Page): Promise<void> {
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 20_000 });
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
}

test("missing manifest shows a recoverable error", async ({ page }) => {
  await page.route("**/data/latest.json*", (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("HTTP 404");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("bundle download failure keeps animation controls disabled", async ({ page }) => {
  await routeManifest(page);
  await page.route("**/data/**/*.xue?*", (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.goto("/");
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(page.getByRole("alert")).toContainText("HTTP 404");
  await expect(slider).toBeDisabled();
  await expect(page.getByRole("button", { name: "Play animation" })).toBeDisabled();
  await expect(page.locator("#data-card")).toContainText("Data loading interrupted");
});

test("corrupted bundle fails checksum verification and shows an error", async ({ page }) => {
  await routeManifest(page);
  const corrupted = Buffer.from(PRATE_FIXTURE);
  corrupted[corrupted.length - 100] ^= 0xff;
  await routeBundle(page, undefined, corrupted);
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("checksum", { timeout: 20_000 });
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeDisabled();
});

test("initial load downloads only the selected variable's bundle", async ({ page }) => {
  const counters: BundleCounters = { tmp2m: 0, prate: 0 };
  await routeManifest(page);
  await routeBundle(page, counters);
  await page.goto("/");
  await waitForReady(page);
  await expect(page.locator("#preload-percent")).toHaveText("100%");
  await expect(page.locator("#preload-bytes")).not.toHaveText("0 B");
  await expect(page.getByRole("button", { name: "Pause animation" })).toBeVisible();
  expect(counters.tmp2m).toBe(0);
  expect(counters.prate).toBe(1);
});

test("stats panel opens from the map context menu and closes from the card", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // Once loading finishes the buffer card retires; only the context menu's
  // 「详细统计信息」 pins it back, YouTube style.
  const dataCard = page.locator("#data-card");
  await expect(dataCard).toBeHidden();
  await page.locator("#map").click({ button: "right", position: { x: 620, y: 300 } });
  const menu = page.getByRole("menu", { name: "Map options" });
  await expect(menu).toBeVisible();
  await menu.getByRole("menuitemcheckbox", { name: "Stats for nerds" }).click();
  await expect(menu).toBeHidden();
  await expect(dataCard).toBeVisible();
  await expect(page.locator("#stat-dataset")).toContainText("gfs.");
  await expect(page.locator("#stat-grid")).not.toHaveText("--");
  await expect(page.locator("#stat-decode-rate")).toContainText("/s");
  await expect(page.locator("#stat-graph")).toBeVisible();
  await page.locator("#stats-close").click();
  await expect(dataCard).toBeHidden();
});

test("switching variables downloads the other bundle once and keeps both resident", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters: BundleCounters = { tmp2m: 0, prate: 0 };
  await routeManifest(page);
  await routeBundle(page, counters);
  await page.goto("/");
  await waitForReady(page);
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page.locator("#variable-title")).toContainText("Temperature");
  await expect(page.locator("#legend-unit")).toHaveText("°C");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled();
  expect(counters).toEqual({ tmp2m: 1, prate: 1 });
  await page.getByRole("button", { name: "PRECIP RATE" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  expect(counters).toEqual({ tmp2m: 1, prate: 1 });
});

test("wind variable activates the particle layer session", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const windButton = page.getByRole("button", { name: "WIND 10M" });
  await expect(windButton).toBeVisible();
  await windButton.click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "wind10m");
  await expect(page.locator("#legend-unit")).toHaveText("m/s");
  await expect(page.locator("#variable-title")).toContainText("Wind");
  await expect(page.locator("#preload-format")).toHaveText("Xue");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled();
  // Scrubbing a wind frame requires both u and v planes to decode.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
});

test("switching to ECMWF loads its own run on a mixed-cadence 240-hour timeline", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  await page.getByRole("button", { name: "ECMWF IFS 0.25°" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-model", "ecmwf");
  await waitForReady(page);
  await expect(page).toHaveURL(/model=ecmwf/);
  await expect(page.locator("#variable-code")).toContainText("ECMWF");
  // The mixed axis (schemaVersion 2 bundles): 3-hourly to 144 hours, then
  // 6-hourly to 240. The de-accumulated prate has no analysis frame:
  // 49 + 16 - 1 = 64 frames starting at F003; the horizon reads +240H.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toHaveAttribute("max", "63");
  await expect(page.locator("#forecast-hour")).toHaveText("F003");
  await expect(page.locator("#track-horizon")).toHaveText("+240H");
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F006");
  // Temperature keeps its analysis frame: 65 frames from F000, and the
  // playhead stays on the same forecast hour across the axis change.
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expect(slider).toHaveAttribute("max", "64");
  await expect(page.locator("#forecast-hour")).toHaveText("F006");
  // The 6-hourly tail past the cadence change scrubs like any other frame.
  await slider.focus();
  await page.keyboard.press("End");
  await expect(page.locator("#forecast-hour")).toHaveText("F240");
  // And back: GFS re-tunes to its own hourly axis.
  await page.getByRole("button", { name: "GFS NOAA 0.25°" }).click();
  await waitForReady(page);
  await expect(page.locator("body")).toHaveAttribute("data-model", "gfs");
  await expect(slider).toHaveAttribute("max", "120");
});

test("the SFLUX station reveals and renders the solar radiation layer", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // GFS ships no dswrf bundle, so the SOLAR button stays hidden there.
  await expect(page.getByRole("button", { name: "SOLAR FLUX" })).toBeHidden();
  await page.getByRole("button", { name: "GFS SFLUX 13KM" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-model", "sflux");
  await waitForReady(page);
  await expect(page).toHaveURL(/model=sflux/);
  const solarButton = page.getByRole("button", { name: "SOLAR FLUX" });
  await expect(solarButton).toBeVisible();
  // The de-averaged prate has no analysis frame: 120 hourly frames from F001.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toHaveAttribute("max", "119");
  await expect(page.locator("#forecast-hour")).toHaveText("F001");
  await solarButton.click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "dswrf");
  await expect(page.locator("#legend-unit")).toHaveText("W/m²");
  await expect(page.locator("#variable-title")).toContainText("Radiation");
  await expect(page.locator("#variable-code")).toContainText("GFS-SFLUX");
  await expect(page).toHaveURL(/model=sflux&type=solar/);
  // dswrf keeps its analysis frame: 121 hourly frames from F000.
  await expect(slider).toHaveAttribute("max", "120");
  await expect(slider).toBeEnabled();
  // Back on GFS the solar button hides again and the selection falls back.
  await page.getByRole("button", { name: "GFS NOAA 0.25°" }).click();
  await waitForReady(page);
  await expect(page.locator("body")).toHaveAttribute("data-model", "gfs");
  await expect(page.getByRole("button", { name: "SOLAR FLUX" })).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
});

test("URL entry ?model=gfs&type=wind opens the wind layer directly", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?model=gfs&type=wind");
  await expect(page.locator("body")).toHaveAttribute("data-variable", "wind10m", { timeout: 20_000 });
  await expect(page.locator("#legend-unit")).toHaveText("m/s");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
});

test("unknown type in the URL falls back to the default variable", async ({ page }) => {
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?model=gfs&type=vorticity");
  await waitForReady(page);
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expect(page).toHaveURL(/model=gfs&type=precip/);
});

test("switching variables updates the shareable URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  await expect(page).toHaveURL(/model=gfs&type=precip/);
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page).toHaveURL(/model=gfs&type=temp/);
  await page.getByRole("button", { name: "WIND 10M" }).click();
  await expect(page).toHaveURL(/model=gfs&type=wind/, { timeout: 20_000 });
});

test("cold start fetches the selected variable's first-frame poster", async ({ page }) => {
  const posters: string[] = [];
  await routeManifest(page);
  await routeBundle(page);
  page.on("request", (request) => {
    if (request.url().includes(".poster.bin")) posters.push(request.url());
  });
  await page.goto("/");
  await waitForReady(page);
  expect(posters.length).toBeGreaterThan(0);
  expect(posters[0]).toContain("prate.poster.bin");
});

test("animation starts automatically, advances, and can pause", async ({ page }) => {
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  const pause = page.getByRole("button", { name: "Pause animation" });
  await expect(pause).toBeVisible();
  await expect(slider).not.toHaveValue("0", { timeout: 10_000 });
  await pause.click();
  await expect(page.getByRole("button", { name: "Play animation" })).toBeVisible();
  const pausedAt = await slider.inputValue();
  await page.waitForTimeout(1_400);
  await expect(slider).toHaveValue(pausedAt);
});

test("reduced motion disables autostart and keeps keyboard scrubbing", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(page.getByRole("button", { name: "Play animation" })).toBeEnabled();
  await page.waitForTimeout(1_200);
  await expect(slider).toHaveValue("0");
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("1");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
});

test("range-capable server streams on demand and never downloads the full body", async ({ page }) => {
  // Full residency needs several playback loops, and the mobile emulation
  // takes the longest: ~53 s measured on a CI-class runner. Budget well past
  // that (test.slow()'s 90 s left no room) so only a real stall fails.
  test.setTimeout(180_000);
  const counters: RangeCounters = { ranged: 0, full: 0 };
  await routeManifest(page);
  await routeBundleWithRanges(page, counters);
  await page.goto("/");
  // Streaming mode: the page becomes interactive on the structural prefix.
  // With windowed prefetch the bundle only becomes fully resident as
  // playback sweeps the timeline, so residency takes roughly one full loop —
  // and more than one whenever a group's range fetch misses its window pass.
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 120_000 });
  await expect(page.locator("#preload-percent")).toHaveText("100%");
  await expect(page.locator("#preload-format")).toHaveText("Xue");
  expect(counters.ranged).toBeGreaterThan(1);
  expect(counters.full).toBe(0);
});

test("scrubbing works while data arrives through range requests", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundleWithRanges(page);
  await page.goto("/");
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toBeEnabled({ timeout: 20_000 });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("1");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
});

// The UI locale follows navigator.language (Playwright defaults to en-US, so
// every other test runs the English UI); ?lang= overrides it, and the footer
// toggle persists the other language and reloads onto it. The basemap label
// language rides the same detection, but the tests stub out the tile API.
test("?lang=zh renders the Chinese UI and the footer toggle switches back", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?lang=zh");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator("#preload-state")).toHaveText("数据包已驻留内存", { timeout: 20_000 });
  await expect(page.getByRole("slider", { name: "预报时次" })).toBeEnabled({ timeout: 20_000 });
  // toHaveText (not toBeVisible): the station panel starts collapsed on
  // phone-sized viewports, hiding the variable buttons.
  const tempLabel = page.locator('button[data-variable="tmp2m"] small');
  await expect(tempLabel).toHaveText("气温");
  const toggle = page.locator("#lang-toggle");
  await expect(toggle).toHaveText("ENGLISH");
  await toggle.click();
  await expect(page).toHaveURL(/lang=en/);
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(tempLabel).toHaveText("2M");
  await expect(toggle).toHaveText("中文");
});

test("rapid scrubbing settles on the final slider value", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.evaluate((element: HTMLInputElement) => {
    for (let value = 0; value <= 120; value += 7) {
      element.value = String(value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
    }
    element.value = "97";
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#forecast-hour")).toHaveText("F097");
  await expect(slider).toHaveValue("97");
});
