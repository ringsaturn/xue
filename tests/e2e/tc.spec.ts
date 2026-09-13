import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The Protomaps API key is origin-locked to the production domains, so from
// 127.0.0.1 every tile request dies on CORS — and a map whose tiles never
// settle occasionally never fires "load", which is what gates initialize().
test.beforeEach(async ({ page }) => {
  await page.route("**/api.protomaps.com/**", (route) =>
    route.fulfill({ status: 204, body: "" }),
  );
});

// The live run under the marks: the synthetic GFS fixture.
const LATEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL("../fixtures/generated/web/latest.json", import.meta.url),
    ),
    "utf8",
  ),
);
const MANIFEST_FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL("../fixtures/generated/web/manifest.json", import.meta.url),
    ),
    "utf8",
  ),
);
const BUNDLES: Record<string, Buffer> = Object.fromEntries(
  ["tmp2m.xue", "prate.xue", "tmp2m.poster.bin", "prate.poster.bin"].map(
    (name) => [
      name,
      readFileSync(
        fileURLToPath(
          new URL(`../fixtures/generated/web/${name}`, import.meta.url),
        ),
      ),
    ],
  ),
);

// The tropical cyclone product: the golden the Python build is held to,
// which is one real issue hour — Norbert (EP14) under NHC and JTWC
// forecasts and four model tracks, plus two invests — as the bucket would
// serve it.
const TC_ROOT = new URL("../fixtures/tc/expected/", import.meta.url);
const TC_POINTER = JSON.parse(
  readFileSync(fileURLToPath(new URL("latest-tc.json", TC_ROOT)), "utf8"),
);
const TC_INDEX = JSON.parse(
  readFileSync(fileURLToPath(new URL("index.json", TC_ROOT)), "utf8"),
);
const TC_STORMS: Record<string, unknown> = Object.fromEntries(
  (TC_INDEX.storms as { path: string }[]).map((storm) => [
    storm.path,
    JSON.parse(
      readFileSync(fileURLToPath(new URL(storm.path, TC_ROOT)), "utf8"),
    ),
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
 * directory it names, every file `?v=`-addressed. */
async function routeTc(page: Page, requests?: TcRequests): Promise<void> {
  await page.route("**/data/latest-tc.json*", (route) =>
    route.fulfill({ json: TC_POINTER }),
  );
  await page.route("**/data/tc.*/index.json*", (route) =>
    route.fulfill({ json: TC_INDEX }),
  );
  await page.route("**/data/tc.*/*.json?*", (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname.split("/").pop() ?? "";
    if (name === "index.json") return route.fallback();
    requests?.storms.push(`${name}?v=${url.searchParams.get("v")}`);
    const body = TC_STORMS[name];
    if (!body) return route.fulfill({ status: 404, body: "missing" });
    return route.fulfill({ json: body as object });
  });
}

async function waitForReady(page: Page): Promise<void> {
  await expect(page.locator("#preload-state")).toHaveText(
    "Bundle fully buffered",
    { timeout: 20_000 },
  );
}

test("the storm tile appears with the product and its sheet focuses a storm", async ({
  page,
}) => {
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

  await tile.click();
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
});

test("?tc= and ?tcagency= open a focused storm with one centre, and hiding writes tc=off", async ({
  page,
}) => {
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

  await tile.click();
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

  await sheet.getByRole("button", { name: "Hide tracks" }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("tc"))
    .toBe("off");
  await expect(tile).toHaveAttribute("aria-pressed", "false");
  await expect(
    sheet.getByRole("button", { name: "Show tracks" }),
  ).toHaveAttribute("aria-pressed", "true");
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
