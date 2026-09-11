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
// One upper-air fill: the temperature family's 850 hPa member, which is what
// gives the temperature tile a level row of its own.
const TMP850_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/tmp850.xue", import.meta.url)),
);
// One level of the pressure family: the viewer draws it as contour lines,
// and it ships no poster, so switching to it exercises the path where nothing
// paints until the first real plane decodes.
const HGT500_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/hgt500.xue", import.meta.url)),
);
// Its half-resolution tier, which is what the level takes as lines over a
// filled field: the lines slot never needs the full grid.
const HGT500_HALF_FIXTURE = readFileSync(
  fileURLToPath(new URL("../fixtures/generated/web/hgt500.half.xue", import.meta.url)),
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
  /** Either tier of the height bundle; counted only when asked for. */
  hgt500?: number;
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
    if (counters?.hgt500 !== undefined && !isProbe && name.startsWith("hgt500.")) counters.hgt500 += 1;
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
          : name === "tmp850.xue"
            ? TMP850_FIXTURE
          : name === "wind10m.xue"
            ? WIND_FIXTURE
            : name === "hgt500.xue"
              ? HGT500_FIXTURE
              : name === "hgt500.half.xue"
                ? HGT500_HALF_FIXTURE
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
  /** Length of every 206 response, in request order — how a narrowed view
   * shows up: a whole temporal group is one long range, a viewport's tiles
   * are several short ones. */
  lengths: number[];
}

/** Serves bundles like a range-capable host (R2): exact 206 responses for
 * single byte ranges, 200 with the whole body otherwise. `latencyMs` holds
 * every response back, which is what keeps a frame pending long enough to
 * interact with it. */
async function routeBundleWithRanges(
  page: Page,
  counters?: RangeCounters,
  latencyMs = 0,
): Promise<void> {
  await page.route("**/data/**/*.xue?*", async (route) => {
    if (latencyMs > 0) await new Promise((resolve) => setTimeout(resolve, latencyMs));
    const isTemperature = new URL(route.request().url()).pathname.endsWith("tmp2m.xue");
    const body = isTemperature ? TMP2M_FIXTURE : PRATE_FIXTURE;
    const match = /^bytes=(\d+)-(\d+)$/.exec(route.request().headers()["range"] ?? "");
    if (match) {
      const start = Number(match[1]);
      const end = Math.min(Number(match[2]), body.length - 1);
      if (counters) {
        counters.ranged += 1;
        counters.lengths.push(end - start + 1);
      }
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

/** Pick a forecast model. The title block is the trigger; the switch itself
 * lives in the sheet it opens. */
async function pickModel(page: Page, name: string): Promise<void> {
  await page.locator("#model-trigger").click();
  await page.getByRole("button", { name }).click();
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

test("wind variable opens the speed field with its particle switch", async ({ page }, testInfo) => {
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
  // The colored speed field is the layer; the particles are an overlay with
  // its own switch, which this page opens off because it asks for reduced
  // motion — a frozen scatter of dots would be worse than none.
  const particles = page.getByRole("button", { name: "Toggle wind particle animation" });
  await expect(particles).toBeVisible();
  await expect(particles).toHaveAttribute("aria-pressed", "false");
  // A default is not a choice: the link carries nothing until the switch is
  // used, so a reduced-motion visitor does not hand out links that turn the
  // overlay off for everyone else.
  await expect(page).not.toHaveURL(/particles=/);
  await particles.click();
  await expect(particles).toHaveAttribute("aria-pressed", "true");
  await expect(page).not.toHaveURL(/particles=/);
  await particles.click();
  await expect(particles).toHaveAttribute("aria-pressed", "false");
  // Switched off by hand, the link says so.
  await expect(page).toHaveURL(/particles=off/);
  // The switch belongs to the wind layer alone.
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(particles).toBeHidden();
});

test("a pressure level loads as its own contour session", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // The rail carries one tile for the whole pressure family; the surface
  // itself is picked on the capsule's level row, which only appears once a
  // pressure layer is on screen.
  await expect(page.locator("#level-row")).toBeHidden();
  await page.getByRole("button", { name: "PRESSURE FIELD" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  await expect(page.locator("#level-row")).toBeVisible();
  const heightButton = page.getByRole("button", { name: "500MB HEIGHT" });
  await expect(heightButton).toBeVisible();
  await expect(heightButton).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#variable-title")).toContainText("500 hPa");
  await expect(page.locator("#legend-unit")).toHaveText("m");
  // The level names itself in the URL — there is no separate ?level=.
  await expect.poll(() => new URL(page.url()).searchParams.get("type")).toBe("hgt500");
  // A bundle with no poster still reaches a decoded frame and a live slider.
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled();
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
});

test("?lines= draws a pressure surface over the filled field", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const counters: BundleCounters = { tmp2m: 0, prate: 0, hgt500: 0 };
  await routeManifest(page);
  await routeBundle(page, counters);
  await page.goto("/?type=precip&lines=hgt500");
  await waitForReady(page);
  // The field is the view: its ground, its legend, its data card. The lines
  // are a second session over it, taking the half tier and loading after
  // the field rather than gating it.
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expect(page.locator("#legend-unit")).toHaveText("mm/h");
  await expect(page.locator("#preload-format")).toHaveText("Xue");
  await expect.poll(() => counters.hgt500).toBe(1);
  expect(counters.prate).toBe(1);
  // The level row belongs to the lines slot, wherever the lines are drawn.
  await expect(page.locator("#level-row")).toBeVisible();
  await expect(page.getByRole("button", { name: "500MB HEIGHT" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "PRECIP RATE" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "PRESSURE FIELD" })).toHaveAttribute("aria-pressed", "false");
  // The lines follow the timeline of the field.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#frame-tooltip")).toContainText("F001");
  // A fill switch keeps the lines: the level row stays, the URL carries both.
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expect(page.locator("#level-row")).toBeVisible();
  await expect(page).toHaveURL(/type=temp/);
  await expect(page).toHaveURL(/lines=hgt500/);
  expect(counters).toEqual({ tmp2m: 1, prate: 1, hgt500: 1 });
  // The pressure tile is the chart alone: the level names itself in `type`
  // and `lines` goes, so the two never contradict each other.
  await page.getByRole("button", { name: "PRESSURE FIELD" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  await expect(page).toHaveURL(/type=hgt500/);
  await expect(page).not.toHaveURL(/lines=/);
  await expect(page.locator("#legend")).toBeHidden();
  // Back to a field from the chart, and the lines stay over it.
  await page.getByRole("button", { name: "PRECIP RATE" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "prate");
  await expect(page).toHaveURL(/type=precip/);
  await expect(page).toHaveURL(/lines=hgt500/);
  await expect(page.locator("#legend")).toBeVisible();
  // Every session stayed resident throughout.
  expect(counters).toEqual({ tmp2m: 1, prate: 1, hgt500: 1 });
});

test("playback keeps moving with lines over the field", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?type=precip&lines=hgt500");
  await waitForReady(page);
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(page.getByRole("button", { name: "Pause animation" })).toBeVisible();
  // Two sessions decode for every frame; the field gates the playhead and
  // the lines catch up, so the playhead has to keep walking the axis rather
  // than wait on both.
  await expect.poll(() => slider.inputValue().then(Number), { timeout: 15_000 }).toBeGreaterThan(8);
  await expect(page.locator("#level-row")).toBeVisible();
});

test("the temperature tile opens a family whose level row picks the surface", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // Precipitation is a single layer: no level row.
  await expect(page.locator("#level-row")).toBeHidden();
  // The family tile opens its surface member, and the row lists the members
  // the run publishes with the surface pressed.
  // The rail's family tile and the level row's surface member share a name,
  // so the rail is addressed by its own group.
  const temperatureTile = page.locator('.variable-rail button[data-variable="tmp2m"]');
  await temperatureTile.click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp2m");
  await expect(page.locator("#level-row")).toBeVisible();
  const surfaceButton = page.locator('#level-row button[data-variable="tmp2m"]');
  const upperButton = page.locator('#level-row button[data-variable="tmp850"]');
  await expect(surfaceButton).toHaveAttribute("aria-pressed", "true");
  await expect(upperButton).toHaveAttribute("aria-pressed", "false");
  // 850 hPa is its own session with its own codebook, palette and title.
  await upperButton.click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp850");
  await expect(upperButton).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#variable-title")).toContainText("850 hPa");
  await expect(page.locator("#legend-unit")).toHaveText("°C");
  await expect(page.locator("#legend")).toBeVisible();
  // The level names itself in the URL, and the family tile stays pressed.
  await expect.poll(() => new URL(page.url()).searchParams.get("type")).toBe("tmp850");
  await expect(temperatureTile).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled();
  // Lines over the upper-air field put a second group beside the family's.
  await page.getByRole("button", { name: "PRESSURE FIELD" }).click();
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  await temperatureTile.click();
  // The family reopens the member last on screen.
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp850");
  await expect(page).toHaveURL(/lines=hgt500/);
  await expect(page.locator("#level-row .level-group")).toHaveCount(2);
  await expect(page.locator('#level-row button[data-variable="hgt500"]')).toHaveAttribute("aria-pressed", "true");
});

test("?type=tmp850 opens the upper-air field straight from the URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?type=t850");
  await waitForReady(page);
  await expect(page.locator("body")).toHaveAttribute("data-variable", "tmp850");
  await expect(page.locator("#level-row")).toBeVisible();
  await expect(page.locator("#variable-code")).toContainText("TMP 850MB");
});

test("?type=hgt500 opens the height view straight from the URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?type=hgt500");
  await waitForReady(page);
  await expect(page.locator("body")).toHaveAttribute("data-variable", "hgt500");
  await expect(page.locator("#variable-code")).toContainText("HGT 500MB");
});

test("switching to ECMWF loads its own run on a mixed-cadence 240-hour timeline", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  await pickModel(page, "ECMWF IFS 0.25°");
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
  await pickModel(page, "GFS NOAA 0.25°");
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
  await pickModel(page, "GFS SFLUX 13KM");
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
  await pickModel(page, "GFS NOAA 0.25°");
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

test("the speed button cycles the frame rate and remembers the choice", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // 121 frames loop for ten seconds at the default rate — no step-down.
  const speed = page.getByRole("button", { name: "Playback speed" });
  await expect(speed).toHaveText("12 FPS");
  await speed.click();
  await expect(speed).toHaveText("24 FPS");
  await speed.click();
  await expect(speed).toHaveText("3 FPS");

  // A chosen rate outranks the per-dataset default on the next visit.
  await page.reload();
  await waitForReady(page);
  await expect(speed).toHaveText("3 FPS");
});

test("a mixed-cadence axis holds its longer steps longer", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "timing-sensitive, one project is enough");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  // ECMWF temperature: 65 frames, 3-hourly to F144 then 6-hourly to F240.
  await pickModel(page, "ECMWF IFS 0.25°");
  await waitForReady(page);
  await page.getByRole("button", { name: "TEMP 2M" }).click();
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toHaveAttribute("max", "64");

  // The slowest rung keeps decode well ahead of playback, so what the clock
  // measures is the pacing and nothing else.
  const speed = page.getByRole("button", { name: "Playback speed" });
  for (let click = 0; click < 4 && (await speed.innerText()).trim() !== "3 FPS"; click += 1) {
    await speed.click();
  }
  await expect(speed).toHaveText("3 FPS");
  const play = page.getByRole("button", { name: "Play animation" });
  const pause = page.getByRole("button", { name: "Pause animation" });

  // The window is closed by the page's own clock, not by a round-tripped
  // pause.click(): on a loaded machine that click lands well after the
  // timeout, and every millisecond of its lateness would be counted as
  // playback progress. Pausing still happens, just after the measurement.
  async function framesAdvancedFrom(index: number): Promise<number> {
    await slider.fill(String(index));
    await play.click();
    const advanced = await page.evaluate(
      ([start, windowMs]) =>
        new Promise<number>((resolve) => {
          const control = document.querySelector("input[type=range]") as HTMLInputElement;
          window.setTimeout(() => resolve(Number(control.value) - start), windowMs);
        }),
      [index, 2_400] as const,
    );
    await pause.click();
    return advanced;
  }

  // 2.4 s at 3 fps buys ~7 three-hourly frames at the head; the tail's steps
  // are twice as long (index 49 is F150), so it must advance at about half
  // the rate. The assertion is that ratio rather than absolute counts: how
  // many frames actually land depends on how fast the machine decodes and
  // paints, but the pacing between the two segments does not.
  const head = await framesAdvancedFrom(0);
  const tail = await framesAdvancedFrom(49);
  // Printed so a failure on a machine this cannot be reproduced on says
  // which half broke: a head that also crawls is a slow runner, a tail that
  // keeps pace with the head is the dwell not being applied at all.
  console.log(`dwell: head=${head} tail=${tail}`);
  expect(head).toBeGreaterThanOrEqual(4);
  expect(tail).toBeGreaterThan(0);
  expect(tail * 1.5).toBeLessThanOrEqual(head);
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

test("range-capable server streams on demand and never downloads the full body", async ({ page }, testInfo) => {
  // A view that spans the grid fetches whole temporal groups; the narrow
  // phone viewport does not, and has its own test below.
  test.skip(testInfo.project.name !== "desktop", "a global view is a desktop-width view");
  // Full residency needs several playback loops. Budget well past the
  // measured worst case (test.slow()'s 90 s left no room) so only a real
  // stall fails.
  test.setTimeout(180_000);
  const counters: RangeCounters = { ranged: 0, full: 0, lengths: [] };
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

test("a phone-sized view buffers its own tiles and says so", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the phone viewport is the narrow one");
  test.setTimeout(180_000);
  const counters: RangeCounters = { ranged: 0, full: 0, lengths: [] };
  await routeManifest(page);
  await routeBundleWithRanges(page, counters);
  await page.goto("/");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  // The phone shows a fraction of the world, so the session never fetches
  // the rest of the grid — and the card reports what it did buffer rather
  // than stalling short of "fully buffered".
  await expect(page.locator("#preload-state")).toHaveText("Viewport fully buffered", { timeout: 120_000 });
  await expect(page.locator("#preload-percent")).not.toHaveText("100%");
  await expect(page.locator("#preload-format")).toContainText("Xue");
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
// every other test runs the English UI); ?lang= overrides it, and the round
// toggle in the top-right persists the other language and reloads onto it. The basemap label
// language rides the same detection, but the tests stub out the tile API.
test("?lang=zh renders the Chinese UI and the toggle switches back", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/?lang=zh");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator("#preload-state")).toHaveText("数据包已驻留内存", { timeout: 20_000 });
  await expect(page.getByRole("slider", { name: "预报时次" })).toBeEnabled({ timeout: 20_000 });
  // toHaveText (not toBeVisible): a rail tile shows its glyph and carries the
  // code and gloss visually hidden, for the accessible name.
  const tempLabel = page.locator('button[data-variable="tmp2m"] small');
  await expect(tempLabel).toHaveText("气温");
  await expect(page.locator('button[data-variable="tmp2m"] .rail-glyph')).toHaveText("温");
  // The toggle names the language it switches to, in one character.
  const toggle = page.locator("#lang-toggle");
  await expect(toggle).toHaveText("EN");
  await toggle.click();
  // The choice lives on this device, not in the link: the param the page
  // opened with goes, so a copied URL opens in each reader's own language.
  await expect(page).not.toHaveURL(/lang=/);
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(tempLabel).toHaveText("2M");
  await expect(toggle).toHaveText("中");
});

// Appearance is resolved before the first paint (an inline script in the
// shell) and fixed for the page; the round toggle persists the other side and
// reloads onto it, exactly like the locale.
test("the appearance toggle round-trips between paper and void", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "light" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const root = page.locator("html");
  await expect(root).toHaveAttribute("data-theme", "light");
  await page.locator("#theme-toggle").click();
  await expect(page).toHaveURL(/theme=dark/);
  await expect(root).toHaveAttribute("data-theme", "dark");
  // The choice outlives the URL: a fresh visit with no param reads it back.
  await page.goto("/");
  await expect(root).toHaveAttribute("data-theme", "dark");
  // An explicit param outranks both the stored choice and the OS preference.
  await page.goto("/?theme=light");
  await expect(root).toHaveAttribute("data-theme", "light");
});

test("clicking the map pins a point and reads its whole series at once", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  await routeBundle(page);
  await page.goto("/");
  await waitForReady(page);
  const panel = page.locator("#probe-panel");
  await expect(panel).toBeHidden();
  await page.locator("#map").click({ position: { x: 620, y: 300 } });
  await expect(panel).toBeVisible();
  // The probe names the variable it reads and the grid cell it reads it at.
  await expect(page.locator("#probe-code")).toHaveText("PRATE SFC");
  await expect(page.locator("#probe-coords")).toContainText("°");
  await expect(page.locator("#probe-value")).toContainText("mm/h");
  // The container is tiled, so the whole series comes out of the one tile
  // holding the cell rather than filling in frame by frame as playback walks
  // the axis.
  const count = page.locator("#probe-count");
  await expect(count).toHaveText("121 / 121");
  await expect(page.locator("#probe-hint")).toHaveText("Series complete");
  // Pinning a second point re-reads the series there: the panel follows the
  // new cell instead of holding the first one's numbers.
  const coords = page.locator("#probe-coords");
  const first = await coords.textContent();
  await page.locator("#map").click({ position: { x: 300, y: 520 } });
  await expect(coords).not.toHaveText(first ?? "");
  await expect(count).toHaveText("121 / 121");
  // And back to the first point, which the request bookkeeping must not
  // mistake for a series it already has.
  await page.locator("#map").click({ position: { x: 620, y: 300 } });
  await expect(coords).toHaveText(first ?? "");
  await expect(count).toHaveText("121 / 121");
  // Scrubbing changes the reading, not the series behind it.
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await slider.focus();
  for (let step = 0; step < 3; step += 1) await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("3");
  await expect(count).toHaveText("121 / 121");
  // Escape retires the panel.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
});

/** Move the playhead without playing: the app treats a slider `input` the
 * same way whether it came from a drag or a keypress. */
async function scrubTo(page: Page, index: number): Promise<void> {
  await page.getByRole("slider", { name: "Forecast hour" }).evaluate(
    (element: HTMLInputElement, value: number) => {
      element.value = String(value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
    },
    index,
  );
}

test("zooming in narrows a streaming session to the viewport's tiles", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  const counters: RangeCounters = { ranged: 0, full: 0, lengths: [] };
  await routeBundleWithRanges(page, counters);
  // The stats panel is what reports the narrowing; pin it the way a returning
  // viewer would have.
  await page.addInitScript(() => window.localStorage.setItem("g2pv-stats-visible", "1"));
  await page.goto("/");
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toBeEnabled({ timeout: 20_000 });
  // A global view fetches whole temporal groups, every tile of them, and says
  // so by naming no tile subset at all.
  const viewport = page.locator("#stat-viewport");
  await expect(viewport).not.toContainText("tiles");
  await scrubTo(page, 30);
  await expect(page.locator("#frame-tooltip")).toContainText("F030");
  const globalLongest = Math.max(...counters.lengths);

  // Zoom deep into one region. Everything fetched from here on is the tiles
  // that region covers.
  const box = (await page.locator("#map").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let step = 0; step < 6; step += 1) await page.mouse.wheel(0, -400);
  await expect(viewport).toContainText(/\d+ \/ 45 tiles/, { timeout: 10_000 });

  counters.lengths.length = 0;
  await scrubTo(page, 90);
  await expect(page.locator("#frame-tooltip")).toContainText("F090");
  await expect.poll(() => counters.lengths.length).toBeGreaterThan(0);
  // A viewport's worth of tiles is strictly less than a whole group, and the
  // frame still arrives: no error, and no fallback to the whole body.
  expect(Math.max(...counters.lengths)).toBeLessThan(globalLongest);
  expect(counters.full).toBe(0);
  await expect(page.getByRole("alert")).toBeHidden();
});

test("a view change under a pending scrub keeps the timeline on the new frame", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  // Slow enough that the frame scrubbed to is still decoding when the view
  // moves under it.
  await routeBundleWithRanges(page, undefined, 300);
  await page.addInitScript(() => window.localStorage.setItem("g2pv-stats-visible", "1"));
  await page.goto("/");
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toBeEnabled({ timeout: 20_000 });
  // Only a narrowed session re-requests anything on a view change, so zoom in
  // until the view names a tile subset of its own.
  const viewport = page.locator("#stat-viewport");
  const box = (await page.locator("#map").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let step = 0; step < 6; step += 1) await page.mouse.wheel(0, -400);
  await expect(viewport).toContainText(/\d+ \/ 45 tiles/, { timeout: 10_000 });
  const tilesBefore = /\d+ \/ 45 tiles/.exec((await viewport.textContent()) ?? "")![0];

  await scrubTo(page, 90);
  await expect(page.locator("#frame-tooltip")).toContainText("F090");
  // A narrower window covers fewer tiles, so the frame is requested again for
  // the new view — and the frame to request is the one asked for, not the one
  // still on screen while it decodes.
  await page.setViewportSize({ width: 600, height: 720 });
  await expect(viewport).not.toContainText(tilesBefore, { timeout: 10_000 });
  await expect(page.locator("#frame-tooltip")).toContainText("F090");
  await expect(page.getByRole("alert")).toBeHidden();
});

test("a streaming session reads a pinned series with a few range requests", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await routeManifest(page);
  const counters: RangeCounters = { ranged: 0, full: 0, lengths: [] };
  await routeBundleWithRanges(page, counters);
  await page.goto("/");
  await expect(page.getByRole("slider", { name: "Forecast hour" })).toBeEnabled({ timeout: 20_000 });
  const before = counters.ranged;
  await page.locator("#map").click({ position: { x: 620, y: 300 } });
  await expect(page.locator("#probe-count")).toHaveText("121 / 121", { timeout: 20_000 });
  // One chunk per temporal group of one tile, never one request per frame.
  expect(counters.ranged - before).toBeLessThan(121);
  expect(counters.full).toBe(0);
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
