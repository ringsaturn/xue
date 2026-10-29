import { expect, test, type Page } from "@playwright/test";

import { withoutStores } from "./artifacts";
import { fixtureBytes, fixtureJson, stubBasemap } from "./fixtures";

test.beforeEach(({ page }) => stubBasemap(page));

// The live run under the marks: the synthetic GFS fixture.
const LATEST_FIXTURE = fixtureJson("latest.json");
// The run as one published before the Zarr store existed (app.spec.ts
// runs on the same shape): the bundles are served whole, and the wait
// below is for the whole-download state. The marks take no session, so
// which path the field came by is not this suite's concern.
const MANIFEST_FIXTURE = withoutStores(
  fixtureJson("manifest.json"),
);
const BUNDLES: Record<string, Buffer> = Object.fromEntries(
  ["tmp2m.xue", "prate.xue", "tmp2m.poster.bin", "prate.poster.bin"].map(
    (name) => [
      name,
      fixtureBytes(name),
    ],
  ),
);

// The tropical cyclone product: the golden the Python build is held to,
// which is one real issue hour — Norbert (EP14) under NHC and JTWC
// forecasts and four model tracks, plus two invests — as the bucket would
// serve it.
const TC_ROOT = new URL("../fixtures/tc/expected/", import.meta.url);
const TC_POINTER = fixtureJson("latest-tc.json", TC_ROOT);
const TC_INDEX = fixtureJson("index.json", TC_ROOT);
const TC_STORMS: Record<string, unknown> = Object.fromEntries(
  (TC_INDEX.storms as { path: string }[]).map((storm) => [
    storm.path,
    fixtureJson(storm.path, TC_ROOT),
  ]),
);

async function routeRun(page: Page): Promise<void> {
  await page.route("**/data/latest.json*", (route) =>
    route.fulfill({ json: LATEST_FIXTURE }),
  );
  await page.route("**/data/gfs.*/manifest.json*", (route) =>
    route.fulfill({ json: MANIFEST_FIXTURE }),
  );
  await page.route("**/data/gfs.*/*.xue?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const body = BUNDLES[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      body,
    });
  });
  await page.route("**/data/gfs.*/*.poster.bin?*", (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const body = BUNDLES[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      body,
    });
  });
}

interface TcRequests {
  storms: string[];
}

/** The product's two layers: the mutable pointer and the immutable issue
 * directory it names (`tc/YYYY/MM/DD/tc.<hour>/`, or flat under the
 * root), every file `?v=`-addressed. */
async function routeTc(page: Page, requests?: TcRequests): Promise<void> {
  await page.route("**/data/latest-tc.json*", (route) =>
    route.fulfill({ json: TC_POINTER }),
  );
  await page.route("**/data/**/tc.*/index.json*", (route) =>
    route.fulfill({ json: TC_INDEX }),
  );
  await page.route("**/data/**/tc.*/*.json?*", (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname.split("/").pop() ?? "";
    if (name === "index.json") return route.fallback();
    requests?.storms.push(`${name}?v=${url.searchParams.get("v")}`);
    const body = TC_STORMS[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ json: body as object });
  });
}

/** Ready, and paused: playback drives a WebGL loop that on the emulated
 * phone leaves the sheet's DOM barely responsive, and nothing here needs
 * the frames to move. */
async function waitForReady(page: Page): Promise<void> {
  await expect(page.locator("#preload-state")).toHaveText(
    "Bundle fully buffered",
    { timeout: 20_000 },
  );
  const pause = page.getByRole("button", { name: "Pause animation" });
  if (await pause.isVisible()) await pause.click();
}

test("the storm tile appears with the product and its sheet focuses a storm", async ({
  page,
}) => {
  // The phone project walks the sheet at a couple of seconds a step.
  test.slow();
  await routeRun(page);
  const requests: TcRequests = { storms: [] };
  await routeTc(page, requests);
  await page.goto("/");
  await waitForReady(page);

  const tile = page.locator("#tc-tile");
  await expect(tile).toBeVisible();
  // The overview draws every named system, so the tile reads as pressed
  // and the two invests came down beside Norbert.
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => requests.storms.length).toBe(3);
  expect(
    requests.storms.every((request) => /\?v=[0-9a-f]{8}$/.test(request)),
  ).toBe(true);
  expect(page.url()).not.toContain("tc=");

  // The options hang from the storms' chip on the level row; the tile is
  // the tracks' switch.
  const chip = page.locator('.level-group[data-slot="tc"] button');
  await expect(chip).toHaveText("All");
  await chip.click();
  const sheet = page.locator("#tc-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("button", { name: /NORBERT/ })).toBeVisible();
  await expect(
    sheet.locator(".tc-group", { hasText: "DISTURBANCES" }),
  ).toBeVisible();
  // The agency chips list the centres forecasting the drawn storms.
  await expect(
    sheet.getByRole("button", { name: "NHC", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    sheet.getByRole("button", { name: "JTWC", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");

  await sheet.getByRole("button", { name: /NORBERT/ }).click();
  await expect(sheet).toBeHidden();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("tc"))
    .toBe("EP142026");
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  await expect(chip).toHaveText("NORBERT");
});

test("?tc= and ?tcagency= open a focused storm with one centre, and the tile switches the tracks off and back", async ({
  page,
}) => {
  // The phone project walks the sheet at a couple of seconds a step.
  test.slow();
  await routeRun(page);
  await routeTc(page);
  await page.goto("/?tc=ep142026&tcagency=nhc");
  await waitForReady(page);

  const tile = page.locator("#tc-tile");
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  // The lower-case id in the link is written back canonically.
  await expect
    .poll(() => new URL(page.url()).searchParams.get("tc"))
    .toBe("EP142026");

  const chip = page.locator('.level-group[data-slot="tc"] button');
  await chip.click();
  const sheet = page.locator("#tc-sheet");
  await expect(
    sheet.getByRole("button", { name: "NHC", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    sheet.getByRole("button", { name: "JTWC", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");

  // Turning the other centre on makes the set whole again, and a whole set
  // says nothing in the URL.
  await sheet.getByRole("button", { name: "JTWC", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.has("tcagency"))
    .toBe(false);

  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();

  // One press of the tile takes the tracks and their chip away…
  await tile.click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("tc"))
    .toBe("off");
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  await expect(chip).toBeHidden();
  // …and another brings them back on the storm that was focused.
  await tile.click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("tc"))
    .toBe("EP142026");
  await expect(tile).toHaveAttribute("aria-pressed", "true");
  await expect(chip).toBeVisible();
});

test("without the product the tile stays hidden", async ({ page }) => {
  await routeRun(page);
  await page.route("**/data/latest-tc.json*", (route) =>
    route.fulfill({ status: 404, body: "missing" }),
  );
  await page.goto("/");
  await waitForReady(page);
  await expect(page.locator("#tc-tile")).toBeHidden();
});
