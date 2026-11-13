import { readFileSync } from "node:fs";

import { expect, test, type Page, type Route } from "@playwright/test";

import { withoutStores } from "./artifacts";
import { fixtureBytes, fixtureJson, stubBasemap } from "./fixtures";

test.beforeEach(({ page }) => stubBasemap(page));

// The live run under the marks: the synthetic GFS fixture, served whole as
// tc.spec.ts serves it. The marks take no session.
const LATEST_FIXTURE = fixtureJson("latest.json");
const MANIFEST_FIXTURE = withoutStores(fixtureJson("manifest.json"));
const BUNDLES: Record<string, Buffer> = Object.fromEntries(
  ["tmp2m.xue", "prate.xue", "tmp2m.poster.bin", "prate.poster.bin"].map((name) => [name, fixtureBytes(name)]),
);

// The air-quality and warnings products: the goldens the Python builds are
// held to (docs/aq.md §8, docs/alerts.md §11), as the bucket would serve
// them, every file under its `?v=`.
const AQ_ROOT = new URL("../fixtures/aq/expected/", import.meta.url);
const ALERTS_ROOT = new URL("../fixtures/alerts/expected/", import.meta.url);

async function routeRun(page: Page): Promise<void> {
  await page.route("**/data/latest.json*", (route) => route.fulfill({ json: LATEST_FIXTURE }));
  await page.route("**/data/gfs.*/manifest.json*", (route) => route.fulfill({ json: MANIFEST_FIXTURE }));
  const bundle = (route: Route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const body = BUNDLES[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ status: 200, contentType: "application/octet-stream", body });
  };
  await page.route("**/data/gfs.*/*.xue?*", bundle);
  await page.route("**/data/gfs.*/*.poster.bin?*", bundle);
}

/** A product's pointer and the files of its round, read off the golden by
 * the path under the data root. Range requests are answered whole: the
 * shell slices a 200 at the span it asked for. */
async function routeProduct(page: Page, product: "aq" | "alerts", root: URL): Promise<void> {
  await page.route(`**/data/latest-${product}.json*`, (route) =>
    route.fulfill({ json: fixtureJson(`latest-${product}.json`, root) }),
  );
  await page.route(`**/data/${product}/**`, (route) => {
    const path = new URL(route.request().url()).pathname.split("/data/")[1] ?? "";
    try {
      return route.fulfill({ status: 200, body: readFileSync(new URL(path, root)) });
    } catch {
      return route.fulfill({ status: 404, body: "missing" });
    }
  });
  // The boundary sets are not part of the goldens: an area-coded warning
  // simply draws nothing here, and the polygon warnings carry the test.
  await page.route("**/data/alerts-areas/**", (route) => route.fulfill({ status: 404, body: "missing" }));
}

async function waitForReady(page: Page): Promise<void> {
  await expect(page.locator("#preload-state")).toHaveText("Bundle fully buffered", { timeout: 20_000 });
  const pause = page.getByRole("button", { name: "Pause animation" });
  if (await pause.isVisible()) await pause.click();
}

test("the air-quality tile toggles the stations and a station opens its card", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeRun(page);
  await routeProduct(page, "aq", AQ_ROOT);
  // Over Central/Western, Hong Kong (hkepd:0), close enough that its dot
  // sits under the middle of the map.
  await page.goto("/#map=12/22.2849/114.1444");
  await waitForReady(page);

  const tile = page.locator("#aq-tile");
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  await tile.click();
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => new URL(page.url()).searchParams.get("stations")).toBe("aq");

  await expect(async () => {
    await page.locator("#map").click();
    await expect(page.locator(".station-popup")).toContainText("Central/Western", { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  const card = page.locator(".station-popup");
  await expect(card).toContainText("PM2.5");
  await expect(card).toContainText("µg/m³");
  await expect(card).toContainText("Preliminary data");
  await expect(card).toContainText("DATA.GOV.HK");
  // Never an index: the product publishes concentrations.
  await expect(card).not.toContainText("AQI");

  await tile.click();
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => new URL(page.url()).searchParams.has("stations")).toBe(false);
});

test("the warnings tile draws what is in force, a click inside one opens its card and a second pins the point", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop interaction coverage");
  await routeRun(page);
  await routeProduct(page, "alerts", ALERTS_ROOT);
  // Inside the golden's storm-based Tornado Warning near Phoenix.
  await page.goto("/?stations=alerts#map=12/33.46/-111.85");
  await waitForReady(page);

  const tile = page.locator("#alerts-tile");
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAttribute("aria-pressed", "true");

  await expect(async () => {
    await page.locator("#map").click();
    await expect(page.locator(".station-popup")).toContainText("Tornado Warning", { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  const card = page.locator(".station-popup");
  await expect(card).toContainText("In force now");
  await expect(card).toContainText("Level 4");
  await expect(card).toContainText("NWS Phoenix AZ");
  await expect(card).toContainText("US National Weather Service");
  await expect(card).toContainText("Click the area again");
  // The first click is the card's alone; a second inside the same warning,
  // clear of the card's tip, pins the point, whose panel names it too.
  const box = (await page.locator("#map").boundingBox())!;
  await page.locator("#map").click({ position: { x: box.width / 2 - 60, y: box.height / 2 } });
  await expect(page.locator(".station-popup")).toHaveCount(0);
  await expect(page.locator("#probe-alert")).toContainText("Tornado Warning");

  await tile.click();
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => new URL(page.url()).searchParams.has("stations")).toBe(false);
});

test("without the products the tiles stay hidden", async ({ page }) => {
  await routeRun(page);
  for (const product of ["aq", "alerts"])
    await page.route(`**/data/latest-${product}.json*`, (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.goto("/");
  await waitForReady(page);
  await expect(page.locator("#aq-tile")).toBeHidden();
  await expect(page.locator("#alerts-tile")).toBeHidden();
});
