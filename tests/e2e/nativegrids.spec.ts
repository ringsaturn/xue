import { expect, test, type Page } from "@playwright/test";

import { routeStores } from "./artifacts";
import { fixtureJson, fixturePath, stubBasemap } from "./fixtures";

/**
 * The two shapes of grid no published run has yet, opened end to end from the
 * synthetic runs under `native/` (tests/prepare_web_fixture.py): a schemaVersion
 * 4 bundle on a Lambert grid, and a schemaVersion 3 Gaussian one. A shell that
 * refuses either would refuse a live run the moment a source starts writing it,
 * and a run is otherwise what proves the manifest, the group and the sessions
 * take them.
 */

test.beforeEach(({ page }) => stubBasemap(page));

async function routeRun(page: Page, model: string, pointer: string): Promise<string[]> {
  const root = `native/${model}/`;
  const errors: string[] = [];
  // The stats rows carry the grid's size, and only fill while shown.
  await page.addInitScript(() => localStorage.setItem("g2pv-stats-visible", "1"));
  page.on("pageerror", (error) => {
    if (/grid|projection/i.test(error.message)) errors.push(error.message);
  });
  await page.route(`**/data/${pointer}*`, (route) => route.fulfill({ json: fixtureJson(`${root}${pointer}`) }));
  await page.route(`**/data/${model}.*/manifest.json*`, (route) => route.fulfill({ json: fixtureJson(`${root}manifest.json`) }));
  await routeStores(page, `**/data/${model}.*/*.zarr/**`, fixturePath(root));
  return errors;
}

async function expectPlays(page: Page): Promise<void> {
  await expect(page.locator("#preload-format")).toHaveText("Zarr");
  const slider = page.getByRole("slider", { name: "Forecast hour" });
  await expect(slider).toBeEnabled({ timeout: 20_000 });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#forecast-hour")).toHaveText("F001");
}

test("a bundle on a Lambert grid opens and plays on its own cells", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const errors = await routeRun(page, "hrrr", "latest-hrrr.json");
  await page.goto("/?model=hrrr#map=4/38/-97.5");
  await expect(page.locator("body")).toHaveAttribute("data-model", "hrrr");
  await expectPlays(page);
  await expect(page.locator("#stat-grid")).toHaveText("90 × 53");
  expect(errors).toEqual([]);
});

test("a bundle whose rows are Gaussian opens and plays", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const errors = await routeRun(page, "sflux", "latest-sflux.json");
  await page.goto("/?model=sflux");
  await expect(page.locator("body")).toHaveAttribute("data-model", "sflux");
  await expectPlays(page);
  await expect(page.locator("#stat-grid")).toHaveText("144 × 72");
  expect(errors).toEqual([]);
});
